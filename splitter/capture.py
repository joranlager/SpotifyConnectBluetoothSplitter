"""Capture what Windows plays on its default output (WASAPI loopback).

A Spotify client that cannot hand us decoded audio (Soloist, the official desktop
app, ...) plays it on the default output device. We listen to that device's
loopback and fan the sound out to the speakers. Nothing is decrypted here: this
is ordinary system audio capture, like a mixer app does.

Windows only, through the `PyAudioWPatch` package.

The default output device must not also be one of the speakers, or the captured
sound would be played back into the device being captured.
"""

from __future__ import annotations

import logging
import sys
from array import array
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

FRAMES_PER_BUFFER = 1024


class CaptureError(RuntimeError):
    """Capture is unavailable or could not start."""


@dataclass(frozen=True)
class CaptureInfo:
    device_index: int
    name: str  # loopback device name, e.g. "Speakers (X) [Loopback]"
    rate: int
    channels: int

    @property
    def output_name(self) -> str:
        """Name of the playback device being captured (without the loopback suffix)."""
        return self.name.removesuffix(" [Loopback]")


def to_stereo(data: bytes, channels: int) -> bytes:
    """Convert interleaved little-endian int16 audio with any channel count to stereo."""
    if channels == 2 or not data:
        return data
    samples = array("h")
    samples.frombytes(data[: len(data) - len(data) % (2 * channels)])
    frames = len(samples) // channels
    out = array("h", [0]) * (frames * 2)
    if channels == 1:
        out[0::2] = samples[:frames]
        out[1::2] = samples[:frames]
    else:  # keep front left and right, drop the rest
        usable = frames * channels
        out[0::2] = samples[0:usable:channels]
        out[1::2] = samples[1:usable:channels]
    return out.tobytes()


def _import_pyaudio():
    if sys.platform != "win32":
        raise CaptureError("Loopback capture of the default output is only supported on Windows.")
    try:
        import pyaudiowpatch as pa
    except ImportError as exc:
        raise CaptureError("The PyAudioWPatch package is not installed. Run: pip install PyAudioWPatch") from exc
    return pa


def probe_loopback() -> CaptureInfo:
    """Find the loopback twin of the current Windows default output device."""
    pa = _import_pyaudio()
    p = pa.PyAudio()
    try:
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        except OSError as exc:
            raise CaptureError("WASAPI is not available on this system") from exc
        default = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if default.get("isLoopbackDevice"):
            loop = default
        else:
            loop = next(
                (lb for lb in p.get_loopback_device_info_generator() if default["name"] in lb["name"]),
                None,
            )
        if loop is None:
            raise CaptureError(f"no loopback device for the default output {default['name']!r}")
        channels = int(loop["maxInputChannels"])
        if channels < 1:
            raise CaptureError(f"the default output {default['name']!r} cannot be captured")
        return CaptureInfo(
            device_index=int(loop["index"]),
            name=str(loop["name"]),
            rate=int(loop["defaultSampleRate"]),
            channels=channels,
        )
    finally:
        p.terminate()


class LoopbackCapture:
    """Delivers the sound of the default output as stereo S16LE chunks."""

    def __init__(self, info: CaptureInfo, on_audio: Callable[[bytes], None]) -> None:
        self._info = info
        self._on_audio = on_audio
        self._pa = None
        self._stream = None

    @property
    def info(self) -> CaptureInfo:
        return self._info

    @property
    def active(self) -> bool:
        stream = self._stream
        try:
            return stream is not None and stream.is_active()
        except OSError:
            return False

    def start(self) -> None:
        pa = _import_pyaudio()
        info = self._info
        self._pa = pa.PyAudio()

        def callback(in_data, frame_count, time_info, status):
            try:
                self._on_audio(to_stereo(in_data, info.channels))
            except Exception:  # an exception here would stop the capture thread
                log.exception("capture callback failed")
            return (None, pa.paContinue)

        try:
            self._stream = self._pa.open(
                format=pa.paInt16,
                channels=info.channels,
                rate=info.rate,
                input=True,
                input_device_index=info.device_index,
                frames_per_buffer=FRAMES_PER_BUFFER,
                stream_callback=callback,
            )
        except Exception as exc:
            self.stop()
            raise CaptureError(f"could not capture {info.output_name!r}: {exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        pa, self._pa = self._pa, None
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception as exc:
            log.debug("error closing capture stream: %s", exc)
        finally:
            if pa is not None:
                pa.terminate()
