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
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
)
from .macro_engine import MacroEngine, primary_cycle_steps, secondary_cycle_steps
from .models import (
    AimState,
    ControlEvent,
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    OutputAction,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerProgressUpdate,
    WorkerRequest,
    WorkerResult,
)
from .state_machine import MacroStateMachine


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
        self.shift_owned = False
        self.shift_scan = 0
        self.shift_scans: list[int] = []
        self.reload_owned = False

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
        self.mouse_up()
        self.aim_up()
        self.shift_up()
        self.reload_up()

    def release_shift_inputs(self) -> None:
        self.aim_up()
        self.shift_up()


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
        self.aim_started = False
        self.aim_sent = False
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
        elif self.request.kind is WorkerKind.AIM_OFF_TRANSACTION:
            if self.harness.auto_complete_shift:
                self.finish_aim_off_transaction()
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

    def begin_aim_off(self) -> None:
        if (
            self.request.kind not in (
                WorkerKind.SHIFT_TRANSACTION,
                WorkerKind.AIM_OFF_TRANSACTION,
            )
            or not self.request.cancel_aim
            or self.completed
        ):
            raise AssertionError("transaction has no active aim-off output")
        self.harness.backend.aim_down()
        self.aim_started = True
        if self.request.kind is WorkerKind.AIM_OFF_TRANSACTION:
            self.progress(
                WorkerProgress.AIM_OFF_REPLAY_DOWN,
                "owned tagged deferred RMB-off replay pressed",
            )

    def finish_aim_off(self) -> None:
        if (
            self.request.kind not in (
                WorkerKind.SHIFT_TRANSACTION,
                WorkerKind.AIM_OFF_TRANSACTION,
            )
            or not self.request.cancel_aim
            or self.completed
        ):
            raise AssertionError("transaction has no active aim-off output")
        if not self.aim_started:
            self.begin_aim_off()
        self.harness.clock.advance_ms(20)
        self.harness.backend.aim_up()
        self.aim_sent = True
        if self.request.kind is WorkerKind.SHIFT_TRANSACTION:
            self.progress(
                WorkerProgress.AIM_OFF_SENT,
                "owned tagged MB2 aim-off pair completed before Shift replay",
            )
        else:
            self.progress(
                WorkerProgress.AIM_OFF_REPLAY_UP,
                "owned tagged deferred RMB-off replay released",
            )

    def finish_aim_off_transaction(self) -> None:
        if (
            self.request.kind is not WorkerKind.AIM_OFF_TRANSACTION
            or self.completed
        ):
            raise AssertionError("only a deferred RMB-off transaction can finish")
        self.begin_aim_off()
        self.finish_aim_off()
        self.finish(WorkerResult(True))

    def begin_shift_replay(self) -> None:
        if self.request.kind is not WorkerKind.SHIFT_TRANSACTION or self.completed:
            raise AssertionError("only a Shift transaction can begin replay")
        if self.request.cancel_aim and not self.aim_sent:
            self.finish_aim_off()
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
        if self.request.kind in (
            WorkerKind.SHIFT_TRANSACTION,
            WorkerKind.AIM_OFF_TRANSACTION,
        ):
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
            elif self.request.kind in (
                WorkerKind.SHIFT_TRANSACTION,
                WorkerKind.AIM_OFF_TRANSACTION,
            ):
                self.harness.backend.release_shift_inputs()
            elif self.request.kind is WorkerKind.BYPASS:
                self.harness.backend.mouse_up()
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
                self.aim_started,
                self.aim_sent,
                self.shift_started,
                self.shift_sent,
                exc,
            )
        if self.harness.auto_complete_cancel:
            self.finish(WorkerResult(False, canceled=True))
        return (
            self.aim_started,
            self.aim_sent,
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
        auto_complete_aim_off: bool = True,
        foreground_active: bool = True,
        foreground_certain: bool = True,
    ) -> None:
        self.config = replace(
            config,
            diagnostics=replace(config.diagnostics, state_tracing=trace),
        )
        self.auto_complete_preparation = auto_complete_preparation
        self.auto_complete_cancel = auto_complete_cancel
        self.auto_complete_shift = auto_complete_aim_off
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
        self.aim_on()
        self.clock.advance_ms(self.config.controls.toggle_debounce_ms)
        self.click()
        expected = (
            MacroState.RUNNING_PRIMARY
            if mode is WeaponMode.PRIMARY
            else MacroState.RUNNING_SECONDARY
        )
        if self.machine.state is not expected:
            raise AssertionError(f"expected {expected.name}, got {self.machine.state.name}")

    def aim_on(self) -> None:
        if self.machine.aim_state is AimState.AIM_ON:
            return
        if self.machine.aim_state not in (AimState.AIM_OFF, AimState.UNKNOWN):
            raise AssertionError(
                f"cannot establish AIM_ON from {self.machine.aim_state.name}"
            )
        if self.policy.mouse(WM_RBUTTONDOWN, 0, 0):
            raise AssertionError("idle physical RMB-down must pass through")
        if self.policy.mouse(WM_RBUTTONUP, 0, 0):
            raise AssertionError("idle physical RMB-up must pass through")
        self.drain()
        if self.machine.aim_state is not AimState.AIM_ON:
            raise AssertionError("physical RMB did not establish AIM_ON")

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
        report.startswith("START_REJECTED:") and "reason=AIM_REQUIRED" in report
        for report in h.reports
    )
    return "un-aimed PRIMARY was rejected without output or audio"


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
    return "MB2 aim chord passed through while both modes toggled normally"


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
            "MB2_DOWN",
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


