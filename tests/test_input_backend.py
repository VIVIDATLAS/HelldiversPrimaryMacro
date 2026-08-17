from __future__ import annotations

import ctypes
import unittest

from helldivers_macro.input_backend import (
    INPUT,
    INPUT_MARKER,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    InputApiError,
    SendInputBackend,
    validate_ctypes_layouts,
)


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeUser32:
    def __init__(self, results=None) -> None:
        self.results = list(results or [])
        self.inputs: list[INPUT] = []
        self.MapVirtualKeyW = Function(lambda vk, map_type: 0x13)

        def send(count, pointer, size):
            self.inputs.append(
                INPUT.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(INPUT)))
            )
            return self.results.pop(0) if self.results else 1

        self.SendInput = Function(send)


class InputBackendTests(unittest.TestCase):
    def test_pointer_sized_layouts_are_valid(self) -> None:
        validate_ctypes_layouts()

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


if __name__ == "__main__":
    unittest.main()
