"""Playback engine: decodes the current track and pushes PCM into a sink.

The player owns one thread. Commands (load, play, pause, seek, stop ...) are
queued to it; loading a track (network work) happens on worker threads so the
audio thread never blocks on the network.

Audio is pushed to a `Sink`. The sink reports how much audio it still holds, and
the player keeps that near TARGET_MS. This paces playback by the output
device's real consumption, so there is no clock drift between the two.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

import numpy as np

from .audio import SAMPLE_RATE, AudioLoader, LoadedTrack, Unavailable

log = logging.getLogger(__name__)

TARGET_MS = 250.0  # audio kept queued in the sink
BLOCK_FRAMES = 2048  # ~46 ms per decode step
PRELOAD_BEFORE_END_MS = 30_000
MAX_VOLUME = 65535


class Sink(Protocol):
    """Where decoded audio goes: signed 16-bit little-endian, stereo, 44.1 kHz."""

    def write(self, pcm: bytes) -> None: ...

    def pending_ms(self) -> float:
        """Milliseconds of written audio not yet played."""
        ...

    def flush(self) -> None:
        """Drop audio that was written but not yet played."""
        ...


class NullSink:
    """Discards audio at real-time speed. Useful for tests and headless runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._end = 0.0  # monotonic time at which queued audio runs out
        self.bytes_written = 0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            now = time.monotonic()
            self._end = max(self._end, now) + len(pcm) / (SAMPLE_RATE * 4)
            self.bytes_written += len(pcm)

    def pending_ms(self) -> float:
        with self._lock:
            return max(0.0, self._end - time.monotonic()) * 1000.0

    def flush(self) -> None:
        with self._lock:
            self._end = 0.0


def volume_to_gain(volume: int) -> float:
    """Map Spotify's linear 0..65535 volume to an audio gain with a 60 dB range."""
    if volume <= 0:
        return 0.0
    v = min(volume, MAX_VOLUME) / MAX_VOLUME
    return float(10 ** (-3.0 * (1.0 - v)))


# Event callback: (kind, play_id, data). Called on the player thread: keep it quick.
EventCallback = Callable[[str, int, dict], None]


