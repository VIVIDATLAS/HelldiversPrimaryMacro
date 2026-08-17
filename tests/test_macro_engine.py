from __future__ import annotations

from pathlib import Path
import threading
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_backend import InputApiError, InputCoordination
from helldivers_macro.macro_engine import (
    MacroEngine,
    MacroWorker,
    primary_cycle_steps,
    secondary_cycle_steps,
)
from helldivers_macro.models import (
    CycleStep,
    OutputAction,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerProgressUpdate,
    WorkerRequest,
    WorkerResult,
)


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class RecordingBackend:
    def __init__(
        self,
        fail_on: str | None = None,
        clock=None,
    ) -> None:
        self.events: list[str] = []
        self.timed_events: list[tuple[str, int]] = []
        self.mouse_owned = False
        self.aim_owned = False
        self.shift_owned = False
        self.shift_scan = 0
        self.reload_owned = False
        self.fail_on = fail_on
        self.release_calls = 0
        self._clock = clock or (lambda: 0.0)

    def _event(self, name: str) -> None:
        self.events.append(name)
        self.timed_events.append((name, round(self._clock() * 1000)))
        if name == self.fail_on:
            raise InputApiError(f"failure at {name}")

    def mouse_down(self) -> None:
        self.mouse_owned = True
        self._event("MB1_DOWN")

    def mouse_up(self) -> None:
        if not self.mouse_owned:
            return
        self._event("MB1_UP")
        self.mouse_owned = False

    def aim_down(self) -> None:
        self.aim_owned = True
        self._event("MB2_DOWN")

    def aim_up(self) -> None:
        if not self.aim_owned:
            return
        self._event("MB2_UP")
        self.aim_owned = False

    def shift_down(self, scan_code: int) -> None:
        self.shift_owned = True
        self.shift_scan = scan_code
        self._event("SHIFT_DOWN")

    def shift_up(self) -> None:
        if not self.shift_owned:
            return
        self._event("SHIFT_UP")
        self.shift_owned = False
        self.shift_scan = 0

    def reload_down(self) -> None:
        self.reload_owned = True
        self._event("R_DOWN")

    def reload_up(self) -> None:
        if not self.reload_owned:
            return
        self._event("R_UP")
        self.reload_owned = False

    def release_all(self) -> None:
        self.release_calls += 1
        if self.mouse_owned:
            self._event("RELEASE_MB1")
            self.mouse_owned = False
        if self.aim_owned:
            self._event("RELEASE_MB2")
            self.aim_owned = False
        if self.shift_owned:
            self._event("RELEASE_SHIFT")
            self.shift_owned = False
            self.shift_scan = 0
        if self.reload_owned:
            self._event("RELEASE_R")
            self.reload_owned = False

    def release_shift_inputs(self) -> None:
        if self.aim_owned:
            self._event("RELEASE_MB2")
            self.aim_owned = False
        if self.shift_owned:
            self._event("RELEASE_SHIFT")
            self.shift_owned = False
            self.shift_scan = 0


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, event: threading.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return event.is_set()


class TrackingLock:
    def __init__(self) -> None:
        self.held = False

    def __enter__(self):
        if self.held:
            raise AssertionError("unexpected recursive fake lock acquisition")
        self.held = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.held = False


