from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
import threading
from typing import Callable

from .config import AudioConfig, ToneConfig


LOGGER = logging.getLogger(__name__)


class AudioPlaybackError(RuntimeError):
    """Raised to the caller when requested audio diagnostics could not play."""


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
        self._beep = beep if beep is not None else WinsoundBackend()
        self._queue: queue.Queue[_ToneRequest | None] = queue.Queue()
        self._sequence_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._next_sequence = 1
        self._last_played = 0
        self._errors: list[BaseException] = []
        self._started = False
        self._accepting = False
        self._thread = threading.Thread(
            target=self._run, name="audio-notifications", daemon=False
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self._started = True
            self._accepting = True
            try:
                self._thread.start()
            except BaseException:
                self._started = False
                self._accepting = False
                raise

    def notify_on(self) -> None:
        self._enqueue(self._config.on, "ON")

    def notify_off(self) -> None:
        self._enqueue(self._config.off, "OFF")

    def _enqueue(self, tone: ToneConfig, label: str) -> None:
        # Keep acceptance and enqueue atomic relative to close(), so a shutdown
        # sentinel can never overtake an accepted tone.
        with self._lifecycle_lock:
            if not self._started or not self._accepting:
                raise RuntimeError("audio notifier is not accepting notifications")
            with self._sequence_lock:
                sequence = self._next_sequence
                self._next_sequence += 1
                self._queue.put_nowait(_ToneRequest(sequence, tone, label))

    def wait_until_idle(self, *, raise_errors: bool = False) -> None:
        self._queue.join()
        if raise_errors:
            self._raise_worker_errors()

    def close(self, *, drain: bool = True, raise_errors: bool = False) -> None:
        with self._lifecycle_lock:
            if not self._started:
                if raise_errors:
                    self._raise_worker_errors()
                return
            self._accepting = False
            # FIFO ordering guarantees that every notification accepted before
            # _accepting changed is ahead of this sentinel.
            self._queue.put_nowait(None)
        if drain:
            self._queue.join()
        self._thread.join()
        with self._lifecycle_lock:
            self._started = False
        if raise_errors:
            self._raise_worker_errors()

    def stop(self, *, drain: bool = True) -> None:
        """Compatibility wrapper used by the live application."""
        self.close(drain=drain, raise_errors=False)

    def _raise_worker_errors(self) -> None:
        with self._error_lock:
            errors = list(self._errors)
        if errors:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            raise AudioPlaybackError(f"audio playback failed: {summary}") from errors[0]

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
                except (RuntimeError, OSError) as exc:
                    with self._error_lock:
                        self._errors.append(exc)
                    LOGGER.exception("winsound %s notification failed", request.label)
                except Exception as exc:
                    with self._error_lock:
                        self._errors.append(exc)
                    LOGGER.exception("audio %s notification failed", request.label)
            finally:
                self._queue.task_done()
