"""The Spotify Connect receiver: ties discovery, session, state and player together.

Threads
- loop thread:   owns the connect state; every handler below runs here.
- dealer thread: receives websocket traffic and posts it to the loop.
- player thread: decodes audio; posts its events to the loop.
- http threads:  zeroconf requests; a login is handed to the loop once the
                 access point has accepted the credentials.

The command handling follows librespot's `spirc` (MIT).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from google.protobuf import json_format

from .ap import AccessPoint, ApError, Credentials, LoginError, TRY_ANOTHER_AP
from .audio import AudioLoader, TrackInfo
from .dealer import Dealer, Message, Request
from .discovery import Discovery
from .http import (
    ApResolver,
    AuthError,
    ClientToken,
    HttpError,
    Login5,
    SpClient,
    new_http_session,
    parse_context,
)
from .ids import is_playable_uri
from .player import MAX_VOLUME, Player, Sink
from .proto import connect_pb2
from .proto import context_pb2
from .proto import context_player_options_pb2 as options_pb
from .proto import devices_pb2
from .proto import player_pb2
from .proto import transfer_state_pb2
from .state import AUTOPLAY, DEFAULT, ConnectState, PlayStatus, StateError, is_autoplay

log = logging.getLogger(__name__)

UPDATE_STATE_DELAY = 0.2
VOLUME_UPDATE_DELAY = 0.5
CONTEXT_FETCH_THRESHOLD = 2
MAX_EXTRA_PAGES = 50
MAX_CONSECUTIVE_UNAVAILABLE = 20
MAX_CONSECUTIVE_KEY_REFUSALS = 3
KEY_REFUSED_MESSAGE = (
    "Spotify refused to give this app the keys needed to play tracks. "
    "Some accounts are affected (a Spotify-side change that also breaks other "
    "open-source players), and no setting here fixes it."
)
RECONNECT_DELAYS = (2, 5, 10, 30, 60)

_DEVICE_TYPES = {
    "computer": (devices_pb2.COMPUTER, "Computer"),
    "speaker": (devices_pb2.SPEAKER, "Speaker"),
    "tv": (devices_pb2.TV, "TV"),
    "avr": (devices_pb2.AVR, "AVR"),
    "stb": (devices_pb2.STB, "STB"),
    "audiodongle": (devices_pb2.AUDIO_DONGLE, "AudioDongle"),
}


@dataclass
class ReceiverConfig:
    device_name: str = "spconnect"
    device_type: str = "speaker"
    bitrate: int = 320  # 96, 160 or 320
    initial_volume: int = MAX_VOLUME // 2  # 0..65535, used until a volume is saved
    volume_steps: int = 64
    normalise: bool = True
    autoplay: bool | None = None  # None: follow the account's setting
    state_path: str | None = None  # JSON file for the device id, credentials and volume
    zeroconf_port: int = 0  # 0: pick a free port


def now_ms() -> int:
    return int(time.time() * 1000)


def _parse_into(data, message):
    """Parse a JSON-like dict into a protobuf message, tolerating unknown fields."""
    if data is None:
        return message
    try:
        json_format.ParseDict(data, message, ignore_unknown_fields=True)
    except json_format.ParseError as exc:
        log.debug("could not fully parse %s: %s", type(message).__name__, exc)
    return message


def parse_provided_track(data: dict | None) -> player_pb2.ProvidedTrack:
    """Parse a track from a command, keeping at least its uri, uid and metadata."""
    track = player_pb2.ProvidedTrack()
    if not isinstance(data, dict):
        return track
    try:
        json_format.ParseDict(data, track, ignore_unknown_fields=True)
        return track
    except json_format.ParseError as exc:
        log.debug("track needs manual parsing: %s", exc)
    track = player_pb2.ProvidedTrack()
    for key in ("uri", "uid", "provider"):
        if isinstance(data.get(key), str):
            setattr(track, key, data[key])
    for key, value in (data.get("metadata") or {}).items():
        if isinstance(value, str):
            track.metadata[key] = value
    return track


class _Session:
    """Everything tied to one logged-in user."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.http = new_http_session()
        self.resolver = ApResolver(self.http)
        self.client_token = ClientToken(self.http, device_id)
        self.login5 = Login5(self.http, self.client_token, device_id)
        self.spclient = SpClient(self.http, self.resolver, self.login5, self.client_token, device_id)
        self.ap: AccessPoint | None = None
        self.dealer: Dealer | None = None
        self.username = ""
        self.session_id = uuid.uuid4().hex
        self.connection_id = ""
        self.closed = False

    def connect(self, credentials: Credentials) -> Credentials:
        """Log in at an access point. Returns the reusable credentials."""
        last: Exception | None = None
        for address in self.resolver.get("accesspoint")[:6]:
            ap = AccessPoint(address, self.device_id)
            try:
                ap.connect()
                reusable = ap.authenticate(credentials)
            except LoginError as exc:
                if exc.code == TRY_ANOTHER_AP:
                    last = exc
                    continue
                raise
            except ApError as exc:
                last = exc
                log.debug("access point %s failed: %s", address, exc)
                continue
            self.ap = ap
            self.username = reusable.username or ""
            self.login5.set_credentials(self.username, reusable.auth_data)
            ap.start()
            log.info("authenticated as %s via %s", self.username, address)
            return reusable
        raise ApError(f"could not log in at any access point: {last}")

    def close(self) -> None:
        self.closed = True
        if self.dealer is not None:
            self.dealer.close()
        if self.ap is not None:
            self.ap.close()
        try:
            self.http.close()
        except Exception:
            pass