class MacroSequenceTests(unittest.TestCase):
    def test_exact_primary_sequence_and_timing(self) -> None:
        inter_shot = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 35),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 50),
        ]
        final_shot = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 35),
            CycleStep(OutputAction.MB1_UP),
        ]
        steps = primary_cycle_steps(CONFIG)
        self.assertEqual(steps[: 44 * 4], inter_shot * 44)
        self.assertEqual(steps[44 * 4 : 44 * 4 + 3], final_shot)
        final_up_index = 44 * 4 + 2
        self.assertIs(steps[final_up_index].action, OutputAction.MB1_UP)
        self.assertIs(steps[final_up_index + 1].action, OutputAction.R_DOWN)
        self.assertEqual(
            steps[-4:],
            [
                CycleStep(OutputAction.R_DOWN),
                CycleStep(OutputAction.WAIT, 25),
                CycleStep(OutputAction.R_UP),
                CycleStep(OutputAction.WAIT, 2000),
            ],
        )
        self.assertEqual(sum(step.duration_ms for step in steps), 5800)
        self.assertEqual(
            sum(step == CycleStep(OutputAction.WAIT, 50) for step in steps),
            44,
        )

    def test_primary_runtime_emits_exact_clicks_and_reload_timing(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        cancel = threading.Event()
        progress: list[WorkerProgressUpdate] = []
        final_up_observed_after_r_down = []

        def report(update: WorkerProgressUpdate) -> None:
            progress.append(update)
            if update.phase is WorkerProgress.FINAL_SHOT_UP:
                final_up_observed_after_r_down.append(backend.events[-1] == "R_DOWN")
            if update.phase is WorkerProgress.RELOAD_COMPLETED:
                cancel.set()

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        result = engine.run_macro(
            WeaponMode.PRIMARY, cancel, threading.Event(), report
        )

        self.assertTrue(result.canceled)
        downs = [at for name, at in backend.timed_events if name == "MB1_DOWN"]
        ups = [at for name, at in backend.timed_events if name == "MB1_UP"]
        self.assertEqual(len(downs), 45)
        self.assertEqual(len(ups), 45)
        self.assertEqual([up - down for down, up in zip(downs, ups)], [35] * 45)
        self.assertEqual(
            [later - earlier for earlier, later in zip(downs, downs[1:])],
            [85] * 44,
        )
        self.assertEqual(
            [next_down - up for up, next_down in zip(ups, downs[1:])],
            [50] * 44,
        )
        self.assertTrue(all(down < up for down, up in zip(downs, ups)))
        self.assertEqual(
            [name for name, _at in backend.timed_events],
            ["MB1_DOWN", "MB1_UP"] * 45 + ["R_DOWN", "R_UP"],
        )
        self.assertEqual(
            backend.timed_events[-2:],
            [("R_DOWN", 3775), ("R_UP", 3800)],
        )
        self.assertEqual(downs[-1], 3740)
        self.assertEqual(ups[-1], 3775)
        self.assertEqual(backend.timed_events[-2][1] - ups[-1], 0)
        self.assertEqual(final_up_observed_after_r_down, [True])
        self.assertEqual(
            [round(update.occurred_at * 1000) for update in progress if update.phase is WorkerProgress.RELOAD_COMPLETED],
            [5800],
        )
        self.assertEqual(
            sum(update.phase is WorkerProgress.SHOT_BEGAN for update in progress),
            45,
        )

    def _run_primary_canceled_at(
        self, cancel_at_ms: int
    ) -> tuple[RecordingBackend, WorkerResult, list[WorkerProgressUpdate]]:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        cancel = threading.Event()
        progress: list[WorkerProgressUpdate] = []

        def wait(event: threading.Event, seconds: float) -> bool:
            result = fake_time.wait(event, seconds)
            if round(fake_time.now * 1000) >= cancel_at_ms:
                cancel.set()
            return result

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
        )
        result = engine.run_macro(
            WeaponMode.PRIMARY,
            cancel,
            threading.Event(),
            progress.append,
        )
        return backend, result, progress

    def test_cancel_during_primary_down_releases_mb1(self) -> None:
        backend, result, _progress = self._run_primary_canceled_at(5)
        self.assertTrue(result.canceled)
        self.assertEqual(
            backend.timed_events,
            [("MB1_DOWN", 0), ("RELEASE_MB1", 5)],
        )
        self.assertFalse(backend.mouse_owned)

    def test_cancel_during_primary_up_interval_stops_shots_and_reload(self) -> None:
        backend, result, _progress = self._run_primary_canceled_at(40)
        self.assertTrue(result.canceled)
        self.assertEqual(backend.events, ["MB1_DOWN", "MB1_UP"])
        self.assertNotIn("R_DOWN", backend.events)

    def test_cancel_after_primary_shot_44_prevents_shot_45_and_reload(self) -> None:
        backend, result, _progress = self._run_primary_canceled_at(3695)
        self.assertTrue(result.canceled)
        self.assertEqual(backend.events.count("MB1_DOWN"), 44)
        self.assertEqual(backend.events.count("MB1_UP"), 44)
        self.assertNotIn("R_DOWN", backend.events)

    def test_cancel_during_final_primary_down_releases_mb1_and_prevents_reload(self) -> None:
        backend, result, _progress = self._run_primary_canceled_at(3745)
        self.assertTrue(result.canceled)
        self.assertEqual(backend.events.count("MB1_DOWN"), 45)
        self.assertEqual(backend.events.count("MB1_UP"), 44)
        self.assertEqual(backend.events[-1], "RELEASE_MB1")
        self.assertNotIn("R_DOWN", backend.events)

    def test_cancel_during_primary_reload_down_releases_r(self) -> None:
        backend, result, _progress = self._run_primary_canceled_at(3780)
        self.assertTrue(result.canceled)
        self.assertEqual(backend.events[-2:], ["R_DOWN", "RELEASE_R"])
        self.assertFalse(backend.reload_owned)

    def test_cancel_during_primary_reload_wait_prevents_full_progress(self) -> None:
        backend, result, progress = self._run_primary_canceled_at(3805)
        self.assertTrue(result.canceled)
        self.assertEqual(backend.events[-2:], ["R_DOWN", "R_UP"])
        self.assertNotIn(
            WorkerProgress.RELOAD_COMPLETE,
            [update.phase for update in progress],
        )
        failed = [
            update
            for update in progress
            if update.phase is WorkerProgress.RELOAD_FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(round(failed[0].occurred_at * 1000), 3805)
        self.assertIn("canceled", failed[0].reason)

    def test_exact_secondary_sequence_and_timing(self) -> None:
        steps = secondary_cycle_steps(CONFIG)
        inter_shot = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 35),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 85),
        ]
        final_shot = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 35),
            CycleStep(OutputAction.MB1_UP),
        ]
        self.assertEqual(steps[: 12 * 4], inter_shot * 12)
        self.assertEqual(steps[12 * 4 : 12 * 4 + 3], final_shot)
        final_up_index = 12 * 4 + 2
        self.assertIs(steps[final_up_index + 1].action, OutputAction.R_DOWN)
        self.assertEqual(
            sum(step == CycleStep(OutputAction.WAIT, 85) for step in steps),
            12,
        )
        self.assertEqual(
            steps[-4:],
            [
                CycleStep(OutputAction.R_DOWN),
                CycleStep(OutputAction.WAIT, 25),
                CycleStep(OutputAction.R_UP),
                CycleStep(OutputAction.WAIT, 2000),
            ],
        )
        self.assertEqual(sum(step.duration_ms for step in steps), 3500)

    def test_secondary_runtime_has_zero_gap_before_reload(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        cancel = threading.Event()
        progress: list[WorkerProgressUpdate] = []
        final_up_observed_after_r_down: list[bool] = []

        def report(update: WorkerProgressUpdate) -> None:
            progress.append(update)
            if update.phase is WorkerProgress.FINAL_SHOT_UP:
                final_up_observed_after_r_down.append(
                    backend.events[-1] == "R_DOWN"
                )
            if update.phase is WorkerProgress.RELOAD_COMPLETED:
                cancel.set()

        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        ).run_macro(WeaponMode.SECONDARY, cancel, threading.Event(), report)

        self.assertTrue(result.canceled)
        downs = [at for name, at in backend.timed_events if name == "MB1_DOWN"]
        ups = [at for name, at in backend.timed_events if name == "MB1_UP"]
        self.assertEqual(len(downs), 13)
        self.assertEqual(len(ups), 13)
        self.assertEqual([up - down for down, up in zip(downs, ups)], [35] * 13)
        self.assertEqual(
            [later - earlier for earlier, later in zip(downs, downs[1:])],
            [120] * 12,
        )
        self.assertEqual(
            [next_down - up for up, next_down in zip(ups, downs[1:])],
            [85] * 12,
        )
        self.assertEqual(downs[-1], 1440)
        self.assertEqual(ups[-1], 1475)
        self.assertEqual(
            backend.timed_events[-2:],
            [("R_DOWN", 1475), ("R_UP", 1500)],
        )
        self.assertEqual(backend.timed_events[-2][1] - ups[-1], 0)
        self.assertEqual(final_up_observed_after_r_down, [True])
        completed = [
            round(update.occurred_at * 1000)
            for update in progress
            if update.phase is WorkerProgress.RELOAD_COMPLETED
        ]
        self.assertEqual(completed, [3500])

    def test_secondary_cancellation_releases_owned_mb1_and_r(self) -> None:
        for cancel_at in (1445, 1480, 1505):
            with self.subTest(cancel_at=cancel_at):
                fake_time = FakeTime()
                backend = RecordingBackend(clock=fake_time.clock)
                cancel = threading.Event()

                def wait(event: threading.Event, seconds: float) -> bool:
                    result = fake_time.wait(event, seconds)
                    if round(fake_time.now * 1000) >= cancel_at:
                        cancel.set()
                    return result

                result = MacroEngine(
                    CONFIG,
                    backend,
                    lambda: True,
                    clock=fake_time.clock,
                    wait=wait,
                ).run_macro(
                    WeaponMode.SECONDARY,
                    cancel,
                    threading.Event(),
                )
                self.assertTrue(result.canceled)
                self.assertFalse(backend.mouse_owned)
                self.assertFalse(backend.reload_owned)

    def test_shift_transaction_orders_aim_off_before_same_scan_replay(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        progress = []
        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        ).send_shift_transaction(
            0x36,
            True,
            threading.Event(),
            threading.Event(),
            progress.append,
        )
        self.assertTrue(result.success)
        self.assertEqual(
            backend.timed_events,
            [
                ("MB2_DOWN", 0),
                ("MB2_UP", 20),
                ("SHIFT_DOWN", 20),
                ("SHIFT_UP", 40),
            ],
        )
        self.assertEqual(backend.shift_scan, 0)
        self.assertEqual(
            [update.phase for update in progress],
            [
                WorkerProgress.AIM_OFF_SENT,
                WorkerProgress.SHIFT_REPLAY_DOWN,
                WorkerProgress.SHIFT_REPLAY_UP,
            ],
        )
        self.assertFalse(backend.aim_owned)
        self.assertFalse(backend.shift_owned)

    def test_shift_transaction_skips_mb2_when_aim_is_not_known_on(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        ).send_shift_transaction(
            0x2A,
            False,
            threading.Event(),
            threading.Event(),
        )
        self.assertTrue(result.success)
        self.assertEqual(
            backend.timed_events,
            [("SHIFT_DOWN", 0), ("SHIFT_UP", 20)],
        )

    def test_shift_transaction_waits_never_hold_output_lock(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        lock = TrackingLock()

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertFalse(lock.held)
            return fake_time.wait(event, seconds)

        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
            io_lock=lock,
        ).send_shift_transaction(
            0x2A,
            True,
            threading.Event(),
            threading.Event(),
        )
        self.assertTrue(result.success)
        self.assertFalse(lock.held)

    def test_deferred_aim_off_replays_one_owned_pair_without_shift_or_reload(self) -> None:
        fake_time = FakeTime()
        backend = RecordingBackend(clock=fake_time.clock)
        lock = TrackingLock()
        progress = []

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertFalse(lock.held)
            return fake_time.wait(event, seconds)

        result = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
            io_lock=lock,
        ).send_aim_off_transaction(
            threading.Event(),
            threading.Event(),
            progress.append,
        )
        self.assertTrue(result.success)
        self.assertEqual(
            backend.timed_events,
            [("MB2_DOWN", 0), ("MB2_UP", 20)],
        )
        self.assertEqual(
            [update.phase for update in progress],
            [
                WorkerProgress.AIM_OFF_REPLAY_DOWN,
                WorkerProgress.AIM_OFF_REPLAY_UP,
            ],
        )
        self.assertFalse(backend.aim_owned)
        self.assertFalse(lock.held)

    def test_macro_waits_never_hold_output_lock(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        lock = TrackingLock()
        cancel = threading.Event()

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertFalse(lock.held)
            result = fake_time.wait(event, seconds)
            cancel.set()
            return result

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
            io_lock=lock,
        )
        self.assertTrue(
            engine.run_macro(WeaponMode.PRIMARY, cancel, threading.Event()).canceled
        )
        self.assertFalse(lock.held)

    def test_focus_loss_releases_owned_mouse(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        checks = iter((True, False))
        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: next(checks, False),
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        result = engine.run_macro(
            WeaponMode.PRIMARY, threading.Event(), threading.Event()
        )
        self.assertIsNotNone(result.error)
        self.assertIn("MB1_DOWN", backend.events)
        self.assertIn("RELEASE_MB1", backend.events)
        self.assertFalse(backend.mouse_owned)

    def test_input_failure_runs_cleanup(self) -> None:
        backend = RecordingBackend(fail_on="MB1_DOWN")
        engine = MacroEngine(CONFIG, backend, lambda: True)
        result = engine.run_macro(
            WeaponMode.PRIMARY, threading.Event(), threading.Event()
        )
        self.assertIsInstance(result.error, InputApiError)
        self.assertIn("RELEASE_MB1", backend.events)
        self.assertEqual(backend.release_calls, 1)
        self.assertFalse(backend.mouse_owned)

    def test_cancelable_wait_uses_no_more_than_five_milliseconds(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        cancel = threading.Event()

        def wait(event: threading.Event, seconds: float) -> bool:
            result = fake_time.wait(event, seconds)
            cancel.set()
            return result

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
        )
        result = engine.run_macro(WeaponMode.PRIMARY, cancel, threading.Event())
        self.assertTrue(result.canceled)
        self.assertTrue(fake_time.waits)
        self.assertLessEqual(max(fake_time.waits), 0.005)
        self.assertFalse(backend.mouse_owned)

    def test_reload_preparation_succeeds_only_after_complete_wait(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        result = engine.prepare_reload(
            WeaponMode.PRIMARY, 500, threading.Event(), threading.Event()
        )
        self.assertTrue(result.success)
        self.assertEqual(backend.events[:2], ["R_DOWN", "R_UP"])
        self.assertAlmostEqual(fake_time.now, 2.525, places=3)
        self.assertFalse(backend.reload_owned)

    def test_preparation_waits_never_hold_output_lock(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        lock = TrackingLock()

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertFalse(lock.held)
            return fake_time.wait(event, seconds)

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
            io_lock=lock,
        )
        result = engine.prepare_reload(
            WeaponMode.PRIMARY, 500, threading.Event(), threading.Event()
        )
        self.assertTrue(result.success)
        self.assertFalse(lock.held)

    def test_nonblocking_preparation_cancel_does_not_acquire_output_lock(self) -> None:
        backend = RecordingBackend()
        engine = MacroEngine(CONFIG, backend, lambda: True, io_lock=TrackingLock())
        worker = MacroWorker(
            1,
            WorkerRequest(WorkerKind.PREPARATION, WeaponMode.PRIMARY),
            engine,
            threading.Event(),
            lambda _token, _result: None,
            lambda _token, _progress: None,
        )
        worker.cancel()
        self.assertTrue(worker.cancel_event.is_set())
        self.assertFalse(engine.io_lock.held)

    def test_retired_preparation_cleanup_cannot_release_macro_mouse(self) -> None:
        backend = RecordingBackend()
        backend.mouse_owned = True
        cancel = threading.Event()
        cancel.set()
        engine = MacroEngine(CONFIG, backend, lambda: True)
        result = engine.prepare_reload(
            WeaponMode.PRIMARY, 500, cancel, threading.Event()
        )
        self.assertTrue(result.canceled)
        self.assertTrue(backend.mouse_owned)
        self.assertNotIn("MB1_UP", backend.events)

    def test_fast_bypass_replays_after_generated_release(self) -> None:
        backend = RecordingBackend()
        backend.mouse_owned = True
        fake_time = FakeTime()
        released = threading.Event()
        released.set()
        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        result = engine.forward_bypass(
            released, 20, threading.Event(), threading.Event()
        )
        self.assertTrue(result.success)
        self.assertEqual(
            backend.events,
            ["RELEASE_MB1", "MB1_DOWN", "MB1_UP"],
        )
        self.assertGreaterEqual(fake_time.now, 0.020)

    def test_held_bypass_stays_down_until_physical_release(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        released = threading.Event()

        def wait(event: threading.Event, seconds: float) -> bool:
            self.assertTrue(backend.mouse_owned)
            fake_time.wait(event, seconds)
            released.set()
            return False

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=wait,
        )
        result = engine.forward_bypass(
            released, 20, threading.Event(), threading.Event()
        )
        self.assertTrue(result.success)
        self.assertEqual(backend.events, ["MB1_DOWN", "MB1_UP"])

    def test_focus_loss_discards_bypass_after_releasing_owned_input(self) -> None:
        backend = RecordingBackend()
        backend.mouse_owned = True
        engine = MacroEngine(CONFIG, backend, lambda: False)
        result = engine.forward_bypass(
            threading.Event(), 20, threading.Event(), threading.Event()
        )
        self.assertIsNotNone(result.error)
        self.assertEqual(backend.events, ["RELEASE_MB1"])
        self.assertFalse(backend.mouse_owned)

    def test_macro_reports_full_only_after_complete_reload_wait(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        cancel = threading.Event()
        progress = []

        def report(update: WorkerProgressUpdate) -> None:
            progress.append(update)
            if update.phase is WorkerProgress.RELOAD_COMPLETED:
                cancel.set()

        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        result = engine.run_macro(
            WeaponMode.PRIMARY,
            cancel,
            threading.Event(),
            report,
        )
        self.assertTrue(result.canceled)
        phases = [update.phase for update in progress]
        self.assertEqual(phases.count(WorkerProgress.SHOT_BEGAN), 45)
        self.assertEqual(phases[-1], WorkerProgress.RELOAD_COMPLETED)
        self.assertAlmostEqual(fake_time.now, 5.800, places=3)

    def test_shift_preserves_reload_in_a_later_macro_cycle(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        engine = MacroEngine(
            CONFIG,
            backend,
            lambda: True,
            clock=fake_time.clock,
            wait=fake_time.wait,
        )
        completed: list[WorkerResult] = []
        preserved: list[bool] = []
        reload_downs = 0
        worker: MacroWorker

        def progress(_token: int, update: WorkerProgressUpdate) -> None:
            nonlocal reload_downs
            if update.phase is WorkerProgress.RELOAD_KEY_DOWN:
                reload_downs += 1
                if reload_downs == 2:
                    preserved.append(worker.sprint_stop())

        worker = MacroWorker(
            1,
            WorkerRequest(WorkerKind.MACRO, WeaponMode.PRIMARY),
            engine,
            threading.Event(),
            lambda _token, result: completed.append(result),
            progress,
        )
        worker.start()
        worker.activate()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(preserved, [True])
        self.assertEqual(reload_downs, 2)
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].success)
        self.assertFalse(completed[0].canceled)
        self.assertEqual(backend.events.count("R_DOWN"), 2)
        self.assertEqual(backend.events.count("R_UP"), 2)

    def test_owned_worker_publishes_firing_snapshot_at_exact_phase_boundaries(self) -> None:
        backend = RecordingBackend()
        fake_time = FakeTime()
        coordination = InputCoordination()
        observations: list[tuple[WorkerProgress, bool]] = []
        worker: MacroWorker

        def progress(_token: int, update: WorkerProgressUpdate) -> None:
            if update.phase in (
                WorkerProgress.SHOT_BEGAN,
                WorkerProgress.RELOAD_KEY_DOWN,
            ):
                observations.append((update.phase, coordination.firing_active()))
            if update.phase is WorkerProgress.RELOAD_KEY_DOWN:
                self.assertTrue(worker.sprint_stop())

        worker = MacroWorker(
            1,
            WorkerRequest(WorkerKind.MACRO, WeaponMode.PRIMARY),
            MacroEngine(
                CONFIG,
                backend,
                lambda: True,
                clock=fake_time.clock,
                wait=fake_time.wait,
            ),
            threading.Event(),
            lambda _token, _result: None,
            progress,
            coordination,
        )
        coordination.macro_started()
        worker.start()
        worker.activate()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(observations)
        self.assertTrue(
            all(active for phase, active in observations if phase is WorkerProgress.SHOT_BEGAN)
        )
        self.assertEqual(
            [active for phase, active in observations if phase is WorkerProgress.RELOAD_KEY_DOWN],
            [False],
        )
        self.assertFalse(coordination.firing_active())


if __name__ == "__main__":
    unittest.main()
