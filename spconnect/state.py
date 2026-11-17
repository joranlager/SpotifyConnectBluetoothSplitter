"""Connect state: what this device reports to Spotify and how it tracks its queue.

The whole state lives in one PutStateRequest protobuf (device info + player
state). This module keeps it consistent while tracks advance, contexts are
loaded, the queue is edited and shuffle/repeat are toggled.

The model follows librespot's (MIT): the player state exposes a window of up to
10 previous and 80 upcoming tracks, topped up from the resolved context.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
import uuid
from dataclasses import dataclass, field

from . import version
from .ids import SpotifyId
from .proto import connect_pb2
from .proto import context_page_pb2
from .proto import context_pb2
from .proto import devices_pb2
from .proto import media_pb2
from .proto import player_pb2
from .proto import transfer_state_pb2

log = logging.getLogger(__name__)

MAX_PREV_TRACKS = 10
MAX_NEXT_TRACKS = 80

PROVIDER_CONTEXT = "context"
PROVIDER_QUEUE = "queue"
PROVIDER_AUTOPLAY = "autoplay"
PROVIDER_UNAVAILABLE = "unavailable"

DELIMITER = "delimiter"

# metadata keys
M_CONTEXT_URI = "context_uri"
M_ENTITY_URI = "entity_uri"
M_IS_QUEUED = "is_queued"
M_IS_AUTOPLAY = "autoplay.is_autoplay"
M_HIDDEN = "hidden"
M_ITERATION = "iteration"
M_CONTEXT_INDEX = "context_index"

DEFAULT = "default"
AUTOPLAY = "autoplay"

LOCAL_FILES = "spotify:local-files"
SEARCH = "spotify:search"


class StateError(Exception):
    pass


# ----- play status -----------------------------------------------------------------


@dataclass
class PlayStatus:
    kind: str = "stopped"  # stopped | loading_play | loading_pause | playing | paused
    position_ms: int = 0  # for loading_* and paused
    nominal_start_time: int = 0  # for playing: wall-clock ms at which position was 0
    preload_triggered: bool = False

    @property
    def is_loading(self) -> bool:
        return self.kind in ("loading_play", "loading_pause")


# ----- helpers ----------------------------------------------------------------------


def copy_wire_compatible(src, dst):
    """Copy between two message types that share field numbers and types."""
    dst.Clear()
    dst.ParseFromString(src.SerializeToString())
    return dst


def copy_restrictions(src, dst: player_pb2.Restrictions) -> player_pb2.Restrictions:
    """Copy context restrictions into player restrictions (matching field names only)."""
    dst.Clear()
    dst_fields = {f.name: f for f in dst.DESCRIPTOR.fields}
    for f in src.DESCRIPTOR.fields:
        target = dst_fields.get(f.name)
        if target is None or f.message_type is not None or target.message_type is not None:
            continue
        if not f.is_repeated or not target.is_repeated:
            continue
        getattr(dst, f.name).extend(getattr(src, f.name))
    return dst


def is_queue(track: player_pb2.ProvidedTrack) -> bool:
    return track.provider == PROVIDER_QUEUE


def is_autoplay(track: player_pb2.ProvidedTrack) -> bool:
    return track.provider == PROVIDER_AUTOPLAY


def is_context(track: player_pb2.ProvidedTrack) -> bool:
    return track.provider == PROVIDER_CONTEXT


def is_unavailable(track: player_pb2.ProvidedTrack) -> bool:
    return track.provider == PROVIDER_UNAVAILABLE


def is_delimiter(track: player_pb2.ProvidedTrack) -> bool:
    return track.uid.startswith(DELIMITER)


def context_index_of(track: player_pb2.ProvidedTrack) -> int | None:
    value = track.metadata.get(M_CONTEXT_INDEX)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def page_url_to_uri(page_url: str) -> str:
    """hm://artistplaycontext/v1/page/spotify/album/<id>/km_artist -> spotify:album:<id>"""
    parts = page_url[len("hm://") :].split("/") if page_url.startswith("hm://") else page_url.split("/")
    try:
        start = parts.index("spotify")
    except ValueError:
        return ""
    return ":".join(parts[start : start + 3])


