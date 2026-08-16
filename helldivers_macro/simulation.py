from __future__ import annotations

from dataclasses import dataclass, replace
import queue
import threading
from typing import Callable

from .config import AppConfig
from .input_backend import InputCoordination
from .input_hooks import (
    HookPolicy,
    VK_1,
    VK_2,
    VK_LCONTROL,
    VK_LSHIFT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
)
from .macro_engine import MacroEngine
from .models import (
    ControlEvent,
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
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
    def __init__(self) -> None:
        self.active = True
        self.certain = True

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
    def __init__(self, state_probe: Callable[[], str]) -> None:
        self._state_probe = state_probe
        self.events: list[tuple[str, str]] = []
        self.mouse_owned = False
        self.reload_owned = False

    def _record(self, name: str) -> None:
        self.events.append((name, self._state_probe()))

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
        self.reload_up()


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
        self.alive = False

    def start(self) -> None:
        self.started = True
        self.alive = True

    def activate(self) -> None:
        if self.activated:
            return
        self.activated = True
        if self.request.kind is WorkerKind.PREPARATION:
            if self.harness.auto_complete_preparation:
                self.finish_preparation()
        elif self.request.kind is WorkerKind.MACRO:
            self.harness.backend.mouse_down()
            self.harness.put_worker_event(
                ControlEventKind.WORKER_PROGRESS,
                self,
                WorkerProgress.SHOT_BEGAN,
            )
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
            )
        self.finish(result)

    def finish_bypass_release(self) -> None:
        if self.request.kind is not WorkerKind.BYPASS or self.completed:
            return
        if self.request.bypass_release.is_set():
            self.harness.clock.advance_ms(self.request.bypass_click_ms)
            self.harness.backend.mouse_up()
            self.finish(WorkerResult(True))

    def finish(self, result: WorkerResult) -> None:
        if self.completed:
            return
        self.completed = True
        self.alive = False
        self.harness.put_worker_event(ControlEventKind.WORKER_STOPPED, self, result)

    def cancel_and_release(self) -> BaseException | None:
        self.cancel_requested = True
        try:
            self.harness.backend.release_all()
        except BaseException as exc:
            return exc
        if self.harness.auto_complete_cancel:
            self.finish(WorkerResult(False, canceled=True))
        return None

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
    ) -> None:
        self.config = replace(
            config,
            diagnostics=replace(config.diagnostics, state_tracing=trace),
        )
        self.auto_complete_preparation = auto_complete_preparation
        self.auto_complete_cancel = auto_complete_cancel
        self.foreground = FakeForeground()
        self.clock = FakeClock()
        self.audio = FakeAudioNotifier()
        self.coordination = InputCoordination()
        self.events: queue.Queue[ControlEvent] = queue.Queue()
        self.hook_events: list[ControlEventKind] = []
        self.reports: list[str] = []
        self.workers: list[FakeSessionWorker] = []
        holder: list[MacroStateMachine] = []
        self.backend = FakeGeneratedInput(
            lambda: holder[0].state.name if holder else "UNINITIALIZED"
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
        results = [self.policy.keyboard(WM_KEYDOWN, vk, 0)]
        results.extend(
            self.policy.keyboard(WM_KEYDOWN, vk, 0) for _ in range(repeats)
        )
        results.append(self.policy.keyboard(WM_KEYUP, vk, 0))
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
        self.clock.advance_ms(self.config.controls.toggle_debounce_ms)
        self.click()
        expected = (
            MacroState.RUNNING_PRIMARY
            if mode is WeaponMode.PRIMARY
            else MacroState.RUNNING_SECONDARY
        )
        if self.machine.state is not expected:
            raise AssertionError(f"expected {expected.name}, got {self.machine.state.name}")

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
    return "PRIMARY prepared, fired, and stopped"


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
    assert h.click() == (True, True)
    assert h.machine.enabled and h.machine.pending_start and h.machine.preparing
    worker = h.machine.worker
    assert isinstance(worker, FakeSessionWorker)
    worker.finish_preparation()
    h.drain()
    assert h.machine.state is MacroState.RUNNING_PRIMARY
    assert h.audio.events == ["ON"]
    return "UNKNOWN start queued once and fired after preparation"


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
    assert h.machine.generation == baseline_generation + 1
    assert sum(name == "R_DOWN" for name, _state in h.backend.events) == baseline_reloads
    assert not any("RIGHT" in report for report in h.reports)


def _scenario_j(config: AppConfig) -> str:
    _aim_chord(config, WeaponMode.PRIMARY)
    _aim_chord(config, WeaponMode.SECONDARY)
    return "MB2 aim chord passed through while both modes toggled normally"


def _scenario_k(config: AppConfig) -> str:
    h = SimulationHarness(config, auto_complete_preparation=False)
    h.key_press(VK_2)
    before = (
        h.machine.state,
        h.machine.generation,
        h.machine.pending_start,
        tuple(h.audio.events),
    )
    for message in (WM_KEYDOWN, WM_KEYDOWN, WM_KEYUP):
        assert not h.policy.keyboard(message, VK_LSHIFT, 0)
    h.drain()
    assert before == (
        h.machine.state,
        h.machine.generation,
        h.machine.pending_start,
        tuple(h.audio.events),
    )
    h.click()
    waiting = (
        h.machine.state,
        h.machine.generation,
        h.machine.pending_start,
        tuple(h.audio.events),
    )
    h.key_press(VK_LSHIFT, repeats=2)
    assert waiting == (
        h.machine.state,
        h.machine.generation,
        h.machine.pending_start,
        tuple(h.audio.events),
    )
    for mode in (WeaponMode.PRIMARY, WeaponMode.SECONDARY):
        firing = SimulationHarness(config)
        firing.start(mode)
        snapshot = (
            firing.machine.state,
            firing.machine.generation,
            tuple(firing.audio.events),
        )
        firing.key_press(VK_LSHIFT, repeats=2)
        assert snapshot == (
            firing.machine.state,
            firing.machine.generation,
            tuple(firing.audio.events),
        )
    return "Shift was isolated during preparation, waiting, and firing"


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
    assert not h.machine.pending_start and not h.backend.mouse_owned
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
            "Preparation failed for PRIMARY: SHIFT_DOWN",
            "Preparation failed for SECONDARY: SHIFT_DOWN",
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
