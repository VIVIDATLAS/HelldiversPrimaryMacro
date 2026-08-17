from __future__ import annotations

import ctypes
import unittest

from helldivers_macro.input_backend import INPUT_MARKER, InputCoordination
from helldivers_macro.cadence_diagnostics import CadenceDiagnostics
from helldivers_macro.input_hooks import (
    HookPolicy,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    LLMHF_LOWER_IL_INJECTED,
    KBDLLHOOKSTRUCT,
    MSLLHOOKSTRUCT,
    WindowsHookThread,
    VK_1,
    VK_2,
    VK_LCONTROL,
    VK_LSHIFT,
    VK_P,
    VK_RSHIFT,
    SHIFT_SCAN_CODES,
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
from helldivers_macro.models import ControlEventKind, ShiftStroke
from helldivers_macro.windows_abi import (
    ULONG_PTR,
    marker_matches,
    structure_field_type,
)


class HookPolicyTests(unittest.TestCase):
    def make_policy(self, status=(True, True)):
        self.status = status
        self.events = []
        return HookPolicy(lambda: self.status, self.events.append)

    def kinds(self):
        return [event.kind for event in self.events]

    def test_pointer_sized_hook_layouts_are_valid(self) -> None:
        validate_hook_layouts()
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertIs(structure_field_type(KBDLLHOOKSTRUCT, "dwExtraInfo"), ULONG_PTR)
        self.assertIs(structure_field_type(MSLLHOOKSTRUCT, "dwExtraInfo"), ULONG_PTR)
        self.assertEqual(
            ctypes.sizeof(structure_field_type(MSLLHOOKSTRUCT, "dwExtraInfo")),
            ctypes.sizeof(ctypes.c_void_p),
        )
        self.assertEqual(ctypes.sizeof(KBDLLHOOKSTRUCT), 24)
        self.assertEqual(ctypes.alignment(KBDLLHOOKSTRUCT), 8)
        self.assertEqual(KBDLLHOOKSTRUCT.dwExtraInfo.offset, 16)
        self.assertEqual(ctypes.sizeof(MSLLHOOKSTRUCT), 32)
        self.assertEqual(ctypes.alignment(MSLLHOOKSTRUCT), 8)
        self.assertEqual(MSLLHOOKSTRUCT.dwExtraInfo.offset, 24)

    def test_canonical_marker_survives_mouse_and_keyboard_hook_dereference(self) -> None:
        mouse = MSLLHOOKSTRUCT()
        mouse.dwExtraInfo = INPUT_MARKER
        mouse_lparam = ctypes.cast(ctypes.pointer(mouse), ctypes.c_void_p).value
        keyboard = KBDLLHOOKSTRUCT()
        keyboard.dwExtraInfo = INPUT_MARKER
        keyboard_lparam = ctypes.cast(ctypes.pointer(keyboard), ctypes.c_void_p).value

        observed_mouse = WindowsHookThread._read_mouse_hook_data(mouse_lparam)
        observed_keyboard = WindowsHookThread._read_keyboard_hook_data(keyboard_lparam)

        self.assertEqual(INPUT_MARKER, 0x43524F31)
        self.assertEqual(int(observed_mouse.dwExtraInfo), INPUT_MARKER)
        self.assertEqual(int(observed_keyboard.dwExtraInfo), INPUT_MARKER)
        self.assertTrue(marker_matches(observed_mouse.dwExtraInfo, INPUT_MARKER))
        self.assertTrue(marker_matches(observed_keyboard.dwExtraInfo, INPUT_MARKER))
        self.assertFalse(marker_matches(0x12345678, INPUT_MARKER))

    def test_owned_generated_p_passes_without_controller_routing(self) -> None:
        diagnostics = CadenceDiagnostics(
            primary_shots_per_cycle=1,
            fire_device="keyboard",
            clock_ns=lambda: 1,
        )
        self.status = (True, True)
        self.events = []
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            cadence_diagnostics=diagnostics,
            fire_device="keyboard",
            fire_scan_code=0x19,
        )
        diagnostics.macro_worker_started("PRIMARY")
        for action, message in (("P_DOWN", WM_KEYDOWN), ("P_UP", WM_KEYUP)):
            with diagnostics.macro_action(action):
                expected = diagnostics.send_requested()
            self.assertFalse(
                policy.keyboard(
                    message,
                    VK_P,
                    LLKHF_INJECTED,
                    0x19,
                    INPUT_MARKER,
                )
            )
            diagnostics.send_completed(expected, 1, 0)
        data = diagnostics.snapshot()
        self.assertEqual(data["hook_observed"]["P_DOWN"], 1)
        self.assertEqual(data["hook_observed"]["P_UP"], 1)
        self.assertEqual(data["hook_passed"], data["hook_observed"])
        self.assertEqual(sum(data["hook_suppressed"].values()), 0)
        self.assertEqual(sum(data["hook_routed"].values()), 0)
        self.assertEqual(self.events, [])

    def test_physical_p_passes_without_macro_event(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_P, 0, 0x19, 0))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_P, 0, 0x19, 0))
        self.assertEqual(self.events, [])

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

    def test_owned_generated_mb1_is_observed_passed_and_never_routed(self) -> None:
        diagnostics = CadenceDiagnostics(clock_ns=lambda: 1)
        self.status = (True, True)
        self.events = []
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            cadence_diagnostics=diagnostics,
        )
        diagnostics.macro_worker_started("PRIMARY")
        for action, message in (
            ("MB1_DOWN", WM_LBUTTONDOWN),
            ("MB1_UP", WM_LBUTTONUP),
        ):
            with diagnostics.macro_action(action):
                expected = diagnostics.send_requested()
            diagnostics.send_completed(expected, 1, 0)
            self.assertFalse(
                policy.mouse(
                    message,
                    LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED,
                    INPUT_MARKER,
                )
            )
        diagnostics.macro_worker_stopped()

        data = diagnostics.snapshot()
        self.assertEqual(data["hook_observed"]["MB1_DOWN"], 1)
        self.assertEqual(data["hook_observed"]["MB1_UP"], 1)
        self.assertEqual(data["hook_passed"], data["hook_observed"])
        self.assertEqual(sum(data["hook_suppressed"].values()), 0)
        self.assertEqual(sum(data["hook_routed"].values()), 0)
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

    def test_right_click_passes_and_foreground_shift_pair_is_deferred(self) -> None:
        policy = self.make_policy()
        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_RBUTTONUP, 0, 0))
        scan = SHIFT_SCAN_CODES[VK_LSHIFT]
        self.assertTrue(policy.keyboard(WM_KEYDOWN, VK_LSHIFT, 0, scan))
        self.assertTrue(policy.keyboard(WM_KEYDOWN, VK_LSHIFT, 0, scan))
        self.assertTrue(policy.keyboard(WM_KEYUP, VK_LSHIFT, 0, scan))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.PHYSICAL_MB2_DOWN,
                ControlEventKind.PHYSICAL_MB2_UP,
                ControlEventKind.SHIFT_DOWN,
            ],
        )
        self.assertEqual(
            self.events[-1].detail,
            ShiftStroke(VK_LSHIFT, scan),
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

    def test_firing_right_button_pair_is_deferred_suppressed_and_repeat_safe(self) -> None:
        coordination = InputCoordination()
        coordination.macro_started()
        coordination.firing_started()
        policy = self.make_policy()
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            coordination=coordination,
        )

        self.assertTrue(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertTrue(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertTrue(policy.mouse(WM_RBUTTONUP, 0, 0))
        self.assertEqual(self.kinds(), [ControlEventKind.DEFERRED_AIM_OFF])

    def test_right_button_passes_again_after_firing_snapshot_clears(self) -> None:
        coordination = InputCoordination()
        coordination.macro_started()
        coordination.firing_started()
        policy = self.make_policy()
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            coordination=coordination,
        )
        coordination.firing_stopped()

        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertFalse(policy.mouse(WM_RBUTTONUP, 0, 0))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.PHYSICAL_MB2_DOWN,
                ControlEventKind.PHYSICAL_MB2_UP,
            ],
        )

    def test_tagged_right_button_is_ignored_without_disturbing_deferred_pair_latch(self) -> None:
        coordination = InputCoordination()
        coordination.macro_started()
        coordination.firing_started()
        policy = self.make_policy()
        policy = HookPolicy(
            lambda: self.status,
            self.events.append,
            coordination=coordination,
        )
        self.assertFalse(policy.mouse(WM_RBUTTONDOWN, 0, INPUT_MARKER))
        self.assertFalse(policy.mouse(WM_RBUTTONUP, 0, INPUT_MARKER))
        self.assertTrue(policy.mouse(WM_RBUTTONDOWN, 0, 0))
        self.assertTrue(policy.mouse(WM_RBUTTONUP, 0, 0))
        self.assertEqual(self.kinds(), [ControlEventKind.DEFERRED_AIM_OFF])

    def test_left_and_right_shift_autorepeat_emit_one_actionable_edge_each(self) -> None:
        policy = self.make_policy()
        for vk_code in (VK_LSHIFT, VK_RSHIFT):
            scan = SHIFT_SCAN_CODES[vk_code]
            self.assertTrue(policy.keyboard(WM_KEYDOWN, vk_code, 0, scan))
            self.assertTrue(policy.keyboard(WM_KEYDOWN, vk_code, 0, scan))
            self.assertTrue(policy.keyboard(WM_KEYUP, vk_code, 0, scan))
        self.assertEqual(
            self.kinds(),
            [
                ControlEventKind.SHIFT_DOWN,
                ControlEventKind.SHIFT_DOWN,
            ],
        )

    def test_shift_outside_target_passes_without_controller_event(self) -> None:
        policy = self.make_policy((False, True))
        scan = SHIFT_SCAN_CODES[VK_RSHIFT]
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_RSHIFT, 0, scan))
        self.assertFalse(policy.keyboard(WM_KEYDOWN, VK_RSHIFT, 0, scan))
        self.assertFalse(policy.keyboard(WM_KEYUP, VK_RSHIFT, 0, scan))
        self.assertEqual(self.events, [])

    def test_generated_or_marked_shift_is_ignored_without_recursion(self) -> None:
        policy = self.make_policy()
        scan = SHIFT_SCAN_CODES[VK_LSHIFT]
        for flags, marker in ((LLKHF_INJECTED, 0), (0, INPUT_MARKER)):
            self.assertFalse(
                policy.keyboard(WM_KEYDOWN, VK_LSHIFT, flags, scan, marker)
            )
            self.assertFalse(
                policy.keyboard(WM_KEYUP, VK_LSHIFT, flags, scan, marker)
            )
        self.assertEqual(self.events, [])

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
