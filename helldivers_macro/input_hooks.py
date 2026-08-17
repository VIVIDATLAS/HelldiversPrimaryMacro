from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
from typing import TYPE_CHECKING, Callable

from .input_backend import INPUT_MARKER, VK_R, InputCoordination
from .models import (
    ControlEvent,
    ControlEventKind,
    EventSource,
    Mb1PairDecision,
    Mb2PairDecision,
    ShiftStroke,
)
from .windows_abi import (
    ULONG_PTR,
    marker_matches,
    normalize_ulong_ptr,
    structure_field_type,
)

if TYPE_CHECKING:
    from .cadence_diagnostics import CadenceDiagnostics


WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
WM_QUIT = 0x0012
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

VK_1 = 0x31
VK_2 = 0x32
VK_NUMPAD1 = 0x61
VK_NUMPAD2 = 0x62
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_P = 0x50
VK_F23 = 0x86
VK_F24 = 0x87
SHIFT_SCAN_CODES = {VK_LSHIFT: 0x2A, VK_RSHIFT: 0x36}

LRESULT = ctypes.c_ssize_t
CALLBACK_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
HOOKPROC = CALLBACK_FACTORY(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


def validate_hook_layouts() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    if ctypes.sizeof(ULONG_PTR) != pointer_size:
        raise RuntimeError("hook dwExtraInfo is not pointer-sized")
    for structure in (KBDLLHOOKSTRUCT, MSLLHOOKSTRUCT):
        if ctypes.sizeof(structure_field_type(structure, "dwExtraInfo")) != pointer_size:
            raise RuntimeError(f"{structure.__name__}.dwExtraInfo is not pointer-sized")
    expected_keyboard = 24 if pointer_size == 8 else 20
    expected_mouse = 32 if pointer_size == 8 else 24
    actual = (ctypes.sizeof(KBDLLHOOKSTRUCT), ctypes.sizeof(MSLLHOOKSTRUCT))
    expected = (expected_keyboard, expected_mouse)
    if actual != expected:
        raise RuntimeError(f"unexpected hook layouts: {actual}, expected {expected}")


class HookPolicy:
    """Synchronous, allocation-light decision logic used by hook callbacks."""

    def __init__(
        self,
        foreground_status: Callable[[], tuple[bool, bool]],
        event_sink: Callable[[ControlEvent], None],
        generated_mouse_owned: Callable[[], bool] = lambda: False,
        coordination: InputCoordination | None = None,
        diagnostics_enabled: bool = False,
        cadence_diagnostics: CadenceDiagnostics | None = None,
        fire_device: str = "mouse",
        fire_scan_code: int | None = None,
        stratagem_triggers: dict[int, ControlEventKind] | None = None,
    ) -> None:
        self._foreground_status = foreground_status
        self._event_sink = event_sink
        self._generated_mouse_owned = generated_mouse_owned
        self._coordination = coordination or InputCoordination()
        self._diagnostics_enabled = diagnostics_enabled
        self._cadence_diagnostics = cadence_diagnostics
        self._fire_device = fire_device
        self._fire_scan_code = fire_scan_code
        self._stratagem_triggers = stratagem_triggers or {}
        self._keys_down: set[int] = set()
        self._left_pair_decision: Mb1PairDecision | None = None
        self._left_pair_is_manual = False
        self._right_pair_decision: Mb2PairDecision | None = None
        self._deferred_shift_pairs: set[int] = set()
        self._stratagem_pairs: dict[int, bool] = {}

    @property
    def ctrl_down(self) -> bool:
        return VK_LCONTROL in self._keys_down or VK_RCONTROL in self._keys_down

    def initialize_physical_key(self, vk_code: int, is_down: bool) -> None:
        """Seed pre-hook key state without treating it as a new control event."""
        if is_down:
            self._keys_down.add(vk_code)
        else:
            self._keys_down.discard(vk_code)

    def _emit(
        self,
        kind: ControlEventKind,
        detail: object = None,
        *,
        source: EventSource = EventSource.PHYSICAL,
    ) -> None:
        self._event_sink(ControlEvent(kind, detail=detail, source=source))

    def _diagnostic(self, message: str) -> None:
        if self._diagnostics_enabled:
            self._emit(ControlEventKind.DIAGNOSTIC, message)

    def keyboard(
        self,
        message: int,
        vk_code: int,
        flags: int,
        scan_code: int = 0,
        extra_info: int = 0,
    ) -> bool:
        """Observe keys and defer foreground physical Shift pairs."""
        marked = marker_matches(extra_info, INPUT_MARKER)
        diagnostic_action = None
        if self._fire_device == "keyboard" and scan_code == self._fire_scan_code:
            diagnostic_action = (
                "P_DOWN"
                if message in (WM_KEYDOWN, WM_SYSKEYDOWN)
                else "P_UP"
                if message in (WM_KEYUP, WM_SYSKEYUP)
                else None
            )
        elif vk_code == VK_R:
            diagnostic_action = (
                "R_DOWN"
                if message in (WM_KEYDOWN, WM_SYSKEYDOWN)
                else "R_UP"
                if message in (WM_KEYUP, WM_SYSKEYUP)
                else None
            )
        if (
            self._cadence_diagnostics is not None
            and flags & LLKHF_INJECTED
            and diagnostic_action is not None
        ):
            self._cadence_diagnostics.observe_injected_keyboard_event(
                diagnostic_action,
                flags,
                extra_info,
                marker_matches=marked,
                injected_flag=LLKHF_INJECTED,
            )
        if marked:
            if self._cadence_diagnostics is not None:
                if diagnostic_action is not None:
                    self._cadence_diagnostics.observe_owned_hook_event(
                        diagnostic_action,
                        passed=True,
                        suppressed=False,
                        routed=False,
                    )
            return False
        if flags & LLKHF_INJECTED:
            return False
        if message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if vk_code in self._keys_down:
                if vk_code in self._stratagem_pairs:
                    return self._stratagem_pairs[vk_code]
                return vk_code in self._deferred_shift_pairs
            self._keys_down.add(vk_code)
            if vk_code in self._stratagem_triggers:
                active, certain = self._foreground_status()
                suppress = active and certain
                self._stratagem_pairs[vk_code] = suppress
                if suppress and not self._coordination.stratagem_active():
                    self._emit(self._stratagem_triggers[vk_code])
                return suppress
            if vk_code in (VK_LCONTROL, VK_RCONTROL):
                # Publish physical Ctrl and the cancellation gate before any
                # queued controller work or later mouse callback can run.
                self._coordination.ctrl_pressed_in_hook()
                if self._diagnostics_enabled:
                    self._diagnostic(
                        "physical Ctrl down; cleanup_pending="
                        f"{self._coordination.cleanup_pending()}"
                    )
                self._emit(ControlEventKind.CTRL_DOWN)
            elif vk_code in (VK_LSHIFT, VK_RSHIFT):
                active, _certain = self._foreground_status()
                if active:
                    # The pair latch suppresses this edge, autorepeat, and the
                    # matching up. The controller later replays one owned pair
                    # after firing cleanup and conditional aim cancellation.
                    self._deferred_shift_pairs.add(vk_code)
                    self._emit(
                        ControlEventKind.SHIFT_DOWN,
                        detail=ShiftStroke(
                            vk_code,
                            scan_code or SHIFT_SCAN_CODES[vk_code],
                        ),
                    )
                    return True
            elif vk_code in (VK_1, VK_2):
                active, certain = self._foreground_status()
                if active and not self._coordination.stratagem_active():
                    self._emit(
                        ControlEventKind.SELECT_PRIMARY
                        if vk_code == VK_1
                        else ControlEventKind.SELECT_SECONDARY
                    )
                elif not certain:
                    self._emit(
                        ControlEventKind.FOREGROUND_UNCERTAIN,
                        source=EventSource.FOREGROUND,
                    )
        elif message in (WM_KEYUP, WM_SYSKEYUP):
            was_down = vk_code in self._keys_down
            self._keys_down.discard(vk_code)
            if vk_code in self._stratagem_pairs:
                return self._stratagem_pairs.pop(vk_code)
            if was_down and vk_code in (VK_LCONTROL, VK_RCONTROL):
                self._emit(ControlEventKind.CTRL_UP)
            elif vk_code in (VK_LSHIFT, VK_RSHIFT):
                deferred = vk_code in self._deferred_shift_pairs
                self._deferred_shift_pairs.discard(vk_code)
                if deferred:
                    return True
        return False

    def mouse(self, message: int, flags: int, extra_info: int) -> bool:
        """Latch one decision for each genuine physical MB1 down/up pair."""
        marked = marker_matches(extra_info, INPUT_MARKER)
        if (
            self._cadence_diagnostics is not None
            and flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED)
        ):
            self._cadence_diagnostics.observe_injected_mouse_event(
                message,
                flags,
                extra_info,
                marker_matches=marked,
                injected_flag=LLMHF_INJECTED,
                lower_il_flag=LLMHF_LOWER_IL_INJECTED,
            )
        if marked:
            if self._cadence_diagnostics is not None:
                action = (
                    "MB1_DOWN"
                    if message == WM_LBUTTONDOWN
                    else "MB1_UP"
                    if message == WM_LBUTTONUP
                    else None
                )
                if action is not None:
                    self._cadence_diagnostics.observe_owned_hook_event(
                        action, passed=True, suppressed=False, routed=False
                    )
            return False
        if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
            return False

        if message == WM_RBUTTONDOWN:
            if self._right_pair_decision is not None:
                return (
                    self._right_pair_decision
                    is Mb2PairDecision.DEFERRED_AIM_OFF
                )
            active, _certain = self._foreground_status()
            if active and self._coordination.firing_active():
                self._right_pair_decision = Mb2PairDecision.DEFERRED_AIM_OFF
                self._emit(ControlEventKind.DEFERRED_AIM_OFF)
                return True
            self._right_pair_decision = Mb2PairDecision.PASS_THROUGH
            if active:
                self._emit(ControlEventKind.PHYSICAL_MB2_DOWN)
            return False
        if message == WM_RBUTTONUP:
            decision = self._right_pair_decision
            self._right_pair_decision = None
            if decision is Mb2PairDecision.DEFERRED_AIM_OFF:
                return True
            if decision is Mb2PairDecision.PASS_THROUGH:
                self._emit(ControlEventKind.PHYSICAL_MB2_UP)
            return False

        if message == WM_LBUTTONDOWN:
            if self._left_pair_decision is not None:
                return self._left_pair_decision is not Mb1PairDecision.PASS_THROUGH
            active, certain = self._foreground_status()
            mouse_owned = self._generated_mouse_owned()
            cleanup_pending = self._coordination.cleanup_pending()
            self._left_pair_is_manual = False
            if not active:
                decision = Mb1PairDecision.PASS_THROUGH
            elif self._coordination.stratagem_active():
                decision = Mb1PairDecision.SUPPRESS_STRATAGEM_BUSY
            elif not self.ctrl_down:
                decision = Mb1PairDecision.SUPPRESS_TOGGLE
            else:
                self._left_pair_is_manual = True
                if mouse_owned or cleanup_pending:
                    decision = Mb1PairDecision.DEFERRED_BYPASS
                else:
                    decision = Mb1PairDecision.PASS_THROUGH
            self._left_pair_decision = decision
            if self._diagnostics_enabled:
                self._diagnostic(
                    "physical MB1 down decision="
                    f"{decision.name} ctrl={self.ctrl_down} "
                    f"mouse_owned={mouse_owned} "
                    f"cleanup_pending={cleanup_pending}"
                )
            if decision is Mb1PairDecision.SUPPRESS_TOGGLE:
                self._emit(ControlEventKind.PHYSICAL_MB1_DOWN)
            elif decision is Mb1PairDecision.DEFERRED_BYPASS:
                self._emit(ControlEventKind.DEFERRED_BYPASS_DOWN)
            elif self._left_pair_is_manual:
                self._emit(ControlEventKind.MANUAL_BYPASS_DOWN)
            elif not active:
                self._emit(
                    ControlEventKind.FOREGROUND_LOST
                    if certain
                    else ControlEventKind.FOREGROUND_UNCERTAIN,
                    source=EventSource.FOREGROUND,
                )
            return decision is not Mb1PairDecision.PASS_THROUGH
        if message == WM_LBUTTONUP:
            decision = self._left_pair_decision
            self._left_pair_decision = None
            self._left_pair_is_manual = False
            if decision is Mb1PairDecision.SUPPRESS_TOGGLE:
                self._emit(ControlEventKind.PHYSICAL_MB1_UP)
                return True
            if decision is Mb1PairDecision.SUPPRESS_STRATAGEM_BUSY:
                return True
            if decision is Mb1PairDecision.DEFERRED_BYPASS:
                self._emit(ControlEventKind.DEFERRED_BYPASS_UP)
                return True
            # Passed-through physical releases remain visible as cleanup-only
            # observations. This is required to clear neutral rearming after a
            # focus change, and never authorizes a toggle in the controller.
            self._emit(ControlEventKind.PHYSICAL_MB1_UP)
            return False
        return False


