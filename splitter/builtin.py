"""Built-in Spotify Connect receiver (the pure-Python `spconnect` library).

Adapts the fan-out to the receiver's Sink interface and gives the receiver the
same start/stop/running interface the engine uses for the librespot subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path

from spconnect import NullSink, Receiver, ReceiverConfig
from spconnect.audio import SAMPLE_RATE

from .config import AppConfig, bytes_per_second
from .fanout import FanOut

log = logging.getLogger(__name__)

_BYTES_PER_MS = bytes_per_second(SAMPLE_RATE) / 1000.0


class FanOutSink:
    """Feeds decoded audio to every speaker and reports how much is still queued."""

    def __init__(self, fan: FanOut) -> None:
        self._fan = fan
        self._fallback = NullSink()  # paces playback if there are no speakers

    def write(self, pcm: bytes) -> None:
        self._fan.write(pcm)
        self._fallback.write(pcm)

    def pending_ms(self) -> float:
        pending = self._fan.pending_bytes()
        if pending is None:
            return self._fallback.pending_ms()
        return pending / _BYTES_PER_MS

    def flush(self) -> None:
        self._fan.flush()
        self._fallback.flush()


class BuiltinSource:
    def __init__(self, cfg: AppConfig, fan: FanOut, state_path: Path | None) -> None:
        self._receiver = Receiver(
            ReceiverConfig(
                device_name=cfg.device_name,
                bitrate=cfg.bitrate,
                state_path=str(state_path) if state_path else None,
            ),
            FanOutSink(fan),
        )
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._receiver.start()
        self._running = True

    def stop(self) -> None:
        if self._running:
            self._running = False
            self._receiver.stop()

    def status(self) -> dict:
        return self._receiver.status()

    def control(self, action: str) -> None:
        {
            "play": self._receiver.play,
            "pause": self._receiver.pause,
            "next": self._receiver.next,
            "prev": self._receiver.prev,
        }[action]()
