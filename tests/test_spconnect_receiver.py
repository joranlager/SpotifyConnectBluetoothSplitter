"""Command handling of the receiver, driven with fake network services and a fake player."""

import base64

import pytest

from spconnect.audio import TrackInfo
from spconnect.ids import gid_to_base62
from spconnect.player import NullSink
from spconnect.proto import connect_pb2
from spconnect.proto import context_pb2
from spconnect.proto import transfer_state_pb2
from spconnect.receiver import Receiver, ReceiverConfig, parse_context
from spconnect.state import ConnectState

PLAYLIST = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
OTHER_DEVICE = "ff" * 20


def uri(n: int) -> str:
    return "spotify:track:" + gid_to_base62(n.to_bytes(16, "big"))


def context(count: int, ctx_uri: str = PLAYLIST, start: int = 0) -> context_pb2.Context:
    ctx = context_pb2.Context(uri=ctx_uri)
    page = ctx.pages.add()
    for i in range(start, start + count):
        t = page.tracks.add()
        t.uri = uri(i)
        t.uid = f"uid{i}"
    return ctx


class FakeSpClient:
    def __init__(self) -> None:
        self.contexts: dict[str, context_pb2.Context] = {PLAYLIST: context(10)}
        self.autoplay: context_pb2.Context | None = None
        self.puts: list[connect_pb2.PutStateRequest] = []
        self.inactive = 0
        self.country = "NO"
        self.cluster = connect_pb2.Cluster()

    def get_context(self, ctx_uri: str) -> context_pb2.Context:
        if ctx_uri not in self.contexts:
            raise LookupError(ctx_uri)
        out = context_pb2.Context()
        out.CopyFrom(self.contexts[ctx_uri])
        return out

    def get_autoplay_context(self, ctx_uri, recent):
        if self.autoplay is None:
            raise LookupError("no autoplay")
        out = context_pb2.Context()
        out.CopyFrom(self.autoplay)
        return out

    def put_connect_state(self, connection_id, request):
        copy = connect_pb2.PutStateRequest()
        copy.CopyFrom(request)
        self.puts.append(copy)
        return self.cluster

    def put_connect_state_inactive(self, connection_id, notify=False):
        self.inactive += 1

    def delete_connect_state(self):
        pass


class FakeAp:
    attributes = {"autoplay": "0"}


class FakeSession:
    def __init__(self) -> None:
        self.spclient = FakeSpClient()
        self.ap = FakeAp()
        self.connection_id = "conn-1"
        self.session_id = "sess-1"
        self.username = "tester"
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakePlayer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._id = 0

    def load(self, track_uri, start_playing, position_ms=0):
        self._id += 1
        self.calls.append(("load", track_uri, start_playing, position_ms))
        return self._id

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))

        return record

    def position_ms(self):
        return 0

    def last(self, name):
        return next(c for c in reversed(self.calls) if c[0] == name)

    def loads(self):
        return [c for c in self.calls if c[0] == "load"]


class Rig:
    """A Receiver with its network and audio replaced. Handlers are called directly,
    which is what the loop thread does."""

    def __init__(self) -> None:
        self.r = Receiver(ReceiverConfig(device_name="Test"), NullSink())
        self.session = FakeSession()
        self.player = FakePlayer()
        r = self.r
        r._session = self.session
        r._state = ConnectState("Test", r.device_id, "sess-1")
        r._player = self.player
        r._connect_established = True
        self.state = r._state
        self.sp = self.session.spclient

    def drain(self) -> None:
        """Run work the handlers posted to the loop (extra pages, autoplay)."""
        for _ in range(100):
            if self.r._events.empty():
                return
            self.r._events.get_nowait()()

    def command(self, endpoint: str, **fields) -> bool:
        ok = self.r._handle_command(endpoint, {"endpoint": endpoint, **fields}, OTHER_DEVICE)
        self.drain()
        return ok

    def event(self, kind: str, **data) -> None:
        self.r._on_player_event(self.session, kind, self.r._play_id, data)
        self.drain()

    def started(self, position_ms: int = 0, duration_ms: int = 200_000) -> None:
        """Simulate the player reporting that the current track loaded and plays."""
        info = TrackInfo(self.state.current_uri, "Song", ["Artist"], "Album", duration_ms)
        self.event("track_info", info=info)
        self.event("playing", position_ms=position_ms)

    def play_playlist(self, **options) -> None:
        assert self.command(
            "play",
            context={"uri": PLAYLIST, "url": f"context://{PLAYLIST}"},
            play_origin={"feature_identifier": "playlist"},
            options=options,
        )


