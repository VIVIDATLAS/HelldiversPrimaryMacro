from __future__ import annotations

import ast
from pathlib import Path
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_hooks import (
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
from helldivers_macro.models import (
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    PreparationLifecycle,
    WeaponMode,
    WorkerKind,
    WorkerProgress,
    WorkerResult,
)
from helldivers_macro.simulation import FakeSessionWorker, SimulationHarness


ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(ROOT / "config.toml")


class StateMachineTests(unittest.TestCase):
    def test_both_weapons_start_unknown(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        for mode in WeaponMode:
            self.assertIs(h.machine.magazine_state(mode), MagazineState.UNKNOWN)
            self.assertIs(
                h.machine.preparation_lifecycle(mode),
                PreparationLifecycle.IDLE_UNKNOWN,
            )

    def test_unknown_primary_starts_immediately_without_reload_or_clock_advance(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        before = h.clock.now
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        self.assertTrue(h.machine.enabled)
        self.assertFalse(h.machine.preparing)
        self.assertIs(h.machine.state, MacroState.RUNNING_PRIMARY)
        self.assertEqual(len(h.workers), 1)
        self.assertIs(h.workers[0].request.kind, WorkerKind.MACRO)
        self.assertEqual(h.audio.events, ["ON"])
        self.assertEqual(h.clock.now, before)
        self.assertEqual(h.backend.events, [("MB1_DOWN", "RUNNING_PRIMARY")])

    def test_background_preparation_success_marks_full_without_starting(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        h.key_press(VK_2)
        prep = h.machine.worker
        self.assertIsInstance(prep, FakeSessionWorker)
        prep.finish_preparation()
        h.drain()
        self.assertIs(h.machine.state, MacroState.IDLE_SECONDARY)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.FULL
        )
        self.assertEqual(h.audio.events, [])
        self.assertEqual([w.request.kind for w in h.workers].count(WorkerKind.MACRO), 0)

    def test_failed_background_preparation_returns_idle_unknown(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        h.key_press(VK_2)
        prep = h.machine.worker
        self.assertIsInstance(prep, FakeSessionWorker)
        prep.finish_preparation(WorkerResult(False, error=RuntimeError("fake R failure")))
        h.drain()
        self.assertIs(h.machine.state, MacroState.IDLE_SECONDARY)
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.machine.preparing)
        self.assertIs(h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN)
        self.assertIs(
            h.machine.preparation_lifecycle(WeaponMode.SECONDARY),
            PreparationLifecycle.IDLE_UNKNOWN,
        )
        self.assertTrue(any("fake R failure" in message for message in h.reports))

    def test_full_weapon_starts_without_duplicate_reload(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.make_full(WeaponMode.SECONDARY)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.machine.handle(h.events.get_nowait())
        self.assertIs(h.machine.state, MacroState.RUNNING_SECONDARY)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.FULL
        )
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events), reloads
        )
        h.drain()
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )

    def test_secondary_start_preempts_switch_settle_in_trace_order(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=True,
            auto_complete_preparation=False,
            auto_complete_cancel=False,
        )
        h.key_press(VK_2)
        preparation = h.machine.worker
        self.assertIsInstance(preparation, FakeSessionWorker)
        before = h.clock.now
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        self.assertEqual(h.clock.now, before)
        self.assertTrue(preparation.cancel_requested)
        self.assertIs(h.machine.state, MacroState.RUNNING_SECONDARY)
        self.assertEqual(h.audio.events, ["ON"])
        self.assertEqual(h.backend.events, [("MB1_DOWN", "RUNNING_SECONDARY")])
        trace_events = [
            record.split("event=", 1)[1].split(" ", 1)[0]
            for record in h.reports
            if record.startswith("TRACE:")
        ]
        self.assertEqual(
            trace_events[-3:],
            ["MACRO_ENABLED", "PREPARATION_CANCELED", "FIRING_STARTED"],
        )
        trace_records = [
            record for record in h.reports if record.startswith("TRACE:")
        ]
        self.assertTrue(all("elapsed_ms=" in record for record in trace_records))
        enabled = next(record for record in trace_records if "event=MACRO_ENABLED" in record)
        firing = next(record for record in trace_records if "event=FIRING_STARTED" in record)
        elapsed = lambda record: float(record.split("elapsed_ms=", 1)[1].split(" ", 1)[0])
        self.assertLessEqual(elapsed(firing) - elapsed(enabled), 50.0)

    def test_stale_preparation_completion_cannot_publish_full_or_duplicate_start(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_preparation=False,
            auto_complete_cancel=False,
        )
        h.key_press(VK_2)
        preparation = h.machine.worker
        self.assertIsInstance(preparation, FakeSessionWorker)
        h.click()
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        self.assertIs(macro.request.kind, WorkerKind.MACRO)
        preparation.finish(WorkerResult(True))
        h.drain()
        self.assertIs(h.machine.worker, macro)
        self.assertIs(h.machine.state, MacroState.RUNNING_SECONDARY)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )
        self.assertEqual(h.audio.events, ["ON"])
        self.assertEqual(
            [worker.request.kind for worker in h.workers].count(WorkerKind.MACRO),
            1,
        )

    def test_macro_shot_marks_unknown_and_completed_reload_marks_full(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.SECONDARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )
        h.put_worker_event(
            ControlEventKind.WORKER_PROGRESS,
            macro,
            WorkerProgress.RELOAD_COMPLETE,
        )
        h.drain()
        self.assertIs(h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.FULL)
        h.put_worker_event(
            ControlEventKind.WORKER_PROGRESS,
            macro,
            WorkerProgress.SHOT_BEGAN,
        )
        h.drain()
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )

    def test_stop_releases_generated_input_and_off_exactly_once(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.PRIMARY)
        self.assertTrue(h.backend.mouse_owned)
        h.click()
        self.assertFalse(h.backend.mouse_owned)
        self.assertFalse(h.machine.enabled)
        self.assertEqual(h.audio.events, ["ON", "OFF"])
        h.send(ControlEventKind.CTRL_DOWN)
        h.drain()
        self.assertEqual(h.audio.events, ["ON", "OFF"])

    def test_ctrl_stale_completion_cannot_restart_secondary(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.SECONDARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        h.drain()
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.backend.mouse_owned)
        h.policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
        h.drain()
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        self.assertIs(h.machine.selected_mode, WeaponMode.SECONDARY)
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.machine.firing)
        self.assertFalse(h.backend.mouse_owned)
        self.assertFalse(any(w.request.kind is WorkerKind.PREPARATION for w in h.workers[2:]))

    def test_stale_primary_completion_cannot_restart_macro(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.PRIMARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        macro_count = len(
            [worker for worker in h.workers if worker.request.kind is WorkerKind.MACRO]
        )
        h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        h.drain()
        self.assertFalse(h.machine.enabled)
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.machine.firing)
        self.assertIsNone(h.machine.worker)
        self.assertEqual(
            len(
                [
                    worker
                    for worker in h.workers
                    if worker.request.kind is WorkerKind.MACRO
                ]
            ),
            macro_count,
        )
        self.assertEqual(h.audio.events, ["ON", "OFF"])

    def test_ctrl_when_disabled_does_not_mutate_controller(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        before = (
            h.machine.state,
            h.machine.generation,
            tuple(h.audio.events),
        )
        h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        h.policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
        h.drain()
        self.assertEqual(
            before,
            (
                h.machine.state,
                h.machine.generation,
                tuple(h.audio.events),
            ),
        )

    def test_manual_ctrl_mb1_marks_selected_magazine_unknown(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.make_full(WeaponMode.SECONDARY)
        h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertFalse(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertFalse(h.policy.mouse(WM_LBUTTONUP, 0, 0))
        h.drain()
        self.assertIs(
            h.machine.magazine_state(WeaponMode.SECONDARY), MagazineState.UNKNOWN
        )
        self.assertFalse(h.machine.enabled)

    def test_mb1_up_method_is_cleanup_only_by_static_ast(self) -> None:
        tree = ast.parse((ROOT / "helldivers_macro" / "state_machine.py").read_text())
        method = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_mb1_up"
        )
        calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
        self.assertEqual(calls, [])
        assigned = {
            node.targets[0].attr
            for node in method.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Attribute)
        }
        self.assertEqual(assigned, {"_physical_mb1_down", "_neutral_rearm_required"})

    def test_mb1_up_after_state_change_cannot_reject_or_start(self) -> None:
        h = SimulationHarness(CONFIG, trace=True, auto_complete_preparation=False)
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        generation = h.machine.generation
        self.assertTrue(h.policy.mouse(WM_LBUTTONUP, 0, 0))
        h.drain()
        self.assertEqual(h.machine.generation, generation)
        self.assertFalse(any("MB1-up" in report for report in h.reports))

    def test_right_button_has_no_controller_route_or_state_effect(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.PRIMARY)
        before = (h.machine.state, h.machine.generation, tuple(h.audio.events))
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()
        self.assertEqual(
            before, (h.machine.state, h.machine.generation, tuple(h.audio.events))
        )
        self.assertFalse(any("RIGHT" in name for name in ControlEventKind.__members__))

    def test_shift_down_repeat_up_have_no_controller_route(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.SECONDARY)
        before = (h.machine.state, h.machine.generation, tuple(h.audio.events))
        for message in (WM_KEYDOWN, WM_KEYDOWN, WM_KEYUP):
            self.assertFalse(h.policy.keyboard(message, VK_LSHIFT, 0))
        h.drain()
        self.assertEqual(
            before, (h.machine.state, h.machine.generation, tuple(h.audio.events))
        )
        self.assertFalse(any("SHIFT" in name for name in ControlEventKind.__members__))

    def test_focus_loss_during_generated_down_is_atomic_and_no_restart(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.PRIMARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        h.foreground_loss()
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.backend.mouse_owned)
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        h.foreground.active = True
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.machine.firing)
        self.assertEqual(h.audio.events, ["ON", "OFF"])

    def test_focus_loss_between_suppressed_pair_preserves_matching_up(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_preparation=False,
            auto_complete_cancel=False,
        )
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        h.foreground_loss()
        h.foreground.active = True
        self.assertTrue(h.policy.mouse(WM_LBUTTONUP, 0, 0))
        h.drain()
        self.assertFalse(h.machine.physical_mb1_down)
        self.assertFalse(h.machine.enabled)

    def test_foreground_regain_while_held_does_not_replay_or_restart(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        h.foreground_loss()
        h.foreground.active = True
        before_workers = len(h.workers)
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertTrue(h.policy.mouse(WM_LBUTTONUP, 0, 0))
        h.drain()
        self.assertFalse(h.machine.enabled)
        self.assertEqual(len(h.workers), before_workers)

    def test_obsolete_preparation_cannot_overwrite_current_weapon(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_preparation=False,
            auto_complete_cancel=False,
        )
        h.key_press(VK_2)
        old = h.machine.worker
        self.assertIsInstance(old, FakeSessionWorker)
        h.key_press(0x31)
        old.finish(WorkerResult(True))
        h.drain()
        self.assertIs(h.machine.selected_mode, WeaponMode.PRIMARY)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.PRIMARY), MagazineState.UNKNOWN
        )

    def test_trace_reason_is_local_after_independent_activation(self) -> None:
        h = SimulationHarness(CONFIG, trace=True, auto_complete_cancel=False)
        h.start(WeaponMode.SECONDARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        h.policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        h.drain()
        macro.finish(WorkerResult(False, canceled=True))
        h.drain()
        h.policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
        h.clock.advance_ms(CONFIG.controls.toggle_debounce_ms)
        h.click()
        firing = [r for r in h.reports if "event=FIRING_STARTED" in r]
        self.assertGreaterEqual(len(firing), 2)
        self.assertNotIn("CTRL_DOWN", firing[-1])
        self.assertIn("source=", firing[-1])
        self.assertIn("previous=[", firing[-1])
        self.assertIn("result=[", firing[-1])

    def test_trace_sequence_is_strictly_monotonic(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.start(WeaponMode.SECONDARY)
        h.click()
        records = [r for r in h.reports if r.startswith("TRACE:")]
        sequences = [int(r.split("seq=", 1)[1].split(" ", 1)[0]) for r in records]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))


if __name__ == "__main__":
    unittest.main()
