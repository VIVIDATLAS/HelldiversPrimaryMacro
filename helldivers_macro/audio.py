from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
import threading
from typing import Callable

from .config import AudioConfig, ToneConfig


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ToneRequest:
    sequence: int
    tone: ToneConfig
    label: str


class WinsoundBackend:
    def __call__(self, frequency_hz: int, duration_ms: int) -> None:
        import winsound

        winsound.Beep(frequency_hz, duration_ms)


class AudioNotifier:
    """FIFO, nonblocking transition notifications on a dedicated thread."""

    def __init__(
        self,
        config: AudioConfig,
        *,
        beep: Callable[[int, int], None] | None = None,
    ) -> None:
        self._config = config
        self._beep = beep or WinsoundBackend()
        self._queue: queue.Queue[_ToneRequest | None] = queue.Queue()
        self._sequence_lock = threading.Lock()
        self._next_sequence = 1
        self._last_played = 0
        self._started = False
        self._thread = threading.Thread(
            target=self._run, name="audio-notifications", daemon=False
        )

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def notify_on(self) -> None:
        self._enqueue(self._config.on, "ON")

    def notify_off(self) -> None:
        self._enqueue(self._config.off, "OFF")

    def _enqueue(self, tone: ToneConfig, label: str) -> None:
        if not self._started:
            raise RuntimeError("audio notifier has not been started")
        with self._sequence_lock:
            sequence = self._next_sequence
            self._next_sequence += 1
            self._queue.put_nowait(_ToneRequest(sequence, tone, label))

    def wait_until_idle(self) -> None:
        self._queue.join()

    def stop(self, *, drain: bool = True) -> None:
        if not self._started:
            return
        if drain:
            self._queue.join()
        self._queue.put(None)
        self._thread.join()
        self._started = False

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            try:
                if request is None:
                    return
                # A monotonically increasing FIFO sequence rejects any stale or
                # duplicated request without reordering transition sounds.
                if request.sequence <= self._last_played:
                    continue
                self._last_played = request.sequence
                try:
                    self._beep(request.tone.frequency_hz, request.tone.duration_ms)
                except Exception:
                    LOGGER.exception("winsound %s notification failed", request.label)
            finally:
                self._queue.task_done()

