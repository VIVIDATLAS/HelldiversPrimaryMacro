from __future__ import annotations

import unittest

from helldivers_macro.timer_resolution import (
    TimerResolutionError,
    WindowsTimerResolution,
)


class Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeWinMM:
    def __init__(self, *, begin_result: int = 0, end_result: int = 0) -> None:
        self.begin_calls: list[int] = []
        self.end_calls: list[int] = []
        self.timeBeginPeriod = Function(self._begin)
        self.timeEndPeriod = Function(self._end)
        self._begin_result = begin_result
        self._end_result = end_result

    def _begin(self, period_ms: int) -> int:
        self.begin_calls.append(int(period_ms))
        return self._begin_result

    def _end(self, period_ms: int) -> int:
        self.end_calls.append(int(period_ms))
        return self._end_result


class TimerResolutionTests(unittest.TestCase):
    def test_nested_leases_balance_one_begin_and_one_end(self) -> None:
        winmm = FakeWinMM()
        resolution = WindowsTimerResolution(1, winmm=winmm)

        resolution.acquire()
        resolution.acquire()
        self.assertTrue(resolution.active)
        self.assertEqual(winmm.begin_calls, [1])
        resolution.release()
        self.assertTrue(resolution.active)
        self.assertEqual(winmm.end_calls, [])
        resolution.release()

        self.assertFalse(resolution.active)
        self.assertEqual(winmm.end_calls, [1])
        resolution.release()
        self.assertEqual(winmm.end_calls, [1])

    def test_context_manager_releases_after_exception(self) -> None:
        winmm = FakeWinMM()
        resolution = WindowsTimerResolution(1, winmm=winmm)

        with self.assertRaisesRegex(RuntimeError, "session failed"):
            with resolution:
                raise RuntimeError("session failed")

        self.assertEqual(winmm.begin_calls, [1])
        self.assertEqual(winmm.end_calls, [1])
        self.assertFalse(resolution.active)

    def test_failed_acquisition_is_reported_without_unbalanced_end(self) -> None:
        winmm = FakeWinMM(begin_result=97)
        resolution = WindowsTimerResolution(1, winmm=winmm)

        with self.assertRaisesRegex(TimerResolutionError, "MMRESULT 97"):
            resolution.acquire()
        resolution.release()

        self.assertEqual(winmm.begin_calls, [1])
        self.assertEqual(winmm.end_calls, [])
        self.assertFalse(resolution.active)

    def test_failed_release_is_reported_and_not_repeated_blindly(self) -> None:
        winmm = FakeWinMM(end_result=96)
        resolution = WindowsTimerResolution(1, winmm=winmm)
        resolution.acquire()

        with self.assertRaisesRegex(TimerResolutionError, "MMRESULT 96"):
            resolution.release()
        self.assertFalse(resolution.active)
        resolution.release()

        self.assertEqual(winmm.begin_calls, [1])
        self.assertEqual(winmm.end_calls, [1])


if __name__ == "__main__":
    unittest.main()
