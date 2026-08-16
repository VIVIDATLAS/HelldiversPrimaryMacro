from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


# ASCII "HDMACRO1", truncated naturally on a 32-bit pointer. Windows 11 Pro
# deployments for this project are expected to be 64-bit.
INPUT_MARKER = 0x48444D4143524F31
ULONG_PTR = wintypes.WPARAM

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0
VK_R = 0x52


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


class InputApiError(RuntimeError):
    pass


class InputCoordination:
    """Nonblocking hook/controller handoff for cancellation-sensitive MB1 state."""

    def __init__(self) -> None:
        self._macro_active = threading.Event()
        self._cleanup_pending = threading.Event()

    def macro_started(self) -> None:
        self._macro_active.set()

    def cleanup_requested(self) -> None:
        if self._macro_active.is_set():
            self._cleanup_pending.set()

    def ctrl_pressed_in_hook(self) -> None:
        # This runs synchronously in the keyboard hook before CTRL_DOWN is queued.
        self.cleanup_requested()

    def cleanup_completed(self) -> None:
        self._macro_active.clear()
        self._cleanup_pending.clear()

    def cleanup_pending(self) -> bool:
        return self._cleanup_pending.is_set()

    def macro_active(self) -> bool:
        return self._macro_active.is_set()


def validate_ctypes_layouts() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    if ctypes.sizeof(ULONG_PTR) != pointer_size:
        raise RuntimeError("ULONG_PTR is not pointer-sized")
    expected_mouse = 32 if pointer_size == 8 else 24
    expected_keyboard = 24 if pointer_size == 8 else 16
    expected_input = 40 if pointer_size == 8 else 28
    actual = (ctypes.sizeof(MOUSEINPUT), ctypes.sizeof(KEYBDINPUT), ctypes.sizeof(INPUT))
    expected = (expected_mouse, expected_keyboard, expected_input)
    if actual != expected:
        raise RuntimeError(f"unexpected ctypes input layouts: {actual}, expected {expected}")


class SendInputBackend:
    """Owns generated MB1/MB2/R downs and releases only owned inputs."""

    def __init__(self, *, user32: object | None = None) -> None:
        validate_ctypes_layouts()
        if user32 is None:
            if os.name != "nt":
                raise InputApiError("SendInput requires Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        self._user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self._user32.MapVirtualKeyW.restype = wintypes.UINT
        self._r_scan = int(self._user32.MapVirtualKeyW(VK_R, MAPVK_VK_TO_VSC))
        if not self._r_scan:
            raise InputApiError("MapVirtualKeyW could not map reload key R")
        self._lock = threading.RLock()
        self._mouse_owned = False
        self._aim_owned = False
        self._reload_owned = False

    @property
    def mouse_owned(self) -> bool:
        with self._lock:
            return self._mouse_owned

    def mouse_owned_snapshot(self) -> bool:
        """Lock-free hook snapshot; ownership writes are atomic under the GIL."""
        return self._mouse_owned

    @property
    def reload_owned(self) -> bool:
        with self._lock:
            return self._reload_owned

    @property
    def aim_owned(self) -> bool:
        with self._lock:
            return self._aim_owned

    def _send_exactly_one(self, input_value: INPUT, description: str) -> None:
        ctypes.set_last_error(0)
        accepted = int(
            self._user32.SendInput(1, ctypes.byref(input_value), ctypes.sizeof(INPUT))
        )
        if accepted != 1:
            error = ctypes.get_last_error()
            raise InputApiError(
                f"SendInput accepted {accepted}/1 event for {description} (WinError {error})"
            )

    @staticmethod
    def _mouse_input(flags: int) -> INPUT:
        value = INPUT(type=INPUT_MOUSE)
        value.mi = MOUSEINPUT(0, 0, 0, flags, 0, INPUT_MARKER)
        return value

    def _keyboard_input(self, *, key_up: bool) -> INPUT:
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        value = INPUT(type=INPUT_KEYBOARD)
        value.ki = KEYBDINPUT(0, self._r_scan, flags, 0, INPUT_MARKER)
        return value

    def mouse_down(self) -> None:
        with self._lock:
            if self._mouse_owned:
                raise InputApiError("refusing duplicate generated MB1 down")
            self._send_exactly_one(self._mouse_input(MOUSEEVENTF_LEFTDOWN), "MB1 down")
            self._mouse_owned = True

    def mouse_up(self) -> None:
        with self._lock:
            if not self._mouse_owned:
                return
            self._send_exactly_one(self._mouse_input(MOUSEEVENTF_LEFTUP), "MB1 up")
            self._mouse_owned = False

    def aim_down(self) -> None:
        with self._lock:
            if self._aim_owned:
                raise InputApiError("refusing duplicate generated MB2 down")
            self._send_exactly_one(
                self._mouse_input(MOUSEEVENTF_RIGHTDOWN), "MB2 down"
            )
            self._aim_owned = True

    def aim_up(self) -> None:
        with self._lock:
            if not self._aim_owned:
                return
            self._send_exactly_one(
                self._mouse_input(MOUSEEVENTF_RIGHTUP), "MB2 up"
            )
            self._aim_owned = False

    def reload_down(self) -> None:
        with self._lock:
            if self._reload_owned:
                raise InputApiError("refusing duplicate generated R down")
            self._send_exactly_one(self._keyboard_input(key_up=False), "R down")
            self._reload_owned = True

    def reload_up(self) -> None:
        with self._lock:
            if not self._reload_owned:
                return
            self._send_exactly_one(self._keyboard_input(key_up=True), "R up")
            self._reload_owned = False

    def release_all(self) -> None:
        errors: list[str] = []
        with self._lock:
            if self._mouse_owned:
                try:
                    self.mouse_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._aim_owned:
                try:
                    self.aim_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._reload_owned:
                try:
                    self.reload_up()
                except InputApiError as exc:
                    errors.append(str(exc))
        if errors:
            raise InputApiError("; ".join(errors))
