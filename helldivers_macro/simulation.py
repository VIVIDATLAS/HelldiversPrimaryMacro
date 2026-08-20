from __future__ import annotations

from dataclasses import dataclass, replace
import queue
import threading
from typing import Callable

from .config import AppConfig
from .input_backend import InputCoordination
from .input_hooks import (
    HookPolicy,
    SHIFT_SCAN_CODES,
    VK_1,
    VK_2,
    VK_LCONTROL,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_F23,
    VK_F24,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
)
from .macro_engine import MacroEngine, primary_cycle_steps, secondary_cycle_steps
from .models import (
    ControlEvent,
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    OutputAction,
    RmbHoldState,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerProgressUpdate,
    WorkerRequest,
    WorkerResult,
)
from .state_machine import MacroStateMachine
from .stratagems import (
    FOUR_TARGET_SEQUENCES,
    SUPPORT_SEQUENCES,
    sequence_duration_ms,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wait(self, event: threading.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return event.is_set()

    def advance_ms(self, duration_ms: int) -> None:
        self.now += duration_ms / 1000.0


class FakeForeground:
    def __init__(self, active: bool = True, certain: bool = True) -> None:
        self.active = active
        self.certain = certain

    def status(self) -> tuple[bool, bool]:
        return self.active and self.certain, self.certain

    def is_confirmed_active(self) -> bool:
        return self.status()[0]


class FakeAudioNotifier:
    def __init__(self) -> None:
        self.events: list[str] = []

    def notify_on(self) -> None:
        self.events.append("ON")

    def notify_off(self) -> None:
        self.events.append("OFF")


class FakeGeneratedInput:
    def __init__(
        self,
        state_probe: Callable[[], str],
        clock: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self._state_probe = state_probe
        self._clock = clock
        self.events: list[tuple[str, str]] = []
        self.timed_events: list[tuple[str, int]] = []
        self.tagged_events: list[tuple[str, EventSource]] = []
        self.mouse_owned = False
        self.aim_owned = False
        self.last_aim_hold_release_token = 0
        self.shift_owned = False
        self.shift_scan = 0
        self.shift_scans: list[int] = []
        self.reload_owned = False
        self.stratagem_owner: int | None = None
        self.stratagem_scan: tuple[int, bool] | None = None
        self.stratagem_ctrl_owned = False
        self.stratagem_mouse_owned = False

    def _record(self, name: str) -> None:
        state = self._state_probe()
        self.events.append((name, state))
        self.timed_events.append((name, round(self._clock() * 1000)))
        source = (
            EventSource.INJECTED_BYPASS
            if state == MacroState.FORWARDING_BYPASS.name
            else EventSource.INJECTED_OWNED
        )
        self.tagged_events.append((name, source))

    def mouse_owned_snapshot(self) -> bool:
        return self.mouse_owned

    def mouse_down(self) -> None:
        if self.mouse_owned:
            raise RuntimeError("fake duplicate MB1 down")
        self._record("MB1_DOWN")
        self.mouse_owned = True

    def mouse_up(self) -> None:
        if self.mouse_owned:
            self._record("MB1_UP")
            self.mouse_owned = False

    def fire_down(self) -> None:
        self.mouse_down()

    def fire_up(self) -> None:
        self.mouse_up()

    def aim_down(self) -> None:
        if self.aim_owned:
            raise RuntimeError("fake duplicate MB2 down")
        self._record("MB2_DOWN")
        self.aim_owned = True

    def aim_up(self) -> None:
        if self.aim_owned:
            self._record("MB2_UP")
            self.aim_owned = False

    def release_held_aim(self, token: int) -> None:
        if token <= self.last_aim_hold_release_token:
            return
        self._record("MB2_UP")
        self.last_aim_hold_release_token = token

    def shift_down(self, scan_code: int) -> None:
        if self.shift_owned:
            raise RuntimeError("fake duplicate Shift down")
        self._record("SHIFT_DOWN")
        self.shift_owned = True
        self.shift_scan = scan_code
        self.shift_scans.append(scan_code)

    def shift_up(self) -> None:
        if self.shift_owned:
            self._record("SHIFT_UP")
            self.shift_owned = False
            self.shift_scan = 0

    def reload_down(self) -> None:
        if self.reload_owned:
            raise RuntimeError("fake duplicate R down")
        self._record("R_DOWN")
        self.reload_owned = True

    def reload_up(self) -> None:
        if self.reload_owned:
            self._record("R_UP")
            self.reload_owned = False

    def release_all(self) -> None:
        if self.stratagem_owner is not None:
            self.release_stratagem(self.stratagem_owner)
        self.mouse_up()
        self.aim_up()
        self.shift_up()
        self.reload_up()

    def release_shift_inputs(self) -> None:
        self.aim_up()
        self.shift_up()

    def stratagem_key_down(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        if self.stratagem_owner not in (None, token):
            raise RuntimeError("fake stratagem owner conflict")
        self.stratagem_owner = token
        if ctrl:
            self.stratagem_ctrl_owned = True
            self._record("CTRL_DOWN")
        else:
            self.stratagem_scan = (scan_code, extended)
            self._record(f"ARROW_{scan_code:02X}_DOWN_EXTENDED")

    def stratagem_key_up(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        if self.stratagem_owner != token:
            return
        if ctrl and self.stratagem_ctrl_owned:
            self._record("CTRL_UP")
            self.stratagem_ctrl_owned = False
        elif not ctrl and self.stratagem_scan == (scan_code, extended):
            self._record(f"ARROW_{scan_code:02X}_UP_EXTENDED")
            self.stratagem_scan = None

    def stratagem_mouse_down(self, token: int) -> None:
        if self.stratagem_owner not in (None, token):
            raise RuntimeError("fake stratagem owner conflict")
        self.stratagem_owner = token
        self.stratagem_mouse_owned = True
        self._record("STRATAGEM_MB1_DOWN")

    def stratagem_mouse_up(self, token: int) -> None:
        if self.stratagem_owner == token and self.stratagem_mouse_owned:
            self._record("STRATAGEM_MB1_UP")
            self.stratagem_mouse_owned = False

    def release_stratagem(self, token: int) -> None:
        if self.stratagem_owner != token:
            return
        if self.stratagem_scan is not None:
            scan, extended = self.stratagem_scan
            self.stratagem_key_up(token, scan, extended=extended)
        if self.stratagem_ctrl_owned:
            self.stratagem_key_up(token, 0x1D, extended=False, ctrl=True)
        self.stratagem_mouse_up(token)
        self.stratagem_owner = None


class FakeSessionWorker:
    """Controllable worker using the real engine only at fake OS boundaries."""

    def __init__(
        self,
        harness: "SimulationHarness",
        token: int,
        request: WorkerRequest,
    ) -> None:
        self.harness = harness
        self.token = token
        self.request = request
        self.started = False
        self.activated = False
        self.completed = False
        self.cancel_requested = False
        self.finish_after_reload_requested = False
        self.reload_started = False
        self.reload_completed = False
        self.aim_release_started = False
        self.aim_release_sent = False
        self.shift_started = False
        self.shift_sent = False
        self.alive = False

    def progress(self, phase: WorkerProgress, reason: str) -> None:
        self.harness.put_worker_event(
            ControlEventKind.WORKER_PROGRESS,
            self,
            WorkerProgressUpdate(phase, self.harness.clock(), reason),
        )

    def start(self) -> None:
        self.started = True
        self.alive = True

    def activate(self) -> None:
        if self.activated:
            return
        self.activated = True
        if self.request.kind in (
            WorkerKind.PREPARATION,
            WorkerKind.RELOAD_ONLY,
        ):
            if self.harness.auto_complete_preparation:
                self.finish_preparation()
        elif self.request.kind is WorkerKind.MACRO:
            self.harness.backend.mouse_down()
            self.progress(WorkerProgress.SHOT_BEGAN, "generated shot began")
            weapon = (
                self.harness.config.primary
                if self.request.mode is WeaponMode.PRIMARY
                else self.harness.config.secondary
            )
            if weapon.fire_mode == "automatic_hold":
                self.progress(
                    WorkerProgress.FINAL_SHOT_DOWN,
                    "automatic fire hold began",
                )
        elif self.request.kind is WorkerKind.SHIFT_TRANSACTION:
            if self.harness.auto_complete_shift:
                self.finish_shift_transaction()
        elif self.request.kind is WorkerKind.BYPASS:
            self.harness.backend.release_all()
            if not self.harness.foreground.is_confirmed_active():
                self.finish(WorkerResult(False, error=RuntimeError("foreground lost")))
                return
            self.harness.backend.mouse_down()
            if self.request.bypass_release.is_set():
                self.harness.clock.advance_ms(self.request.bypass_click_ms)
                self.harness.backend.mouse_up()
                self.finish(WorkerResult(True))
        elif self.request.kind is WorkerKind.STRATAGEM:
            if self.harness.auto_complete_stratagem:
                result = self.harness.engine.run_stratagem(
                    self.token,
                    self.request.stratagem_sequences,
                    threading.Event(),
                    threading.Event(),
                )
                self.finish(result)

    def finish_preparation(self, result: WorkerResult | None = None) -> None:
        if result is None:
            result = self.harness.engine.prepare_reload(
                self.request.mode,
                self.request.switch_settle_ms,
                threading.Event(),
                threading.Event(),
                lambda update: self.harness.put_worker_event(
                    ControlEventKind.WORKER_PROGRESS,
                    self,
                    update,
                ),
            )
        self.finish(result)

    def begin_reload(self) -> None:
        if self.request.kind not in (
            WorkerKind.PREPARATION,
            WorkerKind.RELOAD_ONLY,
        ) or self.completed:
            raise AssertionError("only an active reload worker can begin reload")
        self.harness.backend.reload_down()

    def complete_cycle_reload(self) -> None:
        if self.request.kind is not WorkerKind.MACRO or self.completed:
            raise AssertionError("only an active macro can complete a cycle")
        steps = (
            primary_cycle_steps(self.harness.config)
            if self.request.mode is WeaponMode.PRIMARY
            else secondary_cycle_steps(self.harness.config)
        )
        shot_count = 1
        weapon = (
            self.harness.config.primary
            if self.request.mode is WeaponMode.PRIMARY
            else self.harness.config.secondary
        )
        total_shots = (
            1 if weapon.fire_mode == "automatic_hold" else weapon.shots_per_cycle
        )
        assert total_shots is not None
        # activate() already scheduled the cycle's first logical fire-down.
        for step in steps[1:]:
            if step.action is OutputAction.WAIT:
                self.harness.clock.advance_ms(step.duration_ms)
            elif step.action is OutputAction.FIRE_DOWN:
                self.harness.backend.fire_down()
                shot_count += 1
                self.progress(WorkerProgress.SHOT_BEGAN, "generated shot began")
                if shot_count == total_shots:
                    self.progress(
                        WorkerProgress.FINAL_SHOT_DOWN,
                        "final configured shot pressed",
                    )
            elif step.action is OutputAction.FIRE_UP:
                self.harness.backend.fire_up()
                if shot_count == total_shots:
                    self.progress(
                        WorkerProgress.FINAL_SHOT_UP,
                        "final configured shot released",
                    )
            elif step.action is OutputAction.R_DOWN:
                self.harness.backend.reload_down()
                self.harness.coordination.firing_stopped()
                self.reload_started = True
                self.progress(
                    WorkerProgress.RELOAD_KEY_DOWN,
                    "reload key pressed after firing phase",
                )
            elif step.action is OutputAction.R_UP:
                self.harness.backend.reload_up()
                self.progress(
                    WorkerProgress.RELOAD_KEY_UP,
                    "configured reload key press completed",
                )
                self.progress(
                    WorkerProgress.RELOAD_WAIT_STARTED,
                    "reload wait began after R-up",
                )
        self.reload_completed = True
        self.progress(
            WorkerProgress.RELOAD_COMPLETED,
            "configured reload wait completed",
        )
        if self.finish_after_reload_requested:
            self.finish(WorkerResult(True))

    def begin_macro_reload(self) -> None:
        if self.request.kind is not WorkerKind.MACRO or self.completed:
            raise AssertionError("only an active macro can begin reload")
        self.harness.backend.mouse_up()
        self.harness.backend.reload_down()
        self.harness.coordination.firing_stopped()
        self.reload_started = True
        self.progress(
            WorkerProgress.RELOAD_KEY_DOWN,
            "reload key pressed after firing phase",
        )

    def complete_macro_reload(self) -> None:
        if not self.reload_in_progress() or self.completed:
            raise AssertionError("macro reload is not active")
        weapon = (
            self.harness.config.primary
            if self.request.mode is WeaponMode.PRIMARY
            else self.harness.config.secondary
        )
        self.harness.clock.advance_ms(weapon.reload_press_ms)
        self.harness.backend.reload_up()
        self.progress(
            WorkerProgress.RELOAD_KEY_UP,
            "configured reload key press completed",
        )
        self.progress(
            WorkerProgress.RELOAD_WAIT_STARTED,
            "reload wait began after R-up",
        )
        self.harness.clock.advance_ms(weapon.reload_wait_ms)
        self.reload_completed = True
        self.progress(
            WorkerProgress.RELOAD_COMPLETED,
            "configured reload wait completed",
        )
        if self.finish_after_reload_requested:
            self.finish(WorkerResult(True))

    def begin_next_cycle(self) -> None:
        if self.request.kind is not WorkerKind.MACRO or self.completed:
            raise AssertionError("only an active macro can begin another cycle")
        self.harness.coordination.firing_started()
        self.harness.backend.mouse_down()
        self.progress(WorkerProgress.SHOT_BEGAN, "generated shot began")

    def finish_bypass_release(self) -> None:
        if self.request.kind is not WorkerKind.BYPASS or self.completed:
            return
        if self.request.bypass_release.is_set():
            self.harness.clock.advance_ms(self.request.bypass_click_ms)
            self.harness.backend.mouse_up()
            self.finish(WorkerResult(True))

    def release_aim_hold(self) -> None:
        if (
            self.request.kind is not WorkerKind.SHIFT_TRANSACTION
            or not self.request.release_aim_hold
            or self.completed
        ):
            raise AssertionError("transaction has no held-aim release")
        self.aim_release_started = True
        self.harness.backend.release_held_aim(self.token)
        self.aim_release_sent = True
        self.progress(
            WorkerProgress.AIM_HOLD_RELEASED,
            "one owned tagged MB2-up neutralized held aim before Shift replay",
        )

    def begin_shift_replay(self) -> None:
        if self.request.kind is not WorkerKind.SHIFT_TRANSACTION or self.completed:
            raise AssertionError("only a Shift transaction can begin replay")
        if self.request.release_aim_hold and not self.aim_release_sent:
            self.release_aim_hold()
        self.harness.backend.shift_down(self.request.shift_scan_code)
        self.shift_started = True
        self.progress(
            WorkerProgress.SHIFT_REPLAY_DOWN,
            "owned tagged physical-scan Shift replay pressed",
        )

    def finish_shift_transaction(self) -> None:
        if self.request.kind is not WorkerKind.SHIFT_TRANSACTION or self.completed:
            raise AssertionError("only a Shift transaction can finish replay")
        if not self.shift_started:
            self.begin_shift_replay()
        self.harness.clock.advance_ms(20)
        self.harness.backend.shift_up()
        self.shift_sent = True
        self.progress(
            WorkerProgress.SHIFT_REPLAY_UP,
            "owned tagged Shift replay released",
        )
        self.finish(WorkerResult(True))

    def finish(self, result: WorkerResult) -> None:
        if self.completed:
            return
        if self.request.kind is WorkerKind.PREPARATION and self.cancel_requested:
            self.harness.backend.reload_up()
        if self.request.kind is WorkerKind.SHIFT_TRANSACTION:
            self.harness.backend.release_shift_inputs()
        self.completed = True
        self.alive = False
        self.harness.put_worker_event(ControlEventKind.WORKER_STOPPED, self, result)

    def cancel(self) -> None:
        self.cancel_requested = True

    def cancel_and_release(self) -> BaseException | None:
        self.cancel()
        try:
            if self.request.kind in (
                WorkerKind.PREPARATION,
                WorkerKind.RELOAD_ONLY,
            ):
                self.harness.backend.reload_up()
            elif self.request.kind is WorkerKind.SHIFT_TRANSACTION:
                self.harness.backend.release_shift_inputs()
            elif self.request.kind is WorkerKind.BYPASS:
                self.harness.backend.mouse_up()
            elif self.request.kind is WorkerKind.STRATAGEM:
                self.harness.backend.release_stratagem(self.token)
            else:
                self.harness.backend.release_all()
        except BaseException as exc:
            return exc
        if self.harness.auto_complete_cancel:
            self.finish(WorkerResult(False, canceled=True))
        return None

    def cancel_shift_and_observe(
        self,
    ) -> tuple[bool, bool, bool, bool, BaseException | None]:
        self.cancel()
        try:
            self.harness.backend.release_shift_inputs()
        except BaseException as exc:
            return (
                self.aim_release_started,
                self.aim_release_sent,
                self.shift_started,
                self.shift_sent,
                exc,
            )
        if self.harness.auto_complete_cancel:
            self.finish(WorkerResult(False, canceled=True))
        return (
            self.aim_release_started,
            self.aim_release_sent,
            self.shift_started,
            self.shift_sent,
            None,
        )

    def reload_in_progress(self) -> bool:
        return self.reload_started and not self.reload_completed

    def sprint_stop(self) -> bool:
        if self.reload_in_progress():
            self.finish_after_reload_requested = True
            return True
        self.cancel_and_release()
        return False

    def join(self, timeout: float | None = None) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class SimulationHarness:
    def __init__(
        self,
        config: AppConfig,
        *,
        trace: bool = True,
        auto_complete_preparation: bool = True,
        auto_complete_cancel: bool = True,
        auto_complete_shift: bool = True,
        auto_complete_stratagem: bool = True,
        foreground_active: bool = True,
        foreground_certain: bool = True,
    ) -> None:
        self.config = replace(
            config,
            diagnostics=replace(config.diagnostics, state_tracing=trace),
        )
        self.auto_complete_preparation = auto_complete_preparation
        self.auto_complete_cancel = auto_complete_cancel
        self.auto_complete_shift = auto_complete_shift
        self.auto_complete_stratagem = auto_complete_stratagem
        self.foreground = FakeForeground(
            foreground_active,
            foreground_certain,
        )
        self.clock = FakeClock()
        self.audio = FakeAudioNotifier()
        self.coordination = InputCoordination()
        self.events: queue.Queue[ControlEvent] = queue.Queue()
        self.hook_events: list[ControlEventKind] = []
        self.reports: list[str] = []
        self.workers: list[FakeSessionWorker] = []
        holder: list[MacroStateMachine] = []
        self.backend = FakeGeneratedInput(
            lambda: holder[0].state.name if holder else "UNINITIALIZED",
            self.clock,
        )
        self.engine = MacroEngine(
            self.config,
            self.backend,
            self.foreground.is_confirmed_active,
            clock=self.clock,
            wait=self.clock.wait,
        )

        def factory(token: int, request: WorkerRequest) -> FakeSessionWorker:
            worker = FakeSessionWorker(self, token, request)
            self.workers.append(worker)
            return worker

        self.machine = MacroStateMachine(
            self.config,
            self.foreground.is_confirmed_active,
            self.audio,
            factory,
            coordination=self.coordination,
            clock=self.clock,
            reporter=self.reports.append,
            foreground_status=self.foreground.status,
        )
        holder.append(self.machine)
        self.policy = HookPolicy(
            self.foreground.status,
            self.put_hook_event,
            self.backend.mouse_owned_snapshot,
            self.coordination,
            stratagem_triggers=(
                {
                    (VK_F23 if self.config.stratagems.four_target_trigger == "F23" else VK_F24): ControlEventKind.STRATAGEM_FOUR,
                    (VK_F23 if self.config.stratagems.support_trigger == "F23" else VK_F24): ControlEventKind.STRATAGEM_SUPPORT,
                }
                if self.config.stratagems.enabled else {}
            ),
        )

    def put_hook_event(self, event: ControlEvent) -> None:
        self.hook_events.append(event.kind)
        self.events.put_nowait(event)

    def put_worker_event(
        self,
        kind: ControlEventKind,
        worker: FakeSessionWorker,
        detail: object,
    ) -> None:
        self.events.put_nowait(
            ControlEvent(
                kind,
                detail=detail,
                worker_token=worker.token,
                source=EventSource.WORKER,
            )
        )

    def send(
        self,
        kind: ControlEventKind,
        source: EventSource = EventSource.PHYSICAL,
    ) -> None:
        self.events.put_nowait(ControlEvent(kind, source=source))

    def drain(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            self.machine.handle(event)

    def key_press(self, vk: int, repeats: int = 0) -> tuple[bool, ...]:
        scan = SHIFT_SCAN_CODES.get(vk, 0)
        results = [self.policy.keyboard(WM_KEYDOWN, vk, 0, scan)]
        results.extend(
            self.policy.keyboard(WM_KEYDOWN, vk, 0, scan) for _ in range(repeats)
        )
        results.append(self.policy.keyboard(WM_KEYUP, vk, 0, scan))
        self.drain()
        return tuple(results)

    def click(self) -> tuple[bool, bool]:
        down = self.policy.mouse(WM_LBUTTONDOWN, 0, 0)
        up = self.policy.mouse(WM_LBUTTONUP, 0, 0)
        self.drain()
        return down, up

    def make_full(self, mode: WeaponMode) -> None:
        # PRIMARY is the default internal selection, so physical 1 is a no-op.
        # Switching away and back exercises the real preparation path.
        if mode is WeaponMode.PRIMARY:
            if self.machine.selected_mode is WeaponMode.PRIMARY:
                self.key_press(VK_2)
            self.key_press(VK_1)
        elif self.machine.selected_mode is not WeaponMode.SECONDARY:
            self.key_press(VK_2)
        if not self.auto_complete_preparation and self.machine.preparing:
            worker = self.machine.worker
            assert isinstance(worker, FakeSessionWorker)
            worker.finish_preparation()
            self.drain()
        if self.machine.magazine_state(mode) is not MagazineState.FULL:
            raise AssertionError(f"{mode.value} did not become FULL")

    def start(self, mode: WeaponMode) -> None:
        self.make_full(mode)
        self.hold_rmb()
        self.clock.advance_ms(self.config.controls.toggle_debounce_ms)
        self.click()
        expected = (
            MacroState.RUNNING_PRIMARY
            if mode is WeaponMode.PRIMARY
            else MacroState.RUNNING_SECONDARY
        )
        if self.machine.state is not expected:
            raise AssertionError(f"expected {expected.name}, got {self.machine.state.name}")

    def hold_rmb(self) -> None:
        if self.machine.rmb_hold_state is RmbHoldState.HELD_VALID:
            return
        if self.machine.rmb_hold_state is not RmbHoldState.RELEASED:
            raise AssertionError(
                "RMB must be physically released before a fresh hold can arm"
            )
        if self.policy.mouse(WM_RBUTTONDOWN, 0, 0):
            raise AssertionError("idle physical RMB-down must pass through")
        self.drain()
        if self.machine.rmb_hold_state is not RmbHoldState.HELD_VALID:
            raise AssertionError("physical RMB hold did not establish authority")

    def release_rmb(self) -> None:
        if self.policy.mouse(WM_RBUTTONUP, 0, 0):
            raise AssertionError("physical RMB-up must pass through")
        self.drain()
        if self.machine.rmb_hold_state is not RmbHoldState.RELEASED:
            raise AssertionError("physical RMB-up did not clear hold authority")

    # Compatibility helper for older non-toggle scenarios.
    aim_on = hold_rmb

    def foreground_loss(self, *, certain: bool = True) -> None:
        self.foreground.active = False
        self.foreground.certain = certain
        self.send(
            ControlEventKind.FOREGROUND_LOST
            if certain
            else ControlEventKind.FOREGROUND_UNCERTAIN,
            EventSource.FOREGROUND,
        )
        self.drain()

    def foreground_acquired(self) -> None:
        self.foreground.active = True
        self.foreground.certain = True
        self.send(ControlEventKind.FOREGROUND_ACTIVE, EventSource.FOREGROUND)
        self.drain()


@dataclass(frozen=True)
class SimulatedSessionResult:
    passed: bool
    mode: WeaponMode
    logical_selections: int
    hook_events: tuple[ControlEventKind, ...]
    input_events: tuple[tuple[str, str], ...]
    audio_events: tuple[str, ...]
    reports: tuple[str, ...]


def simulate_weapon_session(
    config: AppConfig,
    mode: WeaponMode,
    *,
    emit: Callable[[str], None] | None = None,
) -> SimulatedSessionResult:
    h = SimulationHarness(config)
    selection_vk = VK_1 if mode is WeaponMode.PRIMARY else VK_2
    h.key_press(selection_vk, repeats=3)
    selection_kind = (
        ControlEventKind.SELECT_PRIMARY
        if mode is WeaponMode.PRIMARY
        else ControlEventKind.SELECT_SECONDARY
    )
    logical_selections = h.hook_events.count(selection_kind)
    if mode is WeaponMode.SECONDARY:
        h.make_full(mode)
    h.aim_on()
    h.clock.advance_ms(config.controls.toggle_debounce_ms)
    start_pair = h.click()
    running_state = (
        MacroState.RUNNING_PRIMARY
        if mode is WeaponMode.PRIMARY
        else MacroState.RUNNING_SECONDARY
    )
    firing_downs = [event for event in h.backend.events if event[0] == "MB1_DOWN"]
    start_ok = (
        start_pair == (True, True)
        and h.machine.state is running_state
        and h.audio.events == ["ON"]
        and firing_downs[-1][1] == running_state.name
    )
    stop_pair = h.click()
    idle_state = (
        MacroState.IDLE_PRIMARY
        if mode is WeaponMode.PRIMARY
        else MacroState.IDLE_SECONDARY
    )
    stop_ok = (
        stop_pair == (True, True)
        and h.machine.state is idle_state
        and h.machine.worker is None
        and not h.backend.mouse_owned
        and h.audio.events == ["ON", "OFF"]
    )
    delivered_clicks = (
        h.hook_events.count(ControlEventKind.PHYSICAL_MB1_DOWN) == 2
        and h.hook_events.count(ControlEventKind.PHYSICAL_MB1_UP) == 2
    )
    if emit is not None:
        for report in h.reports:
            if report.startswith("TRACE:") or report.startswith("START_REJECTED:"):
                emit(report)
    return SimulatedSessionResult(
        logical_selections == 1 and start_ok and stop_ok and delivered_clicks,
        mode,
        logical_selections,
        tuple(h.hook_events),
        tuple(h.backend.events),
        tuple(h.audio.events),
        tuple(h.reports),
    )


def _scenario_a(config: AppConfig) -> str:
    assert simulate_weapon_session(config, WeaponMode.PRIMARY).passed
    return "PRIMARY fired immediately and stopped"


def _scenario_b(config: AppConfig) -> str:
    assert simulate_weapon_session(config, WeaponMode.SECONDARY).passed
    return "SECONDARY prepared, fired, and stopped"


def _scenario_c(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_1, repeats=4)
    h.key_press(VK_2, repeats=4)
    assert h.hook_events.count(ControlEventKind.SELECT_PRIMARY) == 1
    assert h.hook_events.count(ControlEventKind.SELECT_SECONDARY) == 1
    return "number-row repeat collapsed to one edge per press"


def _scenario_d(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_preparation=False)
    before = h.clock.now
    assert h.click() == (True, True)
    assert h.machine.state is MacroState.IDLE_PRIMARY
    assert not h.machine.enabled and not h.machine.preparing
    assert h.clock.now == before
    assert h.audio.events == [] and h.backend.events == [] and h.workers == []
    assert any(
        report.startswith("START_REJECTED:")
        and "reason=RMB_HOLD_REQUIRED" in report
        for report in h.reports
    )
    return "RMB-released PRIMARY was rejected without output or audio"


def _scenario_e(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_cancel=False)
    h.start(WeaponMode.PRIMARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_LBUTTONUP, 0, 0)
    h.drain()
    macro.finish(WorkerResult(False, canceled=True))
    h.drain()
    bypass = h.workers[-1]
    assert isinstance(bypass, FakeSessionWorker)
    assert bypass.request.kind is WorkerKind.BYPASS
    bypass.finish_bypass_release()
    h.drain()
    names = [name for name, _state in h.backend.events]
    assert names[-3:] == ["MB1_UP", "MB1_DOWN", "MB1_UP"]
    assert not h.backend.mouse_owned
    return "deferred Ctrl bypass followed generated release ordering"


def _scenario_f(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_preparation=False)
    h.key_press(VK_2)
    worker = h.machine.worker
    assert isinstance(worker, FakeSessionWorker)
    worker.finish_preparation(
        WorkerResult(False, error=RuntimeError("fake input failure"))
    )
    h.drain()
    assert h.machine.state is MacroState.IDLE_SECONDARY
    assert h.machine.magazine_state(WeaponMode.SECONDARY) is MagazineState.UNKNOWN
    assert not h.machine.preparing
    return "failed preparation returned to idle UNKNOWN"


def _scenario_g(config: AppConfig) -> str:
    h = SimulationHarness(
        config, auto_complete_preparation=False, auto_complete_cancel=False
    )
    h.key_press(VK_2)
    old = h.machine.worker
    assert isinstance(old, FakeSessionWorker)
    h.key_press(VK_1)
    old.finish(WorkerResult(True))
    h.drain()
    assert h.machine.selected_mode is WeaponMode.PRIMARY
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is not MagazineState.FULL
    return "obsolete preparation could not overwrite current selection"


def _scenario_h(config: AppConfig) -> str:
    h = SimulationHarness(config)
    assert h.audio.events == [] and h.backend.events == []
    h.make_full(WeaponMode.SECONDARY)
    assert h.audio.events == []
    return "preparation produced no ON/OFF and used only fake input"


def _scenario_i(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.start(WeaponMode.SECONDARY)
    before = (
        h.machine.generation,
        tuple(h.audio.events),
        len(h.workers),
        len(h.backend.events),
    )
    h.key_press(VK_2, repeats=5)
    after = (
        h.machine.generation,
        tuple(h.audio.events),
        len(h.workers),
        len(h.backend.events),
    )
    assert before == after
    assert h.machine.state is MacroState.RUNNING_SECONDARY and h.machine.enabled
    return "same-mode 2 press while firing was an internal no-op"


def _aim_chord(config: AppConfig, mode: WeaponMode) -> None:
    h = SimulationHarness(config)
    h.make_full(mode)
    baseline_generation = h.machine.generation
    baseline_reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    passed_mb2: list[str] = []
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    passed_mb2.append("MB2_DOWN")
    assert h.click() == (True, True)
    assert h.machine.firing and h.audio.events == ["ON"]
    assert h.click() == (True, True)
    assert not h.machine.enabled and h.audio.events == ["ON", "OFF"]
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    passed_mb2.append("MB2_UP")
    assert passed_mb2 == ["MB2_DOWN", "MB2_UP"]
    assert h.machine.generation == baseline_generation + 2
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == baseline_reloads
    assert not any("RIGHT" in report for report in h.reports)


def _scenario_j(config: AppConfig) -> str:
    _aim_chord(config, WeaponMode.PRIMARY)
    _aim_chord(config, WeaponMode.SECONDARY)
    return "held MB2 passed through while both macro toggle cycles stayed deterministic"


def _scenario_k(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_preparation=False)
    h.key_press(VK_2)
    preparation = h.machine.worker
    before = (
        h.machine.state,
        h.machine.generation,
        tuple(h.audio.events),
    )
    assert all(h.key_press(VK_LSHIFT, repeats=1))
    assert before == (
        h.machine.state,
        h.machine.generation,
        tuple(h.audio.events),
    )
    assert h.machine.worker is preparation
    assert [name for name, _state in h.backend.events] == [
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    return "deferred Shift replay did not disturb disabled preparation"


def _scenario_l(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_cancel=False)
    h.start(WeaponMode.SECONDARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
    h.drain()
    h.policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
    h.drain()
    macro.finish(WorkerResult(False, canceled=True))
    h.drain()
    assert h.machine.selected_mode is WeaponMode.SECONDARY
    assert not h.machine.enabled and not h.machine.firing
    assert not h.backend.mouse_owned
    assert h.audio.events == ["ON", "OFF"]
    return "Ctrl cancellation survived stale worker completion without restart"


def _scenario_m(config: AppConfig) -> str:
    prep = SimulationHarness(
        config, auto_complete_preparation=False, auto_complete_cancel=False
    )
    prep.key_press(VK_2)
    old_prep = prep.machine.worker
    assert isinstance(old_prep, FakeSessionWorker)
    prep.foreground_loss()
    old_prep.finish(WorkerResult(True))
    prep.drain()
    assert not prep.machine.enabled and not prep.backend.mouse_owned

    firing = SimulationHarness(config, auto_complete_cancel=False)
    firing.start(WeaponMode.PRIMARY)
    old_macro = firing.machine.worker
    assert isinstance(old_macro, FakeSessionWorker) and firing.backend.mouse_owned
    firing.foreground_loss()
    old_macro.finish(WorkerResult(False, canceled=True))
    firing.drain()
    assert not firing.machine.enabled and not firing.backend.mouse_owned

    pair = SimulationHarness(
        config, auto_complete_preparation=False, auto_complete_cancel=False
    )
    pair.aim_on()
    assert pair.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    pair.drain()
    old = pair.machine.worker
    assert isinstance(old, FakeSessionWorker)
    pair.foreground_loss()
    pair.foreground.active = True
    assert pair.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    assert pair.policy.mouse(WM_LBUTTONUP, 0, 0)
    pair.drain()
    old.finish(WorkerResult(True))
    pair.drain()
    assert not pair.machine.enabled and not pair.backend.mouse_owned

    bypass = SimulationHarness(config, auto_complete_cancel=False)
    bypass.start(WeaponMode.SECONDARY)
    macro = bypass.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    bypass.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
    assert bypass.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    bypass.drain()
    bypass.foreground_loss()
    assert bypass.policy.mouse(WM_LBUTTONUP, 0, 0)
    bypass.drain()
    macro.finish(WorkerResult(False, canceled=True))
    bypass.drain()
    assert not bypass.machine.enabled and not bypass.backend.mouse_owned
    assert not any(worker.request.kind is WorkerKind.BYPASS for worker in bypass.workers)
    return "foreground loss neutralized preparation, firing, pairs, and bypass"


def _scenario_n(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True, auto_complete_cancel=False)
    h.start(WeaponMode.SECONDARY)
    old = h.machine.worker
    assert isinstance(old, FakeSessionWorker)
    h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
    h.drain()
    old.finish(WorkerResult(False, canceled=True))
    h.drain()
    h.policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
    h.clock.advance_ms(config.controls.toggle_debounce_ms)
    h.click()
    records = [report for report in h.reports if report.startswith("TRACE:")]
    firing_records = [record for record in records if "event=FIRING_STARTED" in record]
    assert len(firing_records) >= 2
    assert "CTRL_DOWN" not in firing_records[-1]
    sequences = [
        int(record.split("seq=", 1)[1].split(" ", 1)[0]) for record in records
    ]
    assert sequences == sorted(sequences) and len(sequences) == len(set(sequences))
    for record in records:
        assert "source=" in record and "previous=[" in record
        assert "result=[" in record and "generation=" in record and "reason=" in record
    return "trace reasons were transition-local with monotonic sequence numbers"


def _scenario_o(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True, auto_complete_preparation=False)
    h.aim_on()
    before = h.clock.now
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.UNKNOWN
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    assert h.clock.now == before
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    assert h.audio.events == ["ON"]
    assert h.backend.events == [("MB1_DOWN", "RUNNING_PRIMARY")]
    events = [report.split("event=", 1)[1].split(" ", 1)[0] for report in h.reports if report.startswith("TRACE:")]
    assert events[-3:] == ["MACRO_ENABLED", "FIRING_STARTED", "FINAL_SHOT_DOWN"]
    assert not any(name.startswith("R_") for name, _state in h.backend.events)
    assert ControlEventKind.PHYSICAL_MB1_UP not in h.hook_events
    return "UNKNOWN PRIMARY scheduled its first shot at elapsed 0 ms"


def _scenario_p(config: AppConfig) -> str:
    h = SimulationHarness(
        config, trace=True, auto_complete_preparation=False, auto_complete_cancel=False
    )
    h.key_press(VK_2)
    preparation = h.machine.worker
    assert isinstance(preparation, FakeSessionWorker)
    h.aim_on()
    generation = h.machine.generation
    before = h.clock.now
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    assert h.clock.now == before
    assert preparation.cancel_requested
    assert h.machine.generation > generation
    assert h.machine.state is MacroState.RUNNING_SECONDARY
    assert h.audio.events == ["ON"]
    assert h.backend.events[-1] == ("MB1_DOWN", "RUNNING_SECONDARY")
    trace_events = [
        report.split("event=", 1)[1].split(" ", 1)[0]
        for report in h.reports
        if report.startswith("TRACE:")
    ]
    assert trace_events[-3:] == [
        "MACRO_ENABLED",
        "PREPARATION_CANCELED",
        "FIRING_STARTED",
    ]
    preparation.finish(WorkerResult(True))
    h.drain()
    assert h.machine.state is MacroState.RUNNING_SECONDARY
    assert h.machine.magazine_state(WeaponMode.SECONDARY) is MagazineState.UNKNOWN
    assert h.audio.events == ["ON"]
    return "SECONDARY firing preempted switch settle without advancing time"


def _scenario_q(config: AppConfig) -> str:
    h = SimulationHarness(
        config, trace=True, auto_complete_preparation=False, auto_complete_cancel=False
    )
    h.key_press(VK_2)
    secondary = h.machine.worker
    assert isinstance(secondary, FakeSessionWorker)
    h.key_press(VK_1)
    secondary.finish(WorkerResult(False, canceled=True))
    h.drain()
    preparation = h.machine.worker
    assert isinstance(preparation, FakeSessionWorker)
    assert preparation.request.mode is WeaponMode.PRIMARY
    preparation.begin_reload()
    h.aim_on()
    before = h.clock.now
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    assert h.clock.now == before
    assert preparation.cancel_requested
    assert h.machine.state is MacroState.RUNNING_PRIMARY
    assert [name for name, _state in h.backend.events][-2:] == ["R_DOWN", "MB1_DOWN"]
    preparation.finish(WorkerResult(True))
    h.drain()
    self_kinds = [worker.request.kind for worker in h.workers]
    assert self_kinds.count(WorkerKind.MACRO) == 1
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.UNKNOWN
    assert h.audio.events == ["ON"]
    return "active reload was canceled and stale FULL authority was ignored"


def _scenario_r(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_cancel=False)
    h.aim_on()
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    assert h.policy.mouse(WM_LBUTTONUP, 0, 0)
    h.drain()
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    assert not h.machine.enabled and not h.backend.mouse_owned
    assert h.audio.events == ["ON"]
    assert not any(name == "R_DOWN" for name, _state in h.backend.events)
    macro.finish(WorkerResult(False, canceled=True))
    h.drain()
    assert h.audio.events == ["ON", "OFF"]
    assert h.machine.state is MacroState.IDLE_PRIMARY
    assert not h.machine.enabled and not h.backend.mouse_owned
    return "next MB1-down stopped immediately with one OFF after cleanup"


def _scenario_s(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.aim_on()
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    h.drain()
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.UNKNOWN
    macro.complete_cycle_reload()
    h.drain()
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    macro.begin_next_cycle()
    h.drain()
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.UNKNOWN
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    assert h.audio.events == ["ON"]
    names = [name for name, _state in h.backend.events]
    reload_down = names.index("R_DOWN")
    reload_up = names.index("R_UP")
    assert reload_down < reload_up < len(names) - 1
    assert names[-1] == "MB1_DOWN"
    return "first cycle reload synchronized FULL before the next cycle began"


def _scenario_t(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=False)
    h.aim_on()
    started_at = round(h.clock() * 1000)
    assert h.click() == (True, True)
    assert h.audio.events == ["ON"]
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.complete_cycle_reload()
    h.drain()

    cycle_events = [
        (name, at - started_at) for name, at in h.backend.timed_events
    ]
    names = [name for name, _at in cycle_events]
    hold = config.primary.automatic_hold_ms
    assert hold is not None
    assert names == ["MB1_DOWN", "MB1_UP", "R_DOWN", "R_UP"]
    assert all(
        source is EventSource.INJECTED_OWNED
        for _name, source in h.backend.tagged_events
    )
    assert h.hook_events.count(ControlEventKind.PHYSICAL_MB1_DOWN) == 1
    assert h.hook_events.count(ControlEventKind.PHYSICAL_MB1_UP) == 1
    downs = [at for name, at in cycle_events if name == "MB1_DOWN"]
    ups = [at for name, at in cycle_events if name == "MB1_UP"]
    assert len(downs) == len(ups) == 1
    assert ups[0] - downs[0] == hold
    final_down = 0
    final_up = hold + config.primary.post_fire_reload_delay_ms
    reload_up = final_up + config.primary.reload_press_ms
    duration = reload_up + config.primary.reload_wait_ms
    assert cycle_events[-2:] == [("R_DOWN", final_up), ("R_UP", reload_up)]
    assert cycle_events[-2][1] - ups[-1] == 0
    assert round(h.clock() * 1000) - started_at == duration
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    assert h.machine.enabled and h.audio.events == ["ON"]

    macro.begin_next_cycle()
    h.drain()
    assert h.backend.timed_events[-1] == ("MB1_DOWN", started_at + duration)
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.UNKNOWN
    assert h.audio.events == ["ON"]
    return "PRIMARY completed one automatic hold, reloaded FULL, and repeated"


def _run_primary_cancellation(
    config: AppConfig, cancel_at_ms: int
) -> tuple[FakeGeneratedInput, WorkerResult, tuple[WorkerProgressUpdate, ...]]:
    clock = FakeClock()
    started_at = clock()
    backend = FakeGeneratedInput(lambda: "RUNNING_PRIMARY", clock)
    cancel = threading.Event()
    progress: list[WorkerProgressUpdate] = []

    def wait(event: threading.Event, seconds: float) -> bool:
        result = clock.wait(event, seconds)
        if round((clock() - started_at) * 1000) >= cancel_at_ms:
            cancel.set()
        return result

    engine = MacroEngine(
        config,
        backend,
        lambda: True,
        clock=clock,
        wait=wait,
    )
    result = engine.run_macro(
        WeaponMode.PRIMARY,
        cancel,
        threading.Event(),
        progress.append,
    )
    return backend, result, tuple(progress)


def _scenario_u(config: AppConfig) -> str:
    hold = config.primary.automatic_hold_ms
    assert hold is not None
    reload_down = hold + config.primary.post_fire_reload_delay_ms
    reload_up = reload_down + config.primary.reload_press_ms
    cases = (
        ("early automatic hold", 5, False),
        ("mid automatic hold", hold // 2, False),
        ("late automatic hold", hold - 5, False),
        ("R-down", reload_down + 5, True),
        ("reload wait", reload_up + 5, True),
    )
    for label, cancel_at, expect_reload in cases:
        backend, result, progress = _run_primary_cancellation(config, cancel_at)
        names = [name for name, _state in backend.events]
        assert result.canceled, label
        assert names.count("MB1_DOWN") == 1, label
        assert names.count("MB1_UP") == 1, label
        assert ("R_DOWN" in names) is expect_reload, label
        assert ("R_UP" in names) is expect_reload, label
        assert WorkerProgress.RELOAD_COMPLETED not in (
            update.phase for update in progress
        ), label
        assert not backend.mouse_owned and not backend.reload_owned, label
    return "PRIMARY cancellation cleaned its hold at firing/reload positions"


def _scenario_v(config: AppConfig) -> str:
    clock = FakeClock()
    started_at = clock()
    backend = FakeGeneratedInput(lambda: "RUNNING_SECONDARY", clock)
    cancel = threading.Event()
    progress: list[tuple[WorkerProgress, int]] = []

    def report(update: WorkerProgressUpdate) -> None:
        elapsed = round((update.occurred_at - started_at) * 1000)
        progress.append((update.phase, elapsed))
        if update.phase is WorkerProgress.RELOAD_COMPLETED:
            cancel.set()

    result = MacroEngine(
        config,
        backend,
        lambda: True,
        clock=clock,
        wait=clock.wait,
    ).run_macro(WeaponMode.SECONDARY, cancel, threading.Event(), report)
    names = [name for name, _state in backend.events]
    times = [
        (name, at - round(started_at * 1000))
        for name, at in backend.timed_events
    ]
    downs = [at for name, at in times if name == "MB1_DOWN"]
    ups = [at for name, at in times if name == "MB1_UP"]
    period = config.secondary.shot_period_ms
    press = config.secondary.fire_press_ms
    release = period - press
    final_down = (config.secondary.shots_per_cycle - 1) * period
    final_up = final_down + press
    reload_up = final_up + config.secondary.reload_press_ms
    duration = reload_up + config.secondary.reload_wait_ms
    assert result.canceled
    assert names == ["MB1_DOWN", "MB1_UP"] * 13 + ["R_DOWN", "R_UP"]
    assert [up - down for down, up in zip(downs, ups)] == [press] * 13
    assert [later - earlier for earlier, later in zip(downs, downs[1:])] == [period] * 12
    assert [next_down - up for up, next_down in zip(ups, downs[1:])] == [release] * 12
    assert times[-2:] == [("R_DOWN", final_up), ("R_UP", reload_up)]
    assert times[-2][1] == ups[-1]
    assert progress[-1] == (WorkerProgress.RELOAD_COMPLETE, duration)
    return "SECONDARY final fire-up and R-down were consecutive at 1,475 ms"


def _scenario_w(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.aim_on()
    started_at = round(h.clock() * 1000)
    assert h.click() == (True, True)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.complete_cycle_reload()
    h.drain()
    relative = [(name, at - started_at) for name, at in h.backend.timed_events]
    downs = [at for name, at in relative if name == "MB1_DOWN"]
    ups = [at for name, at in relative if name == "MB1_UP"]
    assert len(downs) == len(ups) == 1
    hold = config.primary.automatic_hold_ms
    assert hold is not None
    final_down = 0
    final_up = hold + config.primary.post_fire_reload_delay_ms
    reload_up = final_up + config.primary.reload_press_ms
    duration = reload_up + config.primary.reload_wait_ms
    assert downs[-1] == final_down and ups[-1] == final_up
    assert relative[-2:] == [("R_DOWN", final_up), ("R_UP", reload_up)]
    assert round(h.clock() * 1000) - started_at == duration
    trace_records = [
        record for record in h.reports if record.startswith("TRACE:")
    ]
    trace = {
        record.split("event=", 1)[1].split(" ", 1)[0]: float(
            record.split("elapsed_ms=", 1)[1].split(" ", 1)[0]
        )
        for record in trace_records
    }
    assert trace["FINAL_SHOT_DOWN"] == float(final_down)
    assert trace["FINAL_SHOT_UP"] == float(final_up)
    assert trace["RELOAD_KEY_DOWN"] == float(final_up)
    assert trace["RELOAD_KEY_UP"] == float(reload_up)
    assert trace["RELOAD_WAIT_STARTED"] == float(reload_up)
    assert trace["RELOAD_COMPLETED"] == float(duration)
    for event_name in (
        "FINAL_SHOT_DOWN",
        "FINAL_SHOT_UP",
        "RELOAD_KEY_DOWN",
        "RELOAD_KEY_UP",
        "RELOAD_WAIT_STARTED",
        "RELOAD_COMPLETED",
    ):
        record = next(
            item for item in trace_records if f"event={event_name} " in item
        )
        assert "source=worker" in record
        assert "weapon=PRIMARY" in record
        assert "enabled=true" in record
        assert f"worker_phase={event_name}" in record
        assert "generation=" in record and "reason=" in record
    return f"PRIMARY timing remained unchanged at {duration:,} ms"


def _scenario_x(config: AppConfig) -> str:
    for mode, shift_vk in (
        (WeaponMode.PRIMARY, VK_LSHIFT),
        (WeaponMode.SECONDARY, VK_RSHIFT),
    ):
        h = SimulationHarness(config, trace=True)
        h.start(mode)
        before = len(h.backend.events)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        assert all(h.key_press(shift_vk, repeats=2))
        emitted = [name for name, _state in h.backend.events[before:]]
        assert emitted == [
            "MB1_UP",
            "MB2_UP",
            "SHIFT_DOWN",
            "SHIFT_UP",
        ]
        assert not h.machine.enabled and not h.machine.firing
        assert h.machine.worker is None
        assert h.machine.magazine_state(mode) is MagazineState.UNKNOWN
        assert h.audio.events == ["ON", "OFF"]
        assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
        assert not any(
            worker.request.kind is WorkerKind.RELOAD_ONLY for worker in h.workers
        )
        disabled = [
            report
            for report in h.reports
            if "event=MACRO_DISABLED" in report and "reason=SHIFT_SPRINT" in report
        ]
        assert len(disabled) == 1
    return "left/right Shift stopped both modes and generated no reload"


def _scenario_y(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.start(WeaponMode.PRIMARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.begin_macro_reload()
    h.drain()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    h.key_press(VK_LSHIFT, repeats=2)
    assert not h.machine.enabled and h.audio.events == ["ON", "OFF"]
    assert h.machine.worker is macro and macro.finish_after_reload_requested
    macro.complete_macro_reload()
    h.drain()
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert h.machine.worker is None and not h.machine.enabled
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    assert not any(
        worker.request.kind is WorkerKind.RELOAD_ONLY for worker in h.workers
    )
    assert [name for name, _state in h.backend.events].count("SHIFT_DOWN") == 1
    return "Shift preserved one active reload without duplicating R"


def _scenario_z(config: AppConfig) -> str:
    idle = SimulationHarness(config)
    idle_before = (
        idle.machine.state,
        idle.machine.generation,
        idle.machine.magazine_state(WeaponMode.PRIMARY),
        tuple(idle.audio.events),
    )
    idle.key_press(VK_LSHIFT, repeats=3)
    assert idle_before == (
        idle.machine.state,
        idle.machine.generation,
        idle.machine.magazine_state(WeaponMode.PRIMARY),
        tuple(idle.audio.events),
    )
    assert [name for name, _state in idle.backend.events] == [
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]

    prep = SimulationHarness(config, auto_complete_preparation=False)
    prep.key_press(VK_2)
    worker = prep.machine.worker
    before = (prep.machine.generation, tuple(prep.audio.events))
    prep.key_press(VK_RSHIFT, repeats=3)
    assert prep.machine.worker is worker
    assert before == (prep.machine.generation, tuple(prep.audio.events))
    assert isinstance(worker, FakeSessionWorker) and not worker.cancel_requested
    assert sum(name == "R_DOWN" for name, _state in prep.backend.events) == 0
    worker.finish_preparation()
    prep.drain()
    assert prep.machine.magazine_state(WeaponMode.SECONDARY) is MagazineState.FULL
    return "idle/preparation Shift replayed once without starting another reload"


def _mb2_snapshot(h: SimulationHarness) -> tuple[object, ...]:
    return (
        h.machine.state,
        h.machine.generation,
        h.machine.magazine_state(h.machine.selected_mode),
        h.machine.worker,
        tuple(h.audio.events),
        len(h.backend.events),
    )


def _exercise_hold_twice(h: SimulationHarness) -> None:
    before = _mb2_snapshot(h)
    for _ in range(2):
        assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
    assert _mb2_snapshot(h) == before


def _scenario_aa(config: AppConfig) -> str:
    preparation = SimulationHarness(config, auto_complete_preparation=False)
    preparation.key_press(VK_2)
    _exercise_hold_twice(preparation)
    prep_worker = preparation.machine.worker
    assert isinstance(prep_worker, FakeSessionWorker)
    prep_worker.begin_reload()
    _exercise_hold_twice(preparation)

    sprint = SimulationHarness(config)
    sprint.start(WeaponMode.PRIMARY)
    sprint.key_press(VK_LSHIFT)
    shifts = sum(name == "SHIFT_DOWN" for name, _state in sprint.backend.events)
    _exercise_hold_twice(sprint)
    assert sum(name == "SHIFT_DOWN" for name, _state in sprint.backend.events) == shifts

    idle = SimulationHarness(config)
    _exercise_hold_twice(idle)
    return "repeated disabled RMB hold/release cycles never inverted eligibility"


def _scenario_ab(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    h.drain()
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_VALID
    generation = h.machine.generation
    assert all(h.key_press(VK_LSHIFT, repeats=3))
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_REARM_REQUIRED
    assert h.machine.generation == generation
    assert [name for name, _state in h.backend.events] == [
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert all(
        source is EventSource.INJECTED_OWNED
        for _name, source in h.backend.tagged_events
    )
    assert h.audio.events == []
    sent = len(h.backend.events)
    h.key_press(VK_RSHIFT, repeats=2)
    assert [name for name, _state in h.backend.events[sent:]] == [
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert all(name != "R_DOWN" for name, _state in h.backend.events)
    return "tagged RMB-up neutralized held aim before one Shift replay"


def _scenario_ac(config: AppConfig) -> str:
    pending = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_shift=False,
    )
    pending.hold_rmb()
    pending.key_press(VK_LSHIFT)
    aim_worker = next(
        worker
        for worker in pending.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    pending.policy.mouse(WM_RBUTTONUP, 0, 0)
    pending.drain()
    pending.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    pending.drain()
    assert aim_worker.cancel_requested
    assert pending.machine.rmb_hold_state is RmbHoldState.HELD_VALID
    aim_worker.finish(WorkerResult(True))
    pending.drain()
    assert pending.machine.rmb_hold_state is RmbHoldState.HELD_VALID

    lost = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_shift=False,
    )
    lost.hold_rmb()
    lost.key_press(VK_LSHIFT)
    lost_worker = next(
        worker
        for worker in lost.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    lost.foreground_loss()
    assert not lost.backend.aim_owned
    assert lost.machine.rmb_hold_state is RmbHoldState.HELD_REARM_REQUIRED
    return "pending Shift invalidation and foreground loss preserved safe RMB rearm"


def _scenario_ad(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.start(WeaponMode.PRIMARY)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert all(h.key_press(VK_LSHIFT, repeats=2))
    emitted = [name for name, _state in h.backend.events[before:]]
    assert emitted == [
        "MB1_UP",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert h.audio.events == ["ON", "OFF"] and not h.machine.enabled
    return "firing stopped, held aim released, and Shift replayed without R"


def _scenario_ae(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.start(WeaponMode.SECONDARY)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert all(h.key_press(VK_RSHIFT, repeats=2))
    emitted = [name for name, _state in h.backend.events[before:]]
    assert emitted == [
        "MB1_UP",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert "R_DOWN" not in emitted
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "SECONDARY held-aim release completed before Shift with no R"


def _scenario_af(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    h.drain()
    h.key_press(VK_LSHIFT)
    assert [name for name, _state in h.backend.events] == [
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert h.audio.events == []
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_REARM_REQUIRED
    return "idle held aim was neutralized before sprint with no macro audio"


def _scenario_ag(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT)
    shift_count = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
    for _ in range(2):
        assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        h.drain()
        assert h.machine.rmb_hold_state is RmbHoldState.HELD_VALID
        assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
        assert h.machine.rmb_hold_state is RmbHoldState.RELEASED
    assert sum(name == "SHIFT_DOWN" for name, _state in h.backend.events) == shift_count
    assert not any(name == "R_DOWN" for name, _state in h.backend.events)
    return "persistent sprint RMB hold cycles emitted no additional Shift or R"


def _scenario_ah(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT, repeats=3)
    h.key_press(VK_RSHIFT, repeats=3)
    names = [name for name, _state in h.backend.events]
    assert names == ["SHIFT_DOWN", "SHIFT_UP"] * 2
    assert h.backend.shift_scans == [0x2A, 0x36]
    assert "R_DOWN" not in names and h.audio.events == []
    return "a second deliberate Shift produced exactly one second replay pair"


def _scenario_ai(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.start(WeaponMode.PRIMARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.begin_macro_reload()
    h.drain()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    h.key_press(VK_LSHIFT)
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert macro.finish_after_reload_requested and not h.machine.enabled
    macro.complete_macro_reload()
    h.drain()
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    return "Shift allowed the existing reload to finish without duplicate R"


def _scenario_aj(config: AppConfig) -> str:
    aim_phase = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_shift=False,
    )
    aim_phase.hold_rmb()
    aim_phase.key_press(VK_LSHIFT)
    aim_worker = next(
        worker
        for worker in aim_phase.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    aim_phase.foreground_loss()
    assert not any(
        (
            aim_phase.backend.mouse_owned,
            aim_phase.backend.aim_owned,
            aim_phase.backend.shift_owned,
            aim_phase.backend.reload_owned,
        )
    )

    shift_phase = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_shift=False,
    )
    shift_phase.start(WeaponMode.PRIMARY)
    reloads = sum(
        name == "R_DOWN" for name, _state in shift_phase.backend.events
    )
    shift_phase.key_press(VK_RSHIFT)
    shift_worker = next(
        worker
        for worker in shift_phase.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    shift_worker.begin_shift_replay()
    shift_phase.foreground_loss()
    assert not any(
        (
            shift_phase.backend.mouse_owned,
            shift_phase.backend.aim_owned,
            shift_phase.backend.shift_owned,
            shift_phase.backend.reload_owned,
        )
    )
    assert (
        sum(name == "R_DOWN" for name, _state in shift_phase.backend.events)
        == reloads
    )
    return "foreground loss cleaned pending hold-release, Shift, MB1, and R ownership"


def _scenario_ak(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    assert h.click() == (True, True)
    assert not h.machine.enabled and h.machine.worker is None
    assert h.audio.events == [] and h.backend.events == []
    assert any("event=RMB_HOLD_REQUIRED_REJECTED" in item for item in h.reports)
    return "RMB-released PRIMARY activation was deterministically rejected"


def _scenario_al(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.make_full(WeaponMode.SECONDARY)
    baseline = len(h.backend.events)
    assert h.click() == (True, True)
    assert not h.machine.enabled and h.machine.worker is None
    assert len(h.backend.events) == baseline and h.audio.events == []
    assert any("reason=RMB_HOLD_REQUIRED" in item for item in h.reports)
    return "RMB-released SECONDARY activation was deterministically rejected"


def _scenario_am(config: AppConfig) -> str:
    h = SimulationHarness(config)
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_LBUTTONUP, 0, 0)
    h.drain()
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_VALID
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    assert h.audio.events == ["ON"]
    assert h.backend.events == [("MB1_DOWN", "RUNNING_PRIMARY")]
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    return "rapid physical RMB hold then MB1 was accepted in FIFO order"


def _rmb_release_while_firing(config: AppConfig, mode: WeaponMode) -> None:
    h = SimulationHarness(config, trace=True)
    h.start(mode)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert [name for name, _state in h.backend.events[before:]] == [
        "MB1_UP",
    ]
    assert not h.machine.enabled and not h.machine.firing
    assert h.machine.rmb_hold_state is RmbHoldState.RELEASED
    assert h.audio.events == ["ON", "OFF"]
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert not any(name == "SHIFT_DOWN" for name, _state in h.backend.events[before:])


def _scenario_an(config: AppConfig) -> str:
    _rmb_release_while_firing(config, WeaponMode.PRIMARY)
    return "physical RMB-up stopped PRIMARY with no replay, Shift, or R"


def _scenario_ao(config: AppConfig) -> str:
    _rmb_release_while_firing(config, WeaponMode.SECONDARY)
    return "physical RMB-up stopped SECONDARY with no replay, Shift, or R"


def _scenario_ap(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT)
    sprint_shifts = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
    h.aim_on()
    h.click()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert not h.machine.enabled
    assert h.machine.rmb_hold_state is RmbHoldState.RELEASED
    assert sum(name == "SHIFT_DOWN" for name, _state in h.backend.events) == sprint_shifts
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "persistent sprint hold/fire/RMB-up emitted no extra Shift or R"


def _scenario_aq(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT)
    h.aim_on()
    h.click()
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    h.key_press(VK_RSHIFT, repeats=2)
    assert [name for name, _state in h.backend.events[before:]] == [
        "MB1_UP",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert not h.machine.enabled
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_REARM_REQUIRED
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "persistent sprint hold/fire/Shift preserved safe release ordering"


def _scenario_ar(config: AppConfig) -> str:
    h = SimulationHarness(
        config,
        auto_complete_cancel=False,
    )
    h.start(WeaponMode.PRIMARY)
    h.foreground_loss()
    assert not any(
        (
            h.backend.mouse_owned,
            h.backend.aim_owned,
            h.backend.shift_owned,
            h.backend.reload_owned,
        )
    )
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_REARM_REQUIRED
    assert not h.machine.enabled
    return "foreground loss invalidated held RMB and released all owned input"


def _scenario_as(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.start(WeaponMode.PRIMARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.begin_macro_reload()
    h.drain()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert macro.finish_after_reload_requested and not h.machine.enabled
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    macro.complete_macro_reload()
    h.drain()
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    assert not h.machine.enabled and h.machine.worker is None
    return "RMB-up preserved an already-started reload without duplicate R"


def _scenario_at(config: AppConfig) -> str:
    h = SimulationHarness(
        config,
        trace=True,
        foreground_active=False,
        foreground_certain=True,
    )
    h.foreground_loss()
    assert h.machine.rmb_hold_state is RmbHoldState.RELEASED
    assert not h.machine.target_has_been_active
    h.foreground_acquired()
    assert h.machine.target_has_been_active
    assert h.machine.rmb_hold_state is RmbHoldState.RELEASED
    h.aim_on()
    h.click()
    assert h.machine.rmb_hold_state is RmbHoldState.HELD_VALID
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    events = [
        report.split("event=", 1)[1].split(" ", 1)[0]
        for report in h.reports
        if report.startswith("TRACE:")
    ]
    assert "RMB_HOLD_ESTABLISHED" in events
    assert events[-3:] == ["MACRO_ENABLED", "FIRING_STARTED", "FINAL_SHOT_DOWN"]
    assert not any("event=RMB_HOLD_REQUIRED_REJECTED" in item for item in h.reports)
    return "PowerShell baseline then target RMB/MB1 reached FIRING_STARTED"


def _scenario_au(config: AppConfig) -> str:
    h = SimulationHarness(config)
    started = h.clock()
    assert h.key_press(VK_F23, repeats=4) == (True,) * 6
    assert round((h.clock() - started) * 1000) == sequence_duration_ms(
        FOUR_TARGET_SEQUENCES, config.stratagems
    )
    arrows = [
        int(name[6:8], 16)
        for name, _state in h.backend.events
        if name.startswith("ARROW_") and name.endswith("_DOWN_EXTENDED")
    ]
    assert arrows == [direction.scan_code for entry in FOUR_TARGET_SEQUENCES for direction in entry]
    assert sum(name == "STRATAGEM_MB1_DOWN" for name, _ in h.backend.events) == 4
    assert h.audio.events == [] and h.machine.state is MacroState.IDLE_PRIMARY

    support = SimulationHarness(config)
    started = support.clock()
    assert support.key_press(VK_F24) == (True, True)
    assert round((support.clock() - started) * 1000) == 2040
    arrows = [
        int(name[6:8], 16)
        for name, _state in support.backend.events
        if name.startswith("ARROW_") and name.endswith("_DOWN_EXTENDED")
    ]
    assert arrows == [direction.scan_code for entry in SUPPORT_SEQUENCES for direction in entry]
    assert support.audio.events == [] and not support.machine.enabled
    return (
        "F23/F24 exact immutable sequences completed in "
        f"{sequence_duration_ms(FOUR_TARGET_SEQUENCES, config.stratagems)}/"
        f"{sequence_duration_ms(SUPPORT_SEQUENCES, config.stratagems)} ms without audio"
    )


def _scenario_av(config: AppConfig) -> str:
    h = SimulationHarness(config)
    # One held physical pair produces one activation despite repeat; a fresh
    # pair is required for the second activation.
    h.key_press(VK_F23, repeats=8)
    assert len([w for w in h.workers if w.request.kind is WorkerKind.STRATAGEM]) == 1
    h.key_press(VK_F23)
    assert len([w for w in h.workers if w.request.kind is WorkerKind.STRATAGEM]) == 2

    for active, certain in ((False, True), (False, False)):
        gated = SimulationHarness(
            config, foreground_active=active, foreground_certain=certain
        )
        assert gated.key_press(VK_F23) == (False, False)
        assert not gated.workers and not gated.backend.events
    generated = SimulationHarness(config)
    assert not generated.policy.keyboard(
        WM_KEYDOWN, VK_F24, 0x10, extra_info=0x43524F31
    )
    assert not generated.policy.keyboard(
        WM_KEYUP, VK_F24, 0x10, extra_info=0x43524F31
    )
    assert not generated.workers
    return "repeat collapsed, fresh press reran, and background/uncertain/owned triggers passed"


def _scenario_aw(config: AppConfig) -> str:
    firing = SimulationHarness(config)
    firing.start(WeaponMode.PRIMARY)
    before = list(firing.backend.events)
    firing.key_press(VK_F23)
    assert firing.backend.events == before
    macro = firing.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.begin_macro_reload()
    firing.drain()
    before = list(firing.backend.events)
    firing.key_press(VK_F24)
    assert firing.backend.events == before

    preparing = SimulationHarness(config, auto_complete_preparation=False)
    preparing.key_press(VK_2)
    before = list(preparing.backend.events)
    preparing.key_press(VK_F23)
    assert preparing.backend.events == before

    bypass = SimulationHarness(config)
    bypass.send(ControlEventKind.DEFERRED_BYPASS_DOWN)
    bypass.drain()
    before = list(bypass.backend.events)
    bypass.key_press(VK_F24)
    assert bypass.backend.events == before
    return "firing, reload, preparation, and pending bypass rejected without deferred output"


def _scenario_ax(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_stratagem=False)
    h.key_press(VK_F23)
    worker = h.machine.worker
    assert isinstance(worker, FakeSessionWorker)
    h.backend.stratagem_key_down(worker.token, 0x1D, extended=False, ctrl=True)
    h.backend.stratagem_key_down(worker.token, 0x48, extended=True)
    h.key_press(VK_F24)
    h.key_press(VK_1)
    assert h.click() == (True, True)
    assert h.machine.worker is worker and h.machine.selected_mode is WeaponMode.PRIMARY
    assert h.policy.mouse(WM_RBUTTONDOWN, 0, 0) is False
    h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert h.backend.stratagem_owner is None and not h.machine.stratagem_active
    assert h.audio.events == []

    for shift in (VK_LSHIFT, VK_RSHIFT):
        shifted = SimulationHarness(config, auto_complete_stratagem=False)
        shifted.key_press(VK_F23)
        active = shifted.machine.worker
        assert isinstance(active, FakeSessionWorker)
        shifted.backend.stratagem_mouse_down(active.token)
        assert shifted.key_press(shift) == (True, True)
        assert shifted.backend.stratagem_owner is None
        assert not any(name == "R_DOWN" for name, _ in shifted.backend.events)
    return "active exclusivity held; RMB and both Shifts canceled owned input without R/audio"


def _scenario_ay(config: AppConfig) -> str:
    for shutdown in (False, True):
        h = SimulationHarness(config, auto_complete_stratagem=False)
        h.key_press(VK_F23)
        worker = h.machine.worker
        assert isinstance(worker, FakeSessionWorker)
        h.backend.stratagem_key_down(worker.token, 0x1D, extended=False, ctrl=True)
        h.backend.stratagem_mouse_down(worker.token)
        if shutdown:
            h.machine.shutdown()
        else:
            h.foreground_loss(certain=False)
        assert h.backend.stratagem_owner is None
        assert not h.backend.stratagem_ctrl_owned
        assert not h.backend.stratagem_mouse_owned
    return "foreground uncertainty and shutdown cleaned only stratagem-owned inputs"


def _scenario_az(config: AppConfig) -> str:
    # Deterministically cancel inside every timing phase across all 21 arrow
    # positions; every run must finish with no owned Ctrl/arrow/MB1.
    for cutoff in [5, 25, 45, 65, 205, 225, 245, *(
        25 + position * 40 for position in range(21)
    )]:
        h = SimulationHarness(config)
        cancel = threading.Event()
        origin = h.clock()

        def wait(event: threading.Event, seconds: float) -> bool:
            result = h.clock.wait(event, seconds)
            if round((h.clock() - origin) * 1000) >= cutoff:
                cancel.set()
            return result

        engine = MacroEngine(
            h.config,
            h.backend,
            h.foreground.is_confirmed_active,
            clock=h.clock,
            wait=wait,
        )
        result = engine.run_stratagem(
            900 + cutoff, FOUR_TARGET_SEQUENCES, cancel, threading.Event()
        )
        assert result.canceled
        assert h.backend.stratagem_owner is None
        assert not h.backend.stratagem_scan
        assert not h.backend.stratagem_ctrl_owned
        assert not h.backend.stratagem_mouse_owned
    return "Ctrl settle, all arrow presses/gaps, MB1, and action-delay cancellation cleaned ownership"


def _scenario_ba(config: AppConfig) -> str:
    """Model the user's recovery gesture after an unobservable forced aim loss."""
    h = SimulationHarness(config)
    for cycle in range(3):
        h.hold_rmb()
        h.clock.advance_ms(config.controls.toggle_debounce_ms)
        h.click()
        assert h.machine.enabled
        # A hit/stagger has no observable controller event. Recovery is the
        # explicit physical release/down sequence, never an inferred toggle.
        h.release_rmb()
        assert not h.machine.enabled
        h.clock.advance_ms(config.controls.toggle_debounce_ms)
        h.click()
        assert not h.machine.enabled
        h.hold_rmb()
        assert not h.machine.enabled
        if cycle < 2:
            h.clock.advance_ms(config.controls.toggle_debounce_ms)
            h.click()
            assert h.machine.enabled
            h.release_rmb()
    assert "MB2_DOWN" not in [name for name, _ in h.backend.events]
    return "hit/stagger recovery cycles required release/down and never inverted eligibility"


_SCENARIOS = (
    ("A", _scenario_a),
    ("B", _scenario_b),
    ("C", _scenario_c),
    ("D", _scenario_d),
    ("E", _scenario_e),
    ("F", _scenario_f),
    ("G", _scenario_g),
    ("H", _scenario_h),
    ("I", _scenario_i),
    ("J", _scenario_j),
    ("K", _scenario_k),
    ("L", _scenario_l),
    ("M", _scenario_m),
    ("N", _scenario_n),
    ("O", _scenario_o),
    ("P", _scenario_p),
    ("Q", _scenario_q),
    ("R", _scenario_r),
    ("S", _scenario_s),
    ("T", _scenario_t),
    ("U", _scenario_u),
    ("V", _scenario_v),
    ("W", _scenario_w),
    ("X", _scenario_x),
    ("Y", _scenario_y),
    ("Z", _scenario_z),
    ("AA", _scenario_aa),
    ("AB", _scenario_ab),
    ("AC", _scenario_ac),
    ("AD", _scenario_ad),
    ("AE", _scenario_ae),
    ("AF", _scenario_af),
    ("AG", _scenario_ag),
    ("AH", _scenario_ah),
    ("AI", _scenario_ai),
    ("AJ", _scenario_aj),
    ("AK", _scenario_ak),
    ("AL", _scenario_al),
    ("AM", _scenario_am),
    ("AN", _scenario_an),
    ("AO", _scenario_ao),
    ("AP", _scenario_ap),
    ("AQ", _scenario_aq),
    ("AR", _scenario_ar),
    ("AS", _scenario_as),
    ("AT", _scenario_at),
    ("AU", _scenario_au),
    ("AV", _scenario_av),
    ("AW", _scenario_aw),
    ("AX", _scenario_ax),
    ("AY", _scenario_ay),
    ("AZ", _scenario_az),
    ("BA", _scenario_ba),
)


def run_simulated_session(config: AppConfig) -> int:
    lines: list[str] = []
    try:
        for label, scenario in _SCENARIOS:
            detail = scenario(config)
            line = f"SCENARIO {label}: PASS - {detail}"
            lines.append(line)
            print(line)
        prohibited = (
            "Preparation failed for PRIMARY: RIGHT_DOWN",
            "Preparation failed for SECONDARY: RIGHT_DOWN",
            "START_REJECTED: MB1-up",
        )
        joined = "\n".join(lines)
        if any(text in joined for text in prohibited):
            raise AssertionError("prohibited trace-derived result detected")
    except BaseException as exc:
        print(f"SIMULATION ERROR: {exc}")
        print("DETERMINISTIC CONTROL SIMULATION: FAIL")
        return 1
    print("DETERMINISTIC CONTROL SIMULATION: PASS")
    return 0
