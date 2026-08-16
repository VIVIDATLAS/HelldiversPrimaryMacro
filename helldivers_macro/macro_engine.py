from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from .config import AppConfig
from .input_backend import InputApiError
from .models import (
    CycleStep,
    OutputAction,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerRequest,
    WorkerResult,
)


class ForegroundLost(RuntimeError):
    pass


class GeneratedInput(Protocol):
    def mouse_down(self) -> None: ...
    def mouse_up(self) -> None: ...
    def reload_down(self) -> None: ...
    def reload_up(self) -> None: ...
    def release_all(self) -> None: ...


def primary_cycle_steps(config: AppConfig) -> list[CycleStep]:
    steps: list[CycleStep] = []
    primary = config.primary
    for shot in range(primary.shots_per_cycle):
        steps.extend(
            [
                CycleStep(OutputAction.MB1_DOWN),
                CycleStep(OutputAction.WAIT, primary.fire_hold_ms),
                CycleStep(OutputAction.MB1_UP),
            ]
        )
        if shot < primary.shots_per_cycle - 1:
            steps.append(CycleStep(OutputAction.WAIT, primary.inter_shot_ms))
    steps.extend(
        [
            CycleStep(OutputAction.WAIT, primary.post_last_shot_ms),
            CycleStep(OutputAction.R_DOWN),
            CycleStep(OutputAction.WAIT, primary.reload_press_ms),
            CycleStep(OutputAction.R_UP),
            CycleStep(OutputAction.WAIT, primary.reload_wait_ms),
        ]
    )
    return steps


def secondary_cycle_steps(config: AppConfig) -> list[CycleStep]:
    steps: list[CycleStep] = []
    secondary = config.secondary
    between_ms = secondary.shot_period_ms - secondary.fire_press_ms
    for _ in range(secondary.shots_per_cycle):
        steps.extend(
            [
                CycleStep(OutputAction.MB1_DOWN),
                CycleStep(OutputAction.WAIT, secondary.fire_press_ms),
                CycleStep(OutputAction.MB1_UP),
                CycleStep(OutputAction.WAIT, between_ms),
            ]
        )
    steps.extend(
        [
            CycleStep(OutputAction.R_DOWN),
            CycleStep(OutputAction.WAIT, secondary.reload_press_ms),
            CycleStep(OutputAction.R_UP),
            CycleStep(OutputAction.WAIT, secondary.reload_wait_ms),
        ]
    )
    return steps


