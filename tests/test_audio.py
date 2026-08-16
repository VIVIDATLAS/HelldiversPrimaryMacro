from __future__ import annotations

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import logging
import threading
import time
import unittest
from unittest.mock import patch

from helldivers_macro import app
from helldivers_macro.audio import AudioNotifier, AudioPlaybackError
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

    def test_worker_exceptions_reach_test_mode_caller_after_drain(self) -> None:
        calls = []

        def failing_beep(frequency, duration):
            calls.append((frequency, duration))
            raise RuntimeError("fake background audio failure")

        notifier = AudioNotifier(CONFIG.audio, beep=failing_beep)
        notifier.start()
        notifier.notify_on()
        notifier.notify_off()
        with self.assertLogs("helldivers_macro.audio", logging.ERROR):
            with self.assertRaisesRegex(AudioPlaybackError, "fake background audio"):
                notifier.close(drain=True, raise_errors=True)
        self.assertEqual(calls, [(1000, 100), (500, 150)])

    def test_shutdown_sentinel_cannot_overtake_accepted_tones(self) -> None:
        calls = []
        entered = threading.Event()
        release = threading.Event()

        def controlled_beep(frequency, duration):
            calls.append((frequency, duration))
            if len(calls) == 1:
                entered.set()
                release.wait(1.0)

        notifier = AudioNotifier(CONFIG.audio, beep=controlled_beep)
        notifier.start()
        notifier.notify_on()
        self.assertTrue(entered.wait(0.5))
        notifier.notify_off()
        closer = threading.Thread(target=lambda: notifier.close(drain=True))
        closer.start()
        release.set()
        closer.join(1.0)
        self.assertFalse(closer.is_alive())
        self.assertEqual(calls, [(1000, 100), (500, 150)])

    def test_audio_command_reports_and_drains_both_tones(self) -> None:
        actions = []

        class FakeNotifier:
            def __init__(self, config):
                actions.append("constructed")

            def start(self):
                actions.append("started")

            def notify_on(self):
                actions.append("ON")

            def notify_off(self):
                actions.append("OFF")

            def close(self, *, drain, raise_errors):
                actions.append(("closed", drain, raise_errors))

        with patch.object(app, "ensure_windows_11_pro"), patch.object(
            app, "AudioNotifier", FakeNotifier
        ), redirect_stdout(StringIO()) as output:
            self.assertEqual(app.test_audio(CONFIG), 0)
        self.assertEqual(
            actions,
            ["constructed", "started", "ON", "OFF", ("closed", True, True)],
        )
        self.assertEqual(
            output.getvalue().splitlines(),
            ["Playing ON signal...", "Playing OFF signal...", "Audio test complete."],
        )


if __name__ == "__main__":
    unittest.main()
