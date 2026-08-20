from __future__ import annotations

import ast
from pathlib import Path
import unittest

from helldivers_macro.config import load_config
from helldivers_macro.input_backend import INPUT_MARKER
from helldivers_macro.input_hooks import (
    VK_2,
    VK_F23,
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
from helldivers_macro.models import (
    ControlEventKind,
    EventSource,
    MagazineState,
    MacroState,
    PreparationLifecycle,
    RmbHoldState,
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
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        for mode in WeaponMode:
            self.assertIs(h.machine.magazine_state(mode), MagazineState.UNKNOWN)
            self.assertIs(
                h.machine.preparation_lifecycle(mode),
                PreparationLifecycle.IDLE_UNKNOWN,
            )

    def test_initial_non_target_baseline_keeps_rmb_released_until_acquisition(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=True,
            foreground_active=False,
            foreground_certain=True,
        )
        self.assertFalse(h.machine.target_has_been_active)

        h.foreground_loss()

        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertFalse(h.machine.target_has_been_active)
        self.assertFalse(h.machine.enabled)
        self.assertTrue(any("event=FOREGROUND_BASELINE" in item for item in h.reports))

        h.foreground_acquired()

        self.assertTrue(h.machine.target_has_been_active)
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertTrue(any("event=FOREGROUND_ACQUIRED" in item for item in h.reports))

    def test_power_shell_startup_then_rmb_hold_and_mb1_starts_both_modes(self) -> None:
        for mode in WeaponMode:
            with self.subTest(mode=mode):
                h = SimulationHarness(
                    CONFIG,
                    trace=True,
                    foreground_active=False,
                    foreground_certain=True,
                )
                h.foreground_loss()
                h.foreground_acquired()
                if mode is WeaponMode.SECONDARY:
                    h.key_press(VK_2)
                h.aim_on()

                h.click()

                self.assertTrue(h.machine.enabled)
                self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
                self.assertIs(
                    h.machine.state,
                    MacroState.RUNNING_PRIMARY
                    if mode is WeaponMode.PRIMARY
                    else MacroState.RUNNING_SECONDARY,
                )
                self.assertEqual(h.audio.events, ["ON"])
                self.assertTrue(
                    any("event=MACRO_ENABLED" in item for item in h.reports)
                )
                self.assertTrue(
                    any("event=FIRING_STARTED" in item for item in h.reports)
                )

    def test_genuine_loss_requires_release_then_fresh_rmb_down(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.aim_on()
        h.foreground_loss()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED)

        h.foreground_acquired()
        # A repeat while the same physical pair remains held is ignored.
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED)
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)

        h.click()
        self.assertTrue(h.machine.enabled)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        shifts = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertFalse(h.machine.enabled)
        self.assertEqual(h.audio.events, ["ON", "OFF"])
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )
        self.assertEqual(
            sum(name == "SHIFT_DOWN" for name, _state in h.backend.events),
            shifts,
        )

    def test_hold_authority_requires_physical_untagged_rmb(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)

        h.send(
            ControlEventKind.PHYSICAL_MB2_DOWN,
            EventSource.INJECTED_OWNED,
        )
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)

        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, INPUT_MARKER))
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, INPUT_MARKER))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)

    def test_shift_from_held_requires_release_and_fresh_down(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.hold_rmb()
        before = len(h.backend.events)

        h.key_press(VK_RSHIFT, repeats=3)

        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertEqual(
            [name for name, _state in h.backend.events[before:]],
            ["MB2_UP", "SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)

    def test_unknown_primary_rejects_unmodified_mb1_without_rmb_hold(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        before = h.clock.now
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        self.assertFalse(h.machine.enabled)
        self.assertFalse(h.machine.preparing)
        self.assertIs(h.machine.state, MacroState.IDLE_PRIMARY)
        self.assertEqual(len(h.workers), 0)
        self.assertEqual(h.audio.events, [])
        self.assertEqual(h.clock.now, before)
        self.assertEqual(h.backend.events, [])

    def test_unknown_primary_starts_immediately_with_valid_rmb_hold(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_preparation=False)
        h.aim_on()
        before = h.clock.now
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        h.drain()
        self.assertTrue(h.machine.enabled)
        self.assertIs(h.machine.state, MacroState.RUNNING_PRIMARY)
        self.assertEqual(h.clock.now, before)
        self.assertEqual(h.audio.events, ["ON"])
        self.assertEqual(h.backend.events, [("MB1_DOWN", "RUNNING_PRIMARY")])

    def test_both_modes_reject_released_rmb_without_worker_input_or_audio(self) -> None:
        for mode in WeaponMode:
            with self.subTest(mode=mode):
                h = SimulationHarness(CONFIG, trace=True)
                if mode is WeaponMode.SECONDARY:
                    h.make_full(mode)
                worker_count = len(h.workers)
                input_count = len(h.backend.events)
                h.click()
                self.assertFalse(h.machine.enabled)
                self.assertEqual(len(h.workers), worker_count)
                self.assertEqual(len(h.backend.events), input_count)
                self.assertEqual(h.audio.events, [])
                self.assertTrue(
                    any(
                        record.startswith("START_REJECTED:")
                        and "reason=RMB_HOLD_REQUIRED" in record
                        for record in h.reports
                    )
                )
                self.assertTrue(
                    any("event=RMB_HOLD_REQUIRED_REJECTED" in record for record in h.reports)
                )

    def test_released_rmb_rejects_unmodified_mb1(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.foreground_loss()
        h.foreground.active = True
        h.foreground.certain = True
        h.policy.mouse(WM_LBUTTONUP, 0, 0)
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        h.click()
        self.assertFalse(h.machine.enabled)
        self.assertEqual(h.audio.events, [])
        self.assertFalse(any(name == "MB1_DOWN" for name, _ in h.backend.events))

    def test_rapid_physical_rmb_down_then_mb1_is_fifo_accepted(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertTrue(h.policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertTrue(h.policy.mouse(WM_LBUTTONUP, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
        self.assertTrue(h.machine.enabled)
        self.assertEqual(h.audio.events, ["ON"])
        self.assertEqual(h.backend.events, [("MB1_DOWN", "RUNNING_PRIMARY")])
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()

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
        h.aim_on()
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
        h.aim_on()
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
        h.aim_on()
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

    def test_physical_right_button_tracks_hold_without_macro_state_effect_while_idle(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        before = (
            h.machine.state,
            h.machine.generation,
            h.machine.magazine_state(WeaponMode.PRIMARY),
            h.machine.worker,
            tuple(h.audio.events),
            len(h.backend.events),
        )
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
        self.assertEqual(
            before,
            (
                h.machine.state,
                h.machine.generation,
                h.machine.magazine_state(WeaponMode.PRIMARY),
                h.machine.worker,
                tuple(h.audio.events),
                len(h.backend.events),
            ),
        )
        self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)

    def test_right_button_during_macro_reload_disables_but_preserves_reload(self) -> None:
        macro_reload = SimulationHarness(CONFIG, trace=False)
        macro_reload.start(WeaponMode.PRIMARY)
        macro = macro_reload.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        macro.begin_macro_reload()
        macro_reload.drain()
        reloads = sum(
            name == "R_DOWN" for name, _state in macro_reload.backend.events
        )
        self.assertFalse(macro_reload.policy.mouse(WM_RBUTTONUP, 0, 0))
        macro_reload.drain()
        self.assertFalse(macro_reload.machine.enabled)
        self.assertIs(macro_reload.machine.state, MacroState.RELOADING_PRIMARY)
        self.assertIs(macro_reload.machine.worker, macro)
        self.assertTrue(macro.finish_after_reload_requested)
        self.assertIs(macro_reload.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertEqual(macro_reload.audio.events, ["ON", "OFF"])
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in macro_reload.backend.events),
            reloads,
        )
        self.assertNotIn("SHIFT_DOWN", [name for name, _ in macro_reload.backend.events])
        macro.complete_macro_reload()
        macro_reload.drain()
        self.assertIs(
            macro_reload.machine.magazine_state(WeaponMode.PRIMARY),
            MagazineState.FULL,
        )

    def test_rmb_up_stops_each_weapon_without_replay_or_reload(self) -> None:
        for mode in WeaponMode:
            with self.subTest(mode=mode):
                h = SimulationHarness(CONFIG, trace=True)
                h.start(mode)
                before = len(h.backend.events)
                reloads = sum(name == "R_DOWN" for name, _ in h.backend.events)
                self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
                h.drain()
                emitted = [name for name, _ in h.backend.events[before:]]
                self.assertEqual(emitted, ["MB1_UP"])
                self.assertFalse(h.machine.enabled)
                self.assertFalse(h.machine.firing)
                self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
                self.assertEqual(h.audio.events, ["ON", "OFF"])
                self.assertNotIn("MB2_DOWN", emitted)
                self.assertNotIn("MB2_UP", emitted)
                self.assertEqual(
                    sum(name == "R_DOWN" for name, _ in h.backend.events), reloads
                )

    def test_rmb_release_requires_new_hold_and_new_mb1_to_restart(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.PRIMARY)
        h.release_rmb()
        h.clock.advance_ms(CONFIG.controls.toggle_debounce_ms)
        h.click()
        self.assertFalse(h.machine.enabled)
        h.hold_rmb()
        self.assertFalse(h.machine.enabled)
        h.clock.advance_ms(CONFIG.controls.toggle_debounce_ms)
        h.click()
        self.assertTrue(h.machine.enabled)

    def test_foreground_loss_invalidates_held_rmb_until_neutral_rearm(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.PRIMARY)
        h.foreground_loss()
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        h.foreground_acquired()
        h.click()
        self.assertFalse(h.machine.enabled)
        h.release_rmb()
        h.hold_rmb()
        h.clock.advance_ms(CONFIG.controls.toggle_debounce_ms)
        h.click()
        self.assertTrue(h.machine.enabled)

    def test_generated_or_foreign_injected_rmb_cannot_arm(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        for source in (EventSource.INJECTED_OWNED, EventSource.INJECTED_BYPASS):
            h.send(ControlEventKind.PHYSICAL_MB2_DOWN, source)
            h.drain()
            self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)

    def test_rmb_busy_snapshot_cannot_become_delayed_hold_authority(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_stratagem=False)
        h.key_press(VK_F23)
        worker = h.machine.worker
        self.assertIsInstance(worker, FakeSessionWorker)
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        captured = h.events.get_nowait()
        self.assertIs(captured.kind, ControlEventKind.PHYSICAL_MB2_DOWN)
        self.assertIs(captured.detail, True)
        worker.finish(WorkerResult(True))
        h.drain()
        h.machine.handle(captured)
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertFalse(h.machine.enabled)
        self.assertFalse(
            any(item.request.kind is WorkerKind.MACRO for item in h.workers)
        )

    def test_shift_orders_hold_release_before_replay(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
        macro_generation = h.machine.generation
        before = len(h.backend.events)

        results = h.key_press(VK_LSHIFT, repeats=3)

        self.assertTrue(all(results))
        self.assertEqual(
            [name for name, _state in h.backend.events[before:]],
            ["MB2_UP", "SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertTrue(
            all(
                source is EventSource.INJECTED_OWNED
                for _name, source in h.backend.tagged_events[-3:]
            )
        )
        self.assertEqual(h.backend.shift_scans, [0x2A])
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertEqual(h.machine.generation, macro_generation)
        self.assertEqual(h.audio.events, [])
        self.assertEqual(
            sum("event=RMB_HOLD_RELEASE_REQUESTED" in item for item in h.reports), 1
        )
        self.assertEqual(
            sum("event=RMB_HOLD_RELEASED_FOR_SHIFT" in item for item in h.reports),
            1,
        )
        for event_name in (
            "SHIFT_DEFERRED",
            "SHIFT_TRANSACTION_STARTED",
            "SHIFT_REPLAY_DOWN",
            "SHIFT_REPLAY_UP",
            "SHIFT_TRANSACTION_COMPLETED",
        ):
            self.assertEqual(
                sum(f"event={event_name}" in item for item in h.reports),
                1,
            )
        names = [name for name, _state in h.backend.events]
        self.assertLess(names.index("MB2_UP"), names.index("SHIFT_DOWN"))
        self.assertNotIn("R_DOWN", names)

    def test_shift_while_rmb_released_never_generates_mb2(self) -> None:
        off = SimulationHarness(CONFIG, trace=True)
        off.key_press(VK_LSHIFT, repeats=2)
        self.assertIs(off.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertEqual(
            [name for name, _state in off.backend.events],
            ["SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertEqual(off.audio.events, [])

        unknown = SimulationHarness(CONFIG, trace=True)
        unknown.foreground_loss()
        unknown.foreground.active = True
        unknown.foreground.certain = True
        unknown.key_press(VK_RSHIFT, repeats=2)
        self.assertIs(unknown.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertEqual(
            [name for name, _state in unknown.backend.events],
            ["SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertEqual(unknown.audio.events, [])
        self.assertFalse(
            any(
                name in ("MB2_DOWN", "MB2_UP", "R_DOWN")
                for name, _state in off.backend.events + unknown.backend.events
            )
        )

    def test_fresh_rmb_invalidates_obsolete_pending_shift_worker(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=True,
            auto_complete_cancel=False,
            auto_complete_shift=False,
        )
        h.hold_rmb()
        h.key_press(VK_LSHIFT)
        worker = next(
            item
            for item in h.workers
            if item.request.kind is WorkerKind.SHIFT_TRANSACTION
        )
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )

        h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
        h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        h.drain()

        self.assertTrue(worker.cancel_requested)
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
        self.assertEqual(h.backend.events, [])
        worker.finish(WorkerResult(True))
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)

    def test_sent_stale_shift_release_cannot_arm_a_newer_physical_hold(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_cancel=False,
            auto_complete_shift=False,
        )
        h.hold_rmb()
        h.key_press(VK_LSHIFT)
        worker = next(
            item for item in h.workers
            if item.request.kind is WorkerKind.SHIFT_TRANSACTION
        )
        worker.release_aim_hold()
        h.drain()
        h.policy.mouse(WM_RBUTTONUP, 0, 0)
        h.drain()
        h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        h.drain()
        self.assertTrue(worker.cancel_requested)
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertEqual(
            [name for name, _ in h.backend.events].count("MB2_UP"), 1
        )

    def test_foreground_loss_invalidates_pending_shift_without_mb2_down(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=True,
            auto_complete_cancel=False,
            auto_complete_shift=False,
        )
        h.hold_rmb()
        h.key_press(VK_LSHIFT)
        worker = next(
            item
            for item in h.workers
            if item.request.kind is WorkerKind.SHIFT_TRANSACTION
        )
        h.foreground_loss()

        self.assertFalse(h.backend.aim_owned)
        self.assertFalse(h.backend.shift_owned)
        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertTrue(worker.cancel_requested)
        self.assertNotIn("MB2_DOWN", [name for name, _ in h.backend.events])

    def test_failed_shift_delivery_leaves_hold_rearm_required(self) -> None:
        h = SimulationHarness(CONFIG, trace=True, auto_complete_shift=False)
        h.hold_rmb()
        h.key_press(VK_LSHIFT)
        worker = next(
            item
            for item in h.workers
            if item.request.kind is WorkerKind.SHIFT_TRANSACTION
        )

        worker.finish(WorkerResult(False, error=RuntimeError("fake MB2 failure")))
        h.drain()

        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertEqual(h.backend.events, [])
        self.assertEqual(
            sum("event=SHIFT_TRANSACTION_FAILED" in item for item in h.reports),
            1,
        )
        self.assertEqual(
            sum(
                item.request.kind is WorkerKind.SHIFT_TRANSACTION
                for item in h.workers
            ),
            1,
        )

    def test_shift_hold_release_never_generates_mb2_down(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.hold_rmb()

        h.key_press(VK_LSHIFT, repeats=3)

        self.assertIs(
            h.machine.rmb_hold_state, RmbHoldState.HELD_REARM_REQUIRED
        )
        self.assertEqual(
            [name for name, _state in h.backend.events],
            ["MB2_UP", "SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertNotIn("MB2_DOWN", [name for name, _ in h.backend.events])
        self.assertEqual(h.audio.events, [])

    def test_persistent_sprint_rmb_cycles_never_invert_or_generate_shift(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.key_press(VK_LSHIFT)
        shift_events = sum(
            name == "SHIFT_DOWN" for name, _state in h.backend.events
        )
        for _ in range(2):
            self.assertFalse(h.policy.mouse(WM_RBUTTONDOWN, 0, 0))
            h.drain()
            self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)
            self.assertFalse(h.policy.mouse(WM_RBUTTONUP, 0, 0))
            h.drain()
            self.assertIs(h.machine.rmb_hold_state, RmbHoldState.RELEASED)
        self.assertEqual(
            sum(name == "SHIFT_DOWN" for name, _state in h.backend.events),
            shift_events,
        )
        self.assertNotIn("R_DOWN", [name for name, _state in h.backend.events])

        h.key_press(VK_RSHIFT)
        self.assertEqual(
            sum(name == "SHIFT_DOWN" for name, _state in h.backend.events),
            shift_events + 1,
        )

    def test_weapon_selection_preserves_current_valid_hold(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.policy.mouse(WM_RBUTTONDOWN, 0, 0)
        h.drain()
        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)

        h.key_press(VK_2)

        self.assertIs(h.machine.rmb_hold_state, RmbHoldState.HELD_VALID)

    def test_left_and_right_shift_stop_firing_once_without_reload(self) -> None:
        for mode, vk_code in (
            (WeaponMode.PRIMARY, VK_LSHIFT),
            (WeaponMode.SECONDARY, VK_RSHIFT),
        ):
            with self.subTest(mode=mode):
                h = SimulationHarness(CONFIG, trace=True)
                h.start(mode)
                before = len(h.backend.events)
                reloads_before = sum(
                    name == "R_DOWN" for name, _state in h.backend.events
                )
                results = h.key_press(vk_code, repeats=3)
                self.assertTrue(all(results))
                self.assertEqual(
                    [name for name, _state in h.backend.events[before:]],
                    [
                        "MB1_UP",
                        "MB2_UP",
                        "SHIFT_DOWN",
                        "SHIFT_UP",
                    ],
                )
                self.assertFalse(h.machine.enabled)
                self.assertFalse(h.machine.firing)
                self.assertIsNone(h.machine.worker)
                self.assertIs(h.machine.magazine_state(mode), MagazineState.UNKNOWN)
                self.assertEqual(h.audio.events, ["ON", "OFF"])
                self.assertEqual(
                    sum(name == "R_DOWN" for name, _state in h.backend.events),
                    reloads_before,
                )
                self.assertFalse(
                    any(
                        worker.request.kind is WorkerKind.RELOAD_ONLY
                        for worker in h.workers
                    )
                )
                disabled = [
                    report
                    for report in h.reports
                    if "event=MACRO_DISABLED" in report
                    and "reason=SHIFT_SPRINT" in report
                ]
                self.assertEqual(len(disabled), 1)

    def test_shift_during_macro_reload_preserves_it_without_duplicate(self) -> None:
        h = SimulationHarness(CONFIG, trace=True)
        h.start(WeaponMode.PRIMARY)
        macro = h.machine.worker
        self.assertIsInstance(macro, FakeSessionWorker)
        macro.begin_macro_reload()
        h.drain()
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        h.key_press(VK_LSHIFT, repeats=2)
        self.assertFalse(h.machine.enabled)
        self.assertIs(h.machine.worker, macro)
        self.assertTrue(macro.finish_after_reload_requested)
        self.assertEqual(h.audio.events, ["ON", "OFF"])
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )
        macro.complete_macro_reload()
        h.drain()
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )
        self.assertIsNone(h.machine.worker)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.PRIMARY),
            MagazineState.FULL,
        )
        self.assertFalse(h.machine.enabled)

    def test_shift_disabled_idle_and_preparation_are_state_neutral(self) -> None:
        idle = SimulationHarness(CONFIG, trace=False)
        before = (
            idle.machine.state,
            idle.machine.generation,
            idle.machine.magazine_state(WeaponMode.PRIMARY),
            tuple(idle.audio.events),
        )
        idle.key_press(VK_LSHIFT, repeats=3)
        self.assertEqual(
            before,
            (
                idle.machine.state,
                idle.machine.generation,
                idle.machine.magazine_state(WeaponMode.PRIMARY),
                tuple(idle.audio.events),
            ),
        )
        self.assertEqual(
            [name for name, _state in idle.backend.events],
            ["SHIFT_DOWN", "SHIFT_UP"],
        )
        self.assertNotIn("R_DOWN", [name for name, _state in idle.backend.events])

        prep = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_preparation=False,
        )
        prep.key_press(VK_2)
        worker = prep.machine.worker
        generation = prep.machine.generation
        reloads = sum(name == "R_DOWN" for name, _state in prep.backend.events)
        prep.key_press(VK_RSHIFT, repeats=3)
        self.assertIs(prep.machine.worker, worker)
        self.assertEqual(prep.machine.generation, generation)
        self.assertEqual(prep.audio.events, [])
        self.assertIsInstance(worker, FakeSessionWorker)
        self.assertFalse(worker.cancel_requested)
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in prep.backend.events),
            reloads,
        )
        self.assertEqual(
            [name for name, _state in prep.backend.events].count("SHIFT_DOWN"),
            1,
        )

    def test_stale_firing_completion_cannot_create_reload_after_shift(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.PRIMARY)
        old = h.machine.worker
        self.assertIsInstance(old, FakeSessionWorker)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        h.key_press(VK_LSHIFT)
        self.assertIs(h.machine.state, MacroState.STOPPING)
        self.assertFalse(h.backend.mouse_owned)
        self.assertEqual(h.audio.events, ["ON", "OFF"])
        old.finish(WorkerResult(False, canceled=True))
        h.drain()
        self.assertIsNone(h.machine.worker)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.PRIMARY),
            MagazineState.UNKNOWN,
        )
        worker_count = len(h.workers)
        h.put_worker_event(
            ControlEventKind.WORKER_STOPPED,
            old,
            WorkerResult(False, canceled=True),
        )
        h.drain()
        self.assertEqual(len(h.workers), worker_count)
        self.assertFalse(h.machine.enabled)
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )

    def test_shift_while_macro_is_stopping_never_creates_reload(self) -> None:
        h = SimulationHarness(CONFIG, trace=False, auto_complete_cancel=False)
        h.start(WeaponMode.PRIMARY)
        h.key_press(VK_LSHIFT)
        self.assertIs(h.machine.state, MacroState.STOPPING)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        shifts = sum(name == "SHIFT_DOWN" for name, _state in h.backend.events)
        h.key_press(VK_RSHIFT)
        self.assertIs(h.machine.state, MacroState.STOPPING)
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )
        self.assertEqual(
            sum(name == "SHIFT_DOWN" for name, _state in h.backend.events),
            shifts + 1,
        )
        self.assertEqual(h.audio.events, ["ON", "OFF"])

    def test_foreground_loss_during_shift_transaction_releases_all_owned_input(self) -> None:
        h = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_cancel=False,
            auto_complete_shift=False,
        )
        h.start(WeaponMode.PRIMARY)
        h.key_press(VK_LSHIFT)
        shift_worker = next(
            worker
            for worker in h.workers
            if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
        )
        shift_worker.release_aim_hold()
        shift_worker.begin_shift_replay()
        self.assertTrue(h.backend.shift_owned)
        h.foreground_loss()
        self.assertFalse(h.backend.mouse_owned)
        self.assertFalse(h.backend.aim_owned)
        self.assertFalse(h.backend.shift_owned)
        self.assertFalse(h.backend.reload_owned)
        self.assertFalse(h.machine.enabled)
        self.assertIs(
            h.machine.magazine_state(WeaponMode.PRIMARY),
            MagazineState.UNKNOWN,
        )

        regained = SimulationHarness(
            CONFIG,
            trace=False,
            auto_complete_cancel=False,
            auto_complete_shift=False,
        )
        regained.foreground_loss()
        regained.foreground.active = True
        regained.foreground.certain = True
        regained.key_press(VK_RSHIFT)
        replay = next(
            worker
            for worker in regained.workers
            if worker.request.kind is WorkerKind.SHIFT_TRANSACTION
        )
        replay.begin_shift_replay()
        self.assertTrue(regained.backend.shift_owned)
        regained.foreground_loss()
        self.assertFalse(regained.backend.shift_owned)
        self.assertIs(regained.machine.rmb_hold_state, RmbHoldState.RELEASED)

    def test_weapon_selection_after_shift_still_prepares_selected_weapon(self) -> None:
        h = SimulationHarness(CONFIG, trace=False)
        h.start(WeaponMode.PRIMARY)
        reloads = sum(name == "R_DOWN" for name, _state in h.backend.events)
        h.key_press(VK_LSHIFT)
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads,
        )
        h.key_press(VK_2)
        self.assertIs(h.machine.selected_mode, WeaponMode.SECONDARY)
        self.assertEqual(
            sum(name == "R_DOWN" for name, _state in h.backend.events),
            reloads + 1,
        )
        self.assertFalse(h.machine.enabled)

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
        h.aim_on()
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
        h.aim_on()
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
