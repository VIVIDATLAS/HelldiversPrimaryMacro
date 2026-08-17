from __future__ import annotations

import ctypes
from ctypes import wintypes
import unittest

from helldivers_macro.foreground import (
    ForegroundCache,
    ForegroundMonitor,
    PROCESS_QUERY_LIMITED_INFORMATION,
    WindowsForegroundInspector,
)
from helldivers_macro.models import ForegroundObservation


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeUser32:
    def __init__(self, hwnd=100, pid=42) -> None:
        self.GetForegroundWindow = Function(lambda: hwnd)

        def get_pid(window, pointer):
            ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0] = pid
            return 7

        self.GetWindowThreadProcessId = Function(get_pid)


class FakeKernel32:
    def __init__(self, executable=r"C:\Games\HELLDIVERS2.EXE") -> None:
        self.open_args = None
        self.closed = []

        def open_process(access, inherit, pid):
            self.open_args = (access, bool(inherit), pid)
            return 555

        def query(handle, flags, buffer, length_pointer):
            buffer.value = executable
            ctypes.cast(length_pointer, ctypes.POINTER(wintypes.DWORD))[0] = len(
                executable
            )
            return 1

        self.OpenProcess = Function(open_process)
        self.QueryFullProcessImageNameW = Function(query)
        self.CloseHandle = Function(lambda handle: self.closed.append(handle) or 1)


class FakeClock:
    def __init__(self, value=10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class SequenceInspector:
    def __init__(self, observations) -> None:
        self._observations = iter(observations)

    def inspect(self):
        return next(self._observations)


class CountedShutdown:
    def __init__(self, samples: int) -> None:
        self._samples = samples
        self._waits = 0

    def is_set(self) -> bool:
        return self._waits >= self._samples

    def wait(self, _seconds: float) -> bool:
        self._waits += 1
        return self.is_set()


class ForegroundTests(unittest.TestCase):
    def test_basename_match_is_case_insensitive_and_handle_closes(self) -> None:
        user = FakeUser32()
        kernel = FakeKernel32()
        inspector = WindowsForegroundInspector(
            "helldivers2.exe", user32=user, kernel32=kernel, clock=lambda: 3.0
        )
        result = inspector.inspect()
        self.assertTrue(result.active)
        self.assertTrue(result.certain)
        self.assertEqual(result.pid, 42)
        self.assertEqual(
            kernel.open_args, (PROCESS_QUERY_LIMITED_INFORMATION, False, 42)
        )
        self.assertEqual(kernel.closed, [555])

    def test_different_foreground_is_certain_inactive(self) -> None:
        inspector = WindowsForegroundInspector(
            "helldivers2.exe",
            user32=FakeUser32(),
            kernel32=FakeKernel32(r"C:\Windows\explorer.exe"),
        )
        result = inspector.inspect()
        self.assertFalse(result.active)
        self.assertTrue(result.certain)

    def test_no_window_is_uncertain(self) -> None:
        result = WindowsForegroundInspector(
            "helldivers2.exe",
            user32=FakeUser32(hwnd=0),
            kernel32=FakeKernel32(),
        ).inspect()
        self.assertFalse(result.active)
        self.assertFalse(result.certain)

    def test_cache_freshness(self) -> None:
        clock = FakeClock()
        cache = ForegroundCache(50, clock=clock)
        cache.publish(ForegroundObservation(True, True, clock.value))
        self.assertEqual(cache.status(), (True, True))
        clock.value += 0.051
        self.assertEqual(cache.status(), (False, False))
        self.assertFalse(cache.is_confirmed_active())

    def test_uncertain_observation_never_activates(self) -> None:
        clock = FakeClock()
        cache = ForegroundCache(50, clock=clock)
        cache.publish(ForegroundObservation(True, False, clock.value))
        self.assertEqual(cache.status(), (False, False))

    def test_monitor_publishes_first_confirmed_target_acquisition(self) -> None:
        clock = FakeClock()
        observations = [
            ForegroundObservation(False, True, clock.value, executable="powershell.exe"),
            ForegroundObservation(False, False, clock.value + 0.01, error="transient"),
            ForegroundObservation(True, True, clock.value + 0.02, executable="helldivers2.exe"),
        ]
        inactive: list[bool] = []
        active: list[bool] = []
        monitor = ForegroundMonitor(
            SequenceInspector(observations),
            ForegroundCache(50, clock=clock),
            CountedShutdown(len(observations)),
            1,
            inactive.append,
            lambda: active.append(True),
        )

        monitor._run()

        self.assertEqual(inactive, [True])
        self.assertEqual(active, [True])


if __name__ == "__main__":
    unittest.main()