@pytest.fixture
def rig():
    return Rig()


def transfer_data(current: int, paused: bool = False, position: int = 30_000) -> str:
    from spconnect.receiver import now_ms

    t = transfer_state_pb2.TransferState()
    t.playback.timestamp = now_ms()
    t.playback.position_as_of_timestamp = position
    t.playback.is_paused = paused
    t.playback.current_track.uri = uri(current)
    t.playback.current_track.uid = f"uid{current}"
    t.current_session.context.uri = PLAYLIST
    t.current_session.play_origin.feature_identifier = "playlist"
    return base64.b64encode(t.SerializeToString()).decode()


# ----- play ---------------------------------------------------------------------------------


def test_play_context_starts_first_track_and_becomes_active(rig):
    rig.play_playlist()
    assert rig.state.is_active
    assert rig.player.loads() == [("load", uri(0), True, 0)]
    assert rig.state.current_uri == uri(0)
    assert rig.state.context_uri == PLAYLIST
    assert rig.state.player.play_origin.feature_identifier == "playlist"
    assert rig.state.player.play_origin.device_identifier == OTHER_DEVICE
    assert rig.r._play_status.kind == "loading_play"
    assert len(rig.state.player.next_tracks) == 9


def test_play_with_skip_to_uri_uid_and_index(rig):
    rig.play_playlist(skip_to={"track_uri": uri(4)})
    assert rig.player.last("load")[1] == uri(4)
    assert rig.state.player.index.track == 4

    rig.play_playlist(skip_to={"track_uid": "uid7"})
    assert rig.player.last("load")[1] == uri(7)

    rig.play_playlist(skip_to={"track_index": 2})
    assert rig.player.last("load")[1] == uri(2)


def test_play_initially_paused_and_with_seek(rig):
    rig.play_playlist(initially_paused=True, seek_to=42_000)
    assert rig.player.last("load") == ("load", uri(0), False, 42_000)
    assert rig.r._play_status.kind == "loading_pause"


def test_play_with_shuffle_override_keeps_requested_track_first(rig):
    rig.play_playlist(skip_to={"track_uri": uri(6)}, player_options_override={"shuffling_context": True})
    assert rig.state.shuffling_context
    assert rig.player.last("load")[1] == uri(6)
    upcoming = [t.uri for t in rig.state.player.next_tracks]
    assert sorted(upcoming) == sorted(uri(i) for i in range(10) if i != 6)


def test_play_inline_tracks_without_context_uri(rig):
    ok = rig.command(
        "play",
        context={"pages": [{"tracks": [{"uri": uri(50)}, {"uri": uri(51)}]}]},
        play_origin={},
        options={},
    )
    assert ok
    assert rig.player.last("load")[1] == uri(50)
    assert [t.uri for t in rig.state.player.next_tracks] == [uri(51)]


def test_play_unknown_context_fails_cleanly(rig):
    replies = []

    class Req:
        payload = {
            "message_id": 3,
            "sent_by_device_id": OTHER_DEVICE,
            "command": {"endpoint": "play", "context": {"uri": "spotify:album:missing"}, "options": {}},
        }

        @staticmethod
        def reply(success):
            replies.append(success)

    rig.r._on_request(rig.session, Req)
    assert replies == [False]
    assert rig.player.loads() == []


def test_unsupported_command_is_refused(rig):
    assert rig.command("definitely_not_a_command") is False


