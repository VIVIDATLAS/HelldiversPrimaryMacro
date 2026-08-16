from __future__ import annotations

from pathlib import Path
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_hooks import VK_1, VK_2
from helldivers_macro.models import MagazineState, MacroState, WeaponMode, WorkerKind, WorkerResult
from helldivers_macro.simulation import FakeSessionWorker, SimulationHarness


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class WeaponSelectionTests(unittest.TestCase):
    def test_default_primary_same_mode_press_is_internal_noop(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        before = (h.machine.state, h.machine.generation, len(h.workers), tuple(h.audio.events))
        h.key_press(VK_1, repeats=5)
        self.assertEqual(
            before,
            (h.machine.state, h.machine.generation, len(h.workers), tuple(h.audio.events)),
        )
        ignored = [r for r in h.reports if "SAME_MODE_SELECTION_IGNORED" in r]
        self.assertEqual(len(ignored), 1)

    def test_selecting_secondary_reloads_once_then_marks_full(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.key_press(VK_2, repeats=5)
        self.assertIs(h.machine.selected_mode, WeaponMode.SECONDARY)
        self.assertIs(h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.FULL)
        self.assertIs(h.machine.state, MacroState.IDLE_SECONDARY)
        self.assertEqual([w.request.kind for w in h.workers], [WorkerKind.PREPARATION])
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events), 1
        )
        self.assertEqual(h.audio.events, [])

    def test_same_mode_full_press_does_not_reload_again(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.make_full(WeaponMode.SECONDARY)
        before = (
            h.machine.generation,
            len(h.workers),
            len(h.backend.events),
            h.machine.magazine_state(WeaponMode.SECONDARY),
        )
        h.key_press(VK_2)
        self.assertEqual(
            before,
            (
                h.machine.generation,
                len(h.workers),
                len(h.backend.events),
                h.machine.magazine_state(WeaponMode.SECONDARY),
            ),
        )

    def test_same_mode_during_preparation_preserves_generation_and_worker(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        h.key_press(VK_2)
        worker = h.machine.worker
        generation = h.machine.generation
        h.key_press(VK_2, repeats=3)
        self.assertIs(h.machine.worker, worker)
        self.assertEqual(h.machine.generation, generation)
        self.assertEqual(len(h.workers), 1)

    def test_same_mode_while_firing_does_not_stop_or_emit_audio(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.SECONDARY)
        before = (
            h.machine.state,
            h.machine.generation,
            len(h.workers),
            tuple(h.audio.events),
            h.backend.mouse_owned,
        )
        h.key_press(VK_2, repeats=4)
        self.assertEqual(
            before,
            (
                h.machine.state,
                h.machine.generation,
                len(h.workers),
                tuple(h.audio.events),
                h.backend.mouse_owned,
            ),
        )
        self.assertIs(h.machine.state, MacroState.RUNNING_SECONDARY)

    def test_same_mode_while_stopping_is_noop(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.SECONDARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        h.click()
        self.assertIs(h.machine.state, MacroState.STOPPING)
        before = (h.machine.generation, len(h.workers), tuple(h.audio.events))
        h.key_press(VK_2, repeats=2)
        self.assertEqual(before, (h.machine.generation, len(h.workers), tuple(h.audio.events)))
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        self.assertIs(h.machine.state, MacroState.IDLE_SECONDARY)

    def test_other_weapon_selection_cancels_then_prepares_exactly_once(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_preparation=True,
            auto_complete_cancel=False,
        )
        h.start(WeaponMode.SECONDARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        h.key_press(VK_1)
        self.assertFalse(h.machine.enabled)
        self.assertIs(h.machine.state, MacroState.STOPPING)
        self.assertEqual(h.audio.events, ["ON"])
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        preparations = [
            w for w in h.workers if w.request.kind is WorkerKind.PREPARATION
        ]
        self.assertEqual(len([w for w in preparations if w.request.mode is WeaponMode.PRIMARY]), 1)
        self.assertIs(h.machine.selected_mode, WeaponMode.PRIMARY)
        self.assertIs(h.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.FULL)
        self.assertEqual(h.audio.events, ["ON", "OFF"])


if __name__ == "__main__":
    unittest.main()
