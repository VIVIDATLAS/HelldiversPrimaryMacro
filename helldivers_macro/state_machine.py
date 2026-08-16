from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from .config import AppConfig
from .input_backend import InputCoordination
from .models import (
    ControlEvent,
    ControlEventKind,
    MagazineState,
    MacroState,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerRequest,
    WorkerResult,
)


class AudioSignals(Protocol):
    def notify_on(self) -> None: ...
    def notify_off(self) -> None: ...


class WorkerHandle(Protocol):
    token: int
    request: WorkerRequest

    def start(self) -> None: ...
    def cancel_and_release(self) -> BaseException | None: ...
    def join(self, timeout: float | None = None) -> None: ...
    def is_alive(self) -> bool: ...


WorkerFactory = Callable[[int, WorkerRequest], WorkerHandle]


class MacroStateMachine:
    """Single-control-loop owner for firing, preparation, and bypass work."""

    def __init__(
        self,
        config: AppConfig,
        foreground_active: Callable[[], bool],
        audio: AudioSignals,
        worker_factory: WorkerFactory,
        *,
        coordination: InputCoordination | None = None,
        clock: Callable[[], float] = time.perf_counter,
        reporter: Callable[[str], None] = print,
    ) -> None:
        self._config = config
        self.state = MacroState.IDLE_PRIMARY
        self.selected_mode = WeaponMode.PRIMARY
        self._magazines = {
            WeaponMode.PRIMARY: MagazineState.UNKNOWN,
            WeaponMode.SECONDARY: MagazineState.UNKNOWN,
        }
        self._armed = False
        self._foreground_active = foreground_active
        self._audio = audio
        self._worker_factory = worker_factory
        self._coordination = coordination or InputCoordination()
        self._debounce_seconds = config.controls.toggle_debounce_ms / 1000.0
        self._clock = clock
        self._report = reporter
        self._last_stop_time = float("-inf")
        self._worker: WorkerHandle | None = None
        self._worker_kind: WorkerKind | None = None
        self._worker_mode: WeaponMode | None = None
        self._next_worker_token = 1
        self._off_due = False
        self._firing_began = False
        self._macro_reload_invalidated = False
        self._preparation_invalidated = False
        self._toggle_click_in_progress = False
        self._pending_start = False
        self._pending_selection: WeaponMode | None = None
        self._deferred_release: threading.Event | None = None
        self._deferred_discard = False
        self.fatal_error: BaseException | None = None

    @property
    def worker(self) -> WorkerHandle | None:
        return self._worker

    @property
    def running(self) -> bool:
        return self._worker_kind is WorkerKind.MACRO

    @property
    def preparing(self) -> bool:
        return self._worker_kind is WorkerKind.PREPARATION

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def pending_start(self) -> bool:
        return self._pending_start

    def magazine_state(self, mode: WeaponMode) -> MagazineState:
        return self._magazines[mode]

    def _diagnostic(self, message: str) -> None:
        if self._config.diagnostics.ctrl_bypass_logging:
            self._report(f"[ctrl-bypass] {message}")

    def _idle_state(self) -> MacroState:
        return (
            MacroState.IDLE_PRIMARY
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.IDLE_SECONDARY
        )

    def _waiting_state(self) -> MacroState:
        return (
            MacroState.WAITING_PRIMARY_RELEASE
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.WAITING_SECONDARY_RELEASE
        )

    def _running_state(self) -> MacroState:
        return (
            MacroState.RUNNING_PRIMARY
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.RUNNING_SECONDARY
        )

    def _preparing_state(self) -> MacroState:
        return (
            MacroState.PREPARING_PRIMARY
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.PREPARING_SECONDARY
        )

    def handle(self, event: ControlEvent) -> None:
        if event.kind is ControlEventKind.DIAGNOSTIC:
            self._diagnostic(str(event.detail))
            return
        if self.state is MacroState.SHUTTING_DOWN:
            return

        kind = event.kind
        if kind is ControlEventKind.SELECT_PRIMARY:
            self._select(WeaponMode.PRIMARY)
        elif kind is ControlEventKind.SELECT_SECONDARY:
            self._select(WeaponMode.SECONDARY)
        elif kind is ControlEventKind.PHYSICAL_MB1_DOWN:
            self._mb1_down()
        elif kind is ControlEventKind.PHYSICAL_MB1_UP:
            self._mb1_up()
        elif kind is ControlEventKind.MANUAL_BYPASS_DOWN:
            self._manual_bypass()
        elif kind is ControlEventKind.DEFERRED_BYPASS_DOWN:
            self._deferred_bypass_down()
        elif kind is ControlEventKind.DEFERRED_BYPASS_UP:
            self._deferred_bypass_up()
        elif kind in (
            ControlEventKind.CTRL_DOWN,
            ControlEventKind.SHIFT_DOWN,
            ControlEventKind.RIGHT_DOWN,
        ):
            self._cancel_control()
        elif kind in (
            ControlEventKind.FOREGROUND_LOST,
            ControlEventKind.FOREGROUND_UNCERTAIN,
        ):
            self._foreground_lost()
        elif kind is ControlEventKind.HOOK_FAILURE:
            self.fatal_error = (
                event.detail
                if isinstance(event.detail, BaseException)
                else RuntimeError(f"hook failure: {event.detail}")
            )
            self._report(f"Hook failure: {self.fatal_error}")
            self._foreground_lost()
        elif kind is ControlEventKind.WORKER_PROGRESS:
            self._worker_progress(event)
        elif kind is ControlEventKind.WORKER_STOPPED:
            self._worker_stopped(event)
        elif kind is ControlEventKind.SHUTDOWN:
            self.shutdown()

    def _select(self, mode: WeaponMode) -> None:
        if self._worker_kind is WorkerKind.MACRO:
            self._macro_reload_invalidated = True
        if self._worker_kind is WorkerKind.PREPARATION:
            self._preparation_invalidated = True
        self.selected_mode = mode
        self._magazines[mode] = MagazineState.UNKNOWN
        self._armed = False
        self._pending_start = False
        self._toggle_click_in_progress = False
        self._discard_deferred_bypass()
        self._report(f"Selected weapon mode: {mode.value}; magazine state: UNKNOWN")
        if self._worker is not None:
            self._pending_selection = mode
            self._request_stop()
            return
        self._begin_selection_preparation()

    def _begin_selection_preparation(self) -> None:
        self.state = self._idle_state()
        if not self._foreground_active():
            return
        if self._config.weapons.reload_on_select:
            self._start_preparation(self._config.weapons.switch_settle_ms)
        else:
            self._armed = True

    def _mb1_down(self) -> None:
        if self.running:
            self._request_stop()
            return
        if self.preparing:
            self._toggle_click_in_progress = True
            return
        if self._worker is not None or self.state is MacroState.STOPPING:
            return
        if self.state in (MacroState.IDLE_PRIMARY, MacroState.IDLE_SECONDARY):
            if self._clock() - self._last_stop_time < self._debounce_seconds:
                return
            if self._foreground_active():
                self._toggle_click_in_progress = True
                self.state = self._waiting_state()

    def _mb1_up(self) -> None:
        if self.preparing and self._toggle_click_in_progress:
            self._toggle_click_in_progress = False
            self._pending_start = True
            return
        if self.state not in (
            MacroState.WAITING_PRIMARY_RELEASE,
            MacroState.WAITING_SECONDARY_RELEASE,
        ):
            self._toggle_click_in_progress = False
            return
        self._toggle_click_in_progress = False
        if not self._foreground_active():
            self._pending_start = False
            self.state = self._idle_state()
            return
        self._request_start()

    def _request_start(self) -> None:
        if self._worker is not None or not self._foreground_active():
            self._pending_start = False
            self.state = self._idle_state()
            return
        if self._magazines[self.selected_mode] is MagazineState.FULL:
            self._armed = True
            self._start_macro()
        elif self._config.weapons.reload_before_start_if_unknown:
            self._pending_start = True
            self._armed = False
            self._start_preparation(0)
        else:
            self._armed = True
            self._start_macro()

    def _start_macro(self) -> None:
        self._pending_start = False
        request = WorkerRequest(WorkerKind.MACRO, self.selected_mode)
        if self._start_worker(request):
            self._firing_began = False
            self._macro_reload_invalidated = False
            self.state = self._running_state()
            self._off_due = True
            self._audio.notify_on()

    def _start_preparation(self, settle_ms: int) -> None:
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._armed = False
        self._preparation_invalidated = False
        request = WorkerRequest(
            WorkerKind.PREPARATION,
            self.selected_mode,
            switch_settle_ms=settle_ms,
        )
        if self._start_worker(request):
            self.state = self._preparing_state()
        else:
            self._pending_start = False
            self.state = self._idle_state()

    def _start_deferred_bypass(self) -> None:
        release = self._deferred_release
        if release is None or self._deferred_discard or not self._foreground_active():
            self._discard_deferred_bypass()
            self.state = self._idle_state()
            return
        request = WorkerRequest(
            WorkerKind.BYPASS,
            self.selected_mode,
            bypass_release=release,
            bypass_click_ms=self._config.controls.deferred_bypass_click_ms,
        )
        if self._start_worker(request):
            self.state = MacroState.FORWARDING_BYPASS
            self._diagnostic("cleanup complete; forwarding tagged deferred bypass")
        else:
            self._discard_deferred_bypass()
            self.state = self._idle_state()

    def _start_worker(self, request: WorkerRequest) -> bool:
        if self._worker is not None:
            return False
        token = self._next_worker_token
        self._next_worker_token += 1
        if request.kind is WorkerKind.MACRO:
            # Publish before the worker can generate its first MB1-down, closing
            # the startup race with a simultaneous physical Ctrl press.
            self._coordination.macro_started()
        try:
            worker = self._worker_factory(token, request)
            worker.start()
        except BaseException as exc:
            if request.kind is WorkerKind.MACRO:
                self._coordination.cleanup_completed()
            self._report(f"{request.kind.name.title()} worker failed to start: {exc}")
            return False
        self._worker = worker
        self._worker_kind = request.kind
        self._worker_mode = request.mode
        return True

    def _manual_bypass(self) -> None:
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._armed = False
        self._pending_start = False
        if self._worker_kind is WorkerKind.PREPARATION:
            self._preparation_invalidated = True
        if self._worker_kind is WorkerKind.MACRO:
            self._macro_reload_invalidated = True
        self._diagnostic("Ctrl+MB1 passed through; selected magazine marked UNKNOWN")

    def _deferred_bypass_down(self) -> None:
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._armed = False
        self._pending_start = False
        if self._worker_kind is WorkerKind.MACRO:
            self._macro_reload_invalidated = True
        if self._deferred_release is None:
            self._deferred_release = threading.Event()
            self._deferred_discard = False
        self._diagnostic("deferred Ctrl+MB1 captured; requesting cleanup")
        if self._worker is not None:
            self._request_stop()
        else:
            self._start_deferred_bypass()

    def _deferred_bypass_up(self) -> None:
        if self._deferred_release is not None:
            self._deferred_release.set()
            self._diagnostic("deferred physical MB1 released")

    def _discard_deferred_bypass(self) -> None:
        if self._deferred_release is not None:
            self._deferred_release.set()
        self._deferred_release = None
        self._deferred_discard = True

    def _cancel_control(self) -> None:
        self._pending_start = False
        self._toggle_click_in_progress = False
        if self._worker is not None:
            if self._worker_kind is WorkerKind.BYPASS:
                self._discard_deferred_bypass()
            if self.preparing and self._worker_mode is not None:
                self._preparation_invalidated = True
                self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            self._request_stop()
        elif self.state in (
            MacroState.WAITING_PRIMARY_RELEASE,
            MacroState.WAITING_SECONDARY_RELEASE,
        ):
            self.state = self._idle_state()

    def _foreground_lost(self) -> None:
        self._pending_start = False
        self._toggle_click_in_progress = False
        self._armed = False
        if self._worker_kind is WorkerKind.MACRO:
            self._macro_reload_invalidated = True
        if self._worker_kind is WorkerKind.PREPARATION:
            self._preparation_invalidated = True
        self._discard_deferred_bypass()
        if self._worker_mode is not None:
            self._magazines[self._worker_mode] = MagazineState.UNKNOWN
        if self._worker is not None:
            self._request_stop()
        else:
            self.state = self._idle_state()

    def _request_stop(self) -> None:
        worker = self._worker
        if worker is None or self.state is MacroState.STOPPING:
            return
        if self._worker_kind is WorkerKind.MACRO:
            self._coordination.cleanup_requested()
        if self._worker_kind is WorkerKind.PREPARATION and self._worker_mode is not None:
            self._preparation_invalidated = True
            self._magazines[self._worker_mode] = MagazineState.UNKNOWN
        self.state = MacroState.STOPPING
        release_error = worker.cancel_and_release()
        if release_error is not None:
            if self._worker_mode is not None:
                self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            self._report(f"Generated-input release failed: {release_error}")

    def _worker_progress(self, event: ControlEvent) -> None:
        if self._worker is None or event.worker_token != self._worker.token:
            return
        if self._worker_kind is not WorkerKind.MACRO or self._worker_mode is None:
            return
        if event.detail is WorkerProgress.SHOT_BEGAN:
            self._firing_began = True
            self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            self._armed = False
        elif event.detail is WorkerProgress.RELOAD_COMPLETE:
            if not self._macro_reload_invalidated:
                self._magazines[self._worker_mode] = MagazineState.FULL
                self._armed = True

    def _worker_stopped(self, event: ControlEvent) -> None:
        if self._worker is None or event.worker_token != self._worker.token:
            return
        result = (
            event.detail
            if isinstance(event.detail, WorkerResult)
            else WorkerResult(
                False,
                error=RuntimeError(f"invalid worker result {event.detail!r}"),
            )
        )
        kind = self._worker_kind
        mode = self._worker_mode
        self._worker = None
        self._worker_kind = None
        self._worker_mode = None
        if kind is WorkerKind.MACRO:
            self._coordination.cleanup_completed()
        if result.error is not None:
            if mode is not None:
                self._magazines[mode] = MagazineState.UNKNOWN
            self._armed = False
            label = kind.name.title() if kind else "Worker"
            self._report(f"{label} stopped after error: {result.error}")
            if kind is WorkerKind.MACRO:
                self._discard_deferred_bypass()
        if kind is WorkerKind.PREPARATION:
            if (
                result.success
                and not result.canceled
                and result.error is None
                and not self._preparation_invalidated
                and self._foreground_active()
                and mode is not None
            ):
                self._magazines[mode] = MagazineState.FULL
                self._armed = mode is self.selected_mode
            else:
                if mode is not None:
                    self._magazines[mode] = MagazineState.UNKNOWN
                self._armed = False
        if kind is WorkerKind.BYPASS and mode is not None:
            self._magazines[mode] = MagazineState.UNKNOWN
            self._armed = False
        if kind is WorkerKind.MACRO and self._off_due:
            self._off_due = False
            self._audio.notify_off()
            self._last_stop_time = self._clock()
        self.state = self._idle_state()
        if kind is WorkerKind.PREPARATION:
            preparation_succeeded = (
                result.success
                and not result.canceled
                and result.error is None
                and not self._preparation_invalidated
                and self._foreground_active()
            )
            self._preparation_invalidated = False
        else:
            preparation_succeeded = False

        if self._pending_selection is not None:
            selected = self._pending_selection
            self._pending_selection = None
            self.selected_mode = selected
            self._begin_selection_preparation()
            return
        if (
            kind is not WorkerKind.BYPASS
            and self._deferred_release is not None
            and not self._deferred_discard
        ):
            self._start_deferred_bypass()
            return
        if kind is WorkerKind.PREPARATION:
            should_start = self._pending_start
            self._pending_start = False
            if preparation_succeeded and should_start:
                self._start_macro()
            return
        if kind is WorkerKind.BYPASS:
            self._discard_deferred_bypass()

    def shutdown(self) -> None:
        if self.state is MacroState.SHUTTING_DOWN and self._worker is None:
            return
        self.state = MacroState.SHUTTING_DOWN
        self._pending_start = False
        self._pending_selection = None
        self._toggle_click_in_progress = False
        self._macro_reload_invalidated = True
        self._preparation_invalidated = True
        self._discard_deferred_bypass()
        worker = self._worker
        if worker is not None:
            if self._worker_mode is not None:
                self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            release_error = worker.cancel_and_release()
            if release_error is not None:
                self._report(f"Generated-input release failed: {release_error}")
            worker.join(2.0)
            if worker.is_alive():
                self._report("Worker did not exit within 2 seconds")
            self._worker = None
            self._worker_kind = None
            self._worker_mode = None
        self._coordination.cleanup_completed()
        if self._off_due:
            self._off_due = False
            self._audio.notify_off()