class Player:
    def __init__(self, loader: AudioLoader, sink: Sink, on_event: EventCallback) -> None:
        self._loader = loader
        self._sink = sink
        self._on_event = on_event

        self._cmds: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="track-loader")
        self._id_lock = threading.Lock()
        self._next_id = 0

        # everything below is owned by the player thread
        self._play_id = 0
        self._track: LoadedTrack | None = None
        self._playing = False  # audio should be flowing
        self._loading = False
        self._want_play = False
        self._pending_seek: int | None = None
        self._ended_naturally = False
        self._preload_signalled = False
        self._preload: tuple[str, Future] | None = None
        self._gain = 1.0

        self._pos_lock = threading.Lock()
        self._pos_ms = 0  # last known audible position
        self._pos_at = time.monotonic()
        self._pos_running = False

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._thread.start()

    # ----- public API (any thread) -----------------------------------------

    def load(self, uri: str, start_playing: bool, position_ms: int = 0) -> int:
        """Load a track, replacing the current one. Returns the new play id."""
        with self._id_lock:
            self._next_id += 1
            play_id = self._next_id
        self._cmds.put(("load", play_id, uri, start_playing, max(0, int(position_ms))))
        return play_id

    def preload(self, uri: str) -> None:
        self._cmds.put(("preload", uri))

    def play(self) -> None:
        self._cmds.put(("play",))

    def pause(self) -> None:
        self._cmds.put(("pause",))

    def seek(self, position_ms: int) -> None:
        self._cmds.put(("seek", max(0, int(position_ms))))

    def stop(self) -> None:
        self._cmds.put(("stop",))

    def set_volume(self, volume: int) -> None:
        self._cmds.put(("volume", int(volume)))

    def position_ms(self) -> int:
        """Best estimate of the audible position."""
        with self._pos_lock:
            if self._pos_running:
                return self._pos_ms + int((time.monotonic() - self._pos_at) * 1000)
            return self._pos_ms

    def close(self) -> None:
        self._stop.set()
        self._cmds.put(("noop",))
        self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ----- player thread -------------------------------------------------------

    def _emit(self, kind: str, **data) -> None:
        try:
            self._on_event(kind, self._play_id, data)
        except Exception:
            log.exception("player event callback failed")

    def _set_position(self, position_ms: int, running: bool) -> None:
        with self._pos_lock:
            self._pos_ms = max(0, int(position_ms))
            self._pos_at = time.monotonic()
            self._pos_running = running

    def _audible_ms(self) -> int:
        if self._track is None:
            return 0
        return max(0, int(self._track.position_ms - self._sink.pending_ms()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cmd = self._cmds.get(timeout=0.01 if self._playing else 0.25)
            except queue.Empty:
                cmd = None
            try:
                if cmd is not None:
                    self._handle(cmd)
                if self._playing and self._track is not None:
                    self._pump()
            except Exception:
                log.exception("player thread error")
        self._drop_track()
        self._drop_preload()

    def _handle(self, cmd: tuple) -> None:
        name = cmd[0]
        if name == "load":
            self._cmd_load(*cmd[1:])
        elif name == "loaded":
            self._cmd_loaded(*cmd[1:])
        elif name == "preload":
            self._cmd_preload(cmd[1])
        elif name == "play":
            self._cmd_play()
        elif name == "pause":
            self._cmd_pause()
        elif name == "seek":
            self._cmd_seek(cmd[1])
        elif name == "stop":
            self._cmd_stop()
        elif name == "volume":
            self._gain = volume_to_gain(cmd[1])

    def _drop_track(self) -> None:
        if self._track is not None:
            self._track.close()
            self._track = None

    def _drop_preload(self) -> None:
        if self._preload is not None:
            _, future = self._preload
            self._preload = None
            future.add_done_callback(_close_result)

    def _cmd_load(self, play_id: int, uri: str, start_playing: bool, position_ms: int) -> None:
        gapless = self._ended_naturally and start_playing and position_ms == 0
        self._ended_naturally = False
        self._drop_track()
        if not gapless:
            self._sink.flush()
        self._play_id = play_id
        self._playing = False
        self._loading = True
        self._want_play = start_playing
        self._pending_seek = None
        self._preload_signalled = False
        self._set_position(position_ms, False)
        self._emit("loading", uri=uri, position_ms=position_ms)

        if self._preload is not None and self._preload[0] == uri:
            _, future = self._preload
            self._preload = None
        else:
            self._drop_preload()
            future = self._pool.submit(self._loader.load, uri, position_ms)
        future.add_done_callback(
            lambda f, pid=play_id, u=uri, pos=position_ms: self._cmds.put(("loaded", pid, u, pos, f))
        )

    def _cmd_loaded(self, play_id: int, uri: str, position_ms: int, future: Future) -> None:
        if play_id != self._play_id or not self._loading:
            future.add_done_callback(_close_result)  # superseded by a newer load
            return
        self._loading = False
        try:
            track: LoadedTrack = future.result()
        except Unavailable as exc:
            log.warning("cannot play %s: %s", uri, exc)
            self._emit("unavailable", uri=uri, reason=str(exc))
            return
        except Exception as exc:
            log.exception("loading %s failed", uri)
            self._emit("unavailable", uri=uri, reason=str(exc))
            return

        self._track = track
        target = self._pending_seek if self._pending_seek is not None else position_ms
        self._pending_seek = None
        # A preloaded track starts at zero; move it if another position was asked for.
        if abs(track.position_ms - target) > 50 and target < track.info.duration_ms:
            track.seek_ms(target)
        self._emit("track_info", info=track.info)
        self._playing = self._want_play
        position = track.position_ms
        self._set_position(position, self._playing)
        self._emit("playing" if self._playing else "paused", position_ms=position)

    def _cmd_preload(self, uri: str) -> None:
        if self._preload is not None and self._preload[0] == uri:
            return
        self._drop_preload()
        log.debug("preloading %s", uri)
        self._preload = (uri, self._pool.submit(self._loader.load, uri, 0))

    def _cmd_play(self) -> None:
        if self._loading:
            self._want_play = True
            return
        if self._track is None or self._playing:
            return
        self._playing = True
        position = self._track.position_ms
        self._set_position(position, True)
        self._emit("playing", position_ms=position)

    def _cmd_pause(self) -> None:
        if self._loading:
            self._want_play = False
            return
        if self._track is None or not self._playing:
            return
        # Stop at what is audible now: drop queued audio and rewind the decoder to it.
        position = self._audible_ms()
        self._sink.flush()
        self._playing = False
        position = self._track.seek_ms(position)
        self._set_position(position, False)
        self._emit("paused", position_ms=position)

    def _cmd_seek(self, position_ms: int) -> None:
        if self._loading:
            self._pending_seek = position_ms
            self._set_position(position_ms, False)
            return
        if self._track is None:
            return
        self._sink.flush()
        position = self._track.seek_ms(min(position_ms, max(0, self._track.info.duration_ms - 1)))
        self._preload_signalled = False
        self._set_position(position, self._playing)
        self._emit("seeked", position_ms=position)

    def _cmd_stop(self) -> None:
        was_active = self._track is not None or self._loading
        self._drop_track()
        self._drop_preload()
        self._sink.flush()
        self._playing = False
        self._loading = False
        self._ended_naturally = False
        self._set_position(0, False)
        if was_active:
            self._emit("stopped")

    def _pump(self) -> None:
        track = self._track
        assert track is not None
        for _ in range(8):  # bounded so commands stay responsive
            pending = self._sink.pending_ms()
            if pending >= TARGET_MS:
                break
            data = track.read(BLOCK_FRAMES)
            if data.shape[0] == 0:
                self._on_track_end(track)
                return
            gain = track.normalisation_gain * self._gain
            pcm = (np.clip(data * gain, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            if pending <= 0.0:
                self._sink.flush()  # ran dry: restart cleanly so outputs stay aligned
            self._sink.write(pcm)

        self._set_position(self._audible_ms(), True)
        if (
            not self._preload_signalled
            and track.info.duration_ms - track.position_ms < PRELOAD_BEFORE_END_MS
        ):
            self._preload_signalled = True
            self._emit("time_to_preload")

    def _on_track_end(self, track: LoadedTrack) -> None:
        error = track.error
        uri = track.info.uri
        self._drop_track()
        self._playing = False
        if error is not None:
            log.warning("playback of %s failed: %s", uri, error)
            self._sink.flush()
            self._set_position(0, False)
            self._emit("unavailable", uri=uri, reason=str(error))
            return
        # Leave the queued tail in the sink so the next track follows without a gap.
        self._ended_naturally = True
        self._emit("end_of_track", uri=uri)


def _close_result(future: Future) -> None:
    """Dispose of a loaded track nobody is going to play."""
    if future.cancelled():
        return
    try:
        track = future.result()
    except Exception:
        return
    track.close()
