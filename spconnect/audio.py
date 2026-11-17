"""Audio pipeline: metadata -> file selection -> key -> CDN download -> decrypt -> PCM.

Spotify stores each track as an AES-128-CTR encrypted file on a CDN. For Ogg
Vorbis the first 0xA7 bytes are a Spotify-specific header (it carries the
loudness normalisation values); a standard Ogg Vorbis stream follows.

Decoding uses libsndfile through the `soundfile` package, reading from a
file-like object that downloads and decrypts on demand.
"""

from __future__ import annotations

import io
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass

import numpy as np
import requests
import soundfile as sf

from .ap import AccessPoint, AudioKeyError
from .crypto import audio_decrypt
from .http import HttpError, SpClient
from .ids import SpotifyId
from .proto import audio_files_extension_pb2 as audio_files_pb
from .proto import extension_kind_pb2 as kind_pb
from .proto import metadata_pb2

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2

CHUNK_SIZE = 256 * 1024
SPOTIFY_OGG_HEADER_END = 0xA7
NORMALISATION_OFFSET = 144
READ_TIMEOUT = 30.0

_F = metadata_pb2.AudioFile
_VORBIS = {_F.OGG_VORBIS_96: 96, _F.OGG_VORBIS_160: 160, _F.OGG_VORBIS_320: 320}
_MP3 = {_F.MP3_96: 96, _F.MP3_160: 160, _F.MP3_256: 256, _F.MP3_320: 320}
# Preference order per configured bitrate (same order as librespot).
_FORMAT_ORDER = {
    96: [_F.OGG_VORBIS_96, _F.MP3_96, _F.OGG_VORBIS_160, _F.MP3_160, _F.MP3_256, _F.OGG_VORBIS_320, _F.MP3_320],
    160: [_F.OGG_VORBIS_160, _F.MP3_160, _F.OGG_VORBIS_96, _F.MP3_96, _F.MP3_256, _F.OGG_VORBIS_320, _F.MP3_320],
    320: [_F.OGG_VORBIS_320, _F.MP3_320, _F.MP3_256, _F.OGG_VORBIS_160, _F.MP3_160, _F.OGG_VORBIS_96, _F.MP3_96],
}

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class Unavailable(Exception):
    """The item cannot be played (restricted, no usable file, key refused ...)."""


class AudioFetchError(Exception):
    """Downloading the audio failed."""


# ----- CDN file -------------------------------------------------------------------