@dataclass
class StateContext:
    tracks: list = field(default_factory=list)  # list[ProvidedTrack]
    metadata: dict = field(default_factory=dict)
    restrictions: player_pb2.Restrictions | None = None
    index_track: int = 0  # next context position to load into next_tracks
    index_page: int = 0  # how often the context wrapped (repeat)
    # shuffle bookkeeping, so it can be undone
    _indices: list | None = None
    _first_pos: int | None = None
    initial_track: str | None = None

    def shuffle(self, seed: int, first_uri: str) -> None:
        if len(self.tracks) <= 1:
            return
        if self._indices is not None:
            self.unshuffle()
        rng = random.Random(seed)
        n = len(self.tracks)
        indices = [rng.randrange(0, i + 1) for i in range(n - 1, 0, -1)]
        for i, j in zip(range(n - 1, 0, -1), indices):
            self.tracks[i], self.tracks[j] = self.tracks[j], self.tracks[i]
        self._indices = indices
        self._first_pos = next((i for i, t in enumerate(self.tracks) if t.uri == first_uri), None)
        if self._first_pos is not None:
            self.tracks[0], self.tracks[self._first_pos] = self.tracks[self._first_pos], self.tracks[0]
        self.initial_track = first_uri

    def unshuffle(self) -> None:
        indices, self._indices = self._indices, None
        self.initial_track = None
        if indices is None:
            return
        if self._first_pos is not None:
            self.tracks[0], self.tracks[self._first_pos] = self.tracks[self._first_pos], self.tracks[0]
            self._first_pos = None
        n = len(self.tracks)
        if len(indices) != n - 1:
            return  # the list changed size since shuffling; leave it as it is
        for i in range(1, n):
            j = indices[n - i - 1]
            self.tracks[i], self.tracks[j] = self.tracks[j], self.tracks[i]


# ----- connect state ------------------------------------------------------------------


