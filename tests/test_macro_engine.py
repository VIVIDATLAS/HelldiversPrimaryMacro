from __future__ import annotations

from pathlib import Path
import threading
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_backend import InputApiError
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
    WorkerRequest,
)


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class RecordingBackend:
    def __init__(self, fail_on: str | None = None) -> None:
        self.events: list[str] = []
        self.mouse_owned = False
        self.reload_owned = False
        self.fail_on = fail_on
        self.release_calls = 0

    def _event(self, name: str) -> None:
        self.events.append(name)
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
            self.events.append("RELEASE_MB1")
            self.mouse_owned = False
        if self.reload_owned:
            self.events.append("RELEASE_R")
            self.reload_owned = False


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
        expected = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 900),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 20),
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 900),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 20),
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 900),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 300),
            CycleStep(OutputAction.R_DOWN),
            CycleStep(OutputAction.WAIT, 25),
            CycleStep(OutputAction.R_UP),
            CycleStep(OutputAction.WAIT, 2600),
        ]
        steps = primary_cycle_steps(CONFIG)
        self.assertEqual(steps, expected)
        self.assertEqual(sum(step.duration_ms for step in steps), 5665)
        self.assertEqual(
            sum(step == CycleStep(OutputAction.WAIT, 20) for step in steps), 2
        )

    def test_exact_secondary_sequence_and_timing(self) -> None:
        steps = secondary_cycle_steps(CONFIG)
        shot = [
            CycleStep(OutputAction.MB1_DOWN),
            CycleStep(OutputAction.WAIT, 35),
            CycleStep(OutputAction.MB1_UP),
            CycleStep(OutputAction.WAIT, 145),
        ]
        self.assertEqual(steps[: 13 * 4], shot * 13)
        self.assertEqual(
            steps[-4:],
            [
                CycleStep(OutputAction.R_DOWN),
                CycleStep(OutputAction.WAIT, 25),
                CycleStep(OutputAction.R_UP),
                CycleStep(OutputAction.WAIT, 2000),
            ],
        )
        self.assertEqual(sum(step.duration_ms for step in steps), 4365)

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
        self.assertAlmostEqual(fake_time.now, 3.125, places=3)
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

        def report(update: WorkerProgress) -> None:
            progress.append(update)
            if update is WorkerProgress.RELOAD_COMPLETE:
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
        self.assertEqual(progress.count(WorkerProgress.SHOT_BEGAN), 3)
        self.assertEqual(progress[-1], WorkerProgress.RELOAD_COMPLETE)
        self.assertAlmostEqual(fake_time.now, 5.665, places=3)


if __name__ == "__main__":
    unittest.main()
