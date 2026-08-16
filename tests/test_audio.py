from __future__ import annotations

from pathlib import Path
import logging
import threading
import time
import unittest

from helldivers_macro.audio import AudioNotifier
from helldivers_macro.config import load_config


CONFIG = load_config(Path(__file__).resolve().parent.parent / "config.toml")


class AudioTests(unittest.TestCase):
    def test_on_off_are_fifo_and_exactly_once(self) -> None:
        calls = []
        notifier = AudioNotifier(CONFIG.audio, beep=lambda f, d: calls.append((f, d)))
        notifier.start()
        notifier.notify_on()
        notifier.notify_off()
        notifier.stop(drain=True)
        self.assertEqual(calls, [(1000, 100), (500, 150)])

    def test_startup_queues_no_sound(self) -> None:
        calls = []
        notifier = AudioNotifier(CONFIG.audio, beep=lambda f, d: calls.append((f, d)))
        notifier.start()
        notifier.stop(drain=True)
        self.assertEqual(calls, [])

    def test_beep_failure_is_logged_and_later_tone_survives(self) -> None:
        calls = []

        def beep(frequency, duration):
            calls.append((frequency, duration))
            if len(calls) == 1:
                raise OSError("speaker unavailable")

        notifier = AudioNotifier(CONFIG.audio, beep=beep)
        notifier.start()
        with self.assertLogs("helldivers_macro.audio", logging.ERROR):
            notifier.notify_on()
            notifier.notify_off()
            notifier.stop(drain=True)
        self.assertEqual(calls, [(1000, 100), (500, 150)])

    def test_slow_audio_backend_does_not_block_notification_caller(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_beep(frequency, duration):
            entered.set()
            release.wait(1.0)

        notifier = AudioNotifier(CONFIG.audio, beep=slow_beep)
        notifier.start()
        started = time.perf_counter()
        notifier.notify_on()
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertTrue(entered.wait(0.5))
        release.set()
        notifier.stop(drain=True)


if __name__ == "__main__":
    unittest.main()
