from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from typing import Callable, Protocol

from .cadence_diagnostics import CadenceDiagnostics
from .config import AppConfig
from .input_backend import InputApiError, InputCoordination
from .models import (
    CycleStep,
    OutputAction,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerProgressUpdate,
    WorkerRequest,
    WorkerResult,
)
from .stratagems import Direction, LEFT_CTRL_SCAN_CODE


CONTROL_REPLAY_PRESS_MS = 20


class ForegroundLost(RuntimeError):
    pass


class GeneratedInput(Protocol):
    def fire_down(self) -> None: ...
    def fire_up(self) -> None: ...
    def mouse_down(self) -> None: ...
    def mouse_up(self) -> None: ...
    def aim_down(self) -> None: ...
    def aim_up(self) -> None: ...
    def shift_down(self, scan_code: int) -> None: ...
    def shift_up(self) -> None: ...
    def reload_down(self) -> None: ...
    def reload_up(self) -> None: ...
    def release_shift_inputs(self) -> None: ...
    def release_all(self) -> None: ...
    def stratagem_key_down(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None: ...
    def stratagem_key_up(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None: ...
    def stratagem_mouse_down(self, token: int) -> None: ...
    def stratagem_mouse_up(self, token: int) -> None: ...
    def release_stratagem(self, token: int) -> None: ...


def primary_cycle_steps(config: AppConfig) -> list[CycleStep]:
    primary = config.primary
    if primary.fire_mode == "automatic_hold":
        assert primary.automatic_hold_ms is not None
        steps = [
            CycleStep(OutputAction.FIRE_DOWN),
            CycleStep(OutputAction.WAIT, primary.automatic_hold_ms),
            CycleStep(OutputAction.FIRE_UP),
        ]
        if primary.post_fire_reload_delay_ms:
            steps.append(
                CycleStep(
                    OutputAction.WAIT,
                    primary.post_fire_reload_delay_ms,
                )
            )
        steps.extend(
            [
                CycleStep(OutputAction.R_DOWN),
                CycleStep(OutputAction.WAIT, primary.reload_press_ms),
                CycleStep(OutputAction.R_UP),
                CycleStep(OutputAction.WAIT, primary.reload_wait_ms),
            ]
        )
        return steps

    assert primary.shots_per_cycle is not None
    assert primary.shot_period_ms is not None
    assert primary.fire_press_ms is not None
    steps: list[CycleStep] = []
    between_ms = primary.shot_period_ms - primary.fire_press_ms
    for shot in range(primary.shots_per_cycle):
        steps.extend(
            [
                CycleStep(OutputAction.FIRE_DOWN),
                CycleStep(OutputAction.WAIT, primary.fire_press_ms),
                CycleStep(OutputAction.FIRE_UP),
            ]
        )
        if shot < primary.shots_per_cycle - 1:
            steps.append(CycleStep(OutputAction.WAIT, between_ms))
    steps.extend(
        [
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
    if secondary.fire_mode == "automatic_hold":
        assert secondary.automatic_hold_ms is not None
        steps.extend(
            [
                CycleStep(OutputAction.FIRE_DOWN),
                CycleStep(OutputAction.WAIT, secondary.automatic_hold_ms),
                CycleStep(OutputAction.FIRE_UP),
            ]
        )
        if secondary.post_fire_reload_delay_ms:
            steps.append(
                CycleStep(
                    OutputAction.WAIT,
                    secondary.post_fire_reload_delay_ms,
                )
            )
    else:
        assert secondary.shots_per_cycle is not None
        assert secondary.shot_period_ms is not None
        assert secondary.fire_press_ms is not None
        between_ms = secondary.shot_period_ms - secondary.fire_press_ms
        for shot in range(secondary.shots_per_cycle):
            steps.extend(
                [
                    CycleStep(OutputAction.FIRE_DOWN),
                    CycleStep(OutputAction.WAIT, secondary.fire_press_ms),
                    CycleStep(OutputAction.FIRE_UP),
                ]
            )
            if shot < secondary.shots_per_cycle - 1:
                steps.append(CycleStep(OutputAction.WAIT, between_ms))
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
        cadence_diagnostics: CadenceDiagnostics | None = None,
    ) -> None:
        self._config = config
        self.backend = backend
        self._foreground_active = foreground_active
        self._clock = clock
        self._wait = wait or (lambda event, seconds: event.wait(seconds))
        self.io_lock = io_lock or threading.RLock()
        self._cadence_diagnostics = cadence_diagnostics
        self._poll_seconds = config.controls.poll_ms / 1000.0
        self._fire_action_names = (
            ("P_DOWN", "P_UP")
            if config.output.fire_device == "keyboard"
            else ("MB1_DOWN", "MB1_UP")
        )

    def _diagnostic_action(self, action: OutputAction) -> str:
        if action is OutputAction.FIRE_DOWN:
            return self._fire_action_names[0]
        if action is OutputAction.FIRE_UP:
            return self._fire_action_names[1]
        return action.value

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

    def _perform_outputs(
        self,
        actions: list[OutputAction],
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
        reload_started: Callable[[], None] = lambda: None,
    ) -> None:
        with self.io_lock:
            self._check_active(cancel_event, shutdown_event)
            for action in actions:
                if self._cadence_diagnostics is None:
                    if action is OutputAction.FIRE_DOWN:
                        self.backend.fire_down()
                    elif action is OutputAction.FIRE_UP:
                        self.backend.fire_up()
                    elif action is OutputAction.R_DOWN:
                        self.backend.reload_down()
                        reload_started()
                    elif action is OutputAction.R_UP:
                        self.backend.reload_up()
                    else:
                        raise AssertionError(f"unexpected output action: {action}")
                    continue
                with self._cadence_diagnostics.macro_action(
                    self._diagnostic_action(action)
                ):
                    if action is OutputAction.FIRE_DOWN:
                        self.backend.fire_down()
                    elif action is OutputAction.FIRE_UP:
                        self.backend.fire_up()
                    elif action is OutputAction.R_DOWN:
                        self.backend.reload_down()
                        reload_started()
                    elif action is OutputAction.R_UP:
                        self.backend.reload_up()
                    else:
                        raise AssertionError(f"unexpected output action: {action}")

    def _perform_output(
        self,
        action: OutputAction,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> None:
        self._perform_outputs([action], cancel_event, shutdown_event)

    def _finish_result(
        self,
        *,
        success: bool,
        canceled: bool,
        error: BaseException | None,
        release_owned: Callable[[], None],
    ) -> WorkerResult:
        try:
            with self.io_lock:
                release_owned()
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
        progress: Callable[[WorkerProgressUpdate], None] = lambda _progress: None,
        finish_after_reload: threading.Event | None = None,
        reload_started: Callable[[], None] = lambda: None,
        firing_started: Callable[[], None] = lambda: None,
        firing_stopped: Callable[[], None] = lambda: None,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False
        reload_phase_started = False
        reload_completed = False
        finish_after_reload = finish_after_reload or threading.Event()
        steps = (
            primary_cycle_steps(self._config)
            if mode is WeaponMode.PRIMARY
            else secondary_cycle_steps(self._config)
        )
        weapon = (
            self._config.primary
            if mode is WeaponMode.PRIMARY
            else self._config.secondary
        )
        total_shots = (
            1 if weapon.fire_mode == "automatic_hold" else weapon.shots_per_cycle
        )
        assert total_shots is not None
        shot_count = 0
        if self._cadence_diagnostics is not None:
            self._cadence_diagnostics.macro_worker_started(mode.value)

        def report(phase: WorkerProgress, reason: str) -> None:
            progress(WorkerProgressUpdate(phase, self._clock(), reason))

        try:
            while not success:
                shot_count = 0
                reload_phase_started = False
                reload_completed = False
                firing_snapshot_published = False
                index = 0
                while index < len(steps):
                    step = steps[index]
                    if step.action is OutputAction.WAIT:
                        self._cancelable_wait(
                            step.duration_ms, cancel_event, shutdown_event
                        )
                        if index == len(steps) - 1:
                            reload_completed = True
                            report(
                                WorkerProgress.RELOAD_COMPLETED,
                                "configured reload wait completed",
                            )
                            if finish_after_reload.is_set():
                                success = True
                        index += 1
                        continue

                    actions: list[OutputAction] = []
                    while (
                        index < len(steps)
                        and steps[index].action is not OutputAction.WAIT
                    ):
                        actions.append(steps[index].action)
                        index += 1
                    # Consecutive output actions share one short I/O boundary.
                    # Each profile's final fire-up and R-down therefore have no wait,
                    # controller queue operation, or lock release between them.
                    if (
                        OutputAction.FIRE_DOWN in actions
                        and not firing_snapshot_published
                    ):
                        firing_started()
                        firing_snapshot_published = True
                    self._perform_outputs(
                        actions,
                        cancel_event,
                        shutdown_event,
                        reload_started,
                    )
                    if OutputAction.R_DOWN in actions:
                        firing_stopped()
                        firing_snapshot_published = False
                    for action in actions:
                        if action is OutputAction.FIRE_DOWN:
                            shot_count += 1
                            report(
                                WorkerProgress.SHOT_BEGAN,
                                "generated shot began",
                            )
                            if shot_count == total_shots:
                                report(
                                    WorkerProgress.FINAL_SHOT_DOWN,
                                    "final configured shot pressed",
                                )
                        elif action is OutputAction.FIRE_UP and shot_count == total_shots:
                            report(
                                WorkerProgress.FINAL_SHOT_UP,
                                "final configured shot released",
                            )
                        elif action is OutputAction.R_DOWN:
                            reload_phase_started = True
                            report(
                                WorkerProgress.RELOAD_KEY_DOWN,
                                "reload key pressed after firing phase",
                            )
                        elif action is OutputAction.R_UP:
                            report(
                                WorkerProgress.RELOAD_KEY_UP,
                                "configured reload key press completed",
                            )
                            report(
                                WorkerProgress.RELOAD_WAIT_STARTED,
                                "reload wait began after R-up",
                            )
        except InterruptedError as exc:
            canceled = True
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        except (ForegroundLost, InputApiError) as exc:
            error = exc
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        except BaseException as exc:  # Worker boundary; cleanup still has priority.
            error = exc
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        firing_stopped()
        result = self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            release_owned=self.backend.release_all,
        )
        if self._cadence_diagnostics is not None:
            self._cadence_diagnostics.macro_worker_stopped()
        return result

    def prepare_reload(
        self,
        mode: WeaponMode,
        switch_settle_ms: int,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
        progress: Callable[[WorkerProgressUpdate], None] = lambda _progress: None,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False
        reload_phase_started = False
        reload_completed = False
        weapon = self._config.primary if mode is WeaponMode.PRIMARY else self._config.secondary

        def report(phase: WorkerProgress, reason: str) -> None:
            progress(WorkerProgressUpdate(phase, self._clock(), reason))

        try:
            if switch_settle_ms:
                self._cancelable_wait(
                    switch_settle_ms, cancel_event, shutdown_event
                )
            self._perform_output(OutputAction.R_DOWN, cancel_event, shutdown_event)
            reload_phase_started = True
            report(WorkerProgress.RELOAD_KEY_DOWN, "reload-only R-down issued")
            self._cancelable_wait(
                weapon.reload_press_ms, cancel_event, shutdown_event
            )
            self._perform_output(OutputAction.R_UP, cancel_event, shutdown_event)
            report(
                WorkerProgress.RELOAD_KEY_UP,
                "configured reload key press completed",
            )
            report(
                WorkerProgress.RELOAD_WAIT_STARTED,
                "reload wait began after R-up",
            )
            self._cancelable_wait(
                weapon.reload_wait_ms, cancel_event, shutdown_event
            )
            reload_completed = True
            report(
                WorkerProgress.RELOAD_COMPLETED,
                "configured reload wait completed",
            )
            success = True
        except InterruptedError as exc:
            canceled = True
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        except (ForegroundLost, InputApiError) as exc:
            error = exc
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        except BaseException as exc:
            error = exc
            if reload_phase_started and not reload_completed:
                report(WorkerProgress.RELOAD_FAILED, str(exc))
        return self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            # A retired preparation may overlap a newly started macro. It may
            # release only the R key it could own, never the new macro's MB1.
            release_owned=self.backend.reload_up,
        )

    def send_shift_transaction(
        self,
        shift_scan_code: int,
        cancel_aim: bool,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
        progress: Callable[[WorkerProgressUpdate], None] = lambda _progress: None,
        aim_started: Callable[[], None] = lambda: None,
        aim_sent: Callable[[], None] = lambda: None,
        shift_started: Callable[[], None] = lambda: None,
        shift_sent: Callable[[], None] = lambda: None,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False

        def report(phase: WorkerProgress, reason: str) -> None:
            progress(WorkerProgressUpdate(phase, self._clock(), reason))

        try:
            if cancel_aim:
                with self.io_lock:
                    self._check_active(cancel_event, shutdown_event)
                    self.backend.aim_down()
                    aim_started()
                self._cancelable_wait(
                    CONTROL_REPLAY_PRESS_MS, cancel_event, shutdown_event
                )
                with self.io_lock:
                    self._check_active(cancel_event, shutdown_event)
                    self.backend.aim_up()
                    aim_sent()
                report(
                    WorkerProgress.AIM_OFF_SENT,
                    "owned tagged MB2 aim-off pair completed before Shift replay",
                )

            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                self.backend.shift_down(shift_scan_code)
                shift_started()
            report(
                WorkerProgress.SHIFT_REPLAY_DOWN,
                "owned tagged physical-scan Shift replay pressed",
            )
            self._cancelable_wait(
                CONTROL_REPLAY_PRESS_MS, cancel_event, shutdown_event
            )
            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                self.backend.shift_up()
                shift_sent()
            report(
                WorkerProgress.SHIFT_REPLAY_UP,
                "owned tagged Shift replay released",
            )
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            release_owned=self.backend.release_shift_inputs,
        )

    def send_aim_off_transaction(
        self,
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
        progress: Callable[[WorkerProgressUpdate], None] = lambda _progress: None,
        aim_started: Callable[[], None] = lambda: None,
        aim_sent: Callable[[], None] = lambda: None,
    ) -> WorkerResult:
        """Replay one captured physical RMB pair after firing cleanup."""
        error: BaseException | None = None
        canceled = False
        success = False

        def report(phase: WorkerProgress, reason: str) -> None:
            progress(WorkerProgressUpdate(phase, self._clock(), reason))

        try:
            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                self.backend.aim_down()
                aim_started()
            report(
                WorkerProgress.AIM_OFF_REPLAY_DOWN,
                "owned tagged deferred RMB-off replay pressed",
            )
            self._cancelable_wait(
                CONTROL_REPLAY_PRESS_MS, cancel_event, shutdown_event
            )
            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                self.backend.aim_up()
                aim_sent()
            report(
                WorkerProgress.AIM_OFF_REPLAY_UP,
                "owned tagged deferred RMB-off replay released",
            )
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            release_owned=self.backend.release_shift_inputs,
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
            # This release is deliberately ordered before the physical-click
            # bypass down, even
            # if a prior macro cleanup reported completion with stale ownership.
            with self.io_lock:
                self.backend.release_all()
                self._check_active(cancel_event, shutdown_event)
                self.backend.mouse_down()
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
            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                self.backend.mouse_up()
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            release_owned=self.backend.release_all,
        )

    def run_stratagem(
        self,
        token: int,
        sequences: tuple[tuple[Direction, ...], ...],
        cancel_event: threading.Event,
        shutdown_event: threading.Event,
    ) -> WorkerResult:
        error: BaseException | None = None
        canceled = False
        success = False
        timing = self._config.stratagems

        def output(action: Callable[[], None]) -> None:
            with self.io_lock:
                self._check_active(cancel_event, shutdown_event)
                action()

        try:
            for sequence in sequences:
                output(
                    lambda: self.backend.stratagem_key_down(
                        token, LEFT_CTRL_SCAN_CODE, extended=False, ctrl=True
                    )
                )
                self._cancelable_wait(
                    timing.ctrl_settle_ms, cancel_event, shutdown_event
                )
                for direction in sequence:
                    output(
                        lambda direction=direction: self.backend.stratagem_key_down(
                            token,
                            direction.scan_code,
                            extended=True,
                        )
                    )
                    self._cancelable_wait(
                        timing.key_press_ms, cancel_event, shutdown_event
                    )
                    output(
                        lambda direction=direction: self.backend.stratagem_key_up(
                            token,
                            direction.scan_code,
                            extended=True,
                        )
                    )
                    self._cancelable_wait(
                        timing.key_gap_ms, cancel_event, shutdown_event
                    )
                output(
                    lambda: self.backend.stratagem_key_up(
                        token, LEFT_CTRL_SCAN_CODE, extended=False, ctrl=True
                    )
                )
                output(lambda: self.backend.stratagem_mouse_down(token))
                self._cancelable_wait(
                    timing.action_press_ms, cancel_event, shutdown_event
                )
                output(lambda: self.backend.stratagem_mouse_up(token))
                self._cancelable_wait(
                    timing.action_delay_ms, cancel_event, shutdown_event
                )
            success = True
        except InterruptedError:
            canceled = True
        except (ForegroundLost, InputApiError) as exc:
            error = exc
        except BaseException as exc:
            error = exc
        return self._finish_result(
            success=success,
            canceled=canceled,
            error=error,
            release_owned=lambda: self.backend.release_stratagem(token),
        )


class MacroWorker:
    def __init__(
        self,
        token: int,
        request: WorkerRequest,
        engine: MacroEngine,
        shutdown_event: threading.Event,
        on_complete: Callable[[int, WorkerResult], None],
        on_progress: Callable[[int, WorkerProgressUpdate], None],
        coordination: InputCoordination | None = None,
    ) -> None:
        self.token = token
        self.request = request
        self.mode = request.mode
        self._engine = engine
        self._shutdown = shutdown_event
        self._on_complete = on_complete
        self._on_progress = on_progress
        self._coordination = coordination
        self.cancel_event = threading.Event()
        self._finish_after_reload = threading.Event()
        self._reload_started = threading.Event()
        self._reload_completed = threading.Event()
        self._aim_started = threading.Event()
        self._aim_sent = threading.Event()
        self._shift_started = threading.Event()
        self._shift_sent = threading.Event()
        self._activation = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"{request.kind.name.lower()}-{request.mode.value.lower()}",
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def activate(self) -> None:
        self._activation.set()

    def cancel(self) -> None:
        """Request cancellation without acquiring the output serialization lock."""
        self.cancel_event.set()
        self._activation.set()

    def cancel_and_release(self) -> BaseException | None:
        self.cancel()
        try:
            with self._engine.io_lock:
                if self.request.kind in (
                    WorkerKind.PREPARATION,
                    WorkerKind.RELOAD_ONLY,
                ):
                    self._engine.backend.reload_up()
                elif self.request.kind is WorkerKind.SHIFT_TRANSACTION:
                    self._engine.backend.release_shift_inputs()
                elif self.request.kind is WorkerKind.AIM_OFF_TRANSACTION:
                    self._engine.backend.release_shift_inputs()
                elif self.request.kind is WorkerKind.STRATAGEM:
                    self._engine.backend.release_stratagem(self.token)
                else:
                    context = (
                        self._engine._cadence_diagnostics.macro_cleanup_scope(
                            "controller cancel_and_release"
                        )
                        if self._engine._cadence_diagnostics is not None
                        and self.request.kind is WorkerKind.MACRO
                        else nullcontext()
                    )
                    with context:
                        self._engine.backend.release_all()
        except BaseException as exc:
            return exc
        return None

    def reload_in_progress(self) -> bool:
        return self._reload_started.is_set() and not self._reload_completed.is_set()

    def sprint_stop(self) -> bool:
        """Atomically preserve an active reload or cancel firing and release input."""
        with self._engine.io_lock:
            if self.reload_in_progress():
                self._finish_after_reload.set()
                return True
            self.cancel()
            context = (
                self._engine._cadence_diagnostics.macro_cleanup_scope(
                    "controller sprint stop"
                )
                if self._engine._cadence_diagnostics is not None
                else nullcontext()
            )
            with context:
                self._engine.backend.release_all()
            return False

    def cancel_shift_and_observe(
        self,
    ) -> tuple[bool, bool, bool, bool, BaseException | None]:
        """Cancel a deferred Shift transaction and release only its inputs."""
        self.cancel()
        status = (
            self._aim_started.is_set(),
            self._aim_sent.is_set(),
            self._shift_started.is_set(),
            self._shift_sent.is_set(),
        )
        try:
            with self._engine.io_lock:
                status = (
                    self._aim_started.is_set(),
                    self._aim_sent.is_set(),
                    self._shift_started.is_set(),
                    self._shift_sent.is_set(),
                )
                self._engine.backend.release_shift_inputs()
        except BaseException as exc:
            return (*status, exc)
        return (*status, None)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        self._activation.wait()
        if self.cancel_event.is_set() or self._shutdown.is_set():
            self._on_complete(
                self.token, WorkerResult(False, canceled=True)
            )
            return
        if self.request.kind is WorkerKind.MACRO:
            def macro_progress(update: WorkerProgressUpdate) -> None:
                if update.phase is WorkerProgress.SHOT_BEGAN:
                    # A repeating macro owns fresh reload-phase latches for
                    # each cycle. Shift during a later cycle must not mistake
                    # the previous cycle's completed reload for the current
                    # phase.
                    self._reload_started.clear()
                    self._reload_completed.clear()
                elif update.phase is WorkerProgress.RELOAD_COMPLETED:
                    self._reload_completed.set()
                self._on_progress(self.token, update)

            result = self._engine.run_macro(
                self.mode,
                self.cancel_event,
                self._shutdown,
                macro_progress,
                self._finish_after_reload,
                self._reload_started.set,
                (
                    self._coordination.firing_started
                    if self._coordination is not None
                    else lambda: None
                ),
                (
                    self._coordination.firing_stopped
                    if self._coordination is not None
                    else lambda: None
                ),
            )
        elif self.request.kind in (
            WorkerKind.PREPARATION,
            WorkerKind.RELOAD_ONLY,
        ):
            result = self._engine.prepare_reload(
                self.mode,
                self.request.switch_settle_ms,
                self.cancel_event,
                self._shutdown,
                lambda update: self._on_progress(self.token, update),
            )
        elif self.request.kind is WorkerKind.SHIFT_TRANSACTION:
            result = self._engine.send_shift_transaction(
                self.request.shift_scan_code,
                self.request.cancel_aim,
                self.cancel_event,
                self._shutdown,
                lambda update: self._on_progress(self.token, update),
                self._aim_started.set,
                self._aim_sent.set,
                self._shift_started.set,
                self._shift_sent.set,
            )
        elif self.request.kind is WorkerKind.AIM_OFF_TRANSACTION:
            result = self._engine.send_aim_off_transaction(
                self.cancel_event,
                self._shutdown,
                lambda update: self._on_progress(self.token, update),
                self._aim_started.set,
                self._aim_sent.set,
            )
        elif self.request.kind is WorkerKind.BYPASS:
            result = self._engine.forward_bypass(
                self.request.bypass_release,
                self.request.bypass_click_ms,
                self.cancel_event,
                self._shutdown,
            )
        elif self.request.kind is WorkerKind.STRATAGEM:
            result = self._engine.run_stratagem(
                self.token,
                self.request.stratagem_sequences,
                self.cancel_event,
                self._shutdown,
            )
        else:
            result = WorkerResult(
                False, error=RuntimeError(f"unsupported worker kind {self.request.kind}")
            )
        self._on_complete(self.token, result)