class CdnFile:
    """A remote file fetched in chunks by a background thread, readable at any offset."""

    def __init__(self, http: requests.Session, urls: list[str]) -> None:
        self._http = http
        self._urls = list(urls)
        self._cond = threading.Condition()
        self._closed = False
        self._error: Exception | None = None
        self._want = 0  # chunk index the reader is waiting for / will need next

        first, total = self._fetch(0, CHUNK_SIZE - 1)
        self.size = total
        count = max(1, -(-total // CHUNK_SIZE))
        self._chunks: list[bytes | None] = [None] * count
        self._chunks[0] = first[:CHUNK_SIZE]
        self._missing = count - 1
        self._thread = threading.Thread(target=self._download_loop, name="cdn-download", daemon=True)
        self._thread.start()

    def _fetch(self, start: int, end: int) -> tuple[bytes, int]:
        """GET a byte range. Returns (data, total_size). Tries every CDN url."""
        last: Exception | None = None
        for attempt in range(3):
            for url in self._urls:
                if self._closed:
                    raise AudioFetchError("file closed")
                try:
                    resp = self._http.get(
                        url, headers={"Range": f"bytes={start}-{end}"}, timeout=(5, 20)
                    )
                    if resp.status_code == 206:
                        m = _CONTENT_RANGE.match(resp.headers.get("Content-Range", ""))
                        if not m:
                            raise AudioFetchError("CDN reply has no valid Content-Range")
                        return resp.content, int(m.group(3))
                    if resp.status_code == 200:
                        data = resp.content
                        return data[start : end + 1], len(data)
                    last = HttpError(resp.status_code, "CDN request failed")
                except requests.RequestException as exc:
                    last = exc
                log.debug("CDN fetch of bytes %d-%d failed: %s", start, end, last)
            time.sleep(0.5 * (attempt + 1))
        raise AudioFetchError(f"could not download audio data: {last}")

    def _next_missing(self) -> int | None:
        n = len(self._chunks)
        start = min(self._want, n - 1)
        for i in list(range(start, n)) + list(range(0, start)):
            if self._chunks[i] is None:
                return i
        return None

    def _download_loop(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                idx = self._next_missing()
                if idx is None:
                    return
            start = idx * CHUNK_SIZE
            end = min(start + CHUNK_SIZE, self.size) - 1
            try:
                data, _ = self._fetch(start, end)
            except Exception as exc:
                with self._cond:
                    self._error = exc
                    self._cond.notify_all()
                if not self._closed:
                    log.warning("audio download failed: %s", exc)
                return
            with self._cond:
                if self._chunks[idx] is None:
                    self._chunks[idx] = data
                    self._missing -= 1
                self._cond.notify_all()

    @property
    def complete(self) -> bool:
        with self._cond:
            return self._missing == 0

    def read_at(self, offset: int, n: int) -> bytes:
        """Return up to n bytes at offset, blocking until they are downloaded."""
        if offset >= self.size or n <= 0:
            return b""
        n = min(n, self.size - offset)
        first, last = offset // CHUNK_SIZE, (offset + n - 1) // CHUNK_SIZE
        deadline = time.monotonic() + READ_TIMEOUT
        parts = []
        with self._cond:
            for idx in range(first, last + 1):
                while self._chunks[idx] is None:
                    if self._closed:
                        raise AudioFetchError("file closed")
                    if self._error is not None:
                        raise AudioFetchError(str(self._error))
                    self._want = idx
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AudioFetchError("timed out waiting for audio data")
                    self._cond.wait(remaining)
                parts.append(self._chunks[idx])
            self._want = min(last + 1, len(self._chunks) - 1)
        data = b"".join(parts)
        skip = offset - first * CHUNK_SIZE
        return data[skip : skip + n]

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


class DecryptedStream(io.RawIOBase):
    """File-like view of a CdnFile: decrypts on read and hides `base` leading bytes."""

    def __init__(self, source, key: bytes | None, base: int = 0) -> None:
        super().__init__()
        self._source = source
        self._key = key
        self._base = base
        self._size = max(0, source.size - base)
        self._pos = 0
        self.error: Exception | None = None  # set if a read failed

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence}")
        self._pos = max(0, pos)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        absolute = self._base + self._pos
        try:
            raw = self._source.read_at(absolute, min(n, self._size - self._pos))
        except Exception as exc:
            # The decoder calls us through C callbacks, where an exception cannot
            # propagate. Record it and report end-of-data; the player checks `error`.
            self.error = exc
            return b""
        data = audio_decrypt(self._key, raw, absolute) if self._key else raw
        self._pos += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


# ----- loaded track ---------------------------------------------------------------


@dataclass
class TrackInfo:
    uri: str  # uri that was requested
    name: str
    artists: list[str]
    album: str
    duration_ms: int
    is_episode: bool = False
    bitrate: int = 0
    codec: str = ""


class LoadedTrack:
    """A decodable track. Not thread-safe: use from one thread at a time."""

    def __init__(
        self,
        info: TrackInfo,
        source: CdnFile,
        decoder: sf.SoundFile,
        normalisation_gain: float,
        stream: DecryptedStream | None = None,
    ) -> None:
        self.info = info
        self._source = source
        self._decoder = decoder
        self._stream = stream
        self.normalisation_gain = normalisation_gain
        self.frames_total = decoder.frames
        self._position_frames = 0
        self._channels = decoder.channels
        self._closed = False

    @property
    def position_ms(self) -> int:
        return self._position_frames * 1000 // SAMPLE_RATE

    @property
    def error(self) -> Exception | None:
        """A download error that cut decoding short, if any."""
        return self._stream.error if self._stream is not None else None

    def seek_ms(self, position_ms: int) -> int:
        """Seek and return the actual position in ms."""
        frame = max(0, min(int(position_ms) * SAMPLE_RATE // 1000, max(0, self.frames_total - 1)))
        self._position_frames = self._decoder.seek(frame)
        return self.position_ms

    def read(self, frames: int) -> np.ndarray:
        """Decode up to `frames` frames as float32 stereo. Empty array at end of track."""
        data = self._decoder.read(frames, dtype="float32", always_2d=True)
        if data.shape[0] and self._channels == 1:
            data = np.repeat(data, 2, axis=1)
        self._position_frames += data.shape[0]
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._decoder.close()
        except Exception:
            pass
        self._source.close()


# ----- loader ---------------------------------------------------------------------


def _countries(codes: str) -> list[str]:
    return [codes[i : i + 2] for i in range(0, len(codes) - 1, 2)]


def is_restricted(restrictions, country: str, catalogue: str = "premium") -> bool:
    """Apply a metadata restriction list for the account's country and catalogue."""
    if len(country) != 2:
        return False
    for r in restrictions:
        if catalogue not in r.catalogue_str:
            continue
        which = r.WhichOneof("country_restriction")
        if which == "countries_allowed":
            return country not in _countries(r.countries_allowed)
        if which == "countries_forbidden":
            return country in _countries(r.countries_forbidden)
    return False


def select_file(files, bitrate: int):
    """Pick the best supported audio file for the preferred bitrate, or None."""
    by_format = {f.format: f for f in files if f.file_id}
    for fmt in _FORMAT_ORDER.get(bitrate, _FORMAT_ORDER[320]):
        if fmt in by_format:
            return by_format[fmt]
    return None


def normalisation_factor(gain_db: float, peak: float, pregain_db: float = 0.0) -> float:
    """ReplayGain-style factor: apply the gain but never exceed full scale."""
    factor = 10 ** ((gain_db + pregain_db) / 20.0)
    if peak > 0:
        factor = min(factor, 1.0 / peak)
    return min(factor, 1.0)


class AudioLoader:
    def __init__(
        self,
        spclient: SpClient,
        ap: AccessPoint,
        http: requests.Session,
        bitrate: int = 320,
        normalise: bool = True,
    ) -> None:
        self._sp = spclient
        self._ap = ap
        self._http = http
        self.bitrate = bitrate
        self.normalise = normalise

    def _catalogue(self) -> str:
        return self._ap.attributes.get("catalogue", "premium")

    def _audio_files_for(self, uri: str):
        resp = self._sp.extended_metadata(
            uri, kind_pb.AUDIO_FILES, audio_files_pb.AudioFilesExtensionResponse()
        )
        return [f.file for f in resp.files if f.HasField("file")]

    def _resolve_track(self, sid: SpotifyId):
        """Return (info, gid, files) for a track, following alternatives if restricted."""
        track = self._sp.extended_metadata(sid.uri, kind_pb.TRACK_V4, metadata_pb2.Track())
        country, catalogue = self._ap.country, self._catalogue()
        info = TrackInfo(
            uri=sid.uri,
            name=track.name,
            artists=[a.name for a in track.artist],
            album=track.album.name,
            duration_ms=max(0, track.duration),
        )
        for candidate in [track, *track.alternative]:
            if is_restricted(candidate.restriction, country, catalogue):
                continue
            gid = candidate.gid or sid.gid
            files = list(candidate.file)
            if not files:
                try:
                    files = self._audio_files_for(SpotifyId.from_gid("track", gid).uri)
                except (LookupError, HttpError) as exc:
                    log.debug("no audio files extension for %s: %s", sid.uri, exc)
            if files:
                return info, gid, files
        raise Unavailable(f"{sid.uri} is not available for this account or region")

    def _resolve_episode(self, sid: SpotifyId):
        episode = self._sp.extended_metadata(sid.uri, kind_pb.EPISODE_V4, metadata_pb2.Episode())
        if is_restricted(episode.restriction, self._ap.country, self._catalogue()):
            raise Unavailable(f"{sid.uri} is not available for this account or region")
        info = TrackInfo(
            uri=sid.uri,
            name=episode.name,
            artists=[episode.show.name] if episode.show.name else [],
            album=episode.show.name,
            duration_ms=max(0, episode.duration),
            is_episode=True,
        )
        return info, episode.gid or sid.gid, list(episode.audio)

    def load(self, uri: str, position_ms: int = 0) -> LoadedTrack:
        """Fetch everything needed to decode `uri`. Blocking; call off the audio thread."""
        try:
            sid = SpotifyId.from_uri(uri)
        except ValueError as exc:
            raise Unavailable(str(exc)) from exc
        try:
            if sid.kind == "track":
                info, gid, files = self._resolve_track(sid)
            elif sid.kind == "episode":
                info, gid, files = self._resolve_episode(sid)
            else:
                raise Unavailable(f"cannot play items of type {sid.kind}")
        except (LookupError, HttpError) as exc:
            raise Unavailable(f"no metadata for {uri}: {exc}") from exc

        chosen = select_file(files, self.bitrate)
        if chosen is None:
            raise Unavailable(f"{uri} has no file in a supported format")
        is_vorbis = chosen.format in _VORBIS
        info.bitrate = _VORBIS.get(chosen.format) or _MP3.get(chosen.format, 0)
        info.codec = "vorbis" if is_vorbis else "mp3"

        try:
            key = self._ap.request_audio_key(gid, chosen.file_id)
        except AudioKeyError as exc:
            raise Unavailable(f"no audio key for {uri}: {exc}") from exc
        try:
            urls = self._sp.resolve_audio_storage(chosen.file_id)
        except (LookupError, HttpError) as exc:
            raise Unavailable(f"no storage for {uri}: {exc}") from exc

        source = CdnFile(self._http, urls)
        try:
            gain = 1.0
            if is_vorbis and self.normalise:
                header = DecryptedStream(source, key)
                header.seek(NORMALISATION_OFFSET)
                raw = header.read(16)
                if len(raw) == 16:
                    track_gain, track_peak, _, _ = struct.unpack("<4f", raw)
                    gain = normalisation_factor(track_gain, track_peak)
            stream = DecryptedStream(source, key, SPOTIFY_OGG_HEADER_END if is_vorbis else 0)
            try:
                decoder = sf.SoundFile(stream)
            except Exception as exc:
                raise Unavailable(f"could not decode {uri}: {exc}") from exc
            if decoder.samplerate != SAMPLE_RATE or decoder.channels not in (1, 2):
                decoder.close()
                raise Unavailable(
                    f"unsupported audio format for {uri}: {decoder.samplerate} Hz, "
                    f"{decoder.channels} channels"
                )
            track = LoadedTrack(info, source, decoder, gain, stream)
            if not info.duration_ms:
                info.duration_ms = decoder.frames * 1000 // SAMPLE_RATE
            if 0 < position_ms < info.duration_ms:
                track.seek_ms(position_ms)
            log.info(
                "loaded %s - %s (%s %d kbps, %d s)",
                ", ".join(info.artists) or "?",
                info.name,
                info.codec,
                info.bitrate,
                info.duration_ms // 1000,
            )
            return track
        except BaseException:
            source.close()
            raise
