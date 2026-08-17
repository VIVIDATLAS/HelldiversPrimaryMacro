from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


TIMERR_NOERROR = 0


class TimerResolutionError(RuntimeError):
    pass


class WindowsTimerResolution:
    """Reference-counted process lease for Windows multimedia timer resolution."""

    def __init__(self, period_ms: int = 1, *, winmm: object | None = None) -> None:
        if isinstance(period_ms, bool) or not isinstance(period_ms, int) or period_ms < 1:
            raise ValueError("timer period must be a positive integer")
        if winmm is None:
            if os.name != "nt":
                raise TimerResolutionError("Windows timer resolution requires Windows")
            winmm = ctypes.WinDLL("winmm", use_last_error=True)
        self._period_ms = period_ms
        self._winmm = winmm
        self._winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
        self._winmm.timeBeginPeriod.restype = wintypes.UINT
        self._winmm.timeEndPeriod.argtypes = [wintypes.UINT]
        self._winmm.timeEndPeriod.restype = wintypes.UINT
        self._lock = threading.Lock()
        self._leases = 0

    @property
    def active(self) -> bool:
        with self._lock:
            return self._leases > 0

    def acquire(self) -> None:
        with self._lock:
            if self._leases == 0:
                result = int(self._winmm.timeBeginPeriod(self._period_ms))
                if result != TIMERR_NOERROR:
                    raise TimerResolutionError(
                        f"timeBeginPeriod({self._period_ms}) failed with MMRESULT {result}"
                    )
            self._leases += 1

    def release(self) -> None:
        with self._lock:
            if self._leases == 0:
                return
            self._leases -= 1
            if self._leases != 0:
                return
            result = int(self._winmm.timeEndPeriod(self._period_ms))
            if result != TIMERR_NOERROR:
                raise TimerResolutionError(
                    f"timeEndPeriod({self._period_ms}) failed with MMRESULT {result}"
                )

    def __enter__(self) -> WindowsTimerResolution:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
