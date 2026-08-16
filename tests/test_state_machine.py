from __future__ import annotations

from pathlib import Path
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_backend import InputCoordination
from helldivers_macro.models import (
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
from helldivers_macro.state_machine import MacroStateMachine


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeAudio:
    def __init__(self) -> None:
        self.events: list[str] = []

    def notify_on(self) -> None:
        self.events.append("ON")

    def notify_off(self) -> None:
        self.events.append("OFF")


class FakeWorker:
    def __init__(self, token: int, request: WorkerRequest) -> None:
        self.token = token
        self.request = request
        self.started = False
        self.canceled = 0
        self.alive = False

    def start(self) -> None:
        self.started = True
        self.alive = True

    def cancel_and_release(self):
        self.canceled += 1
        return None

    def join(self, timeout=None) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class Factory:
    def __init__(self) -> None:
        self.workers: list[FakeWorker] = []

    def __call__(self, token: int, request: WorkerRequest) -> FakeWorker:
        worker = FakeWorker(token, request)
        self.workers.append(worker)
        return worker


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = True
        self.clock = FakeClock()
        self.audio = FakeAudio()
        self.factory = Factory()
        self.coordination = InputCoordination()
        self.messages: list[str] = []
        self.machine = MacroStateMachine(
            CONFIG,
            lambda: self.active,
            self.audio,
            self.factory,
            coordination=self.coordination,
            clock=self.clock,
            reporter=self.messages.append,
        )

    def event(self, kind: ControlEventKind, **kwargs) -> None:
        self.machine.handle(ControlEvent(kind, **kwargs))

    def complete(self, worker: FakeWorker, result: WorkerResult) -> None:
        worker.alive = False
        self.machine.handle(
            ControlEvent(
                ControlEventKind.WORKER_STOPPED,
                detail=result,
                worker_token=worker.token,
            )
        )

    def progress(self, worker: FakeWorker, update: WorkerProgress) -> None:
        self.machine.handle(
            ControlEvent(
                ControlEventKind.WORKER_PROGRESS,
                detail=update,
                worker_token=worker.token,
            )
        )

    def click(self) -> None:
        self.event(ControlEventKind.PHYSICAL_MB1_DOWN)
        self.event(ControlEventKind.PHYSICAL_MB1_UP)

    def make_primary_full(self) -> None:
        self.event(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.assertEqual(prep.request.kind, WorkerKind.PREPARATION)
        self.complete(prep, WorkerResult(True))
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.FULL
        )

    def start_primary(self) -> FakeWorker:
        self.make_primary_full()
        self.click()
        worker = self.factory.workers[-1]
        self.assertEqual(worker.request.kind, WorkerKind.MACRO)
        return worker

    def test_both_weapons_start_unknown(self) -> None:
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )

    def test_unknown_start_prepares_before_first_shot_and_on(self) -> None:
        self.click()
        prep = self.factory.workers[-1]
        self.assertEqual(prep.request.kind, WorkerKind.PREPARATION)
        self.assertEqual(prep.request.switch_settle_ms, 0)
        self.assertTrue(self.machine.pending_start)
        self.assertEqual(self.audio.events, [])
        self.complete(prep, WorkerResult(True))
        self.assertEqual(self.factory.workers[-1].request.kind, WorkerKind.MACRO)
        self.assertEqual(self.audio.events, ["ON"])

    def test_full_start_has_no_unnecessary_reload(self) -> None:
        self.make_primary_full()
        count = len(self.factory.workers)
        self.click()
        self.assertEqual(len(self.factory.workers), count + 1)
        self.assertEqual(self.factory.workers[-1].request.kind, WorkerKind.MACRO)

    def test_start_during_preparation_is_queued_once(self) -> None:
        self.event(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.click()
        self.click()
        self.assertTrue(self.machine.pending_start)
        self.assertEqual(len(self.factory.workers), 1)
        self.complete(prep, WorkerResult(True))
        self.assertEqual(len(self.factory.workers), 2)
        self.assertEqual(self.factory.workers[-1].request.kind, WorkerKind.MACRO)

    def test_mid_cycle_cancellation_is_unknown_and_off_once(self) -> None:
        macro = self.start_primary()
        self.progress(macro, WorkerProgress.SHOT_BEGAN)
        self.event(ControlEventKind.CTRL_DOWN)
        self.assertEqual(macro.canceled, 1)
        self.complete(macro, WorkerResult(False, canceled=True))
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.assertEqual(self.audio.events, ["ON", "OFF"])
        self.event(ControlEventKind.CTRL_DOWN)
        self.assertEqual(self.audio.events, ["ON", "OFF"])

    def test_completed_macro_reload_marks_full_until_next_shot(self) -> None:
        macro = self.start_primary()
        self.progress(macro, WorkerProgress.SHOT_BEGAN)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.progress(macro, WorkerProgress.RELOAD_COMPLETE)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.FULL
        )
        self.progress(macro, WorkerProgress.SHOT_BEGAN)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )

    def test_interrupted_preparation_remains_unknown(self) -> None:
        self.event(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.event(ControlEventKind.SHIFT_DOWN)
        self.complete(prep, WorkerResult(False, canceled=True))
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.assertEqual(self.audio.events, [])

    def test_late_success_from_canceled_preparation_cannot_mark_full(self) -> None:
        self.event(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.event(ControlEventKind.CTRL_DOWN)
        self.complete(prep, WorkerResult(True))
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )

    def test_focus_loss_during_preparation_remains_unknown_and_clears_pending(self) -> None:
        self.click()
        prep = self.factory.workers[-1]
        self.assertTrue(self.machine.pending_start)
        self.active = False
        self.event(ControlEventKind.FOREGROUND_LOST)
        self.complete(prep, WorkerResult(False, canceled=True))
        self.assertFalse(self.machine.pending_start)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.assertEqual(self.audio.events, [])

    def test_manual_ctrl_mb1_marks_selected_unknown(self) -> None:
        self.make_primary_full()
        self.event(ControlEventKind.MANUAL_BYPASS_DOWN)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )

    def test_rapid_bypass_waits_for_macro_cleanup_then_forwards(self) -> None:
        macro = self.start_primary()
        self.event(ControlEventKind.CTRL_DOWN)
        self.event(ControlEventKind.DEFERRED_BYPASS_DOWN)
        self.event(ControlEventKind.DEFERRED_BYPASS_UP)
        self.assertEqual(self.machine.state, MacroState.STOPPING)
        self.assertEqual(len(self.factory.workers), 2)
        self.complete(macro, WorkerResult(False, canceled=True))
        bypass = self.factory.workers[-1]
        self.assertEqual(bypass.request.kind, WorkerKind.BYPASS)
        self.assertTrue(bypass.request.bypass_release.is_set())
        self.assertEqual(self.audio.events, ["ON", "OFF"])

    def test_deferred_bypass_invalidates_late_macro_reload_progress(self) -> None:
        macro = self.start_primary()
        self.event(ControlEventKind.CTRL_DOWN)
        self.event(ControlEventKind.DEFERRED_BYPASS_DOWN)
        self.progress(macro, WorkerProgress.RELOAD_COMPLETE)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )

    def test_held_bypass_release_is_forwarded_to_worker(self) -> None:
        macro = self.start_primary()
        self.event(ControlEventKind.CTRL_DOWN)
        self.event(ControlEventKind.DEFERRED_BYPASS_DOWN)
        self.complete(macro, WorkerResult(False, canceled=True))
        bypass = self.factory.workers[-1]
        self.assertFalse(bypass.request.bypass_release.is_set())
        self.event(ControlEventKind.DEFERRED_BYPASS_UP)
        self.assertTrue(bypass.request.bypass_release.is_set())

    def test_focus_loss_discards_deferred_bypass(self) -> None:
        macro = self.start_primary()
        self.event(ControlEventKind.CTRL_DOWN)
        self.event(ControlEventKind.DEFERRED_BYPASS_DOWN)
        self.active = False
        self.event(ControlEventKind.FOREGROUND_LOST)
        self.complete(macro, WorkerResult(False, canceled=True))
        self.assertEqual(len(self.factory.workers), 2)
        self.assertNotEqual(self.factory.workers[-1].request.kind, WorkerKind.BYPASS)

    def test_shutdown_during_cycle_marks_unknown_and_off_once(self) -> None:
        macro = self.start_primary()
        self.progress(macro, WorkerProgress.RELOAD_COMPLETE)
        self.machine.shutdown()
        self.assertEqual(macro.canceled, 1)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.assertEqual(self.audio.events, ["ON", "OFF"])
        self.machine.shutdown()
        self.assertEqual(self.audio.events, ["ON", "OFF"])

    def test_no_overlapping_workers_while_stopping(self) -> None:
        macro = self.start_primary()
        self.event(ControlEventKind.PHYSICAL_MB1_DOWN)
        self.click()
        self.assertEqual(self.factory.workers[-1], macro)
        self.assertEqual(self.machine.state, MacroState.STOPPING)


if __name__ == "__main__":
    unittest.main()
