from __future__ import annotations

from pathlib import Path
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.models import (
    ControlEvent,
    ControlEventKind,
    MagazineState,
    MacroState,
    WeaponMode,
    WorkerKind,
    WorkerRequest,
    WorkerResult,
)
from helldivers_macro.state_machine import MacroStateMachine


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class Audio:
    def __init__(self) -> None:
        self.events = []

    def notify_on(self) -> None:
        self.events.append("ON")

    def notify_off(self) -> None:
        self.events.append("OFF")


class Worker:
    def __init__(self, token: int, request: WorkerRequest) -> None:
        self.token = token
        self.request = request
        self.canceled = 0
        self.alive = True

    def start(self) -> None:
        pass

    def cancel_and_release(self):
        self.canceled += 1
        return None

    def join(self, timeout=None) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class Factory:
    def __init__(self) -> None:
        self.workers = []

    def __call__(self, token, request):
        worker = Worker(token, request)
        self.workers.append(worker)
        return worker


class WeaponSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = Audio()
        self.factory = Factory()
        self.messages = []
        self.machine = MacroStateMachine(
            CONFIG,
            lambda: True,
            self.audio,
            self.factory,
            reporter=self.messages.append,
        )

    def send(self, kind) -> None:
        self.machine.handle(ControlEvent(kind))

    def complete(self, worker, result=WorkerResult(True)) -> None:
        worker.alive = False
        self.machine.handle(
            ControlEvent(
                ControlEventKind.WORKER_STOPPED,
                detail=result,
                worker_token=worker.token,
            )
        )

    def test_number_one_reloads_then_marks_primary_full(self) -> None:
        self.send(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.assertEqual(prep.request.kind, WorkerKind.PREPARATION)
        self.assertEqual(prep.request.mode, WeaponMode.PRIMARY)
        self.assertEqual(prep.request.switch_settle_ms, 500)
        self.assertEqual(self.machine.state, MacroState.PREPARING_PRIMARY)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )
        self.complete(prep)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.FULL
        )
        self.assertTrue(self.machine.armed)
        self.assertEqual(self.machine.state, MacroState.IDLE_PRIMARY)

    def test_number_two_reloads_then_marks_secondary_full(self) -> None:
        self.send(ControlEventKind.SELECT_SECONDARY)
        prep = self.factory.workers[-1]
        self.assertEqual(prep.request.mode, WeaponMode.SECONDARY)
        self.assertEqual(self.machine.state, MacroState.PREPARING_SECONDARY)
        self.complete(prep)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.FULL
        )
        self.assertEqual(self.machine.selected_mode, WeaponMode.SECONDARY)

    def test_selection_never_starts_firing_or_plays_on(self) -> None:
        self.send(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.complete(prep)
        self.assertEqual(len(self.factory.workers), 1)
        self.assertEqual(self.audio.events, [])

    def test_switch_running_stops_once_then_prepares_new_weapon(self) -> None:
        self.send(ControlEventKind.SELECT_PRIMARY)
        prep = self.factory.workers[-1]
        self.complete(prep)
        self.send(ControlEventKind.PHYSICAL_MB1_DOWN)
        self.send(ControlEventKind.PHYSICAL_MB1_UP)
        macro = self.factory.workers[-1]
        self.assertEqual(macro.request.kind, WorkerKind.MACRO)
        self.send(ControlEventKind.SELECT_SECONDARY)
        self.assertEqual(macro.canceled, 1)
        self.assertEqual(self.machine.state, MacroState.STOPPING)
        self.assertEqual(
            self.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )
        self.complete(macro, WorkerResult(False, canceled=True))
        next_prep = self.factory.workers[-1]
        self.assertEqual(next_prep.request.kind, WorkerKind.PREPARATION)
        self.assertEqual(next_prep.request.mode, WeaponMode.SECONDARY)
        self.assertEqual(self.audio.events, ["ON", "OFF"])
        self.complete(next_prep)
        self.assertEqual(self.audio.events, ["ON", "OFF"])
        self.assertEqual(self.machine.state, MacroState.IDLE_SECONDARY)


if __name__ == "__main__":
    unittest.main()
