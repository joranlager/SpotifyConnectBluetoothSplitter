"""Bluetooth (or any) audio output devices, one realtime stream per speaker.

Requires the `sounddevice` package (bundles PortAudio). On Windows, paired
Bluetooth speakers appear as output devices under the "Windows WASAPI" host API;
that API is preferred when the same speaker is listed by several host APIs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from .config import CHANNELS, SAMPLE_RATE
from .fanout import RingBuffer

log = logging.getLogger(__name__)

PREFERRED_HOSTAPI = "Windows WASAPI"
BLOCKSIZE = 1024  # frames per callback (~23 ms at 44.1 kHz)


@dataclass(frozen=True)
class OutputInfo:
    index: int
    name: str
    hostapi: str
    max_channels: int


def refresh_devices() -> None:
    """Make PortAudio rescan the audio devices.

    PortAudio reads the device list once. When a Bluetooth speaker connects or
    disconnects, Windows renumbers the devices, and a stale list makes opening a
    stream fail with a confusing driver error. Only call this while no stream is
    open: it shuts PortAudio down and starts it again.
    """
    sd._terminate()  # noqa: SLF001 - sounddevice documents this pair for refreshing
    sd._initialize()  # noqa: SLF001


def list_outputs(refresh: bool = False) -> list[OutputInfo]:
    if refresh:
        refresh_devices()
    hostapis = sd.query_hostapis()
    result = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] <= 0:
            continue
        result.append(
            OutputInfo(
                index=idx,
                name=dev["name"],
                hostapi=hostapis[dev["hostapi"]]["name"],
                max_channels=dev["max_output_channels"],
            )
        )
    return result


def find_output(match: str, outputs: list[OutputInfo] | None = None) -> OutputInfo:
    """Find one output whose name contains `match` (case-insensitive).

    Raises LookupError if none match. If several match, prefers the WASAPI entry.
    """
    outputs = list_outputs() if outputs is None else outputs
    needle = match.lower()
    candidates = [o for o in outputs if needle in o.name.lower() and o.max_channels >= CHANNELS]
    if not candidates:
        raise LookupError(f"no output device matching {match!r}")
    preferred = [o for o in candidates if o.hostapi == PREFERRED_HOSTAPI]
    return (preferred or candidates)[0]


def scale_pcm(data: bytes, gain: float) -> bytes:
    """Scale little-endian int16 samples by gain (0.0-1.0), clipping safely."""
    if gain >= 1.0 or not data:
        return data
    if gain <= 0.0:
        return bytes(len(data))
    samples = np.frombuffer(data[: len(data) & ~1], dtype="<i2")
    return (samples * gain).astype("<i2").tobytes()


class SpeakerStream:
    """Pulls PCM from a RingBuffer and plays it on one output device.

    Underruns are played as silence so one slow or missing chunk never stops
    the other speakers. `volume` and `muted` can be changed while playing.
    """

    def __init__(
        self,
        name: str,
        device_index: int,
        buffer: RingBuffer,
        hostapi: str = "",
        samplerate: int = SAMPLE_RATE,
    ) -> None:
        self.name = name
        self.buffer = buffer
        self.volume = 1.0
        self.muted = False
        extra = None
        if hostapi == PREFERRED_HOSTAPI:
            # Shared-mode WASAPI only accepts the device's own mixer format (often
            # 48 kHz); auto_convert lets Windows resample our 44.1 kHz stream.
            extra = sd.WasapiSettings(auto_convert=True)
        self._stream = sd.RawOutputStream(
            device=device_index,
            extra_settings=extra,
            samplerate=samplerate,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCKSIZE,
            callback=self._callback,
        )

    @property
    def latency(self) -> float:
        """Reported output latency in seconds (may be approximate for Bluetooth)."""
        lat = self._stream.latency
        return float(lat if not isinstance(lat, tuple) else lat[1])

    def _callback(self, outdata, frames, time_info, status) -> None:
        need = len(outdata)
        data = self.buffer.read(need)
        n = len(data)
        if self.muted:
            data = bytes(n)
        elif self.volume < 1.0:
            data = scale_pcm(data, self.volume)
        outdata[:n] = data
        if n < need:
            outdata[n:] = bytes(need - n)

    @property
    def underruns(self) -> int:
        """Times this speaker ran out of audio in the middle of a stream."""
        return self.buffer.underruns

    def start(self) -> None:
        self._stream.start()

    def close(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()
