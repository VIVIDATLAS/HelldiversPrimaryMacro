from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
import threading
import time
from typing import Callable

from .models import ForegroundObservation


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ForegroundApiError(RuntimeError):
    pass


class WindowsForegroundInspector:
    """Read-only foreground process inspection using minimum query access."""

    def __init__(
        self,
        target_executable: str,
        *,
        clock: Callable[[], float] = time.perf_counter,
        user32: object | None = None,
        kernel32: object | None = None,
    ) -> None:
        self.target_basename = ntpath.basename(target_executable).casefold()
        self._clock = clock
        if user32 is None or kernel32 is None:
            if os.name != "nt":
                raise ForegroundApiError("foreground inspection requires Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = user32
        self._kernel32 = kernel32
        self._configure_api()

    def _configure_api(self) -> None:
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def inspect(self) -> ForegroundObservation:
        timestamp = self._clock()
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return ForegroundObservation(
                False, False, timestamp, error="GetForegroundWindow returned no window"
            )

        pid_value = wintypes.DWORD()
        thread_id = self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
        if not thread_id or not pid_value.value:
            error = ctypes.get_last_error()
            return ForegroundObservation(
                False,
                False,
                timestamp,
                error=f"GetWindowThreadProcessId failed (WinError {error})",
            )

        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid_value.value
        )
        if not handle:
            error = ctypes.get_last_error()
            return ForegroundObservation(
                False,
                False,
                timestamp,
                pid=pid_value.value,
                error=f"OpenProcess failed (WinError {error})",
            )
        try:
            capacity = 32768
            length = wintypes.DWORD(capacity)
            buffer = ctypes.create_unicode_buffer(capacity)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(length)
            ):
                error = ctypes.get_last_error()
                return ForegroundObservation(
                    False,
                    False,
                    timestamp,
                    pid=pid_value.value,
                    error=f"QueryFullProcessImageNameW failed (WinError {error})",
                )
            executable = buffer.value
            active = ntpath.basename(executable).casefold() == self.target_basename
            return ForegroundObservation(
                active,
                True,
                timestamp,
                pid=pid_value.value,
                executable=executable,
            )
        finally:
            self._kernel32.CloseHandle(handle)


class ForegroundCache:
    def __init__(
        self,
        max_age_ms: int,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._max_age_seconds = max_age_ms / 1000.0
        self._clock = clock
        self._lock = threading.Lock()
        self._observation = ForegroundObservation(
            active=False,
            certain=False,
            timestamp=float("-inf"),
            error="foreground has not been sampled",
        )

    def publish(self, observation: ForegroundObservation) -> None:
        with self._lock:
            self._observation = observation

    def observation(self) -> ForegroundObservation:
        with self._lock:
            return self._observation

    def status(self) -> tuple[bool, bool]:
        observation = self.observation()
        fresh = self._clock() - observation.timestamp <= self._max_age_seconds
        confirmed = observation.certain and fresh
        return observation.active and confirmed, confirmed

    def is_confirmed_active(self) -> bool:
        return self.status()[0]


class ForegroundMonitor:
    def __init__(
        self,
        inspector: WindowsForegroundInspector,
        cache: ForegroundCache,
        shutdown_event: threading.Event,
        poll_ms: int,
        on_inactive: Callable[[bool], None],
    ) -> None:
        self._inspector = inspector
        self._cache = cache
        self._shutdown = shutdown_event
        self._poll_seconds = poll_ms / 1000.0
        self._on_inactive = on_inactive
        self._thread = threading.Thread(
            target=self._run, name="foreground-monitor", daemon=False
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        previous_active = False
        previous_certain = False
        while not self._shutdown.is_set():
            try:
                observation = self._inspector.inspect()
            except Exception as exc:  # Safety boundary around the polling thread.
                observation = ForegroundObservation(
                    False, False, time.perf_counter(), error=str(exc)
                )
            self._cache.publish(observation)
            if previous_active and not observation.active:
                self._on_inactive(not observation.certain)
            elif previous_certain and not observation.certain:
                self._on_inactive(True)
            previous_active = observation.active and observation.certain
            previous_certain = observation.certain
            self._shutdown.wait(self._poll_seconds)