class MacroEngine:
    def __init__(
        self,
        config: AppConfig,
        backend: GeneratedInput,
        foreground_active: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.perf_counter,
        wait: Callable[[threading.Event, float], bool] | None = None,
        io_lock: threading.RLock | None = None,
    ) -> None:
        self._config = config
        self.backend = backend
        self._foreground_active = foreground_active
        self._clock = clock
        self._wait = wait or (lambda event, seconds: event.wait(seconds))
        self.io_lock = io_lock or threading.RLock()
        self._poll_seconds = config.controls.poll_ms / 1000.0

    def _check_active(
        self, cancel_event: threading.Event, shutdown_event: threading.Event
    ) -> None:
        if cancel_event.is_set() or shutdown_event.is_set():
            raise InterruptedError("macro canceled")
        if not self._foreground_active():
            raise ForegroundLost("configured target is not confirmed foreground")

    def _cancelable_wait(
        self,
        duration_ms: int,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> None:
        deadline = self._clock() + duration_ms / 1000.0
        while True:
            self._check_active(cancel_event, shutdown_event)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._wait(cancel_event, min(self._poll_seconds, remaining))

    def _perform_output(
        self,
        action: OutputAction,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> None:
        with self.io_lock:
            self._check_active(cancel_event, shutdown_event)
            if action is OutputAction.MB1_DOWN:
                self.backend.mouse_down()
            elif action is OutputAction.MB1_UP:
                self.backend.mouse_up()
            elif action is OutputAction.R_DOWN:
                self.backend.reload_down()
            elif action is OutputAction.R_UP:
                self.backend.reload_up()
            else:
                raise AssertionError(f"unexpected output action: {action}")

    def _finish_result(
        self,
        *,
        success: bool,
        canceled: bool,
        error: BaseException | None,
    ) -> WorkerResult:
        try:
            with self.io_lock:
                self.backend.release_all()
        except BaseException as release_exc:
            if error is None:
                error = release_exc
            else:
                error = RuntimeError(f"{error}; cleanup failed: {release_exc}")
            success = False
        return WorkerResult(success=success, canceled=canceled, error=error)

    def run_macro(
        self,
        mode: WeaponMode,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
        progress: Callable[[WorkerProgress], None] = lambda _progress: None,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        steps = (
            primary_cycle_steps(self._config)
            if mode is WeaponMode.PRIMARY
            else secondary_cycle_steps(self._config)
        )
        try:
            while True:
                for index, step in enumerate(steps):
                    if step.action is OutputAction.WAIT:
                        self._cancelable_wait(
                            step.duration_ms, cancel_event, shutdown_event
                        )
                    else:
                        self._perform_output(step.action, cancel_event, shutdown_event)
                        if step.action is OutputAction.MB1_DOWN:
                            progress(WorkerProgress.SHOT_BEGAN)
                    if index == len(steps) - 1:
                        # The final step is the complete reload wait. It can be
                        # reported only after every foreground/cancel check passed.
                        progress(WorkerProgress.RELOAD_COMPLETE)
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:  # Worker boundary; cleanup still has priority.
            error = exc
        return self._finish_result(success=False, canceled=canceled, error=error)

    def prepare_reload(
        self,
        mode: WeaponMode,
        switch_settle_ms: int,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False
        weapon = self._config.primary if mode is WeaponMode.PRIMARY else self._config.secondary
        try:
            if switch_settle_ms:
                self._cancelable_wait(
                    switch_settle_ms, cancel_event, shutdown_event
                )
            self._perform_output(OutputAction.R_DOWN, cancel_event, shutdown_event)
            self._cancelable_wait(
                weapon.reload_press_ms, cancel_event, shutdown_event
            )
            self._perform_output(OutputAction.R_UP, cancel_event, shutdown_event)
            self._cancelable_wait(
                weapon.reload_wait_ms, cancel_event, shutdown_event
            )
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success, canceled=canceled, error=error
        )

    def forward_bypass(
        self,
        physical_release: threading.Event,
        minimum_click_ms: int,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False
        try:
            # This release is deliberately ordered before the bypass down, even
            # if a prior macro cleanup reported completion with stale ownership.
            with self.io_lock:
                self.backend.release_all()
            self._perform_output(
                OutputAction.MB1_DOWN, cancel_event, shutdown_event
            )
            down_at = self._clock()
            while not physical_release.is_set():
                self._check_active(cancel_event, shutdown_event)
                self._wait(cancel_event, self._poll_seconds)
            held_ms = (self._clock() - down_at) * 1000.0
            remaining_ms = max(0.0, minimum_click_ms - held_ms)
            if remaining_ms:
                self._cancelable_wait(
                    int(remaining_ms + 0.999), cancel_event, shutdown_event
                )
            self._perform_output(OutputAction.MB1_UP, cancel_event, shutdown_event)
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success, canceled=canceled, error=error
        )


class MacroWorker:
    def __init__(
        self,
        token: int,
        request: WorkerRequest,
        engine: MacroEngine,
        shutdown_event: threading.Event,
        on_complete: Callable[[int, WorkerResult], None],
        on_progress: Callable[[int, WorkerProgress], None],
    ) -> None:
        self.token = token
        self.request = request
        self.mode = request.mode
        self._engine = engine
        self._shutdown = shutdown_event
        self._on_complete = on_complete
        self._on_progress = on_progress
        self.cancel_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"{request.kind.name.lower()}-{request.mode.value.lower()}",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def cancel_and_release(self) -> BaseException | None:
        self.cancel_event.set()
        try:
            with self._engine.io_lock:
                self._engine.backend.release_all()
        except BaseException as exc:
            return exc
        return None

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        if self.request.kind is WorkerKind.MACRO:
            result = self._engine.run_macro(
                self.mode,
                self.cancel_event,
                self._shutdown,
                lambda progress: self._on_progress(self.token, progress),
            )
        elif self.request.kind is WorkerKind.PREPARATION:
            result = self._engine.prepare_reload(
                self.mode,
                self.request.switch_settle_ms,
                self.cancel_event,
                self._shutdown,
            )
        elif self.request.kind is WorkerKind.BYPASS:
            result = self._engine.forward_bypass(
                self.request.bypass_release,
                self.request.bypass_click_ms,
                self.cancel_event,
                self._shutdown,
            )
        else:
            result = WorkerResult(
                False, error=RuntimeError(f"unsupported worker kind {self.request.kind}")
            )
        self._on_complete(self.token, result)
