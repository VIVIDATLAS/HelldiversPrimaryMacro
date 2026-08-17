from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cadence_diagnostics import CadenceDiagnostics, _BackendEvent
    from .config import OutputConfig


from .windows_abi import ULONG_PTR, structure_field_type


# ASCII "CRO1". The field remains ULONG_PTR, while this canonical marker fits
# in 32 bits so it survives the observed Windows mouse-hook boundary unchanged.
INPUT_MARKER = 0x43524F31

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
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
        self._firing_active = threading.Event()
        self._cleanup_pending = threading.Event()
        self._stratagem_active = threading.Event()

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
        self._firing_active.clear()
        self._cleanup_pending.clear()

    def firing_started(self) -> None:
        self._firing_active.set()

    def firing_stopped(self) -> None:
        self._firing_active.clear()

    def firing_active(self) -> bool:
        """Lock-free snapshot published at controller/owned-worker phase boundaries."""
        return self._firing_active.is_set()

    def cleanup_pending(self) -> bool:
        return self._cleanup_pending.is_set()

    def macro_active(self) -> bool:
        return self._macro_active.is_set()

    def stratagem_started(self) -> None:
        self._stratagem_active.set()

    def stratagem_stopped(self) -> None:
        self._stratagem_active.clear()

    def stratagem_active(self) -> bool:
        return self._stratagem_active.is_set()


def validate_ctypes_layouts() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    if ctypes.sizeof(ULONG_PTR) != pointer_size:
        raise RuntimeError("ULONG_PTR is not pointer-sized")
    for structure in (MOUSEINPUT, KEYBDINPUT):
        if ctypes.sizeof(structure_field_type(structure, "dwExtraInfo")) != pointer_size:
            raise RuntimeError(f"{structure.__name__}.dwExtraInfo is not pointer-sized")
    expected_mouse = 32 if pointer_size == 8 else 24
    expected_keyboard = 24 if pointer_size == 8 else 16
    expected_input = 40 if pointer_size == 8 else 28
    actual = (ctypes.sizeof(MOUSEINPUT), ctypes.sizeof(KEYBDINPUT), ctypes.sizeof(INPUT))
    expected = (expected_mouse, expected_keyboard, expected_input)
    if actual != expected:
        raise RuntimeError(f"unexpected ctypes input layouts: {actual}, expected {expected}")