class HookInstallError(RuntimeError):
    pass


class WindowsHookThread:
    """Own both low-level hooks and their required Windows message loop."""

    def __init__(
        self,
        policy: HookPolicy,
        event_sink: Callable[[ControlEvent], None],
        *,
        user32: object | None = None,
        kernel32: object | None = None,
    ) -> None:
        validate_hook_layouts()
        if user32 is None or kernel32 is None:
            if os.name != "nt":
                raise HookInstallError("Windows low-level hooks require Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = user32
        self._kernel32 = kernel32
        self._policy = policy
        self._event_sink = event_sink
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread_id = 0
        self._mouse_hook = None
        self._keyboard_hook = None
        # Keep these callback objects alive for at least as long as the hooks.
        self._mouse_callback = HOOKPROC(self._mouse_proc)
        self._keyboard_callback = HOOKPROC(self._keyboard_proc)
        self._configure_api()
        self._thread = threading.Thread(
            target=self._run, name="windows-hook-loop", daemon=False
        )

    def _configure_api(self) -> None:
        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CallNextHookEx.restype = LRESULT
        self._user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.GetMessageW.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = wintypes.SHORT
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def _call_next(self, code: int, wparam: int, lparam: int) -> int:
        return int(self._user32.CallNextHookEx(None, code, wparam, lparam))

    @staticmethod
    def _read_mouse_hook_data(lparam: int) -> MSLLHOOKSTRUCT:
        address = ctypes.c_void_p(normalize_ulong_ptr(lparam))
        return ctypes.cast(address, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

    @staticmethod
    def _read_keyboard_hook_data(lparam: int) -> KBDLLHOOKSTRUCT:
        address = ctypes.c_void_p(normalize_ulong_ptr(lparam))
        return ctypes.cast(address, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

    def _mouse_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code < HC_ACTION:
            return self._call_next(code, wparam, lparam)
        try:
            data = self._read_mouse_hook_data(lparam)
            if self._policy.mouse(int(wparam), int(data.flags), int(data.dwExtraInfo)):
                return 1
        except BaseException as exc:
            self._event_sink(
                ControlEvent(
                    ControlEventKind.HOOK_FAILURE,
                    detail=exc,
                    source=EventSource.SHUTDOWN,
                )
            )
        return self._call_next(code, wparam, lparam)

    def _keyboard_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code < HC_ACTION:
            return self._call_next(code, wparam, lparam)
        try:
            data = self._read_keyboard_hook_data(lparam)
            if self._policy.keyboard(
                int(wparam),
                int(data.vkCode),
                int(data.flags),
                int(data.scanCode),
                int(data.dwExtraInfo),
            ):
                return 1
        except BaseException as exc:
            self._event_sink(
                ControlEvent(
                    ControlEventKind.HOOK_FAILURE,
                    detail=exc,
                    source=EventSource.SHUTDOWN,
                )
            )
        return self._call_next(code, wparam, lparam)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(5.0):
            self._stop_event.set()
            if self._thread_id:
                self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(5.0)
            raise HookInstallError("hook thread did not initialize within 5 seconds")
        if self._startup_error is not None:
            raise HookInstallError(str(self._startup_error)) from self._startup_error

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread.is_alive():
            self._thread.join(5.0)
        if self._thread.is_alive():
            self._event_sink(
                ControlEvent(
                    ControlEventKind.HOOK_FAILURE,
                    detail=HookInstallError("hook thread did not stop"),
                    source=EventSource.SHUTDOWN,
                )
            )

    def _run(self) -> None:
        unexpected_error: BaseException | None = None
        try:
            self._thread_id = int(self._kernel32.GetCurrentThreadId())
            # Force creation of this thread's message queue before publishing ready.
            message = wintypes.MSG()
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            module = self._kernel32.GetModuleHandleW(None)
            self._mouse_hook = self._user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._mouse_callback, module, 0
            )
            if not self._mouse_hook:
                raise HookInstallError(
                    f"SetWindowsHookExW(WH_MOUSE_LL) failed (WinError {ctypes.get_last_error()})"
                )
            self._keyboard_hook = self._user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._keyboard_callback, module, 0
            )
            if not self._keyboard_hook:
                raise HookInstallError(
                    f"SetWindowsHookExW(WH_KEYBOARD_LL) failed (WinError {ctypes.get_last_error()})"
                )
            # Seed keys that were already held when hooks were installed. All
            # subsequent state is maintained exclusively by the keyboard hook;
            # GetAsyncKeyState is never called from either low-level callback.
            for vk_code in (
                VK_LCONTROL,
                VK_RCONTROL,
                VK_LSHIFT,
                VK_RSHIFT,
                VK_1,
                VK_2,
                VK_F23,
                VK_F24,
            ):
                self._policy.initialize_physical_key(
                    vk_code, bool(self._user32.GetAsyncKeyState(vk_code) & 0x8000)
                )
            self._ready.set()
            while not self._stop_event.is_set():
                result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    raise HookInstallError(
                        f"GetMessageW failed (WinError {ctypes.get_last_error()})"
                    )
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
            elif not self._stop_event.is_set():
                unexpected_error = exc
        finally:
            if self._keyboard_hook:
                if not self._user32.UnhookWindowsHookEx(self._keyboard_hook):
                    if not self._stop_event.is_set():
                        unexpected_error = HookInstallError(
                            "UnhookWindowsHookEx failed for keyboard hook"
                        )
                self._keyboard_hook = None
            if self._mouse_hook:
                if not self._user32.UnhookWindowsHookEx(self._mouse_hook):
                    if not self._stop_event.is_set():
                        unexpected_error = HookInstallError(
                            "UnhookWindowsHookEx failed for mouse hook"
                        )
                self._mouse_hook = None
            self._ready.set()
            if unexpected_error is not None:
                self._event_sink(
                    ControlEvent(
                        ControlEventKind.HOOK_FAILURE,
                        detail=unexpected_error,
                        source=EventSource.SHUTDOWN,
                    )
                )