class ConnectState:
    def __init__(
        self,
        device_name: str,
        device_id: str,
        session_id: str,
        device_type: int = devices_pb2.SPEAKER,
        initial_volume: int = 32768,
        volume_steps: int = 64,
    ) -> None:
        self.request = connect_pb2.PutStateRequest()
        self.request.member_type = connect_pb2.CONNECT_STATE
        self.request.put_state_reason = connect_pb2.PLAYER_STATE_CHANGED

        info = self.request.device.device_info
        info.can_play = True
        info.volume = initial_volume
        info.name = device_name
        info.device_id = device_id
        info.device_type = device_type
        info.device_software_version = version.VERSION_STRING
        info.spirc_version = version.SPIRC_VERSION
        info.client_id = version.CLIENT_ID
        caps = info.capabilities
        caps.volume_steps = volume_steps
        caps.gaia_eq_connect_id = True
        caps.can_be_player = True
        caps.needs_full_player_state = True
        caps.is_observable = True
        caps.is_controllable = True
        caps.supports_gzip_pushes = True
        caps.supports_logout = False
        caps.supported_types.extend(["audio/episode", "audio/track"])
        caps.supports_playlist_v2 = True
        caps.supports_transfer_command = True
        caps.supports_command_request = True
        caps.supports_set_options_command = True
        caps.supported_audio_quality = media_pb2.VERY_HIGH
        caps.command_acks = True

        self.request.device.player_state.session_id = session_id

        self.unavailable_uris: list[str] = []
        self.active_since: float | None = None
        self.queue_count = 0
        self.active_context = DEFAULT
        self.fill_up_context = DEFAULT
        self.context: StateContext | None = None
        self.autoplay_context: StateContext | None = None
        self.transfer_shuffle_seed: int | None = None
        self.volume_step_size = max(1, 65535 // max(1, volume_steps))
        self.reset()

    # ----- basic accessors --------------------------------------------------

    @property
    def player(self) -> player_pb2.PlayerState:
        return self.request.device.player_state

    @property
    def device_info(self) -> connect_pb2.DeviceInfo:
        return self.request.device.device_info

    @property
    def is_active(self) -> bool:
        return self.request.is_active

    @property
    def context_uri(self) -> str:
        return self.player.context_uri

    @property
    def has_track(self) -> bool:
        return self.player.HasField("track")

    @property
    def current_uri(self) -> str:
        return self.player.track.uri if self.has_track else ""

    def is_playing(self) -> bool:
        return self.player.is_playing and not self.player.is_paused

    def is_pause(self) -> bool:
        p = self.player
        return p.is_playing and p.is_paused and p.is_buffering

    def reset(self) -> None:
        self.set_active(False)
        self.queue_count = 0
        session_id = self.player.session_id
        p = self.player
        p.Clear()
        p.session_id = session_id
        p.is_system_initiated = True
        p.playback_speed = 1.0
        p.play_origin.SetInParent()
        p.suppressions.SetInParent()
        p.options.SetInParent()

    def set_volume(self, volume: int) -> None:
        self.device_info.volume = max(0, min(65535, int(volume)))

    def set_last_command(self, message_id: int, sent_by: str) -> None:
        self.request.last_command_message_id = message_id
        self.request.last_command_sent_by_device_id = sent_by

    def set_now(self, now_ms: int) -> None:
        self.request.client_side_timestamp = now_ms
        if self.active_since is not None:
            self.request.started_playing_at = int(self.active_since * 1000)

    def set_active(self, value: bool) -> None:
        if value:
            if self.request.is_active:
                return
            self.request.is_active = True
            self.active_since = time.time()
        else:
            self.request.is_active = False
            self.active_since = None

    def set_session_id(self, session_id: str) -> None:
        self.player.session_id = session_id

    def set_status(self, status: PlayStatus) -> None:
        p = self.player
        p.is_paused = status.kind in ("loading_pause", "paused", "stopped")
        p.playback_speed = 0.0 if p.is_paused else 1.0
        # Desktop and mobile need all three flags set while paused, otherwise the
        # play button is greyed out.
        p.is_buffering = p.is_paused or status.is_loading
        p.is_playing = p.is_paused or status.kind in ("loading_play", "playing")
        self.update_restrictions()

    def update_position(self, position_ms: int, timestamp_ms: int) -> None:
        self.player.position_as_of_timestamp = max(0, int(position_ms))
        self.player.timestamp = int(timestamp_ms)

    def update_position_in_relation(self, timestamp_ms: int) -> None:
        p = self.player
        p.position_as_of_timestamp += int(timestamp_ms) - p.timestamp
        if p.position_as_of_timestamp < 0:
            p.position_as_of_timestamp = 0
        p.timestamp = int(timestamp_ms)

    def update_duration(self, duration_ms: int) -> None:
        self.player.duration = int(duration_ms)

    def update_queue_revision(self) -> None:
        digest = hashlib.sha1()
        for t in self.player.next_tracks:
            digest.update(t.uri.encode())
            digest.update(b"\0")
        self.player.queue_revision = str(int.from_bytes(digest.digest()[:8], "big"))

    def _set_index_track(self, value: int | None) -> None:
        if value is None:
            self.player.ClearField("index")
        else:
            self.player.index.track = max(0, int(value))

    # ----- contexts -------------------------------------------------------------

    def get_context(self, kind: str) -> StateContext:
        ctx = self.context if kind == DEFAULT else self.autoplay_context
        if ctx is None:
            raise StateError(f"no {kind} context")
        return ctx

    @staticmethod
    def find_index(ctx: StateContext, predicate) -> int | None:
        for i, t in enumerate(ctx.tracks):
            if predicate(t):
                return i
        return None

    @staticmethod
    def valid_resolve_uri(uri: str) -> str | None:
        if not uri or uri.startswith(SEARCH):
            return None
        return uri

    def reset_context(self, mode: str, new_uri: str | None = None) -> None:
        """mode: 'completely' | 'default_index' | 'when_different' (with new_uri)."""
        if mode == "when_different":
            different = self.context_uri != (new_uri or "") or (new_uri or "").startswith(SEARCH)
            if different:
                mode = "completely"
        if self.context is not None:
            self.context.unshuffle()
        if mode == "completely":
            self.context = None
            self.autoplay_context = None
            self.player.context_uri = ""
            self.player.context_url = ""
        elif mode == "default_index":
            for ctx in (self.context, self.autoplay_context):
                if ctx is not None:
                    ctx.index_track = 0
                    ctx.index_page = 0
        self.fill_up_context = DEFAULT
        self.set_active_context(DEFAULT)
        self.update_restrictions()

    def set_active_context(self, kind: str) -> None:
        self.active_context = kind
        p = self.player
        p.context_metadata.clear()
        p.context_restrictions.Clear()
        p.context_restrictions.SetInParent()
        p.restrictions.Clear()
        p.restrictions.SetInParent()
        ctx = self.context if kind == DEFAULT else self.autoplay_context
        if ctx is None:
            return
        if ctx.restrictions is not None:
            p.restrictions.CopyFrom(ctx.restrictions)
        for k, v in ctx.metadata.items():
            p.context_metadata[k] = v

    def context_to_provided_track(
        self,
        ctx_track,
        context_uri: str | None,
        context_index: int | None = None,
        page_metadata: dict | None = None,
        provider: str | None = None,
    ) -> player_pb2.ProvidedTrack:
        uri = ctx_track.uri if ctx_track.HasField("uri") else ""
        gid = ctx_track.gid if ctx_track.HasField("gid") else b""
        if uri:
            if "?" in uri:
                raise StateError(f"track uri contains invalid characters: {uri}")
        elif len(gid) == 16:
            uri = SpotifyId.from_gid("track", gid).uri
        else:
            raise StateError("context track has neither uri nor gid")
        uri = uri.replace("spotify:unknown:", "spotify:track:")

        track = player_pb2.ProvidedTrack()
        track.uri = uri
        track.uid = ctx_track.uid if ctx_track.uid else uuid.uuid4().hex
        for k, v in (page_metadata or {}).items():
            track.metadata[k] = v
        for k, v in ctx_track.metadata.items():
            track.metadata[k] = v
        if uri in self.unavailable_uris:
            track.provider = PROVIDER_UNAVAILABLE
        else:
            track.provider = provider or PROVIDER_CONTEXT
        if context_uri:
            track.metadata[M_ENTITY_URI] = context_uri
            track.metadata[M_CONTEXT_URI] = context_uri
        if context_index is not None:
            track.metadata[M_CONTEXT_INDEX] = str(context_index)
        if track.provider == PROVIDER_AUTOPLAY:
            track.metadata[M_IS_AUTOPLAY] = "true"
        return track

    def _context_from_page(
        self,
        page,
        metadata: dict,
        restrictions,
        new_context_uri: str | None,
        context_length: int | None,
        provider: str | None,
    ) -> StateContext:
        context_uri = new_context_uri or self.context_uri
        tracks = []
        for i, t in enumerate(page.tracks):
            try:
                tracks.append(
                    self.context_to_provided_track(
                        t,
                        context_uri,
                        context_length + i if context_length is not None else None,
                        dict(page.metadata),
                        provider,
                    )
                )
            except StateError as exc:
                log.debug("skipping context track: %s", exc)
        ctx = StateContext(tracks=tracks, metadata=dict(metadata))
        if restrictions is not None:
            ctx.restrictions = copy_restrictions(restrictions, player_pb2.Restrictions())
        return ctx

    def update_context(self, context: context_pb2.Context, kind: str) -> list[str]:
        """Install a resolved context. Returns uris of further pages still to resolve."""
        if all(len(p.tracks) == 0 for p in context.pages):
            raise StateError("context has no tracks")
        if context.uri.startswith(LOCAL_FILES):
            raise StateError("playback of local files is not supported")

        pages = list(context.pages)
        first = next(p for p in pages if len(p.tracks) > 0)
        rest = [p for p in pages if p is not first]
        restrictions = context.restrictions if context.HasField("restrictions") else None
        uri = context.uri if context.HasField("uri") else None

        if kind == DEFAULT:
            new_ctx = self._context_from_page(first, dict(context.metadata), restrictions, uri, 0, None)
            # Updating the context we are already playing: keep our place in it.
            if not self.context_uri.startswith(SEARCH) and uri is not None and uri == self.context_uri:
                new_index = self._last_index_in_new_context(new_ctx)
                if new_index is not None:
                    found, value = new_index
                    if not found:
                        self._set_index_track(None)
                    new_ctx.index_track = value
                    if self.autoplay_context is not None:
                        self.autoplay_context.index_track = 0
                    self.clear_next_tracks()
            self.context = new_ctx
            url = context.url if context.HasField("url") else ""
            self.player.context_url = "" if SEARCH in url else url
            self.player.context_uri = uri or ""
        else:
            self.autoplay_context = self._context_from_page(
                first, dict(context.metadata), restrictions, uri, None, PROVIDER_AUTOPLAY
            )

        remaining: list[str] = []
        for page in rest:
            if len(page.tracks) > 0:
                if kind == DEFAULT:
                    self.fill_context_from_page(page)
            elif page.page_url:
                page_uri = page_url_to_uri(page.page_url)
                if page_uri:
                    remaining.append(page_uri)
        return remaining

    def _last_index_in_new_context(self, new_ctx: StateContext) -> tuple[bool, int] | None:
        ctx = self.context
        if ctx is None:
            return None
        current = self.player.track if self.has_track else None
        queued_item = current is not None and (
            is_queue(current) or current.metadata.get(M_IS_QUEUED) == "true"
        )
        index: int | None
        if ctx.index_track >= MAX_NEXT_TRACKS:
            index = ctx.index_track - MAX_NEXT_TRACKS
        elif queued_item:
            index = None
            for prev in reversed(self.player.prev_tracks):
                index = self.find_index(new_ctx, lambda t, u=prev.uri: t.uri == u)
                if index is not None:
                    break
        elif current is not None:
            index = self.find_index(new_ctx, lambda t: t.uri == current.uri)
        else:
            index = None
        if index is not None:
            return True, index + 1
        fallback = self.player.index.track if self.player.HasField("index") else 0
        return False, fallback

    def fill_context_from_page(self, page: context_page_pb2.ContextPage) -> None:
        if self.context is None:
            raise StateError("no default context")
        length = len(self.context.tracks)
        extra = self._context_from_page(page, {}, None, None, length, None)
        self.context.tracks.extend(extra.tracks)

    def merge_context(self, page) -> None:
        """Take uids/metadata from a play command's inline page into the resolved context."""
        if page is None or self.context is None:
            return
        by_uri = {}
        for t in self.context.tracks:
            by_uri.setdefault(t.uri, t)
        for new in page.tracks:
            if not new.uri:
                continue
            target = by_uri.get(new.uri)
            if target is None:
                continue
            for k, v in new.metadata.items():
                target.metadata[k] = v
            if new.uid:
                target.uid = new.uid

    def is_skip_track(self, track, iteration: int | None) -> bool:
        ctx = self.context if self.active_context == DEFAULT else self.autoplay_context
        if ctx is None or ctx.initial_track is None or ctx.initial_track != track.uri:
            return False
        return iteration is None or iteration == 0

    # ----- track window ---------------------------------------------------------------

    @staticmethod
    def _new_delimiter(iteration: int) -> player_pb2.ProvidedTrack:
        d = player_pb2.ProvidedTrack()
        d.uri = f"spotify:{DELIMITER}"
        d.uid = f"{DELIMITER}{iteration}"
        d.provider = PROVIDER_CONTEXT
        d.metadata[M_HIDDEN] = "true"
        d.metadata[M_ITERATION] = str(iteration)
        return d

    def _push_prev(self, track) -> None:
        prev = self.player.prev_tracks
        if len(prev) >= MAX_PREV_TRACKS:
            del prev[0]
        prev.add().CopyFrom(track)

    def set_track(self, track) -> None:
        self.player.track.CopyFrom(track)

    def set_current_track(self, index: int) -> None:
        ctx = self.get_context(self.active_context)
        if not 0 <= index < len(ctx.tracks):
            raise StateError(f"cannot find track {index} in a context of {len(ctx.tracks)}")
        self.set_track(ctx.tracks[index])
        self._set_index_track(index)

    def set_current_track_random(self) -> None:
        ctx = self.get_context(self.active_context)
        if not ctx.tracks:
            raise StateError("context has no tracks")
        self.set_current_track(random.randrange(len(ctx.tracks)))

    def reset_playback_to_position(self, new_index: int | None) -> None:
        index = new_index or 0
        self._set_index_track(index)
        self.get_context(self.active_context).index_track = index + 1
        self.fill_up_context = self.active_context

        current = self.player.track if self.has_track else None
        if current is None or not (is_queue(current) or self.is_skip_track(current, None)):
            self.set_current_track(index)

        del self.player.prev_tracks[:]
        if index > 0:
            ctx = self.get_context(self.active_context)
            start = max(0, index - MAX_PREV_TRACKS)
            for t in ctx.tracks[start:index]:
                self.player.prev_tracks.add().CopyFrom(t)

        self.clear_next_tracks()
        self.fill_up_next_tracks()
        self.update_restrictions()

    def clear_prev_tracks(self) -> None:
        del self.player.prev_tracks[:]

    def clear_next_tracks(self) -> None:
        """Drop upcoming context tracks but keep queued ones at the front."""
        nxt = self.player.next_tracks
        first_non_queued = next((i for i, t in enumerate(nxt) if not is_queue(t)), None)
        if first_non_queued is not None:
            del nxt[first_non_queued:]

    def fill_up_next_tracks(self) -> None:
        try:
            ctx = self.get_context(self.fill_up_context)
        except StateError:
            return
        new_index = ctx.index_track
        iteration = ctx.index_page
        nxt = self.player.next_tracks
        repeat = self.repeat_context

        guard = 0
        while len(nxt) < MAX_NEXT_TRACKS:
            guard += 1
            if guard > 100_000:
                break
            ctx = self.get_context(self.fill_up_context)
            if new_index < len(ctx.tracks):
                candidate = ctx.tracks[new_index]
                new_index += 1
                if is_unavailable(candidate) or self.is_skip_track(candidate, iteration):
                    continue
                nxt.add().CopyFrom(candidate)
                continue
            # ran past the end of the fill-up context
            if repeat:
                if not any(not is_unavailable(t) for t in ctx.tracks):
                    break
                nxt.add().CopyFrom(self._new_delimiter(iteration))
                iteration += 1
                new_index = 0
            elif self.fill_up_context != AUTOPLAY and self.autoplay_context is not None:
                ctx.index_track = new_index
                self.fill_up_context = AUTOPLAY
                new_index = self.autoplay_context.index_track
                nxt.add().CopyFrom(self._new_delimiter(iteration))
            else:
                break

        ctx = self.get_context(self.fill_up_context)
        ctx.index_track = new_index
        if self.fill_up_context == DEFAULT:
            ctx.index_page = iteration
        self.update_queue_revision()

    def next_track(self) -> bool:
        """Advance to the next track. Returns False when nothing is left."""
        if self.repeat_track:
            self.set_repeat_track(False)

        if self.has_track:
            old = player_pb2.ProvidedTrack()
            old.CopyFrom(self.player.track)
            self.player.ClearField("track")
            if is_context(old) or is_autoplay(old):
                self._push_prev(old)

        nxt = self.player.next_tracks
        new_track = None
        while len(nxt) > 0:
            candidate = player_pb2.ProvidedTrack()
            candidate.CopyFrom(nxt[0])
            del nxt[0]
            if is_delimiter(candidate):
                self._push_prev(candidate)
                continue
            if is_unavailable(candidate):
                continue
            new_track = candidate
            break
        if new_track is None:
            return False

        self.fill_up_next_tracks()

        if is_queue(new_track):
            self._set_index_track(None)
        elif is_autoplay(new_track):
            self.set_active_context(AUTOPLAY)
            self._set_index_track(None)
        else:
            self._set_index_track(context_index_of(new_track))

        self.set_track(new_track)
        self.update_restrictions()
        return True

    def prev_track(self) -> bool:
        """Go back one track. Returns False when there is no previous track."""
        if self.has_track:
            old = player_pb2.ProvidedTrack()
            old.CopyFrom(self.player.track)
            self.player.ClearField("track")
            if is_context(old) or is_autoplay(old):
                self._insert_next(0, old)

        prev = self.player.prev_tracks
        if len(prev) > 0 and is_delimiter(prev[-1]):
            delimiter = player_pb2.ProvidedTrack()
            delimiter.CopyFrom(prev[-1])
            del prev[-1]
            self._insert_next(0, delimiter)

        nxt = self.player.next_tracks
        while len(nxt) > MAX_NEXT_TRACKS:
            del nxt[-1]

        if len(prev) == 0:
            return False
        new_track = player_pb2.ProvidedTrack()
        new_track.CopyFrom(prev[-1])
        del prev[-1]

        if self.active_context == AUTOPLAY and is_context(new_track):
            self.set_active_context(DEFAULT)

        self.fill_up_next_tracks()
        self.set_track(new_track)
        if self.player.HasField("index") and self.player.index.track > 0:
            self.player.index.track -= 1
        self.update_restrictions()
        return True

    def _insert_next(self, position: int, track) -> None:
        nxt = self.player.next_tracks
        items = [player_pb2.ProvidedTrack() for _ in nxt]
        for dst, src in zip(items, nxt):
            dst.CopyFrom(src)
        copy = player_pb2.ProvidedTrack()
        copy.CopyFrom(track)
        items.insert(position, copy)
        del nxt[:]
        nxt.extend(items)

    def set_next_tracks(self, tracks: list) -> None:
        current_uri = self.current_uri
        for t in tracks:
            if t.metadata.get(M_IS_QUEUED) == "true":
                t.provider = PROVIDER_QUEUE
                t.uid = f"q{self.queue_count}"
                self.queue_count += 1
            if not t.uri:
                t.uri = current_uri
        del self.player.next_tracks[:]
        self.player.next_tracks.extend(tracks)

    def set_prev_tracks(self, tracks: list) -> None:
        del self.player.prev_tracks[:]
        self.player.prev_tracks.extend(tracks)

    def add_to_queue(self, track, update_revision: bool = True) -> None:
        t = player_pb2.ProvidedTrack()
        t.CopyFrom(track)
        t.uid = f"q{self.queue_count}"
        self.queue_count += 1
        t.provider = PROVIDER_QUEUE
        t.metadata[M_IS_QUEUED] = "true"
        nxt = self.player.next_tracks
        position = next((i for i, x in enumerate(nxt) if not is_queue(x)), len(nxt))
        self._insert_next(position, t)
        while len(nxt) > MAX_NEXT_TRACKS:
            del nxt[-1]
        if update_revision:
            self.update_queue_revision()
        self.update_restrictions()

    def preview_next_uri(self) -> str | None:
        if self.repeat_track:
            return self.current_uri or None
        for t in self.player.next_tracks:
            if is_delimiter(t) or is_unavailable(t):
                continue
            return t.uri
        return None

    def has_next_tracks(self, minimum: int = 1) -> bool:
        return len(self.player.next_tracks) >= minimum

    def recent_track_uris(self) -> list[str]:
        uris = [t.uri for t in self.player.prev_tracks if not is_delimiter(t)]
        if self.current_uri:
            uris.append(self.current_uri)
        return uris

    def mark_unavailable(self, uri: str) -> None:
        for field_name in ("next_tracks", "prev_tracks"):
            items = getattr(self.player, field_name)
            keep = [t for t in items if t.uri != uri]
            if len(keep) != len(items):
                copies = [player_pb2.ProvidedTrack() for _ in keep]
                for dst, src in zip(copies, keep):
                    dst.CopyFrom(src)
                del items[:]
                items.extend(copies)
        for ctx in (self.context, self.autoplay_context):
            if ctx is not None:
                for t in ctx.tracks:
                    if t.uri == uri:
                        t.provider = PROVIDER_UNAVAILABLE
        if uri not in self.unavailable_uris:
            self.unavailable_uris.append(uri)
        self.fill_up_next_tracks()

    # ----- options --------------------------------------------------------------------

    @property
    def shuffling_context(self) -> bool:
        return self.player.options.shuffling_context

    @property
    def repeat_context(self) -> bool:
        return self.player.options.repeating_context

    @property
    def repeat_track(self) -> bool:
        return self.player.options.repeating_track

    def set_repeat_context(self, value: bool) -> None:
        self.player.options.repeating_context = bool(value)

    def set_repeat_track(self, value: bool) -> None:
        self.player.options.repeating_track = bool(value)

    def set_shuffle(self, value: bool) -> None:
        self.player.options.shuffling_context = bool(value)

    def reset_options(self) -> None:
        self.set_shuffle(False)
        self.set_repeat_track(False)
        self.set_repeat_context(False)

    def _shuffle_allowed(self) -> None:
        reasons = self.player.restrictions.disallow_toggling_shuffle_reasons
        if len(reasons) > 0:
            raise StateError(f"shuffle is not allowed: {reasons[0]}")

    def shuffle_new(self) -> None:
        self._shuffle_allowed()
        self._shuffle(random.randrange(100_000_000_000, 1_000_000_000_000), self.current_uri)

    def _shuffle(self, seed: int, initial_track: str) -> None:
        self.clear_prev_tracks()
        self.clear_next_tracks()
        self.reset_context("default_index")
        ctx = self.get_context(DEFAULT)
        ctx.shuffle(seed, initial_track)
        self.fill_up_next_tracks()

    def handle_shuffle(self, shuffle: bool) -> None:
        self.set_shuffle(shuffle)
        if shuffle:
            self.shuffle_new()
            return
        self.reset_context("default_index")
        if not self.has_track:
            return
        ctx = self.get_context(DEFAULT)
        # The context is back in its original order, so the recorded index is exact
        # (and, unlike a search by uri, right for a track that appears twice).
        index = context_index_of(self.player.track)
        if index is None or not (0 <= index < len(ctx.tracks)) or ctx.tracks[index].uri != self.current_uri:
            index = self.find_index(ctx, lambda t: t.uri == self.current_uri)
        self.reset_playback_to_position(index)

    def handle_set_repeat_context(self, repeat: bool) -> None:
        self.set_repeat_context(repeat)
        if repeat and self.fill_up_context == AUTOPLAY:
            self.fill_up_context = DEFAULT
        ctx = self.get_context(DEFAULT)
        index = self.find_index(ctx, lambda t: t.uri == self.current_uri)
        if index is None:
            raise StateError("current track is not in the context")
        self.reset_playback_to_position(index)

    def handle_set_queue(self, next_tracks: list, prev_tracks: list) -> None:
        self.set_next_tracks(next_tracks)
        self.set_prev_tracks(prev_tracks)
        self.update_queue_revision()

    # ----- restrictions ---------------------------------------------------------------

    def clear_restrictions(self) -> None:
        p = self.player
        p.context_restrictions.Clear()
        p.context_restrictions.SetInParent()
        p.restrictions.Clear()
        p.restrictions.SetInParent()

    def update_restrictions(self) -> None:
        p = self.player
        r = p.restrictions
        r.SetInParent()
        if self.is_playing():
            del r.disallow_pausing_reasons[:]
            del r.disallow_resuming_reasons[:]
            r.disallow_resuming_reasons.append("not_paused")
        if self.is_pause():
            del r.disallow_resuming_reasons[:]
            del r.disallow_pausing_reasons[:]
            r.disallow_pausing_reasons.append("not_playing")

        del r.disallow_peeking_prev_reasons[:]
        del r.disallow_skipping_prev_reasons[:]
        if len(p.prev_tracks) == 0:
            r.disallow_peeking_prev_reasons.append("no previous tracks")
            r.disallow_skipping_prev_reasons.append("no previous tracks")

        del r.disallow_toggling_shuffle_reasons[:]
        del r.disallow_toggling_repeat_context_reasons[:]
        del r.disallow_toggling_repeat_track_reasons[:]
        if self.has_track and is_autoplay(p.track):
            r.disallow_toggling_shuffle_reasons.append("autoplay")
            r.disallow_toggling_repeat_context_reasons.append("autoplay")
            r.disallow_toggling_repeat_track_reasons.append("autoplay")

    # ----- transfer -------------------------------------------------------------------

    def current_track_from_transfer(
        self, transfer: transfer_state_pb2.TransferState
    ) -> player_pb2.ProvidedTrack:
        playing_queue = transfer.queue.is_playing_queue
        if playing_queue and len(transfer.queue.tracks) > 0:
            source = transfer.queue.tracks[0]
        elif transfer.playback.HasField("current_track"):
            source = transfer.playback.current_track
        else:
            raise StateError("the transfer names no current track")
        ctx_uri = transfer.current_session.context.uri or None
        return self.context_to_provided_track(
            source, ctx_uri, None, None, PROVIDER_QUEUE if playing_queue else None
        )

    def handle_initial_transfer(self, transfer: transfer_state_pb2.TransferState, ctx_uri: str | None) -> None:
        current_metadata = dict(self.context.metadata) if self.context is not None else {}
        p = self.player
        p.is_buffering = False
        if transfer.HasField("options"):
            copy_wire_compatible(transfer.options, p.options)
        p.is_paused = transfer.playback.is_paused
        p.is_playing = not p.is_paused
        speed = transfer.playback.playback_speed
        p.playback_speed = speed if speed != 0 else 1.0

        self.transfer_shuffle_seed = None
        session = transfer.current_session
        if transfer.HasField("current_session"):
            if session.HasField("play_origin"):
                copy_wire_compatible(session.play_origin, p.play_origin)
            if session.HasField("suppressions"):
                copy_wire_compatible(session.suppressions, p.suppressions)
            if session.HasField("context"):
                if session.context.HasField("restrictions"):
                    copy_restrictions(session.context.restrictions, p.restrictions)
                for k, v in session.context.metadata.items():
                    p.context_metadata[k] = v

        uri = ctx_uri or "spotify:unknown"
        p.context_url = f"context://{uri}"
        p.context_uri = uri
        for k, v in current_metadata.items():
            p.context_metadata[k] = v

        self.clear_prev_tracks()
        self.clear_next_tracks()
        self.update_queue_revision()

    def finish_transfer(self, transfer: transfer_state_pb2.TransferState) -> None:
        if self.has_track:
            track = player_pb2.ProvidedTrack()
            track.CopyFrom(self.player.track)
        else:
            track = self.current_track_from_transfer(transfer)

        kind = AUTOPLAY if track.metadata.get(M_IS_AUTOPLAY) == "true" else DEFAULT
        if kind == AUTOPLAY and self.autoplay_context is None:
            kind = DEFAULT
        self.set_active_context(kind)
        self.fill_up_context = kind
        ctx = self.get_context(kind)

        uid = transfer.current_session.current_uid
        if uid and is_queue(track):
            index = self.find_index(ctx, lambda t: t.uid == uid)
            if index is not None and index > 0:
                index -= 1
        else:
            index = self.find_index(
                ctx, lambda t: t.uri == track.uri or (bool(track.uid) and t.uid == track.uid)
            )

        if not self.has_track:
            self.set_track(track)
        if index is not None:
            self._set_index_track(index)

        playing_queue = transfer.queue.is_playing_queue
        for i, queued in enumerate(transfer.queue.tracks):
            if playing_queue and i == 0:
                continue  # that entry is the track playing right now
            try:
                self.add_to_queue(
                    self.context_to_provided_track(queued, self.context_uri, None, None, PROVIDER_QUEUE),
                    update_revision=False,
                )
            except StateError as exc:
                log.debug("skipping queued track from transfer: %s", exc)

        shuffled = False
        if self.shuffling_context:
            try:
                self.shuffle_new()
                shuffled = True
            except StateError as exc:
                log.info("not shuffling the transferred context: %s", exc)
                self.set_shuffle(False)
        if not shuffled:
            if index is not None:
                self.reset_playback_to_position(index)
            else:
                # The transferred track is not part of the context (a queued or
                # injected track): keep playing it and continue into the context.
                self.reset_playback_keep_current()
        self.update_restrictions()

    def reset_playback_keep_current(self) -> None:
        """Rebuild the upcoming tracks from the top of the context, keeping the current track."""
        self._set_index_track(None)
        self.get_context(self.active_context).index_track = 0
        self.fill_up_context = self.active_context
        self.clear_prev_tracks()
        self.clear_next_tracks()
        self.fill_up_next_tracks()