class Receiver:
    def __init__(
        self,
        config: ReceiverConfig,
        sink: Sink,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self._sink = sink
        self._on_change = on_change

        self._events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._login_lock = threading.Lock()

        self._saved = self._load_saved()
        self.device_id: str = self._saved.get("device_id") or os.urandom(20).hex()
        self._saved["device_id"] = self.device_id

        kind = _DEVICE_TYPES.get(config.device_type.lower(), _DEVICE_TYPES["speaker"])
        self._device_type_proto, self._device_type_name = kind
        self._discovery: Discovery | None = None

        # owned by the loop thread
        self._session: _Session | None = None
        self._state: ConnectState | None = None
        self._player: Player | None = None
        self._play_status = PlayStatus()
        self._play_id = 0
        self._connect_established = False
        self._update_state_at: float | None = None
        self._update_volume_at: float | None = None
        self._activation_timestamp = 0
        self._last_cluster_timestamp = 0
        self._consecutive_unavailable = 0
        self._consecutive_key_refusals = 0
        self._reconnect_attempt = 0
        self._reconnect_at: float | None = None
        self._track_info: TrackInfo | None = None

        self._status_lock = threading.Lock()
        self._status: dict = {"state": "starting", "username": "", "error": None}
        self._save_state_file()

    # ----- persistence ---------------------------------------------------------

    def _load_saved(self) -> dict:
        path = self.config.state_path
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return {}

    def _save_state_file(self) -> None:
        path = self.config.state_path
        if not path:
            return
        try:
            target = Path(path)
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._saved, fh, indent=2)
            os.replace(tmp, target)
        except OSError as exc:
            log.warning("could not save %s: %s", path, exc)

    def _stored_credentials(self) -> Credentials | None:
        creds = self._saved.get("credentials")
        if not isinstance(creds, dict):
            return None
        try:
            return Credentials(
                username=creds["username"],
                auth_type=int(creds["auth_type"]),
                auth_data=base64.b64decode(creds["auth_data"]),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def _store_credentials(self, creds: Credentials | None) -> None:
        if creds is None:
            self._saved.pop("credentials", None)
        else:
            self._saved["credentials"] = {
                "username": creds.username,
                "auth_type": creds.auth_type,
                "auth_data": base64.b64encode(creds.auth_data).decode(),
            }
        self._save_state_file()

    # ----- public API ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="spconnect", daemon=True)
        self._thread.start()
        self._discovery = Discovery(
            self.config.device_name,
            self.device_id,
            self._on_zeroconf_login,
            device_type=self._device_type_name,
            port=self.config.zeroconf_port,
        )
        self._discovery.start()
        self._set_status(state="waiting")
        stored = self._stored_credentials()
        if stored is not None:
            self._post(lambda: self._login_with(stored, initial=True))

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        done = threading.Event()
        self._post(lambda: (self._shutdown(), done.set()))
        done.wait(8)
        self._stopping.set()
        self._post(lambda: None)
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._discovery is not None:
            self._discovery.close()

    def status(self) -> dict:
        with self._status_lock:
            out = dict(self._status)
        player = self._player
        out["position_ms"] = player.position_ms() if player is not None and out.get("track") else 0
        return out

    def play(self) -> None:
        self._post(lambda: self._local(self._handle_play))

    def pause(self) -> None:
        self._post(lambda: self._local(self._handle_pause))

    def next(self) -> None:
        self._post(lambda: self._local(lambda: self._handle_next(None)))

    def prev(self) -> None:
        self._post(lambda: self._local(self._handle_prev))

    def set_volume(self, volume: int) -> None:
        """Set the volume, 0..65535."""
        self._post(lambda: self._set_volume(volume))

    # ----- loop plumbing -----------------------------------------------------------

    def _post(self, fn: Callable[[], object]) -> None:
        self._events.put(fn)

    def _local(self, fn: Callable[[], None]) -> None:
        if self._state is None or not self._state.is_active:
            return
        fn()
        self._schedule_update()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            deadlines = [d for d in (self._update_state_at, self._update_volume_at, self._reconnect_at) if d]
            timeout = max(0.0, min(deadlines) - time.monotonic()) if deadlines else 1.0
            try:
                fn = self._events.get(timeout=min(timeout, 1.0))
            except queue.Empty:
                fn = None
            try:
                if fn is not None:
                    fn()
                self._run_timers()
            except Exception:
                log.exception("receiver loop error")

    def _run_timers(self) -> None:
        now = time.monotonic()
        if self._update_volume_at is not None and now >= self._update_volume_at:
            self._update_volume_at = None
            self._put_state(connect_pb2.VOLUME_CHANGED)
            # The web player needs a regular state update as well to keep its position.
            self._notify()
            self._update_state_at = None
        if self._update_state_at is not None and now >= self._update_state_at:
            self._update_state_at = None
            self._notify()
        if self._reconnect_at is not None and now >= self._reconnect_at:
            self._reconnect_at = None
            stored = self._stored_credentials()
            if stored is not None and self._session is None:
                self._login_with(stored, initial=False)

    def _schedule_update(self) -> None:
        if self._update_state_at is None:
            self._update_state_at = time.monotonic() + UPDATE_STATE_DELAY

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            self._status.update(changes)
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("on_change callback failed")

    def _refresh_status(self) -> None:
        state = self._state
        if state is None:
            return
        info = self._track_info
        track = None
        if info is not None and state.has_track:
            track = {
                "uri": info.uri,
                "name": info.name,
                "artists": list(info.artists),
                "album": info.album,
                "duration_ms": info.duration_ms,
                "bitrate": info.bitrate,
                "codec": info.codec,
            }
        self._set_status(
            state="active" if state.is_active else "ready",
            playing=self._play_status.kind in ("playing", "loading_play"),
            track=track,
            volume=state.device_info.volume,
            context_uri=state.context_uri,
            shuffle=state.shuffling_context,
            repeat_context=state.repeat_context,
            repeat_track=state.repeat_track,
        )

    # ----- login / session lifecycle -------------------------------------------------

    def _on_zeroconf_login(self, credentials: Credentials) -> bool:
        """Runs on a zeroconf HTTP thread. True once the access point accepted the login."""
        with self._login_lock:
            session = _Session(self.device_id)
            try:
                reusable = session.connect(credentials)
            except (ApError, OSError) as exc:
                log.warning("login for %s failed: %s", credentials.username, exc)
                session.close()
                self._set_status(error=f"Login failed: {exc}")
                return False
            done = threading.Event()
            self._post(lambda: (self._install_session(session, reusable), done.set()))
            done.wait(20)
            return True

    def _login_with(self, credentials: Credentials, initial: bool) -> None:
        """Log in with stored credentials. Runs on the loop thread."""
        self._set_status(state="connecting", error=None)
        session = _Session(self.device_id)
        try:
            reusable = session.connect(credentials)
        except LoginError as exc:
            log.warning("stored credentials were rejected: %s", exc)
            session.close()
            self._store_credentials(None)
            self._set_status(state="waiting", error="Saved login expired. Select the device in Spotify again.")
            return
        except (ApError, OSError) as exc:
            session.close()
            log.warning("could not connect to Spotify: %s", exc)
            self._set_status(state="waiting", error=f"Could not connect to Spotify: {exc}")
            self._plan_reconnect()
            return
        self._install_session(session, reusable)

    def _plan_reconnect(self) -> None:
        if self._stopping.is_set() or self._stored_credentials() is None:
            return
        delay = RECONNECT_DELAYS[min(self._reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
        self._reconnect_attempt += 1
        self._reconnect_at = time.monotonic() + delay
        log.info("reconnecting to Spotify in %d s", delay)

    def _install_session(self, session: _Session, reusable: Credentials) -> None:
        """Make `session` the current one and bring up its dealer. Loop thread."""
        self._teardown_session(notify_spotify=False)
        self._reconnect_at = None
        self._store_credentials(reusable)
        self._session = session
        self._connect_established = False
        self._play_status = PlayStatus()
        self._track_info = None
        self._consecutive_unavailable = 0

        volume = self._saved.get("volume")
        if not isinstance(volume, int) or not 0 <= volume <= MAX_VOLUME:
            volume = self.config.initial_volume
        self._state = ConnectState(
            self.config.device_name,
            self.device_id,
            session.session_id,
            device_type=self._device_type_proto,
            initial_volume=volume,
            volume_steps=self.config.volume_steps,
        )
        assert session.ap is not None
        loader = AudioLoader(
            session.spclient, session.ap, session.http, self.config.bitrate, self.config.normalise
        )
        self._player = Player(
            loader,
            self._sink,
            lambda kind, play_id, data, s=session: self._post(
                lambda: self._on_player_event(s, kind, play_id, data)
            ),
        )
        self._player.set_volume(volume)

        session.ap.on_closed = lambda s=session: self._post(lambda: self._on_session_lost(s, "access point"))
        session.spclient.country = session.ap.wait_for_country(3.0)

        dealer = Dealer(lambda: session.resolver.get("dealer"), session.login5.access_token)
        session.dealer = dealer

        def post_message(handler):
            return lambda msg, s=session: self._post(lambda: self._guard(s, handler, msg))

        dealer.listen("hm://pusher/v1/connections/", post_message(self._on_connection_id))
        dealer.listen("hm://connect-state/v1/cluster", post_message(self._on_cluster_update))
        dealer.listen("hm://connect-state/v1/connect/volume", post_message(self._on_volume_message))
        dealer.listen("hm://connect-state/v1/connect/logout", post_message(self._on_logout_message))
        dealer.handle(
            "hm://connect-state/v1/player/command",
            lambda req, s=session: self._post(lambda: self._on_request(s, req)),
        )
        dealer.on_lost = lambda s=session: self._post(lambda: self._on_session_lost(s, "dealer"))

        try:
            session.client_token.get()
            session.login5.access_token()
            dealer.start()
        except Exception as exc:
            log.warning("could not finish connecting to Spotify: %s", exc)
            self._teardown_session(notify_spotify=False)
            self._set_status(state="waiting", error=f"Could not connect to Spotify: {exc}")
            self._plan_reconnect()
            return

        self._reconnect_attempt = 0
        if self._discovery is not None:
            self._discovery.set_active_user(session.username)
        self._set_status(state="connecting", username=session.username, error=None, track=None, playing=False)

    def _guard(self, session: _Session, handler, arg) -> None:
        if session is self._session:
            handler(arg)

    def _on_session_lost(self, session: _Session, what: str) -> None:
        if session is not self._session or self._stopping.is_set():
            return
        log.warning("lost the %s connection; reconnecting", what)
        self._teardown_session(notify_spotify=False)
        self._set_status(state="waiting", error="Connection to Spotify lost. Reconnecting…", track=None, playing=False)
        self._plan_reconnect()

    def _teardown_session(self, notify_spotify: bool) -> None:
        session, self._session = self._session, None
        player, self._player = self._player, None
        state, self._state = self._state, None
        self._update_state_at = None
        self._update_volume_at = None
        self._connect_established = False
        self._play_status = PlayStatus()
        self._track_info = None
        if player is not None:
            player.stop()
            player.close()
        if session is None:
            return
        if notify_spotify and session.connection_id and state is not None:
            try:
                if state.is_active:
                    session.spclient.put_connect_state_inactive(session.connection_id, notify=False)
                session.spclient.delete_connect_state()
            except Exception as exc:
                log.debug("could not sign off from connect state: %s", exc)
        session.close()

    def _shutdown(self) -> None:
        self._reconnect_at = None
        self._teardown_session(notify_spotify=True)
        self._set_status(state="stopped", track=None, playing=False)

    # ----- connect state push ---------------------------------------------------------

    def _put_state(self, reason: int) -> connect_pb2.Cluster | None:
        session, state = self._session, self._state
        if session is None or state is None or not session.connection_id:
            return None
        state.set_now(now_ms())
        state.request.put_state_reason = reason
        try:
            cluster = session.spclient.put_connect_state(session.connection_id, state.request)
        except (HttpError, AuthError, OSError, Exception) as exc:  # noqa: BLE001
            log.warning("connect state update failed: %s", exc)
            return None
        finally:
            state.request.put_state_reason = connect_pb2.PLAYER_STATE_CHANGED
        return cluster

    def _notify(self) -> None:
        state = self._state
        if state is None or not self._connect_established:
            return
        state.set_status(self._play_status)
        if state.is_playing():
            state.update_position_in_relation(now_ms())
        self._put_state(connect_pb2.PLAYER_STATE_CHANGED)
        self._refresh_status()

    # ----- dealer messages ---------------------------------------------------------------

    def _on_connection_id(self, msg: Message) -> None:
        session, state = self._session, self._state
        if session is None or state is None:
            return
        connection_id = msg.headers.get("Spotify-Connection-Id", "")
        if not connection_id:
            log.warning("connection message without a connection id")
            return
        session.connection_id = connection_id
        state.set_status(self._play_status)
        cluster = self._put_state(connect_pb2.NEW_DEVICE)
        if cluster is None:
            log.error("could not register the device with Spotify")
            self._on_session_lost(session, "connect state")
            return
        self._connect_established = True
        log.info("device '%s' is registered with Spotify Connect", self.config.device_name)
        self._last_cluster_timestamp = cluster.player_state.timestamp
        self._refresh_status()

        # Take over automatically when the service hands us a pending transfer.
        same_session = cluster.player_state.session_id in ("", session.session_id)
        if cluster.active_device_id or not same_session or not cluster.transfer_data:
            return
        transfer = transfer_state_pb2.TransferState()
        try:
            transfer.ParseFromString(cluster.transfer_data)
            self._handle_transfer(transfer, restore_paused=None)
            self._notify()
        except Exception as exc:
            log.warning("could not take over playback automatically: %s", exc)

    def _on_cluster_update(self, msg: Message) -> None:
        state = self._state
        if state is None or msg.payload is None:
            return
        update = connect_pb2.ClusterUpdate()
        update.ParseFromString(msg.payload)
        if not update.HasField("cluster"):
            return
        cluster = update.cluster
        timestamp = cluster.player_state.timestamp
        if not state.is_active:
            self._last_cluster_timestamp = timestamp
            return
        if cluster.active_device_id == self.device_id:
            # Another device changed something; refresh ours so positions stay in sync.
            self._schedule_update()
            return
        # Someone else is named active. Updates that predate our own activation are
        # stale (they describe the device we took over from) and must be ignored.
        if timestamp <= self._activation_timestamp:
            log.debug("ignoring stale cluster update naming %s active", cluster.active_device_id)
            return
        name = cluster.device[cluster.active_device_id].name if cluster.active_device_id in cluster.device else "another device"
        log.info("playback moved to %s", name)
        self._handle_disconnect()
        self._handle_stop()
        self._refresh_status()

    def _on_volume_message(self, msg: Message) -> None:
        if msg.payload is None:
            return
        command = connect_pb2.SetVolumeCommand()
        command.ParseFromString(msg.payload)
        self._set_volume(command.volume)

    def _on_logout_message(self, _msg: Message) -> None:
        log.info("Spotify asked this device to log out")
        self._teardown_session(notify_spotify=True)
        self._store_credentials(None)
        if self._discovery is not None:
            self._discovery.set_active_user("")
        self._set_status(state="waiting", username="", track=None, playing=False, error=None)

    # ----- remote commands -------------------------------------------------------------------

    def _on_request(self, session: _Session, request: Request) -> None:
        if session is not self._session or self._state is None:
            request.reply(False)
            return
        payload = request.payload
        command = payload.get("command") or {}
        endpoint = command.get("endpoint", "")
        sent_by = payload.get("sent_by_device_id", "")
        try:
            self._state.set_last_command(int(payload.get("message_id") or 0), sent_by)
        except (TypeError, ValueError):
            pass
        log.info("command '%s' from %s", endpoint, sent_by or "?")
        try:
            ok = self._handle_command(endpoint, command, sent_by)
        except StateError as exc:
            log.warning("command '%s' failed: %s", endpoint, exc)
            ok = False
        except Exception:
            log.exception("command '%s' failed", endpoint)
            ok = False
        request.reply(ok)

    def _handle_command(self, endpoint: str, command: dict, sent_by: str) -> bool:
        state = self._state
        assert state is not None
        options = command.get("options") or {}

        if endpoint == "transfer":
            data = command.get("data")
            if not data:
                return True  # selected with nothing to hand over
            transfer = transfer_state_pb2.TransferState()
            transfer.ParseFromString(base64.b64decode(data))
            self._handle_transfer(transfer, restore_paused=options.get("restore_paused"))
            state.player.play_origin.device_identifier = sent_by
            self._notify()
            return True

        if endpoint == "update_context":
            context = parse_context(command.get("context"))
            if context.uri and context.uri != state.context_uri:
                log.debug("ignoring context update for %s", context.uri)
                return True
            for key, value in context.metadata.items():
                state.player.context_metadata[key] = value
            self._schedule_update()
            return True

        if endpoint == "play":
            self._handle_play_command(command, options, sent_by)
        elif endpoint == "pause":
            self._handle_pause()
        elif endpoint == "resume":
            if self._play_status.kind == "stopped":
                self._load_track(True, 0)
            else:
                self._handle_play()
        elif endpoint == "seek_to":
            position = self._seek_target(command)
            if position is None:
                return False
            self._handle_seek(position)
        elif endpoint == "set_shuffling_context":
            self._handle_shuffle(bool(command.get("value")))
        elif endpoint == "set_repeating_context":
            state.handle_set_repeat_context(bool(command.get("value")))
        elif endpoint == "set_repeating_track":
            state.set_repeat_track(bool(command.get("value")))
        elif endpoint == "set_options":
            if command.get("repeating_context") is not None:
                state.handle_set_repeat_context(bool(command["repeating_context"]))
            if command.get("repeating_track") is not None:
                state.set_repeat_track(bool(command["repeating_track"]))
            if command.get("shuffling_context") is not None:
                self._handle_shuffle(bool(command["shuffling_context"]))
        elif endpoint == "add_to_queue":
            track = parse_provided_track(command.get("track"))
            if not track.uri:
                return False
            state.add_to_queue(track)
        elif endpoint == "set_queue":
            state.handle_set_queue(
                [parse_provided_track(t) for t in command.get("next_tracks") or []],
                [parse_provided_track(t) for t in command.get("prev_tracks") or []],
            )
        elif endpoint == "skip_next":
            track = command.get("track") or {}
            self._handle_next(track.get("uri") or None)
        elif endpoint == "skip_prev":
            self._handle_prev()
        else:
            log.warning("unsupported command '%s'", endpoint)
            return False

        self._schedule_update()
        return True

    def _seek_target(self, command: dict) -> int | None:
        relative = command.get("relative") or ""
        try:
            if relative == "current":
                return max(0, self._position() + int(command.get("position") or 0))
            if relative == "beginning":
                return max(0, int(command.get("position") or 0))
            value = command.get("value")
            if value is None:
                value = command.get("position")
            return max(0, int(value))
        except (TypeError, ValueError):
            log.warning("unsupported seek_to command: %s", command)
            return None

    # ----- play / load -----------------------------------------------------------------------

    def _autoplay_enabled(self) -> bool:
        if self.config.autoplay is not None:
            return self.config.autoplay
        session = self._session
        return bool(session and session.ap and session.ap.attributes.get("autoplay") == "1")

    def _handle_activate(self) -> None:
        state = self._state
        assert state is not None
        if not state.is_active:
            self._activation_timestamp = max(self._activation_timestamp, self._last_cluster_timestamp)
        state.set_active(True)

    def _handle_play_command(self, command: dict, options: dict, sent_by: str) -> None:
        state = self._state
        assert state is not None
        self._handle_activate()

        context = parse_context(command.get("context"))
        page = context.pages[-1] if len(context.pages) > 0 else None
        skip_to = options.get("skip_to") or {}
        override = options.get("player_options_override")
        overrides = _parse_into(override, options_pb.ContextPlayerOptionOverrides()) if override else None

        try:
            fallback_index = int(skip_to.get("track_index")) if skip_to.get("track_index") is not None else None
        except (TypeError, ValueError):
            fallback_index = None

        state.reset_context("when_different", context.uri) if context.uri else state.reset_context("completely")
        state.reset_options()

        if context.uri:
            self._load_context_from_uri(context.uri, page)
        else:
            tracks = [t for p in context.pages for t in p.tracks]
            if not tracks:
                raise StateError("play command has neither a context uri nor tracks")
            self._load_context_from_tracks(tracks)

        state.set_active_context(DEFAULT)
        state.merge_context(page)
        state.clear_next_tracks()
        state.clear_restrictions()

        ctx = state.get_context(DEFAULT)
        index: int | None = None
        requested = False
        if skip_to.get("track_uid"):
            requested = True
            index = state.find_index(ctx, lambda t: t.uid == skip_to["track_uid"])
        if index is None and skip_to.get("track_uri"):
            requested = True
            index = state.find_index(ctx, lambda t: t.uri == skip_to["track_uri"])
        if index is None and fallback_index is not None:
            requested = True
            index = fallback_index if 0 <= fallback_index < len(ctx.tracks) else None
        if requested and index is None:
            log.warning("could not find the requested track in the context, starting at the top")
            index = 0

        shuffle = False
        if overrides is not None:
            shuffle = overrides.shuffling_context
            state.set_shuffle(shuffle)
            state.set_repeat_context(overrides.repeating_context)
            state.set_repeat_track(overrides.repeating_track)

        if shuffle:
            if index is not None:
                state.set_current_track(index)
            else:
                state.set_current_track_random()
            state.shuffle_new()
        else:
            state.set_current_track(index or 0)
            state.reset_playback_to_position(index)
        self._add_autoplay_when_required()

        play_origin = command.get("play_origin")
        if play_origin:
            state.player.play_origin.Clear()
            _parse_into(play_origin, state.player.play_origin)
        state.player.play_origin.device_identifier = sent_by

        try:
            seek_to = max(0, int(options.get("seek_to") or 0))
        except (TypeError, ValueError):
            seek_to = 0
        start_playing = not bool(options.get("initially_paused"))
        if state.has_track:
            self._load_track(start_playing, seek_to)
        else:
            self._handle_stop()

    def _resolve_context(self, uri: str, fallback: str | None) -> context_pb2.Context:
        """Fetch a context by uri (or by `fallback` when the uri cannot be resolved)."""
        session = self._session
        assert session is not None
        resolve_uri = ConnectState.valid_resolve_uri(uri) or fallback
        if not resolve_uri:
            raise StateError(f"context {uri!r} cannot be resolved")
        try:
            context = session.spclient.get_context(resolve_uri)
        except (HttpError, AuthError, LookupError, OSError) as exc:
            raise StateError(f"could not resolve context {resolve_uri}: {exc}") from exc
        # Some contexts list their first page only by url; fetch it so there is
        # something to start from.
        if all(len(p.tracks) == 0 for p in context.pages):
            for page in context.pages:
                if not page.page_url.startswith("hm://"):
                    continue
                try:
                    loaded = session.spclient.get_page(page.page_url)
                except Exception as exc:
                    log.debug("could not load context page %s: %s", page.page_url, exc)
                    continue
                if len(loaded.tracks) > 0:
                    page.CopyFrom(loaded)
                    break
        context.uri = uri
        context.url = f"context://{uri}"
        return context

    def _load_context_from_uri(self, uri: str, page) -> None:
        state = self._state
        assert state is not None
        fallback = None
        if page is not None and len(page.tracks) > 0 and page.tracks[0].uri:
            fallback = page.tracks[0].uri
        if state.context_uri == uri and state.context is not None and ConnectState.valid_resolve_uri(uri):
            log.debug("context %s did not change, not resolving again", uri)
            return
        context = self._resolve_context(uri, fallback)
        self._apply_context(context, DEFAULT)

    def _apply_context(self, context: context_pb2.Context, kind: str) -> None:
        """Install a resolved context and queue the loading of its remaining pages."""
        state = self._state
        session = self._session
        assert state is not None and session is not None
        next_page_url = ""
        for p in context.pages:
            if p.next_page_url:
                next_page_url = p.next_page_url
        remaining = state.update_context(context, kind)
        if kind != DEFAULT:
            return
        generation = (session, state.context)
        for page_uri in remaining[:MAX_EXTRA_PAGES]:
            self._post(lambda u=page_uri: self._append_context_uri(generation, u))
        if next_page_url:
            self._post(lambda: self._append_next_page(generation, next_page_url, MAX_EXTRA_PAGES))

    def _same_context(self, generation) -> bool:
        session, ctx = generation
        return session is self._session and self._state is not None and self._state.context is ctx

    def _append_context_uri(self, generation, page_uri: str) -> None:
        if not self._same_context(generation):
            return
        try:
            context = generation[0].spclient.get_context(page_uri)
        except Exception as exc:
            log.debug("could not load context page %s: %s", page_uri, exc)
            return
        for page in context.pages:
            if len(page.tracks) > 0:
                self._state.fill_context_from_page(page)
        self._after_context_grew()

    def _append_next_page(self, generation, page_url: str, budget: int) -> None:
        if not self._same_context(generation) or budget <= 0:
            return
        try:
            page = generation[0].spclient.get_page(page_url)
        except Exception as exc:
            log.debug("could not load next context page: %s", exc)
            return
        if len(page.tracks) > 0:
            self._state.fill_context_from_page(page)
            self._after_context_grew()
        if page.next_page_url:
            self._post(lambda: self._append_next_page(generation, page.next_page_url, budget - 1))

    def _after_context_grew(self) -> None:
        state = self._state
        if state is None:
            return
        if not state.shuffling_context and len(state.player.next_tracks) < 80:
            state.fill_up_next_tracks()
            self._schedule_update()

    def _load_context_from_tracks(self, tracks) -> None:
        state = self._state
        assert state is not None
        context = context_pb2.Context()
        context.uri = "spotify:web-api"
        context.url = "context://spotify:web-api"
        page = context.pages.add()
        for t in tracks:
            page.tracks.add().CopyFrom(t)
        state.update_context(context, DEFAULT)

    def _add_autoplay_when_required(self) -> None:
        state = self._state
        if state is None or state.has_next_tracks(CONTEXT_FETCH_THRESHOLD):
            return
        if not self._autoplay_enabled() or not state.context_uri or state.repeat_context:
            return
        uri = state.context_uri
        if "spotify:show:" in uri or "spotify:episode:" in uri or uri == "spotify:web-api":
            return
        session = self._session
        generation = (session, state.context)
        self._post(lambda: self._resolve_autoplay(generation, uri))

    def _resolve_autoplay(self, generation, uri: str) -> None:
        if not self._same_context(generation):
            return
        state = self._state
        assert state is not None
        if state.has_next_tracks(CONTEXT_FETCH_THRESHOLD):
            return
        recent = [u for u in state.recent_track_uris() if not u.startswith("spotify:local")]
        try:
            context = generation[0].spclient.get_autoplay_context(uri, recent)
            state.update_context(context, AUTOPLAY)
            state.fill_up_next_tracks()
        except Exception as exc:
            log.info("no autoplay for %s: %s", uri, exc)
            return
        log.info("autoplay will continue after %s", uri)
        self._schedule_update()

    def _load_track(self, start_playing: bool, position_ms: int) -> None:
        state, player = self._state, self._player
        assert state is not None and player is not None
        if not state.has_track:
            self._handle_stop()
            return
        uri = state.current_uri
        if not is_playable_uri(uri):
            # _handle_next already steps over unplayable items, so this only happens
            # for the first track of a play or transfer.
            log.warning("cannot play %s, skipping", uri)
            self._play_status = PlayStatus("loading_play" if start_playing else "loading_pause")
            self._handle_next(None)
            return
        position_ms = max(0, int(position_ms))
        self._track_info = None
        self._play_id = player.load(uri, start_playing, position_ms)
        state.update_position(position_ms, now_ms())
        self._play_status = PlayStatus("loading_play" if start_playing else "loading_pause", position_ms=position_ms)
        state.set_status(self._play_status)

    # ----- transfer ------------------------------------------------------------------------------

    def _handle_transfer(self, transfer: transfer_state_pb2.TransferState, restore_paused: str | None) -> None:
        state = self._state
        assert state is not None
        session_ctx = transfer.current_session.context
        raw_uri = session_ctx.uri if session_ctx.HasField("uri") else ""
        ctx_uri: str | None = raw_uri if raw_uri not in ("", "-") else None

        if ctx_uri:
            state.reset_context("when_different", ctx_uri)
        else:
            state.reset_context("completely")
        state.player.ClearField("track")

        try:
            state.set_track(state.current_track_from_transfer(transfer))
        except StateError as exc:
            log.warning("transfer names no usable track: %s", exc)

        autoplay = state.has_track and (
            is_autoplay(state.player.track) or state.player.track.metadata.get("autoplay.is_autoplay") == "true"
        )
        if autoplay and ctx_uri:
            ctx_uri = ctx_uri.replace("station:", "")
        fallback = state.current_uri

        resolved = False
        if ctx_uri:
            try:
                self._apply_context(self._resolve_context(ctx_uri, fallback or None), DEFAULT)
                resolved = True
            except StateError as exc:
                log.warning("%s", exc)
        if not resolved:
            tracks = [t for p in session_ctx.pages for t in p.tracks]
            if tracks:
                self._load_context_from_tracks(tracks)
                ctx_uri = None
                resolved = True
        if not resolved and fallback:
            # Nothing else to go on: play the transferred track as a context of one.
            self._apply_context(self._resolve_context(fallback, None), DEFAULT)
            ctx_uri = fallback
            resolved = True
        if not resolved:
            raise StateError("the transfer carries neither a resolvable context nor a track")

        self._activation_timestamp = max(self._activation_timestamp, transfer.playback.timestamp)
        self._handle_activate()
        state.handle_initial_transfer(transfer, ctx_uri or state.context_uri or None)

        if autoplay and ctx_uri:
            try:
                recent = [u for u in state.recent_track_uris() if not u.startswith("spotify:local")]
                assert self._session is not None
                state.update_context(self._session.spclient.get_autoplay_context(ctx_uri, recent), AUTOPLAY)
            except Exception as exc:
                log.info("could not restore the autoplay context: %s", exc)

        timestamp = now_ms()
        playback = transfer.playback
        position = int(playback.position_as_of_timestamp)
        paused = bool(playback.is_paused)
        if not paused and position > 0:
            elapsed = timestamp - playback.timestamp
            if 0 < elapsed < 10 * 60 * 1000:
                position += elapsed
        position = max(0, position)
        if restore_paused == "resume":
            paused = False
        elif restore_paused == "pause":
            paused = True

        state.finish_transfer(transfer)
        self._add_autoplay_when_required()
        self._load_track(not paused, position)

    # ----- transport controls ------------------------------------------------------------------

    def _wants_to_play(self) -> bool:
        """Whether playback should carry on into the next track."""
        return self._play_status.kind in ("playing", "loading_play")

    def _position(self) -> int:
        status = self._play_status
        if status.kind == "playing":
            return max(0, now_ms() - status.nominal_start_time)
        if status.kind == "stopped":
            return 0
        return status.position_ms

    def _handle_play(self) -> None:
        state, player = self._state, self._player
        assert state is not None and player is not None
        status = self._play_status
        if status.kind == "paused":
            player.play()
            state.update_position(status.position_ms, now_ms())
            self._play_status = PlayStatus(
                "playing",
                nominal_start_time=now_ms() - status.position_ms,
                preload_triggered=status.preload_triggered,
            )
        elif status.kind == "loading_pause":
            player.play()
            self._play_status = PlayStatus("loading_play", position_ms=status.position_ms)

    def _handle_pause(self) -> None:
        state, player = self._state, self._player
        assert state is not None and player is not None
        status = self._play_status
        if status.kind == "playing":
            player.pause()
            position = max(0, now_ms() - status.nominal_start_time)
            state.update_position(position, now_ms())
            self._play_status = PlayStatus("paused", position_ms=position, preload_triggered=status.preload_triggered)
        elif status.kind == "loading_play":
            player.pause()
            self._play_status = PlayStatus("loading_pause", position_ms=status.position_ms)

    def _handle_seek(self, position_ms: int) -> None:
        state, player = self._state, self._player
        assert state is not None and player is not None
        duration = state.player.duration
        if duration and position_ms > duration:
            log.warning("ignoring seek to %d ms of %d ms", position_ms, duration)
            return
        state.update_position(position_ms, now_ms())
        player.seek(position_ms)
        status = self._play_status
        if status.kind == "playing":
            status.nominal_start_time = now_ms() - position_ms
        elif status.kind != "stopped":
            status.position_ms = position_ms

    def _handle_shuffle(self, shuffle: bool) -> None:
        state = self._state
        assert state is not None
        state.handle_shuffle(shuffle)
        if shuffle:
            self._add_autoplay_when_required()

    def _handle_next(self, track_uri: str | None) -> None:
        state = self._state
        assert state is not None
        continue_playing = self._wants_to_play()
        has_next = track_uri is not None and state.current_uri == track_uri
        if not has_next:
            while True:
                has_next = state.next_track()
                if not has_next:
                    break
                if track_uri is not None and state.current_uri != track_uri:
                    continue  # skipping ahead to the requested track
                if not is_playable_uri(state.current_uri):
                    log.info("skipping unplayable item %s", state.current_uri)
                    continue
                break
        if has_next:
            self._add_autoplay_when_required()
            self._load_track(continue_playing, 0)
        else:
            log.info("nothing left to play")
            self._handle_stop()

    def _handle_prev(self) -> None:
        state = self._state
        assert state is not None
        if self._position() >= 3000:
            self._handle_seek(0)
            return
        repeat = state.repeat_context
        keep_playing = self._wants_to_play()
        if state.prev_track():
            self._load_track(keep_playing, 0)
            return
        try:
            state.reset_playback_to_position(None)
        except StateError as exc:
            log.debug("could not reset playback: %s", exc)
        if repeat and state.has_track:
            self._load_track(keep_playing, 0)
        else:
            self._handle_stop()

    def _handle_stop(self) -> None:
        state, player = self._state, self._player
        if state is None or player is None:
            return
        player.stop()
        self._play_status = PlayStatus()
        self._track_info = None
        state.update_position(0, now_ms())
        state.clear_next_tracks()
        try:
            state.reset_playback_to_position(None)
        except StateError:
            pass
        state.set_status(self._play_status)

    def _handle_disconnect(self) -> None:
        state, session = self._state, self._session
        if state is None or session is None:
            return
        self._play_status = PlayStatus()
        state.update_position_in_relation(now_ms())
        self._notify()
        state.reset()
        state.reset_context("completely")
        self._update_state_at = None
        if session.connection_id:
            try:
                session.spclient.put_connect_state_inactive(session.connection_id, notify=False)
            except Exception as exc:
                log.warning("could not report the device as inactive: %s", exc)

    def _set_volume(self, volume: int) -> None:
        state, player = self._state, self._player
        volume = max(0, min(MAX_VOLUME, int(volume)))
        self._saved["volume"] = volume
        if state is None or player is None:
            self._save_state_file()
            return
        if state.device_info.volume == volume:
            return
        state.set_volume(volume)
        player.set_volume(volume)
        self._save_state_file()
        self._update_volume_at = time.monotonic() + VOLUME_UPDATE_DELAY
        self._refresh_status()

    # ----- player events -----------------------------------------------------------------------

    def _on_player_event(self, session: _Session, kind: str, play_id: int, data: dict) -> None:
        state = self._state
        if session is not self._session or state is None or play_id != self._play_id:
            return  # belongs to an older track or session
        status = self._play_status
        timestamp = now_ms()

        if kind == "track_info":
            info: TrackInfo = data["info"]
            self._track_info = info
            state.update_duration(info.duration_ms)
            self._consecutive_unavailable = 0
            self._consecutive_key_refusals = 0
        elif kind == "end_of_track":
            self._handle_next(state.current_uri if state.repeat_track else None)
        elif kind == "loading":
            state.update_position(status.position_ms if status.is_loading else 0, timestamp)
        elif kind == "seeked":
            state.update_position(data["position_ms"], timestamp)
        elif kind == "playing":
            position = int(data["position_ms"])
            nominal = timestamp - position
            if status.kind == "playing":
                if abs(status.nominal_start_time - nominal) <= 100:
                    return
                status.nominal_start_time = nominal
                state.update_position(position, timestamp)
            elif status.is_loading:
                state.update_position(position, timestamp)
                self._play_status = PlayStatus("playing", nominal_start_time=nominal)
            else:
                return
        elif kind == "paused":
            position = int(data["position_ms"])
            if status.kind == "stopped":
                return
            state.update_position(position, timestamp)
            self._play_status = PlayStatus("paused", position_ms=position)
        elif kind == "stopped":
            if status.kind == "stopped":
                return
            self._play_status = PlayStatus()
        elif kind == "time_to_preload":
            status.preload_triggered = True
            next_uri = state.preview_next_uri()
            if next_uri and is_playable_uri(next_uri) and self._player is not None:
                self._player.preload(next_uri)
            return
        elif kind == "unavailable":
            uri = data.get("uri", "")
            state.mark_unavailable(uri)
            self._consecutive_unavailable += 1
            # Spotify refusing the decryption key is a property of the account, not
            # of one track: skipping on through the playlist only repeats it.
            key_refused = "audio key" in str(data.get("reason", ""))
            if key_refused:
                self._consecutive_key_refusals += 1
            limit = MAX_CONSECUTIVE_KEY_REFUSALS if key_refused else MAX_CONSECUTIVE_UNAVAILABLE
            if state.current_uri == uri:
                if self._consecutive_unavailable >= limit:
                    if key_refused:
                        log.error(
                            "Spotify refused the audio key for %d tracks in a row; "
                            "stopping. This is a known limit for some accounts "
                            "(Spotify now requires PlayPlay DRM for them).",
                            self._consecutive_key_refusals,
                        )
                        self._set_status(error=KEY_REFUSED_MESSAGE)
                    else:
                        log.error("too many unplayable tracks in a row, stopping")
                    self._consecutive_unavailable = 0
                    self._consecutive_key_refusals = 0
                    self._handle_stop()
                else:
                    self._handle_next(None)
        else:
            return

        self._schedule_update()
        self._refresh_status()