# ----- transport ---------------------------------------------------------------------------


def test_pause_and_resume_keep_position(rig):
    rig.play_playlist()
    rig.started(position_ms=0)
    assert rig.r._play_status.kind == "playing"

    rig.r._play_status.nominal_start_time -= 5_000  # five seconds have been played
    assert rig.command("pause")
    assert rig.player.last("pause") == ("pause",)
    status = rig.r._play_status
    assert status.kind == "paused" and 4_900 <= status.position_ms <= 5_200

    assert rig.command("resume")
    assert rig.player.last("play") == ("play",)
    assert rig.r._play_status.kind == "playing"
    assert 4_900 <= rig.r._position() <= 5_300


def test_seek_updates_position(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("seek_to", value=90_000)
    assert rig.player.last("seek") == ("seek", 90_000)
    assert 89_900 <= rig.r._position() <= 90_300
    assert rig.state.player.position_as_of_timestamp == 90_000

    assert rig.command("seek_to", relative="current", position=-30_000)
    assert 59_000 <= rig.player.last("seek")[1] <= 61_000


def test_seek_past_the_end_is_ignored(rig):
    rig.play_playlist()
    rig.started(duration_ms=100_000)
    before = len(rig.player.calls)
    rig.command("seek_to", value=500_000)
    assert len(rig.player.calls) == before


def test_skip_next_and_prev(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("skip_next")
    assert rig.player.last("load") == ("load", uri(1), True, 0)
    rig.started()
    assert rig.command("skip_next")
    assert rig.state.current_uri == uri(2)
    rig.started()

    # less than three seconds in: previous track
    assert rig.command("skip_prev")
    assert rig.player.last("load") == ("load", uri(1), True, 0)
    rig.started()

    # further in: restart the current track instead
    rig.r._play_status.nominal_start_time -= 10_000
    assert rig.command("skip_prev")
    assert rig.player.last("seek") == ("seek", 0)
    assert rig.state.current_uri == uri(1)


def test_skip_next_to_a_specific_track(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("skip_next", track={"uri": uri(5)})
    assert rig.player.last("load")[1] == uri(5)


def test_skip_while_paused_loads_paused(rig):
    rig.play_playlist()
    rig.started()
    rig.command("pause")
    rig.command("skip_next")
    assert rig.player.last("load") == ("load", uri(1), False, 0)


def test_end_of_track_advances_and_end_of_context_stops(rig):
    rig.sp.contexts[PLAYLIST] = context(2)
    rig.play_playlist()
    rig.started()
    rig.event("end_of_track")
    assert rig.player.last("load") == ("load", uri(1), True, 0)
    rig.started()
    rig.event("end_of_track")
    assert rig.player.last("stop") == ("stop",)
    assert rig.r._play_status.kind == "stopped"


def test_repeat_track_replays_the_same_track(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("set_repeating_track", value=True)
    rig.event("end_of_track")
    assert rig.player.last("load") == ("load", uri(0), True, 0)
    assert rig.state.repeat_track


def test_repeat_context_wraps(rig):
    rig.sp.contexts[PLAYLIST] = context(2)
    rig.play_playlist()
    rig.started()
    assert rig.command("set_repeating_context", value=True)
    rig.event("end_of_track")
    rig.started()
    rig.event("end_of_track")
    assert rig.player.last("load") == ("load", uri(0), True, 0)


def test_autoplay_continues_after_the_context(rig):
    rig.r.config.autoplay = True
    rig.sp.contexts[PLAYLIST] = context(1)
    rig.sp.autoplay = context(3, "spotify:station:playlist:x", start=900)
    rig.play_playlist()
    rig.started()
    rig.event("end_of_track")
    assert rig.player.last("load") == ("load", uri(900), True, 0)
    assert rig.state.player.track.provider == "autoplay"


def test_shuffle_and_options_commands(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("set_shuffling_context", value=True)
    assert rig.state.shuffling_context and rig.state.current_uri == uri(0)
    assert rig.command("set_options", shuffling_context=False, repeating_context=True, repeating_track=False)
    assert not rig.state.shuffling_context and rig.state.repeat_context
    assert [t.uri for t in rig.state.player.next_tracks][:3] == [uri(1), uri(2), uri(3)]


def test_queue_commands(rig):
    rig.play_playlist()
    rig.started()
    assert rig.command("add_to_queue", track={"uri": uri(77)})
    assert rig.state.player.next_tracks[0].uri == uri(77)
    assert rig.command("skip_next")
    assert rig.player.last("load")[1] == uri(77)
    rig.started()
    assert rig.command(
        "set_queue",
        next_tracks=[{"uri": uri(8), "provider": "context"}, {"uri": uri(9), "provider": "context"}],
        prev_tracks=[],
    )
    rig.event("end_of_track")
    assert rig.player.last("load")[1] == uri(8)


def test_unavailable_track_is_skipped(rig):
    rig.play_playlist()
    rig.event("unavailable", uri=uri(0), reason="restricted")
    assert rig.player.last("load") == ("load", uri(1), True, 0)


def test_preload_requests_the_next_track(rig):
    rig.play_playlist()
    rig.started()
    rig.event("time_to_preload")
    assert rig.player.last("preload") == ("preload", uri(1))


def test_events_from_an_older_track_are_ignored(rig):
    rig.play_playlist()
    rig.started()
    rig.command("skip_next")
    rig.r._on_player_event(rig.session, "end_of_track", rig.r._play_id - 1, {})
    assert rig.state.current_uri == uri(1)


# ----- transfer ------------------------------------------------------------------------------


def test_transfer_continues_the_same_track_at_its_position(rig):
    assert rig.command("transfer", data=transfer_data(3, position=30_000), options={})
    call = rig.player.last("load")
    assert call[1] == uri(3) and call[2] is True
    assert 30_000 <= call[3] <= 32_000  # position plus the time since it was measured
    assert rig.state.is_active
    assert rig.state.player.index.track == 3
    assert rig.state.player.play_origin.device_identifier == OTHER_DEVICE
    # the state is pushed right away so the controller sees the takeover
    assert rig.sp.puts and rig.sp.puts[-1].is_active


def test_transfer_paused_stays_paused_unless_asked_to_resume(rig):
    assert rig.command("transfer", data=transfer_data(2, paused=True, position=12_345), options={})
    assert rig.player.last("load") == ("load", uri(2), False, 12_345)

    assert rig.command("transfer", data=transfer_data(2, paused=True), options={"restore_paused": "resume"})
    assert rig.player.last("load")[2] is True


def test_transfer_without_data_is_accepted_and_does_nothing(rig):
    assert rig.command("transfer", options={})
    assert rig.player.loads() == []


def test_transfer_with_unresolvable_context_falls_back_to_the_track(rig):
    rig.sp.contexts.pop(PLAYLIST)
    rig.sp.contexts[uri(5)] = context(1, uri(5), start=5)
    assert rig.command("transfer", data=transfer_data(5), options={})
    assert rig.player.last("load")[1] == uri(5)


# ----- connect state ------------------------------------------------------------------------


def test_notify_reports_playing_state(rig):
    rig.play_playlist()
    rig.started(position_ms=1_000)
    rig.r._notify()
    put = rig.sp.puts[-1]
    assert put.is_active and put.member_type == connect_pb2.CONNECT_STATE
    assert put.put_state_reason == connect_pb2.PLAYER_STATE_CHANGED
    p = put.device.player_state
    assert p.is_playing and not p.is_paused and not p.is_buffering
    assert p.track.uri == uri(0) and p.duration == 200_000
    assert p.position_as_of_timestamp >= 1_000
    assert put.device.device_info.name == "Test"
    assert put.client_side_timestamp > 0 and put.started_playing_at > 0


def test_volume_message_sets_player_and_state(rig):
    rig.r._set_volume(20_000)
    assert rig.player.last("set_volume") == ("set_volume", 20_000)
    assert rig.state.device_info.volume == 20_000
    rig.r._update_volume_at = 0.0001
    rig.r._run_timers()
    reasons = [p.put_state_reason for p in rig.sp.puts]
    assert connect_pb2.VOLUME_CHANGED in reasons
    rig.r._set_volume(999_999)
    assert rig.state.device_info.volume == 65_535


def cluster_update(active_device: str, timestamp: int):
    from spconnect.dealer import Message

    update = connect_pb2.ClusterUpdate()
    update.cluster.active_device_id = active_device
    update.cluster.player_state.timestamp = timestamp
    return Message(uri="hm://connect-state/v1/cluster", headers={}, payload=update.SerializeToString())


def test_playback_moving_to_another_device_stops_us(rig):
    from spconnect.receiver import now_ms

    rig.play_playlist()
    rig.started()
    rig.r._on_cluster_update(cluster_update(OTHER_DEVICE, now_ms() + 1_000))
    assert not rig.state.is_active
    assert rig.player.last("stop") == ("stop",)
    assert rig.sp.inactive == 1


def test_stale_cluster_update_does_not_stop_us(rig):
    assert rig.command("transfer", data=transfer_data(1), options={})
    rig.started()
    stale = rig.r._activation_timestamp - 5_000
    rig.r._on_cluster_update(cluster_update(OTHER_DEVICE, stale))
    assert rig.state.is_active
    assert ("stop",) not in rig.player.calls


def test_cluster_update_naming_us_keeps_playing(rig):
    from spconnect.receiver import now_ms

    rig.play_playlist()
    rig.started()
    rig.r._on_cluster_update(cluster_update(rig.r.device_id, now_ms() + 1_000))
    assert rig.state.is_active


def test_connection_id_registers_the_device(rig):
    from spconnect.dealer import Message

    rig.r._connect_established = False
    rig.session.connection_id = ""
    rig.r._on_connection_id(Message(uri="hm://pusher/v1/connections/x", headers={"Spotify-Connection-Id": "abc"}))
    assert rig.session.connection_id == "abc"
    assert rig.r._connect_established
    assert rig.sp.puts[-1].put_state_reason == connect_pb2.NEW_DEVICE
    assert not rig.sp.puts[-1].is_active
    assert rig.r.status()["state"] == "ready"


# ----- misc -----------------------------------------------------------------------------------


def test_parse_context_tolerates_unknown_and_odd_fields():
    ctx = parse_context(
        {
            "uri": PLAYLIST,
            "metadata": {"a": "b"},
            "something_new": {"x": 1},
            "pages": [{"tracks": [{"uri": uri(1), "uid": "u1", "metadata": {"k": "v"}, "extra": 5}]}],
        }
    )
    assert ctx.uri == PLAYLIST
    assert ctx.pages[0].tracks[0].uri == uri(1)
    assert ctx.pages[0].tracks[0].metadata["k"] == "v"
    assert parse_context(None).uri == ""


def test_state_file_roundtrip(tmp_path):
    from spconnect.ap import Credentials

    path = tmp_path / "state.json"
    first = Receiver(ReceiverConfig(state_path=str(path)), NullSink())
    first._store_credentials(Credentials("user", 1, b"\x00\x01secret"))
    first._set_volume(12_345)

    second = Receiver(ReceiverConfig(state_path=str(path)), NullSink())
    assert second.device_id == first.device_id
    assert second._stored_credentials() == Credentials("user", 1, b"\x00\x01secret")
    assert second._saved["volume"] == 12_345
    second._store_credentials(None)
    assert Receiver(ReceiverConfig(state_path=str(path)), NullSink())._stored_credentials() is None


def test_status_snapshot(rig):
    rig.play_playlist()
    rig.started()
    status = rig.r.status()
    assert status["state"] == "active" and status["playing"] is True
    assert status["track"]["name"] == "Song"
    assert status["context_uri"] == PLAYLIST