class SendInputBackend:
    """Owns generated fire/MB1-bypass/MB2/Shift/R input and releases it safely."""

    def __init__(
        self,
        *,
        user32: object | None = None,
        cadence_diagnostics: CadenceDiagnostics | None = None,
        output: OutputConfig | None = None,
    ) -> None:
        validate_ctypes_layouts()
        if user32 is None:
            if os.name != "nt":
                raise InputApiError("SendInput requires Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        self._cadence_diagnostics = cadence_diagnostics
        self._fire_device = "mouse" if output is None else output.fire_device
        self._fire_scan_code = None if output is None else output.fire_scan_code
        if self._fire_device not in {"keyboard", "mouse"}:
            raise InputApiError(f"unsupported fire output device {self._fire_device!r}")
        if self._fire_device == "keyboard" and not (
            type(self._fire_scan_code) is int
            and 1 <= self._fire_scan_code <= 0xFF
        ):
            raise InputApiError("keyboard fire output requires scan code 1..255")
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
        self._fire_key_owned = False
        self._aim_owned = False
        self._shift_owned = False
        self._shift_scan = 0
        self._reload_owned = False
        self._stratagem_owner: int | None = None
        self._stratagem_scan: tuple[int, bool] | None = None
        self._stratagem_ctrl_owned = False
        self._stratagem_mouse_owned = False

    @property
    def mouse_owned(self) -> bool:
        with self._lock:
            return self._mouse_owned

    def mouse_owned_snapshot(self) -> bool:
        """Lock-free hook snapshot; ownership writes are atomic under the GIL."""
        return self._mouse_owned

    @property
    def fire_owned(self) -> bool:
        with self._lock:
            return (
                self._fire_key_owned
                if self._fire_device == "keyboard"
                else self._mouse_owned
            )

    @property
    def fire_action_names(self) -> tuple[str, str]:
        return (
            ("P_DOWN", "P_UP")
            if self._fire_device == "keyboard"
            else ("MB1_DOWN", "MB1_UP")
        )

    @property
    def reload_owned(self) -> bool:
        with self._lock:
            return self._reload_owned

    @property
    def aim_owned(self) -> bool:
        with self._lock:
            return self._aim_owned

    @property
    def shift_owned(self) -> bool:
        with self._lock:
            return self._shift_owned

    def _send_exactly_one(self, input_value: INPUT, description: str) -> None:
        diagnostic_event: _BackendEvent | None = None
        if self._cadence_diagnostics is not None:
            diagnostic_event = self._cadence_diagnostics.send_requested()
        ctypes.set_last_error(0)
        try:
            accepted = int(
                self._user32.SendInput(
                    1, ctypes.byref(input_value), ctypes.sizeof(INPUT)
                )
            )
        except BaseException:
            if self._cadence_diagnostics is not None:
                self._cadence_diagnostics.send_completed(
                    diagnostic_event, 0, ctypes.get_last_error()
                )
            raise
        error = ctypes.get_last_error() if accepted != 1 else 0
        if self._cadence_diagnostics is not None:
            self._cadence_diagnostics.send_completed(
                diagnostic_event, accepted, error
            )
        if accepted != 1:
            raise InputApiError(
                f"SendInput accepted {accepted}/1 event for {description} (WinError {error})"
            )

    @staticmethod
    def _mouse_input(flags: int) -> INPUT:
        value = INPUT(type=INPUT_MOUSE)
        value.mi = MOUSEINPUT(0, 0, 0, flags, 0, INPUT_MARKER)
        return value

    @staticmethod
    def _keyboard_input(
        scan_code: int, *, key_up: bool, extended: bool = False
    ) -> INPUT:
        flags = (
            KEYEVENTF_SCANCODE
            | (KEYEVENTF_EXTENDEDKEY if extended else 0)
            | (KEYEVENTF_KEYUP if key_up else 0)
        )
        value = INPUT(type=INPUT_KEYBOARD)
        value.ki = KEYBDINPUT(0, scan_code, flags, 0, INPUT_MARKER)
        return value

    def _claim_stratagem(self, token: int) -> None:
        if self._stratagem_owner not in (None, token):
            raise InputApiError("stratagem output is owned by another worker")
        self._stratagem_owner = token

    def stratagem_key_down(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        with self._lock:
            self._claim_stratagem(token)
            if ctrl:
                if self._stratagem_ctrl_owned:
                    raise InputApiError("refusing duplicate stratagem Ctrl down")
            elif self._stratagem_scan is not None:
                raise InputApiError("refusing overlapping stratagem arrow down")
            self._send_exactly_one(
                self._keyboard_input(scan_code, key_up=False, extended=extended),
                "stratagem Ctrl down" if ctrl else "stratagem arrow down",
            )
            if ctrl:
                self._stratagem_ctrl_owned = True
            else:
                self._stratagem_scan = (scan_code, extended)

    def stratagem_key_up(
        self, token: int, scan_code: int, *, extended: bool, ctrl: bool = False
    ) -> None:
        with self._lock:
            if self._stratagem_owner != token:
                return
            if ctrl:
                if not self._stratagem_ctrl_owned:
                    return
            elif self._stratagem_scan != (scan_code, extended):
                return
            self._send_exactly_one(
                self._keyboard_input(scan_code, key_up=True, extended=extended),
                "stratagem Ctrl up" if ctrl else "stratagem arrow up",
            )
            if ctrl:
                self._stratagem_ctrl_owned = False
            else:
                self._stratagem_scan = None

    def stratagem_mouse_down(self, token: int) -> None:
        with self._lock:
            self._claim_stratagem(token)
            if self._stratagem_mouse_owned:
                raise InputApiError("refusing duplicate stratagem MB1 down")
            self._send_exactly_one(
                self._mouse_input(MOUSEEVENTF_LEFTDOWN), "stratagem MB1 down"
            )
            self._stratagem_mouse_owned = True

    def stratagem_mouse_up(self, token: int) -> None:
        with self._lock:
            if self._stratagem_owner != token or not self._stratagem_mouse_owned:
                return
            self._send_exactly_one(
                self._mouse_input(MOUSEEVENTF_LEFTUP), "stratagem MB1 up"
            )
            self._stratagem_mouse_owned = False

    def release_stratagem(self, token: int) -> None:
        """Release only inputs held by the matching stratagem generation."""
        errors: list[str] = []
        with self._lock:
            if self._stratagem_owner != token:
                return
            if self._stratagem_scan is not None:
                scan_code, extended = self._stratagem_scan
                try:
                    self.stratagem_key_up(
                        token, scan_code, extended=extended, ctrl=False
                    )
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._stratagem_ctrl_owned:
                try:
                    self.stratagem_key_up(
                        token, 0x1D, extended=False, ctrl=True
                    )
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._stratagem_mouse_owned:
                try:
                    self.stratagem_mouse_up(token)
                except InputApiError as exc:
                    errors.append(str(exc))
            if not (
                self._stratagem_scan
                or self._stratagem_ctrl_owned
                or self._stratagem_mouse_owned
            ):
                self._stratagem_owner = None
        if errors:
            raise InputApiError("; ".join(errors))

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

    def fire_down(self) -> None:
        if self._fire_device == "mouse":
            self.mouse_down()
            return
        with self._lock:
            if self._fire_key_owned:
                raise InputApiError("refusing duplicate generated P down")
            self._send_exactly_one(
                self._keyboard_input(self._fire_scan_code, key_up=False),
                "P fire down",
            )
            self._fire_key_owned = True

    def fire_up(self) -> None:
        if self._fire_device == "mouse":
            self.mouse_up()
            return
        with self._lock:
            if not self._fire_key_owned:
                return
            self._send_exactly_one(
                self._keyboard_input(self._fire_scan_code, key_up=True),
                "P fire up",
            )
            self._fire_key_owned = False

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

    def shift_down(self, scan_code: int) -> None:
        with self._lock:
            if self._shift_owned:
                raise InputApiError("refusing duplicate generated Shift down")
            if not 0 < scan_code <= 0xFFFF:
                raise InputApiError(f"invalid physical Shift scan code {scan_code}")
            self._send_exactly_one(
                self._keyboard_input(scan_code, key_up=False), "Shift down"
            )
            self._shift_scan = scan_code
            self._shift_owned = True

    def shift_up(self) -> None:
        with self._lock:
            if not self._shift_owned:
                return
            self._send_exactly_one(
                self._keyboard_input(self._shift_scan, key_up=True), "Shift up"
            )
            self._shift_owned = False
            self._shift_scan = 0

    def reload_down(self) -> None:
        with self._lock:
            if self._reload_owned:
                raise InputApiError("refusing duplicate generated R down")
            self._send_exactly_one(
                self._keyboard_input(self._r_scan, key_up=False), "R down"
            )
            self._reload_owned = True

    def reload_up(self) -> None:
        with self._lock:
            if not self._reload_owned:
                return
            self._send_exactly_one(
                self._keyboard_input(self._r_scan, key_up=True), "R up"
            )
            self._reload_owned = False

    def release_shift_inputs(self) -> None:
        """Release only inputs a deferred Shift transaction can own."""
        errors: list[str] = []
        with self._lock:
            if self._aim_owned:
                try:
                    self.aim_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._shift_owned:
                try:
                    self.shift_up()
                except InputApiError as exc:
                    errors.append(str(exc))
        if errors:
            raise InputApiError("; ".join(errors))

    def release_all(self) -> None:
        errors: list[str] = []
        with self._lock:
            if self._stratagem_owner is not None:
                try:
                    self.release_stratagem(self._stratagem_owner)
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._fire_key_owned:
                try:
                    if self._cadence_diagnostics is None:
                        self.fire_up()
                    else:
                        self._cadence_diagnostics.record_cleanup_release("P_UP")
                        with self._cadence_diagnostics.macro_action(
                            "P_UP", intended=False
                        ):
                            self.fire_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._mouse_owned:
                try:
                    if self._cadence_diagnostics is None:
                        self.mouse_up()
                    else:
                        self._cadence_diagnostics.record_cleanup_release("MB1_UP")
                        with self._cadence_diagnostics.macro_action(
                            "MB1_UP", intended=False
                        ):
                            self.mouse_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._aim_owned:
                try:
                    self.aim_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._shift_owned:
                try:
                    self.shift_up()
                except InputApiError as exc:
                    errors.append(str(exc))
            if self._reload_owned:
                try:
                    if self._cadence_diagnostics is None:
                        self.reload_up()
                    else:
                        self._cadence_diagnostics.record_cleanup_release("R_UP")
                        with self._cadence_diagnostics.macro_action(
                            "R_UP", intended=False
                        ):
                            self.reload_up()
                except InputApiError as exc:
                    errors.append(str(exc))
        if errors:
            raise InputApiError("; ".join(errors))