def _toggle_aim_twice(h: SimulationHarness) -> None:
    before = _mb2_snapshot(h)
    for _ in range(2):
        assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
    assert _mb2_snapshot(h) == before


def _scenario_aa(config: AppConfig) -> str:
    preparation = SimulationHarness(config, auto_complete_preparation=False)
    preparation.key_press(VK_2)
    _toggle_aim_twice(preparation)
    prep_worker = preparation.machine.worker
    assert isinstance(prep_worker, FakeSessionWorker)
    prep_worker.begin_reload()
    _toggle_aim_twice(preparation)

    sprint = SimulationHarness(config)
    sprint.start(WeaponMode.PRIMARY)
    sprint.key_press(VK_LSHIFT)
    shifts = sum(name == "SHIFT_DOWN" for name, _state in sprint.backend.events)
    _toggle_aim_twice(sprint)
    assert sum(name == "SHIFT_DOWN" for name, _state in sprint.backend.events) == shifts

    idle = SimulationHarness(config)
    _toggle_aim_twice(idle)
    return "disabled physical MB2 toggled aim with zero macro-state effect"


def _scenario_ab(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert h.machine.aim_state is AimState.AIM_ON
    generation = h.machine.generation
    assert all(h.key_press(VK_LSHIFT, repeats=3))
    assert h.machine.aim_state is AimState.AIM_OFF
    assert h.machine.generation == generation
    assert [name for name, _state in h.backend.events] == [
        "MB2_DOWN",
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
    return "aim-off MB2 completed before one tagged Shift replay"


def _scenario_ac(config: AppConfig) -> str:
    pending = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_aim_off=False,
    )
    pending.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    pending.policy.mouse(WM_RBUTTONUP, 0, 0)
    pending.drain()
    pending.key_press(VK_LSHIFT)
    aim_worker = next(
        worker
        for worker in pending.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    pending.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    pending.policy.mouse(WM_RBUTTONUP, 0, 0)
    pending.drain()
    assert aim_worker.cancel_requested
    assert pending.machine.aim_state is AimState.UNKNOWN
    aim_worker.finish(WorkerResult(True))
    pending.drain()
    assert pending.machine.aim_state is AimState.UNKNOWN

    lost = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_aim_off=False,
    )
    lost.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    lost.policy.mouse(WM_RBUTTONUP, 0, 0)
    lost.drain()
    lost.key_press(VK_LSHIFT)
    lost_worker = next(
        worker
        for worker in lost.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    lost_worker.begin_aim_off()
    lost.foreground_loss()
    assert not lost.backend.aim_owned
    assert lost.machine.aim_state is AimState.UNKNOWN

    native_config = replace(
        config,
        controls=replace(
            config.controls,
            shift_cancels_aim_natively=True,
        ),
    )
    native = SimulationHarness(native_config)
    native.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    native.policy.mouse(WM_RBUTTONUP, 0, 0)
    native.drain()
    native.key_press(VK_LSHIFT, repeats=2)
    assert native.machine.aim_state is AimState.AIM_OFF
    assert [name for name, _state in native.backend.events] == [
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    return "pending transaction invalidation and native Shift replay remained safe"


def _scenario_ad(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.start(WeaponMode.PRIMARY)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert all(h.key_press(VK_LSHIFT, repeats=2))
    emitted = [name for name, _state in h.backend.events[before:]]
    assert emitted == [
        "MB1_UP",
        "MB2_DOWN",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert h.audio.events == ["ON", "OFF"] and not h.machine.enabled
    return "firing plus aim stopped, aimed off, and replayed Shift without R"


def _scenario_ae(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.start(WeaponMode.SECONDARY)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert all(h.key_press(VK_RSHIFT, repeats=2))
    emitted = [name for name, _state in h.backend.events[before:]]
    assert emitted == [
        "MB1_UP",
        "MB2_DOWN",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert "R_DOWN" not in emitted
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "SECONDARY aim-off completed before one Shift replay and no R"


def _scenario_af(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    h.key_press(VK_LSHIFT)
    assert [name for name, _state in h.backend.events] == [
        "MB2_DOWN",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert h.audio.events == [] and h.machine.aim_state is AimState.AIM_OFF
    return "idle aiming exited aim before sprint toggle with no macro audio"


def _scenario_ag(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT)
    shift_count = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
    for expected in (AimState.AIM_ON, AimState.AIM_OFF):
        assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
        assert h.machine.aim_state is expected
    assert sum(name == "SHIFT_DOWN" for name, _state in h.backend.events) == shift_count
    assert not any(name == "R_DOWN" for name, _state in h.backend.events)
    return "persistent sprint RMB ON/OFF emitted no additional Shift or R"


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
        auto_complete_aim_off=False,
    )
    aim_phase.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    aim_phase.policy.mouse(WM_RBUTTONUP, 0, 0)
    aim_phase.drain()
    aim_phase.key_press(VK_LSHIFT)
    aim_worker = next(
        worker
        for worker in aim_phase.workers
        if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
    )
    aim_worker.begin_aim_off()
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
        auto_complete_aim_off=False,
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
    return "foreground loss cleaned pending MB2, Shift, MB1, and R ownership"


def _scenario_ak(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    assert h.click() == (True, True)
    assert not h.machine.enabled and h.machine.worker is None
    assert h.audio.events == [] and h.backend.events == []
    assert any("event=AIM_REQUIRED_REJECTED" in item for item in h.reports)
    return "un-aimed PRIMARY activation was deterministically rejected"


def _scenario_al(config: AppConfig) -> str:
    h = SimulationHarness(config, trace=True)
    h.make_full(WeaponMode.SECONDARY)
    baseline = len(h.backend.events)
    assert h.click() == (True, True)
    assert not h.machine.enabled and h.machine.worker is None
    assert len(h.backend.events) == baseline and h.audio.events == []
    assert any("reason=AIM_REQUIRED" in item for item in h.reports)
    return "un-aimed SECONDARY activation was deterministically rejected"


def _scenario_am(config: AppConfig) -> str:
    h = SimulationHarness(config)
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_LBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_LBUTTONUP, 0, 0)
    h.drain()
    assert h.machine.aim_state is AimState.AIM_ON
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    assert h.audio.events == ["ON"]
    assert h.backend.events == [("MB1_DOWN", "RUNNING_PRIMARY")]
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    return "rapid physical aim-on then MB1 was accepted in FIFO order"


def _rmb_off_while_firing(config: AppConfig, mode: WeaponMode) -> None:
    h = SimulationHarness(config, trace=True)
    h.start(mode)
    before = len(h.backend.events)
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert [name for name, _state in h.backend.events[before:]] == [
        "MB1_UP",
        "MB2_DOWN",
        "MB2_UP",
    ]
    assert not h.machine.enabled and not h.machine.firing
    assert h.machine.aim_state is AimState.AIM_OFF
    assert h.audio.events == ["ON", "OFF"]
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    assert not any(name == "SHIFT_DOWN" for name, _state in h.backend.events[before:])


def _scenario_an(config: AppConfig) -> str:
    _rmb_off_while_firing(config, WeaponMode.PRIMARY)
    return "deferred RMB-off stopped PRIMARY before replay with no Shift or R"


def _scenario_ao(config: AppConfig) -> str:
    _rmb_off_while_firing(config, WeaponMode.SECONDARY)
    return "deferred RMB-off stopped SECONDARY before replay with no Shift or R"


def _scenario_ap(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.key_press(VK_LSHIFT)
    sprint_shifts = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
    h.aim_on()
    h.click()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert not h.machine.enabled and h.machine.aim_state is AimState.AIM_OFF
    assert sum(name == "SHIFT_DOWN" for name, _state in h.backend.events) == sprint_shifts
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "persistent sprint aim/fire/RMB-off emitted no extra Shift or R"


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
        "MB2_DOWN",
        "MB2_UP",
        "SHIFT_DOWN",
        "SHIFT_UP",
    ]
    assert not h.machine.enabled and h.machine.aim_state is AimState.AIM_OFF
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    return "persistent sprint aim/fire/Shift preserved stop-aim-off-toggle ordering"


def _scenario_ar(config: AppConfig) -> str:
    h = SimulationHarness(
        config,
        auto_complete_cancel=False,
        auto_complete_aim_off=False,
    )
    h.start(WeaponMode.PRIMARY)
    assert h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    aim_worker = next(
        worker
        for worker in h.workers
        if worker.request.kind is WorkerKind.AIM_OFF_TRANSACTION
    )
    aim_worker.begin_aim_off()
    h.drain()
    h.foreground_loss()
    assert not any(
        (
            h.backend.mouse_owned,
            h.backend.aim_owned,
            h.backend.shift_owned,
            h.backend.reload_owned,
        )
    )
    assert h.machine.aim_state is AimState.UNKNOWN and not h.machine.enabled
    return "foreground loss canceled deferred RMB-off and released all owned input"


def _scenario_as(config: AppConfig) -> str:
    h = SimulationHarness(config)
    h.start(WeaponMode.PRIMARY)
    macro = h.machine.worker
    assert isinstance(macro, FakeSessionWorker)
    macro.begin_macro_reload()
    h.drain()
    reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
    assert not h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
    assert not h.policy.mouse(WM_RBUTTONUP, 0, 0)
    h.drain()
    assert macro.finish_after_reload_requested and not h.machine.enabled
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == reloads
    macro.complete_macro_reload()
    h.drain()
    assert h.machine.magazine_state(WeaponMode.PRIMARY) is MagazineState.FULL
    assert not h.machine.enabled and h.machine.worker is None
    return "RMB-off preserved an already-started reload without duplicate R"


def _scenario_at(config: AppConfig) -> str:
    h = SimulationHarness(
        config,
        trace=True,
        foreground_active=False,
        foreground_certain=True,
    )
    h.foreground_loss()
    assert h.machine.aim_state is AimState.AIM_OFF
    assert not h.machine.target_has_been_active
    h.foreground_acquired()
    assert h.machine.target_has_been_active
    assert h.machine.aim_state is AimState.AIM_OFF
    h.aim_on()
    h.click()
    assert h.machine.aim_state is AimState.AIM_ON
    assert h.machine.enabled and h.machine.state is MacroState.RUNNING_PRIMARY
    events = [
        report.split("event=", 1)[1].split(" ", 1)[0]
        for report in h.reports
        if report.startswith("TRACE:")
    ]
    assert "AIM_PHYSICAL_ON" in events
    assert events[-3:] == ["MACRO_ENABLED", "FIRING_STARTED", "FINAL_SHOT_DOWN"]
    assert not any("event=AIM_REQUIRED_REJECTED" in item for item in h.reports)
    return "PowerShell baseline then target RMB/MB1 reached FIRING_STARTED"


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
