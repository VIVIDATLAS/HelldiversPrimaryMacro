from __future__ import annotations

import unittest

from helldivers_macro.input_backend import INPUT_MARKER, InputCoordination
from helldivers_macro.input_hooks import (
    HookPolicy,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    VK_1,
    VK_2,
    VK_LCONTROL,
    VK_LSHIFT,
    VK_RSHIFT,
    VK_NUMPAD1,
    VK_NUMPAD2,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    validate_hook_layouts,
)
from helldivers_macro.models import ControlEventKind


class HookPolicyTests(unittest.TestCase):
    def make_policy(self, status=(True, True)):
        self.status = status
        self.events = []
        return HookPolicy(lambda: self.status, self.events.append)

    def kinds(self):
        return [event.kind for event in self.events]

    def test_pointer_sized_hook_layouts_are_valid(self) -> None:
        validate_hook_layouts()

    def test_number_row_selection_passes_and_ignores_repeat(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_1, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_1, 0))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_1, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_1, 0))
        self.assertEqual(
            self.kinds(),
            [ControlEventKind.SELECT_PRIMARY, ControlEventKind.SELECT_PRIMARY],
        )

    def test_number_two_selects_secondary_and_is_never_suppressed(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_2, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_2, 0))
        self.assertEqual(self.kinds(), [ControlEventKind.SELECT_SECONDARY])

    def test_key_up_resets_each_selection_edge_latch(self) -> None:
        policy = self.make_policy()
        for vk_code in (VK_1, VK_2):
            policy.keyboard(WM_KEYDOWN, vk_code, 0)
            policy.keyboard(WM_KEYUP, vk_code, 0)
            policy.keyboard(WM_KEYDOWN, vk_code, 0)
            policy.keyboard(WM_KEYUP, vk_code, 0)
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.SELECT_PRIMARY,
                ControlEventKind.SELECT_PRIMARY,
                ControlEventKind.SELECT_SECONDARY,
                ControlEventKind.SELECT_SECONDARY,
            ],
        )

    def test_number_row_and_numpad_keys_are_distinct(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_NUMPAD1, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_NUMPAD2, 0))
        self.assertEqual(self.events, [])

    def test_injected_keyboard_does_not_select_or_change_ctrl(self) -> None:
        policy = self.make_policy()
        policy.keyboard(WM_KEYDOWN, VK_1, LLKHF_INJECTED)
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, LLKHF_INJECTED)
        self.assertEqual(self.events, [])
        self.assertFalse(policy.ctrl_down)

    def test_ctrl_already_held_when_hook_starts_prevents_suppression(self) -> None:
        policy = self.make_policy()
        policy.initialize_physical_key(VK_LCONTROL, True)
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.MANUAL_BYPASS_DOWN,
                ControlEventKind.PHYSICAL_MB1_UP,
            ],
        )

    def test_unmodified_target_mb1_pair_is_suppressed(self) -> None:
        policy = self.make_policy()
        self.assertTrue(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.status = (False, True)
        self.assertTrue(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.PHYSICAL_MB1_DOWN,
                ControlEventKind.PHYSICAL_MB1_UP,
            ],
        )

    def test_passed_down_has_passed_up_even_if_ctrl_is_released(self) -> None:
        policy = self.make_policy()
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertNotIn(ControlEventKind.PHYSICAL_MB1_DOWN, self.kinds())

    def test_ctrl_mb1_passes_and_never_toggles(self) -> None:
        policy = self.make_policy()
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.CTRL_DOWN,
                ControlEventKind.MANUAL_BYPASS_DOWN,
                ControlEventKind.PHYSICAL_MB1_UP,
            ],
        )

    def test_ctrl_state_is_visible_before_controller_queue_processing(self) -> None:
        observations = []
        policy = None

        def sink(event):
            self.events.append(event)
            if event.kind is ControlEventKind.CTRL_DOWN:
                observations.append(policy.ctrl_down)

        self.status = (True, True)
        self.events = []
        policy = HookPolicy(lambda: self.status, sink)
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertEqual(observations, [True])
        # No queued event has been processed by a controller, yet mouse sees Ctrl.
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))

    def test_rapid_ctrl_mb1_latches_deferred_bypass(self) -> None:
        coordination = InputCoordination()
        coordination.macro_started()
        self.status = (True, True)
        self.events = []
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            lambda: True,
            coordination,
        )
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertTrue(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        coordination.cleanup_completed()
        self.assertTrue(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.CTRL_DOWN,
                ControlEventKind.DEFERRED_BYPASS_DOWN,
                ControlEventKind.DEFERRED_BYPASS_UP,
            ],
        )

    def test_pair_decision_never_mixes_deferred_and_pass_through(self) -> None:
        coordination = InputCoordination()
        coordination.macro_started()
        policy = self.make_policy()
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            lambda: False,
            coordination,
        )
        policy.keyboard(WM_KEYDOWN, VK_LCONTROL, 0)
        self.assertTrue(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        coordination.cleanup_completed()
        policy.keyboard(WM_KEYUP, VK_LCONTROL, 0)
        self.assertTrue(policy.mouse(WM_LBUTTONUP, 0, 0))

    def test_generated_and_injected_mouse_events_pass_without_control(self) -> None:
        policy = self.make_policy()
        for flags, marker in ((LLMHF_INJECTED, 0), (0, INPUT_MARKER)):
            for down, up in (
                (WM_LBUTTONDOWN, WM_LBUTTONUP),
                (WM_RBUTTONDOWN, WM_RBUTTONUP),
            ):
                self.assertFalse(policy.mouse(down, flags, marker))
                self.assertFalse(policy.mouse(up, flags, marker))
        self.assertEqual(self.events, [])

    def test_outside_target_mb1_always_passes(self) -> None:
        policy = self.make_policy((False, True))
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.status = (True, True)
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [ControlEventKind.FOREGROUND_LOST, ControlEventKind.PHYSICAL_MB1_UP],
        )

    def test_stale_foreground_passes_and_disarms(self) -> None:
        policy = self.make_policy((False, False))
        self.assertFalse(policy.mouse(WM_LBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_LBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.FOREGROUND_UNCERTAIN,
                ControlEventKind.PHYSICAL_MB1_UP,
            ],
        )

    def test_right_click_and_shift_edges_pass_and_normalize(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_RBUTTONUP, 0, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_LSHIFT, 0))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_LSHIFT, 0))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_LSHIFT, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.PHYSICAL_MB2_DOWN,
                ControlEventKind.PHYSICAL_MB2_UP,
                ControlEventKind.SHIFT_DOWN,
                ControlEventKind.SHIFT_UP,
            ],
        )

    def test_right_button_repeat_emits_one_physical_down_edge(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_RBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.PHYSICAL_MB2_DOWN,
                ControlEventKind.PHYSICAL_MB2_UP,
            ],
        )

    def test_left_and_right_shift_autorepeat_emit_one_actionable_edge_each(self) -> None:
        policy = self.make_policy()
        for vk_code in (VK_LSHIFT, VK_RSHIFT):
            self.assertFalse(policy.keyboard(WM_KEYDOWN, vk_code, 0))
            self.assertFalse(policy.keyboard(WM_KEYDOWN, vk_code, 0))
            self.assertFalse(policy.keyboard(WM_KEYUP, vk_code, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.SHIFT_DOWN,
                ControlEventKind.SHIFT_UP,
                ControlEventKind.SHIFT_DOWN,
                ControlEventKind.SHIFT_UP,
            ],
        )

    def test_diagnostics_ignore_unrelated_input(self) -> None:
        self.status = (True, True)
        self.events = []
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            diagnostics_enabled=True,
        )
        policy.mouse(WM_RBUTTONDOWN, 0, 0)
        policy.keyboard(WM_KEYDOWN, VK_LSHIFT, 0)
        self.assertNotIn(ControlEventKind.DIAGNOSTIC, self.kinds())


if __name__ == "__main__":
    unittest.main()
