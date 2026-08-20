from __future__ import annotations

import ctypes
from pathlib import Path
import unittest

from helldivers_macro.config import OutputConfig
from helldivers_macro.input_backend import (
    INPUT,
    INPUT_MARKER,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEINPUT,
    InputApiError,
    SendInputBackend,
    validate_ctypes_layouts,
)
from helldivers_macro.windows_abi import ULONG_PTR, structure_field_type


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeUser32:
    def __init__(self, results=None, last_error: int = 0) -> None:
        self.results = list(results or [])
        self.last_error = last_error
        self.inputs: list[INPUT] = []
        self.MapVirtualKeyW = Function(lambda vk, map_type: 0x13)

        def send(count, pointer, size):
            self.inputs.append(
                INPUT.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(INPUT)))
            )
            result = self.results.pop(0) if self.results else 1
            if result != 1:
                ctypes.set_last_error(self.last_error)
            return result

        self.SendInput = Function(send)


class InputBackendTests(unittest.TestCase):
    def test_pointer_sized_layouts_are_valid(self) -> None:
        validate_ctypes_layouts()

    def test_x64_marker_is_pointer_sized_and_round_trips_without_sign_change(self) -> None:
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertIs(structure_field_type(MOUSEINPUT, "dwExtraInfo"), ULONG_PTR)
        self.assertIs(structure_field_type(KEYBDINPUT, "dwExtraInfo"), ULONG_PTR)
        self.assertEqual(
            ctypes.sizeof(structure_field_type(MOUSEINPUT, "dwExtraInfo")),
            ctypes.sizeof(ctypes.c_void_p),
        )
        self.assertEqual(ctypes.sizeof(MOUSEINPUT), 32)
        self.assertEqual(ctypes.alignment(MOUSEINPUT), 8)
        self.assertEqual(MOUSEINPUT.dwExtraInfo.offset, 24)
        self.assertEqual(ctypes.sizeof(KEYBDINPUT), 24)
        self.assertEqual(ctypes.alignment(KEYBDINPUT), 8)
        self.assertEqual(KEYBDINPUT.dwExtraInfo.offset, 16)
        self.assertEqual(ctypes.sizeof(INPUT), 40)
        self.assertEqual(ctypes.alignment(INPUT), 8)
        self.assertEqual(INPUT_MARKER, 0x43524F31)
        self.assertLessEqual(INPUT_MARKER, 0xFFFFFFFF)
        value = SendInputBackend._mouse_input(MOUSEEVENTF_LEFTDOWN)
        self.assertEqual(value.mi.dwExtraInfo, INPUT_MARKER)

    def test_obsolete_full_width_marker_has_no_active_ownership_assumption(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for relative in (
            "helldivers_macro/input_backend.py",
            "helldivers_macro/input_hooks.py",
            "helldivers_macro/cadence_diagnostics.py",
            "README.md",
            "AGENTS.md",
        ):
            with self.subTest(relative=relative):
                text = (root / relative).read_text(encoding="utf-8").casefold()
                self.assertNotIn("48444d4143524f31", text)

    def test_mouse_events_are_marked_and_balanced(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.mouse_down()
        backend.mouse_up()
        self.assertEqual([item.mi.dwFlags for item in user.inputs], [2, 4])
        self.assertEqual(user.inputs[0].mi.dwFlags, MOUSEEVENTF_LEFTDOWN)
        self.assertEqual(user.inputs[1].mi.dwFlags, MOUSEEVENTF_LEFTUP)
        self.assertTrue(all(item.mi.dwExtraInfo == INPUT_MARKER for item in user.inputs))
        self.assertFalse(backend.mouse_owned)

    def test_keyboard_fire_uses_owned_p_scan_code_and_never_mouse(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(
            user32=user,
            output=OutputConfig("keyboard", 0x19),
        )
        backend.fire_down()
        self.assertTrue(backend.fire_owned)
        backend.fire_up()
        self.assertFalse(backend.fire_owned)
        self.assertEqual([item.type for item in user.inputs], [1, 1])
        self.assertEqual([item.ki.wScan for item in user.inputs], [0x19, 0x19])
        self.assertEqual(
            [item.ki.dwFlags for item in user.inputs],
            [KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP],
        )
        self.assertTrue(all(item.ki.dwExtraInfo == INPUT_MARKER for item in user.inputs))

    def test_mouse_fire_fallback_uses_owned_mb1(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(
            user32=user,
            output=OutputConfig("mouse", None),
        )
        backend.fire_down()
        backend.fire_up()
        self.assertEqual([item.type for item in user.inputs], [0, 0])
        self.assertEqual(
            [item.mi.dwFlags for item in user.inputs],
            [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP],
        )

    def test_release_all_releases_owned_keyboard_fire_key(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(
            user32=user,
            output=OutputConfig("keyboard", 0x19),
        )
        backend.fire_down()
        backend.release_all()
        self.assertFalse(backend.fire_owned)
        self.assertEqual(
            [item.ki.dwFlags for item in user.inputs],
            [KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP],
        )

    def test_reload_uses_scan_code_and_is_balanced(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.reload_down()
        backend.reload_up()
        self.assertEqual(user.inputs[0].ki.wVk, 0)
        self.assertEqual(user.inputs[0].ki.wScan, 0x13)
        self.assertEqual(user.inputs[0].ki.dwFlags, KEYEVENTF_SCANCODE)
        self.assertEqual(
            user.inputs[1].ki.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
        )
        self.assertTrue(all(item.ki.dwExtraInfo == INPUT_MARKER for item in user.inputs))
        self.assertFalse(backend.reload_owned)

    def test_aim_events_are_marked_owned_and_balanced(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.aim_down()
        backend.aim_up()
        self.assertEqual(
            [item.mi.dwFlags for item in user.inputs],
            [MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP],
        )
        self.assertTrue(
            all(item.mi.dwExtraInfo == INPUT_MARKER for item in user.inputs)
        )
        self.assertFalse(backend.aim_owned)

    def test_hold_release_is_marked_up_only_token_idempotent_and_retryable(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.release_held_aim(7)
        backend.release_held_aim(7)
        backend.release_held_aim(6)
        self.assertEqual(
            [item.mi.dwFlags for item in user.inputs], [MOUSEEVENTF_RIGHTUP]
        )
        self.assertEqual(user.inputs[0].mi.dwExtraInfo, INPUT_MARKER)

        retry_user = FakeUser32([0, 1], last_error=5)
        retry = SendInputBackend(user32=retry_user)
        with self.assertRaises(InputApiError):
            retry.release_held_aim(8)
        retry.release_held_aim(8)
        self.assertEqual(
            [item.mi.dwFlags for item in retry_user.inputs],
            [MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_RIGHTUP],
        )

    def test_shift_replay_preserves_scan_code_and_is_marked_owned(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.shift_down(0x36)
        backend.shift_up()
        self.assertEqual([item.ki.wScan for item in user.inputs], [0x36, 0x36])
        self.assertEqual(
            [item.ki.dwFlags for item in user.inputs],
            [KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP],
        )
        self.assertTrue(
            all(item.ki.dwExtraInfo == INPUT_MARKER for item in user.inputs)
        )
        self.assertFalse(backend.shift_owned)

    def test_shift_cleanup_does_not_release_macro_or_reload_inputs(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.mouse_down()
        backend.reload_down()
        backend.aim_down()
        backend.shift_down(0x2A)
        backend.release_shift_inputs()
        self.assertTrue(backend.mouse_owned)
        self.assertTrue(backend.reload_owned)
        self.assertFalse(backend.aim_owned)
        self.assertFalse(backend.shift_owned)
        backend.release_all()

    def test_failed_up_retains_ownership_and_cleanup_retries(self) -> None:
        user = FakeUser32([1, 0, 1])
        backend = SendInputBackend(user32=user)
        backend.mouse_down()
        with self.assertRaisesRegex(InputApiError, "accepted 0/1"):
            backend.mouse_up()
        self.assertTrue(backend.mouse_owned)
        backend.release_all()
        self.assertFalse(backend.mouse_owned)
        self.assertEqual(
            [item.mi.dwFlags for item in user.inputs],
            [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_LEFTUP],
        )

    def test_release_all_attempts_all_owned_inputs(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.mouse_down()
        backend.aim_down()
        backend.shift_down(0x2A)
        backend.reload_down()
        backend.release_all()
        self.assertFalse(backend.mouse_owned)
        self.assertFalse(backend.aim_owned)
        self.assertFalse(backend.shift_owned)
        self.assertFalse(backend.reload_owned)
        self.assertEqual(len(user.inputs), 8)

    def test_duplicate_down_is_refused_without_extra_input(self) -> None:
        user = FakeUser32()
        backend = SendInputBackend(user32=user)
        backend.mouse_down()
        with self.assertRaisesRegex(InputApiError, "duplicate"):
            backend.mouse_down()
        self.assertEqual(len(user.inputs), 1)
        backend.release_all()

    def test_zero_and_short_sendinput_results_report_last_error_and_fail_safely(self) -> None:
        from helldivers_macro.cadence_diagnostics import CadenceDiagnostics

        for accepted in (0, 2):
            with self.subTest(accepted=accepted):
                diagnostics = CadenceDiagnostics(clock_ns=lambda: 0)
                user = FakeUser32([accepted], last_error=1234)
                backend = SendInputBackend(
                    user32=user,
                    cadence_diagnostics=diagnostics,
                )
                diagnostics.macro_worker_started("PRIMARY")
                with diagnostics.macro_action("MB1_DOWN"):
                    with self.assertRaisesRegex(
                        InputApiError,
                        rf"accepted {accepted}/1.*WinError 1234",
                    ):
                        backend.mouse_down()
                diagnostics.macro_worker_stopped()
                self.assertFalse(backend.mouse_owned)
                data = diagnostics.snapshot()
                self.assertEqual(data["send_requested"]["MB1_DOWN"], 1)
                self.assertEqual(data["send_accepted"]["MB1_DOWN"], accepted)
                self.assertEqual(data["send_failures"], 1)
                self.assertIn("WinError=1234", data["send_last_errors"][0])


if __name__ == "__main__":
    unittest.main()
