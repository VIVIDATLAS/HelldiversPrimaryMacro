from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from .config import AppConfig
from .input_backend import InputCoordination
from .models import (
    AimState,
    ControlEvent,
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    PreparationLifecycle,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerProgressUpdate,
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
    def activate(self) -> None: ...
    def cancel(self) -> None: ...
    def cancel_and_release(self) -> BaseException | None: ...
    def join(self, timeout: float | None = None) -> None: ...
    def is_alive(self) -> bool: ...
    def reload_in_progress(self) -> bool: ...
    def sprint_stop(self) -> bool: ...
    def cancel_aim_and_observe(
        self,
    ) -> tuple[bool, bool, BaseException | None]: ...


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
        foreground_status: Callable[[], tuple[bool, bool]] | None = None,
    ) -> None:
        self._config = config
        self.state = MacroState.IDLE_PRIMARY
        self.selected_mode = WeaponMode.PRIMARY
        self._magazines = {
            WeaponMode.PRIMARY: MagazineState.UNKNOWN,
            WeaponMode.SECONDARY: MagazineState.UNKNOWN,
        }
        self._preparation_lifecycles = {
            WeaponMode.PRIMARY: PreparationLifecycle.IDLE_UNKNOWN,
            WeaponMode.SECONDARY: PreparationLifecycle.IDLE_UNKNOWN,
        }
        self._armed = False
        self._enabled = False
        self._physical_mb1_down = False
        self._neutral_rearm_required = False
        self._foreground_active = foreground_active
        self._foreground_status = foreground_status
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
        self._retired_preparations: dict[int, WorkerHandle] = {}
        self._generation = 0
        self._active_preparation_generation: int | None = None
        self._off_pending = False
        self._on_announced = False
        self._firing_began = False
        self._macro_reload_invalidated = False
        self._preparation_invalidated = False
        self._reload_only_invalidated = False
        self._aim_state = AimState.AIM_OFF
        self._aim_generation = 0
        self._aim_worker: WorkerHandle | None = None
        self._retired_aim_workers: dict[int, WorkerHandle] = {}
        self._worker_phase = "IDLE"
        self._preserved_macro_reload_token: int | None = None
        self._pending_sprint_reload: tuple[WeaponMode, int] | None = None
        self._pending_selection: WeaponMode | None = None
        self._worker_cancel_reason: str | None = None
        self._deferred_release: threading.Event | None = None
        self._deferred_discard = False
        self._foreground_loss_latched = False
        self._trace_sequence = 0
        self._trace_started_at = self._clock()
        self.fatal_error: BaseException | None = None

    @property
    def worker(self) -> WorkerHandle | None:
        return self._worker

    @property
    def running(self) -> bool:
        return self._worker_kind is WorkerKind.MACRO

    @property
    def firing(self) -> bool:
        return self.running and self.state in (
            MacroState.RUNNING_PRIMARY,
            MacroState.RUNNING_SECONDARY,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def aim_state(self) -> AimState:
        return self._aim_state

    @property
    def physical_mb1_down(self) -> bool:
        return self._physical_mb1_down

    @property
    def preparing(self) -> bool:
        return self._worker_kind is WorkerKind.PREPARATION

    @property
    def reloading(self) -> bool:
        if self._worker_kind is WorkerKind.RELOAD_ONLY:
            return True
        return (
            self._worker_kind is WorkerKind.MACRO
            and self._worker is not None
            and self._worker.reload_in_progress()
        )

    @property
    def armed(self) -> bool:
        return self._armed

    def magazine_state(self, mode: WeaponMode) -> MagazineState:
        return self._magazines[mode]

    @property
    def preparation_generation(self) -> int | None:
        return self._active_preparation_generation

    @property
    def generation(self) -> int:
        return self._generation

    def preparation_lifecycle(self, mode: WeaponMode) -> PreparationLifecycle:
        return self._preparation_lifecycles[mode]

    def _diagnostic(self, message: str) -> None:
        if self._config.diagnostics.ctrl_bypass_logging:
            self._report(f"[ctrl-bypass] {message}")

    def _foreground_label(self) -> str:
        if self._foreground_status is None:
            return "ACTIVE" if self._foreground_active() else "INACTIVE_OR_UNCERTAIN"
        active, certain = self._foreground_status()
        if not certain:
            return "UNCERTAIN"
        return "ACTIVE_CERTAIN" if active else "INACTIVE_CERTAIN"

    def _snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.name,
            "weapon": self.selected_mode.value,
            "magazine": self._magazines[self.selected_mode].value,
            "enabled": self._enabled,
            "firing": self.firing,
            "physical_mb1_down": self._physical_mb1_down,
            "neutral_rearm": self._neutral_rearm_required,
            "generation": self._generation,
            "aim_state": self._aim_state.name,
            "worker_phase": self._worker_phase,
            "foreground": self._foreground_label(),
        }

    @staticmethod
    def _format_snapshot(snapshot: dict[str, object]) -> str:
        return ",".join(
            f"{key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in snapshot.items()
        )

    def _trace(
        self,
        event: str,
        *,
        source: EventSource,
        previous: dict[str, object],
        reason: str,
        rejected: bool = False,
        occurred_at: float | None = None,
    ) -> None:
        if not self._config.diagnostics.state_tracing:
            return
        self._trace_sequence += 1
        normalized = event.strip().upper().replace(" ", "_")
        prefix = "START_REJECTED: " if rejected else "TRACE: "
        event_time = self._clock() if occurred_at is None else occurred_at
        elapsed_ms = (event_time - self._trace_started_at) * 1000.0
        self._report(
            f"{prefix}seq={self._trace_sequence} elapsed_ms={elapsed_ms:.3f} "
            f"event={normalized} "
            f"source={source.value} "
            f"previous=[{self._format_snapshot(previous)}] "
            f"result=[{self._format_snapshot(self._snapshot())}] "
            f"generation={self._generation} reason={reason}"
        )

    def _set_state(self, state: MacroState) -> None:
        self.state = state

    def _set_preparation_lifecycle(
        self,
        mode: WeaponMode,
        lifecycle: PreparationLifecycle,
    ) -> None:
        self._preparation_lifecycles[mode] = lifecycle

    def _start_rejected(
        self,
        reason: str,
        source: EventSource,
        previous: dict[str, object],
    ) -> None:
        self._trace(
            "START_REJECTED",
            source=source,
            previous=previous,
            reason=reason,
            rejected=True,
        )

    def _idle_state(self) -> MacroState:
        return (
            MacroState.IDLE_PRIMARY
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.IDLE_SECONDARY
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

    def _reloading_state(self) -> MacroState:
        return (
            MacroState.RELOADING_PRIMARY
            if self.selected_mode is WeaponMode.PRIMARY
            else MacroState.RELOADING_SECONDARY
        )

    def handle(self, event: ControlEvent) -> None:
        if event.kind is ControlEventKind.DIAGNOSTIC:
            self._diagnostic(str(event.detail))
            return
        if self.state is MacroState.SHUTTING_DOWN:
            return

        kind = event.kind
        if kind is ControlEventKind.SELECT_PRIMARY:
            self._select(WeaponMode.PRIMARY, event.source)
        elif kind is ControlEventKind.SELECT_SECONDARY:
            self._select(WeaponMode.SECONDARY, event.source)
        elif kind is ControlEventKind.PHYSICAL_MB1_DOWN:
            self._mb1_down(event.source)
        elif kind is ControlEventKind.PHYSICAL_MB1_UP:
            self._mb1_up()
        elif kind is ControlEventKind.PHYSICAL_MB2_DOWN:
            self._mb2_down(event.source)
        elif kind is ControlEventKind.PHYSICAL_MB2_UP:
            return
        elif kind is ControlEventKind.MANUAL_BYPASS_DOWN:
            self._manual_bypass(event.source)
        elif kind is ControlEventKind.DEFERRED_BYPASS_DOWN:
            self._deferred_bypass_down(event.source)
        elif kind is ControlEventKind.DEFERRED_BYPASS_UP:
            self._deferred_bypass_up()
        elif kind is ControlEventKind.CTRL_DOWN:
            self._ctrl_down(event.source)
        elif kind is ControlEventKind.CTRL_UP:
            return
        elif kind is ControlEventKind.SHIFT_DOWN:
            self._shift_down(event.source)
        elif kind is ControlEventKind.SHIFT_UP:
            return
        elif kind in (
            ControlEventKind.FOREGROUND_LOST,
            ControlEventKind.FOREGROUND_UNCERTAIN,
        ):
            self._foreground_lost(kind.name, event.source)
        elif kind is ControlEventKind.HOOK_FAILURE:
            self.fatal_error = (
                event.detail
                if isinstance(event.detail, BaseException)
                else RuntimeError(f"hook failure: {event.detail}")
            )
            self._report(f"Hook failure: {self.fatal_error}")
            self._foreground_lost("hook failure", EventSource.SHUTDOWN)
        elif kind is ControlEventKind.WORKER_PROGRESS:
            self._worker_progress(event)
        elif kind is ControlEventKind.WORKER_STOPPED:
            self._worker_stopped(event)
        elif kind is ControlEventKind.SHUTDOWN:
            self.shutdown()

    def _select(self, mode: WeaponMode, source: EventSource) -> None:
        previous = self._snapshot()
        if mode is self.selected_mode:
            self._trace(
                "SAME_MODE_SELECTION_IGNORED",
                source=source,
                previous=previous,
                reason="weapon already internally selected",
            )
            return
        if self._foreground_active():
            self._foreground_loss_latched = False
        self._pending_sprint_reload = None

        if self._enabled:
            self._disable("weapon selection", source)
        else:
            # A different selection invalidates preparation/bypass work even
            # when firing has not been enabled.
            if self._worker is not None:
                self._generation += 1
            if self._worker_kind is WorkerKind.PREPARATION:
                self._preparation_invalidated = True
            if self._worker_kind is WorkerKind.RELOAD_ONLY:
                self._reload_only_invalidated = True
            if self._worker_kind is WorkerKind.MACRO:
                self._macro_reload_invalidated = True

        self.selected_mode = mode
        self._magazines[mode] = MagazineState.UNKNOWN
        self._set_preparation_lifecycle(mode, PreparationLifecycle.IDLE_UNKNOWN)
        self._armed = False
        self._discard_deferred_bypass()
        self._report(f"Selected weapon mode: {mode.value}; magazine state: UNKNOWN")
        if self._worker is not None:
            self._pending_selection = mode
            self._request_stop("weapon selection")
        else:
            self._begin_selection_preparation(source)
        self._trace(
            "WEAPON_SELECTED",
            source=source,
            previous=previous,
            reason="different physical selection edge",
        )

    def _begin_selection_preparation(self, source: EventSource) -> None:
        self._set_state(self._idle_state())
        if not self._foreground_active():
            return
        if self._config.weapons.reload_on_select:
            self._start_preparation(
                self._config.weapons.switch_settle_ms,
                source,
                "different weapon selected",
            )
        else:
            self._armed = True
            self._set_preparation_lifecycle(
                self.selected_mode, PreparationLifecycle.IDLE_FULL_ARMED
            )

    def _mb1_down(self, source: EventSource) -> None:
        previous = self._snapshot()
        if self._physical_mb1_down:
            self._start_rejected("duplicate physical MB1-down", source, previous)
            return
        self._physical_mb1_down = True
        if self._neutral_rearm_required:
            self._start_rejected("neutral MB1 release required", source, previous)
            return
        if not self._foreground_active():
            self._start_rejected("foreground is not confirmed", source, previous)
            return
        self._foreground_loss_latched = False
        if self._enabled:
            self._disable("physical MB1-down toggle", source)
            return
        if self._worker is not None and not (
            self._worker_kind is WorkerKind.PREPARATION
            and self._worker_mode is self.selected_mode
        ):
            self._start_rejected("worker cleanup is still in progress", source, previous)
            return
        if self._clock() - self._last_stop_time < self._debounce_seconds:
            self._start_rejected("toggle debounce is active", source, previous)
            return

        self._enabled = True
        # Each enabled session owns a fresh authority generation. This also
        # invalidates any selection preparation before it is detached below.
        self._generation += 1
        if self._magazines[self.selected_mode] is not MagazineState.FULL:
            self._magazines[self.selected_mode] = MagazineState.UNKNOWN
            self._set_preparation_lifecycle(
                self.selected_mode, PreparationLifecycle.IDLE_UNKNOWN
            )
            self._armed = False
        self._trace(
            "MACRO_ENABLED",
            source=source,
            previous=previous,
            reason="accepted physical MB1-down",
        )
        try:
            self._audio.notify_on()
            self._on_announced = True
        except BaseException as exc:
            # Audio must never delay or prevent cancellation or firing.
            self._report(f"ON audio notification failed: {exc}")
        if self.preparing:
            self._cancel_preparation_for_immediate_start(source)
        self._start_macro(source, "immediate physical MB1-down activation")

    def _mb1_up(self) -> None:
        # Cleanup-only by construction. This method must never authorize start,
        # toggle enabled state, start preparation, or emit audio.
        self._physical_mb1_down = False
        self._neutral_rearm_required = False

    def _mb2_down(self, source: EventSource) -> None:
        previous = self._snapshot()
        if self._aim_state is AimState.AIM_OFF_PENDING:
            self._invalidate_pending_aim(
                "physical MB2 invalidated pending aim-off output",
                source,
            )
            return
        if self._aim_state is AimState.AIM_OFF:
            self._aim_state = AimState.AIM_ON
            event_name = "AIM_PHYSICAL_ON"
            reason = "physical toggle-aim MB2-down assumed aim ON"
        elif self._aim_state is AimState.AIM_ON:
            self._aim_state = AimState.AIM_OFF
            event_name = "AIM_PHYSICAL_OFF"
            reason = "physical toggle-aim MB2-down assumed aim OFF"
        else:
            # A physical toggle cannot recover a known state from UNKNOWN.
            return
        self._trace(
            event_name,
            source=source,
            previous=previous,
            reason=reason,
        )

    def _start_macro(self, source: EventSource, reason: str) -> None:
        previous = self._snapshot()
        request = WorkerRequest(
            WorkerKind.MACRO,
            self.selected_mode,
            generation=self._generation,
        )
        self._firing_began = False
        self._macro_reload_invalidated = False
        def trace_firing_started() -> None:
            self._trace(
                "FIRING_STARTED",
                source=source,
                previous=previous,
                reason=reason,
            )

        if not self._start_worker(
            request,
            self._running_state(),
            before_activate=trace_firing_started,
        ):
            self._enabled = False
            if self._on_announced:
                self._off_pending = True
                self._finish_off()
            self._start_rejected("macro worker could not start", source, previous)

    def _cancel_preparation_for_immediate_start(
        self, source: EventSource
    ) -> None:
        worker = self._worker
        if worker is None or self._worker_kind is not WorkerKind.PREPARATION:
            return
        previous = self._snapshot()
        mode = self._worker_mode
        # cancel() only sets events; it does not acquire the backend lock, wait,
        # join, or release the new macro's MB1 ownership.
        worker.cancel()
        self._retired_preparations[worker.token] = worker
        self._worker = None
        self._worker_kind = None
        self._worker_mode = None
        self._worker_cancel_reason = None
        self._active_preparation_generation = None
        self._preparation_invalidated = False
        if mode is not None:
            self._magazines[mode] = MagazineState.UNKNOWN
            self._set_preparation_lifecycle(
                mode, PreparationLifecycle.IDLE_UNKNOWN
            )
        self._set_state(self._idle_state())
        self._trace(
            "PREPARATION_CANCELED",
            source=source,
            previous=previous,
            reason="immediate_start",
        )

    def _start_preparation(
        self,
        settle_ms: int,
        source: EventSource,
        reason: str,
    ) -> None:
        previous = self._snapshot()
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._armed = False
        self._preparation_invalidated = False
        self._generation += 1
        generation = self._generation
        self._active_preparation_generation = generation
        request = WorkerRequest(
            WorkerKind.PREPARATION,
            self.selected_mode,
            switch_settle_ms=settle_ms,
            preparation_generation=generation,
            generation=generation,
        )
        if not self._start_worker(request, self._preparing_state()):
            self._set_preparation_lifecycle(
                self.selected_mode,
                PreparationLifecycle.PREPARATION_FAILED,
            )
            self._set_preparation_lifecycle(
                self.selected_mode,
                PreparationLifecycle.IDLE_UNKNOWN,
            )
            self._active_preparation_generation = None
            self._enabled = False
            self._set_state(self._idle_state())
            self._trace(
                "PREPARATION_FAILED",
                source=EventSource.WORKER,
                previous=previous,
                reason="worker start failed",
            )
            return
        self._trace(
            "PREPARATION_STARTED",
            source=source,
            previous=previous,
            reason=reason,
        )

    def _start_deferred_bypass(self) -> None:
        release = self._deferred_release
        if release is None or self._deferred_discard or not self._foreground_active():
            self._discard_deferred_bypass()
            self._set_state(self._idle_state())
            return
        request = WorkerRequest(
            WorkerKind.BYPASS,
            self.selected_mode,
            bypass_release=release,
            bypass_click_ms=self._config.controls.deferred_bypass_click_ms,
            generation=self._generation,
        )
        if self._start_worker(request, MacroState.FORWARDING_BYPASS):
            self._diagnostic("cleanup complete; forwarding tagged deferred bypass")
        else:
            self._discard_deferred_bypass()
            self._set_state(self._idle_state())

    def _start_worker(
        self,
        request: WorkerRequest,
        target_state: MacroState,
        *,
        before_activate: Callable[[], None] | None = None,
    ) -> bool:
        if self._worker is not None:
            return False
        token = self._next_worker_token
        self._next_worker_token += 1
        try:
            worker = self._worker_factory(token, request)
        except BaseException as exc:
            self._report(f"{request.kind.name.title()} worker failed to construct: {exc}")
            return False

        # Publish ownership before starting the gated thread. Real MacroWorker
        # instances wait on activate(), so neither completion nor generated
        # output can race ahead of the state transition below.
        self._worker = worker
        self._worker_kind = request.kind
        self._worker_mode = request.mode
        self._worker_phase = f"{request.kind.name}_STARTING"
        if request.kind is WorkerKind.MACRO:
            self._coordination.macro_started()
        try:
            worker.start()
            if request.kind is WorkerKind.PREPARATION:
                self._set_preparation_lifecycle(
                    request.mode, PreparationLifecycle.PREPARING
                )
            self._set_state(target_state)
            if before_activate is not None:
                before_activate()
            worker.activate()
        except BaseException as exc:
            release_error: BaseException | None = None
            try:
                if request.kind is WorkerKind.PREPARATION:
                    worker.cancel()
                else:
                    release_error = worker.cancel_and_release()
                worker.activate()
            except BaseException as cleanup_exc:
                release_error = cleanup_exc
            if request.kind is WorkerKind.MACRO:
                # Publish cleanup completion only after the owned-input release
                # attempt, preserving the deferred-bypass ordering contract.
                self._coordination.cleanup_completed()
            self._worker = None
            self._worker_kind = None
            self._worker_mode = None
            self._worker_phase = "IDLE"
            self._set_state(self._idle_state())
            self._report(f"{request.kind.name.title()} worker failed to start: {exc}")
            if release_error is not None:
                self._report(f"Generated-input release failed: {release_error}")
            if request.kind is WorkerKind.MACRO and self._on_announced:
                self._off_pending = True
                self._finish_off()
            return False
        return True

    def _manual_bypass(self, source: EventSource) -> None:
        previous = self._snapshot()
        self._physical_mb1_down = True
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._set_preparation_lifecycle(
            self.selected_mode, PreparationLifecycle.IDLE_UNKNOWN
        )
        self._armed = False
        if self._worker is not None:
            self._generation += 1
            self._preparation_invalidated = self.preparing
            self._macro_reload_invalidated = self.running
            self._reload_only_invalidated = (
                self._worker_kind is WorkerKind.RELOAD_ONLY
            )
            self._request_stop("manual Ctrl+MB1")
        self._diagnostic("Ctrl+MB1 passed through; selected magazine marked UNKNOWN")
        self._trace(
            "MANUAL_BYPASS",
            source=source,
            previous=previous,
            reason="physical Ctrl+MB1 passed through",
        )

    def _deferred_bypass_down(self, source: EventSource) -> None:
        previous = self._snapshot()
        self._physical_mb1_down = True
        self._magazines[self.selected_mode] = MagazineState.UNKNOWN
        self._set_preparation_lifecycle(
            self.selected_mode, PreparationLifecycle.IDLE_UNKNOWN
        )
        self._armed = False
        if self._deferred_release is None:
            self._deferred_release = threading.Event()
            self._deferred_discard = False
        if self._worker is not None:
            self._generation += 1
            self._preparation_invalidated = self.preparing
            self._macro_reload_invalidated = self.running
            self._reload_only_invalidated = (
                self._worker_kind is WorkerKind.RELOAD_ONLY
            )
            self._request_stop("deferred Ctrl+MB1")
        else:
            self._start_deferred_bypass()
        self._diagnostic("deferred Ctrl+MB1 captured; requesting cleanup")
        self._trace(
            "DEFERRED_BYPASS_CAPTURED",
            source=source,
            previous=previous,
            reason="owned input cleanup required",
        )

    def _deferred_bypass_up(self) -> None:
        self._physical_mb1_down = False
        self._neutral_rearm_required = False
        if self._deferred_release is not None:
            self._deferred_release.set()
            self._diagnostic("deferred physical MB1 released")

    def _discard_deferred_bypass(self) -> None:
        if self._deferred_release is not None:
            self._deferred_release.set()
        self._deferred_release = None
        self._deferred_discard = True

    def _finish_off(self) -> None:
        if not self._off_pending:
            return
        self._off_pending = False
        self._on_announced = False
        self._audio.notify_off()
        self._last_stop_time = self._clock()

    def _disable(self, reason: str, source: EventSource) -> None:
        if not self._enabled:
            return
        previous = self._snapshot()
        # Publish disabled before cancellation. A stale completion can never
        # observe an enabled controller and resurrect the macro.
        self._enabled = False
        self._generation += 1
        self._off_pending = True
        self._macro_reload_invalidated = self.running
        self._preparation_invalidated = self.preparing
        if self._worker is not None:
            self._request_stop(reason)
        else:
            self._set_state(self._idle_state())
            self._finish_off()
        self._trace(
            "MACRO_DISABLED",
            source=source,
            previous=previous,
            reason=reason,
        )

    def _ctrl_down(self, source: EventSource) -> None:
        if self._enabled:
            self._disable("CTRL_DOWN", source)

    def _shift_down(self, source: EventSource) -> None:
        # The hook always passes Shift through. Macro sprint-stop behavior is
        # conditional on enabled state; aim cancellation is independent.
        if self._enabled:
            previous = self._snapshot()
            worker = self._worker
            mode = self._worker_mode or self.selected_mode
            self._enabled = False
            self._generation += 1
            self._off_pending = True

            preserved_reload = False
            if worker is not None and self._worker_kind is WorkerKind.MACRO:
                try:
                    preserved_reload = worker.sprint_stop()
                except BaseException as exc:
                    self._report(f"Shift sprint-stop cleanup failed: {exc}")
                    self._macro_reload_invalidated = True
                    self._worker_cancel_reason = "SHIFT_SPRINT"
                    self._coordination.cleanup_requested()
                    self._set_state(MacroState.STOPPING)
                else:
                    if preserved_reload:
                        self._preserved_macro_reload_token = worker.token
                        self._macro_reload_invalidated = False
                        self._worker_cancel_reason = "SHIFT_SPRINT"
                        self._set_state(self._reloading_state())
                    else:
                        self._macro_reload_invalidated = True
                        self._pending_sprint_reload = (mode, self._generation)
                        self._worker_cancel_reason = "SHIFT_SPRINT"
                        self._coordination.cleanup_requested()
                        self._set_state(MacroState.STOPPING)
            else:
                self._set_state(self._idle_state())

            self._trace(
                "MACRO_DISABLED",
                source=source,
                previous=previous,
                reason="SHIFT_SPRINT",
            )
            # Shift OFF is queued immediately and deduplicated by _off_pending;
            # it does not wait for firing cleanup or the allowed sprint reload.
            self._finish_off()

        self._shift_aim(source)

    def _shift_aim(self, source: EventSource) -> None:
        previous = self._snapshot()
        if self._config.controls.shift_cancels_aim_natively:
            self._aim_generation += 1
            self._aim_state = AimState.AIM_OFF
            self._trace(
                "AIM_OFF_SKIPPED",
                source=source,
                previous=previous,
                reason="Shift natively cancels aim; no generated MB2",
            )
            return
        if self._aim_state is not AimState.AIM_ON:
            self._trace(
                "AIM_OFF_SKIPPED",
                source=source,
                previous=previous,
                reason=f"conditional aim-off skipped from {self._aim_state.name}",
            )
            return

        self._aim_generation += 1
        self._aim_state = AimState.AIM_OFF_PENDING
        self._trace(
            "AIM_OFF_REQUESTED",
            source=source,
            previous=previous,
            reason="Shift requested one conditional owned MB2 pair",
        )
        self._start_aim_off_worker(source)

    def _start_aim_off_worker(self, source: EventSource) -> None:
        previous = self._snapshot()
        if self._aim_worker is not None:
            self._aim_state = AimState.UNKNOWN
            self._trace(
                "AIM_OFF_FAILED",
                source=source,
                previous=previous,
                reason="an aim-off worker is already active",
            )
            return
        token = self._next_worker_token
        self._next_worker_token += 1
        request = WorkerRequest(
            WorkerKind.AIM_OFF,
            self.selected_mode,
            generation=self._aim_generation,
        )
        try:
            worker = self._worker_factory(token, request)
        except BaseException as exc:
            self._aim_state = AimState.UNKNOWN
            self._report(f"Aim-off worker failed to construct: {exc}")
            self._trace(
                "AIM_OFF_FAILED",
                source=source,
                previous=previous,
                reason=f"worker construction failed: {exc}",
            )
            return
        self._aim_worker = worker
        try:
            worker.start()
            worker.activate()
        except BaseException as exc:
            _started, _sent, release_error = worker.cancel_aim_and_observe()
            self._retired_aim_workers[worker.token] = worker
            self._aim_worker = None
            self._aim_state = AimState.UNKNOWN
            self._report(f"Aim-off worker failed to start: {exc}")
            if release_error is not None:
                self._report(f"Generated MB2 release failed: {release_error}")
            self._trace(
                "AIM_OFF_FAILED",
                source=source,
                previous=previous,
                reason=f"worker startup failed: {exc}",
            )

    def _invalidate_pending_aim(self, reason: str, source: EventSource) -> None:
        previous = self._snapshot()
        self._aim_generation += 1
        worker = self._aim_worker
        release_error: BaseException | None = None
        if worker is not None:
            _started, _sent, release_error = worker.cancel_aim_and_observe()
            self._retired_aim_workers[worker.token] = worker
            self._aim_worker = None
        self._aim_state = AimState.UNKNOWN
        if release_error is not None:
            self._report(f"Generated MB2 release failed: {release_error}")
        self._trace(
            "AIM_OFF_FAILED",
            source=source,
            previous=previous,
            reason=reason,
        )

    def _foreground_lost(self, reason: str, source: EventSource) -> None:
        previous = self._snapshot()
        if self._foreground_loss_latched:
            return
        self._foreground_loss_latched = True
        if (
            self._aim_state is AimState.AIM_OFF_PENDING
            or self._aim_worker is not None
        ):
            self._invalidate_pending_aim(
                f"foreground loss invalidated aim-off output: {reason}",
                source,
            )
        else:
            self._aim_generation += 1
            self._aim_state = AimState.UNKNOWN
        was_enabled = self._enabled
        self._enabled = False
        self._neutral_rearm_required = True
        self._armed = False
        self._generation += 1
        if was_enabled:
            self._off_pending = True
        self._macro_reload_invalidated = self.running
        self._preparation_invalidated = self.preparing
        self._reload_only_invalidated = (
            self._worker_kind is WorkerKind.RELOAD_ONLY
        )
        self._pending_sprint_reload = None
        self._discard_deferred_bypass()
        affected_mode = self._worker_mode
        if affected_mode is not None:
            self._magazines[affected_mode] = MagazineState.UNKNOWN
            self._set_preparation_lifecycle(
                affected_mode, PreparationLifecycle.IDLE_UNKNOWN
            )
        if self._worker is not None:
            self._request_stop(reason)
        else:
            self._set_state(self._idle_state())
            self._finish_off()
        self._trace(
            "FOREGROUND_LOST",
            source=source,
            previous=previous,
            reason=reason,
        )

    def _request_stop(self, reason: str) -> None:
        worker = self._worker
        if worker is None or self.state is MacroState.STOPPING:
            return
        self._worker_cancel_reason = reason
        if self._worker_kind is WorkerKind.MACRO:
            self._coordination.cleanup_requested()
        if self._worker_kind is WorkerKind.PREPARATION and self._worker_mode is not None:
            self._preparation_invalidated = True
            self._magazines[self._worker_mode] = MagazineState.UNKNOWN
        if self._worker_kind is WorkerKind.RELOAD_ONLY:
            self._reload_only_invalidated = True
        self._set_state(MacroState.STOPPING)
        if self._worker_kind is WorkerKind.PREPARATION:
            # Preparation cancellation is event-only and never waits for its
            # thread or the output lock. Completion remains generation-gated.
            worker.cancel()
            release_error = None
        else:
            release_error = worker.cancel_and_release()
        if release_error is not None:
            if self._worker_mode is not None:
                self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            self._report(f"Generated-input release failed: {release_error}")

    def _worker_progress(self, event: ControlEvent) -> None:
        if self._worker is None or event.worker_token != self._worker.token:
            return
        if self._worker_mode is None:
            return
        update = (
            event.detail
            if isinstance(event.detail, WorkerProgressUpdate)
            else WorkerProgressUpdate(
                event.detail,
                self._clock(),
                "worker phase update",
            )
            if isinstance(event.detail, WorkerProgress)
            else None
        )
        if update is None:
            return
        previous = self._snapshot()
        self._worker_phase = update.phase.name
        if update.phase is not WorkerProgress.SHOT_BEGAN:
            self._trace(
                update.phase.name,
                source=EventSource.WORKER,
                previous=previous,
                reason=update.reason,
                occurred_at=update.occurred_at,
            )

        if self._worker_kind is WorkerKind.RELOAD_ONLY:
            if update.phase is WorkerProgress.RELOAD_FAILED:
                self._reload_only_invalidated = True
            return
        if self._worker_kind is WorkerKind.PREPARATION:
            if update.phase is WorkerProgress.RELOAD_FAILED:
                self._preparation_invalidated = True
            return
        if self._worker_kind is not WorkerKind.MACRO:
            return

        generation_matches = self._worker.request.generation == self._generation
        preserved_reload = event.worker_token == self._preserved_macro_reload_token
        if update.phase is WorkerProgress.SHOT_BEGAN:
            if not generation_matches or not self._enabled:
                return
            self._firing_began = True
            self._magazines[self._worker_mode] = MagazineState.UNKNOWN
            self._set_preparation_lifecycle(
                self._worker_mode, PreparationLifecycle.IDLE_UNKNOWN
            )
            self._armed = False
        elif update.phase is WorkerProgress.RELOAD_COMPLETED:
            valid_authority = (
                generation_matches and self._enabled
            ) or preserved_reload
            if (
                valid_authority
                and not self._macro_reload_invalidated
                and self._foreground_active()
                and self._worker_mode is self.selected_mode
            ):
                self._magazines[self._worker_mode] = MagazineState.FULL
                self._armed = True
                self._set_preparation_lifecycle(
                    self._worker_mode, PreparationLifecycle.IDLE_FULL_ARMED
                )
        elif update.phase is WorkerProgress.RELOAD_FAILED:
            self._macro_reload_invalidated = True

    def _start_reload_only(self, mode: WeaponMode, generation: int) -> None:
        previous = self._snapshot()
        if (
            generation != self._generation
            or mode is not self.selected_mode
            or not self._foreground_active()
            or self._worker is not None
        ):
            return
        self._reload_only_invalidated = False
        self._magazines[mode] = MagazineState.UNKNOWN
        self._armed = False
        request = WorkerRequest(
            WorkerKind.RELOAD_ONLY,
            mode,
            generation=generation,
        )
        if not self._start_worker(request, self._reloading_state()):
            self._reload_only_invalidated = True
            self._set_state(self._idle_state())
            return
        self._trace(
            "SPRINT_RELOAD_STARTED",
            source=EventSource.WORKER,
            previous=previous,
            reason="SHIFT_SPRINT firing cleanup completed",
        )

    def _worker_stopped(self, event: ControlEvent) -> None:
        previous = self._snapshot()
        retired_aim = (
            self._retired_aim_workers.pop(event.worker_token, None)
            if event.worker_token is not None
            else None
        )
        if retired_aim is not None:
            return
        if (
            self._aim_worker is not None
            and event.worker_token == self._aim_worker.token
        ):
            result = (
                event.detail
                if isinstance(event.detail, WorkerResult)
                else WorkerResult(
                    False,
                    error=RuntimeError(f"invalid aim worker result {event.detail!r}"),
                )
            )
            request = self._aim_worker.request
            self._aim_worker = None
            aim_succeeded = (
                result.success
                and not result.canceled
                and result.error is None
                and request.generation == self._aim_generation
                and self._aim_state is AimState.AIM_OFF_PENDING
                and self._foreground_active()
            )
            if aim_succeeded:
                self._aim_state = AimState.AIM_OFF
                self._trace(
                    "AIM_OFF_SENT",
                    source=EventSource.WORKER,
                    previous=previous,
                    reason="one owned tagged MB2 pair completed",
                )
            else:
                self._aim_state = AimState.UNKNOWN
                reason = (
                    f"aim-off output error: {result.error}"
                    if result.error is not None
                    else "aim-off output was canceled or obsolete"
                )
                self._trace(
                    "AIM_OFF_FAILED",
                    source=EventSource.WORKER,
                    previous=previous,
                    reason=reason,
                )
            return
        retired = (
            self._retired_preparations.pop(event.worker_token, None)
            if event.worker_token is not None
            else None
        )
        if retired is not None:
            self._trace(
                "OBSOLETE_PREPARATION_RESULT_IGNORED",
                source=EventSource.WORKER,
                previous=previous,
                reason="immediate_start generation invalidation",
            )
            return
        if self._worker is None or event.worker_token != self._worker.token:
            self._trace(
                "OBSOLETE_WORKER_RESULT_IGNORED",
                source=EventSource.WORKER,
                previous=previous,
                reason="worker token mismatch",
            )
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
        request = self._worker.request
        cancellation_reason = self._worker_cancel_reason
        generation_matches = request.generation == self._generation
        preserved_macro_reload = (
            kind is WorkerKind.MACRO
            and event.worker_token == self._preserved_macro_reload_token
        )
        self._worker = None
        self._worker_kind = None
        self._worker_mode = None
        self._worker_cancel_reason = None
        self._worker_phase = "IDLE"
        if kind is WorkerKind.MACRO:
            self._coordination.cleanup_completed()

        if kind is WorkerKind.PREPARATION:
            preparation_succeeded = (
                result.success
                and not result.canceled
                and result.error is None
                and not self._preparation_invalidated
                and self._foreground_active()
                and mode is not None
                and generation_matches
                and request.preparation_generation
                == self._active_preparation_generation
                and mode is self.selected_mode
            )
            if preparation_succeeded and mode is not None:
                self._magazines[mode] = MagazineState.FULL
                self._armed = True
                self._set_preparation_lifecycle(
                    mode, PreparationLifecycle.IDLE_FULL_ARMED
                )
                self._set_state(self._idle_state())
                self._trace(
                    "PREPARATION_COMPLETED",
                    source=EventSource.WORKER,
                    previous=previous,
                    reason="reload input and foreground wait completed",
                )
            else:
                failure_reason = self._preparation_failure_reason(
                    result, generation_matches, cancellation_reason
                )
                if mode is not None:
                    self._magazines[mode] = MagazineState.UNKNOWN
                    self._set_preparation_lifecycle(
                        mode, PreparationLifecycle.PREPARATION_FAILED
                    )
                self._armed = False
                self._report(
                    f"Preparation failed for "
                    f"{mode.value if mode is not None else 'unknown weapon'}: "
                    f"{failure_reason}"
                )
                if mode is not None:
                    self._set_preparation_lifecycle(
                        mode, PreparationLifecycle.IDLE_UNKNOWN
                    )
                self._set_state(self._idle_state())
                self._trace(
                    "PREPARATION_FAILED",
                    source=EventSource.WORKER,
                    previous=previous,
                    reason=failure_reason,
                )
            if self._active_preparation_generation == request.preparation_generation:
                self._active_preparation_generation = None
            self._preparation_invalidated = False

        elif kind is WorkerKind.MACRO:
            if result.error is not None:
                self._report(f"Macro stopped after error: {result.error}")
                self._discard_deferred_bypass()
            preserved_reload_succeeded = (
                preserved_macro_reload
                and result.success
                and not result.canceled
                and result.error is None
                and not self._macro_reload_invalidated
                and mode is not None
                and self._magazines[mode] is MagazineState.FULL
            )
            if mode is not None and not preserved_reload_succeeded and (
                result.error is not None
                or self._firing_began
                or self._macro_reload_invalidated
            ):
                self._magazines[mode] = MagazineState.UNKNOWN
                self._set_preparation_lifecycle(
                    mode, PreparationLifecycle.IDLE_UNKNOWN
                )
                self._armed = False
            if self._enabled:
                self._enabled = False
                self._generation += 1
                self._off_pending = True
            self._set_state(self._idle_state())
            self._trace(
                "FIRING_STOPPED",
                source=EventSource.WORKER,
                previous=previous,
                reason=cancellation_reason
                or (f"input API failure: {result.error}" if result.error else "worker stopped"),
            )
            if preserved_macro_reload:
                self._preserved_macro_reload_token = None

        elif kind is WorkerKind.RELOAD_ONLY:
            reload_succeeded = (
                result.success
                and not result.canceled
                and result.error is None
                and not self._reload_only_invalidated
                and generation_matches
                and self._foreground_active()
                and mode is not None
                and mode is self.selected_mode
            )
            if reload_succeeded and mode is not None:
                self._magazines[mode] = MagazineState.FULL
                self._armed = True
                self._set_preparation_lifecycle(
                    mode, PreparationLifecycle.IDLE_FULL_ARMED
                )
                reason = "SHIFT_SPRINT reload completed while macro disabled"
                event_name = "SPRINT_RELOAD_COMPLETED"
            else:
                if mode is not None:
                    self._magazines[mode] = MagazineState.UNKNOWN
                    self._set_preparation_lifecycle(
                        mode, PreparationLifecycle.IDLE_UNKNOWN
                    )
                self._armed = False
                reason = (
                    f"input/foreground error: {result.error}"
                    if result.error is not None
                    else cancellation_reason or "sprint reload invalidated"
                )
                event_name = "SPRINT_RELOAD_FAILED"
            self._set_state(self._idle_state())
            self._trace(
                event_name,
                source=EventSource.WORKER,
                previous=previous,
                reason=reason,
            )
            self._reload_only_invalidated = False

        elif kind is WorkerKind.BYPASS:
            if mode is not None:
                self._magazines[mode] = MagazineState.UNKNOWN
                self._set_preparation_lifecycle(
                    mode, PreparationLifecycle.IDLE_UNKNOWN
                )
            self._armed = False
            self._set_state(self._idle_state())
            self._trace(
                "DEFERRED_BYPASS_COMPLETED",
                source=EventSource.INJECTED_BYPASS,
                previous=previous,
                reason="tagged bypass pair completed",
            )

        if self._off_pending:
            self._finish_off()

        if self._pending_selection is not None:
            self._pending_selection = None
            self._begin_selection_preparation(EventSource.PHYSICAL)
            return
        if self._pending_sprint_reload is not None and kind is WorkerKind.MACRO:
            sprint_mode, sprint_generation = self._pending_sprint_reload
            self._pending_sprint_reload = None
            if result.error is None:
                self._start_reload_only(sprint_mode, sprint_generation)
            return
        if (
            kind is not WorkerKind.BYPASS
            and self._deferred_release is not None
            and not self._deferred_discard
        ):
            self._start_deferred_bypass()
            return
        if kind is WorkerKind.PREPARATION:
            return
        if kind is WorkerKind.BYPASS:
            self._discard_deferred_bypass()

    def _preparation_failure_reason(
        self,
        result: WorkerResult,
        generation_matches: bool,
        cancellation_reason: str | None,
    ) -> str:
        if result.error is not None:
            return f"input/foreground error: {result.error}"
        if not generation_matches:
            return "obsolete preparation generation"
        if self._preparation_invalidated:
            return cancellation_reason or "preparation invalidated"
        if result.canceled:
            return cancellation_reason or "preparation canceled"
        if not self._foreground_active():
            return "foreground is not confirmed"
        return "worker did not verify reload completion"

    def shutdown(self) -> None:
        if (
            self.state is MacroState.SHUTTING_DOWN
            and self._worker is None
            and self._aim_worker is None
        ):
            return
        previous = self._snapshot()
        was_enabled = self._enabled
        self._enabled = False
        self._generation += 1
        self._set_state(MacroState.SHUTTING_DOWN)
        self._pending_selection = None
        self._neutral_rearm_required = True
        self._macro_reload_invalidated = True
        self._preparation_invalidated = True
        self._reload_only_invalidated = True
        self._aim_generation += 1
        self._aim_state = AimState.UNKNOWN
        self._pending_sprint_reload = None
        self._preserved_macro_reload_token = None
        if was_enabled:
            self._off_pending = True
        self._discard_deferred_bypass()
        aim_worker = self._aim_worker
        if aim_worker is not None:
            _started, _sent, aim_release_error = (
                aim_worker.cancel_aim_and_observe()
            )
            if aim_release_error is not None:
                self._report(
                    f"Generated MB2 release failed: {aim_release_error}"
                )
            aim_worker.join(2.0)
            if aim_worker.is_alive():
                self._report("Aim-off worker did not exit within 2 seconds")
            self._aim_worker = None
        worker = self._worker
        if worker is not None:
            if self._worker_mode is not None:
                self._magazines[self._worker_mode] = MagazineState.UNKNOWN
                self._set_preparation_lifecycle(
                    self._worker_mode,
                    PreparationLifecycle.IDLE_UNKNOWN,
                )
            if self._worker_kind is WorkerKind.PREPARATION:
                worker.cancel()
                release_error = None
            else:
                release_error = worker.cancel_and_release()
            if release_error is not None:
                self._report(f"Generated-input release failed: {release_error}")
            if self._worker_kind is not WorkerKind.PREPARATION:
                worker.join(2.0)
                if worker.is_alive():
                    self._report("Worker did not exit within 2 seconds")
            self._worker = None
            self._worker_kind = None
            self._worker_mode = None
            self._worker_phase = "IDLE"
        for retired_worker in self._retired_preparations.values():
            retired_worker.cancel()
        self._retired_preparations.clear()
        for retired_aim in self._retired_aim_workers.values():
            retired_aim.cancel_aim_and_observe()
            retired_aim.join(2.0)
        self._retired_aim_workers.clear()
        self._coordination.cleanup_completed()
        self._finish_off()
        self._trace(
            "SHUTDOWN",
            source=EventSource.SHUTDOWN,
            previous=previous,
            reason="application shutdown",
        )
