import pytest

from spconnect.ids import gid_to_base62
from spconnect.proto import context_pb2
from spconnect.proto import player_pb2
from spconnect.proto import transfer_state_pb2
from spconnect.state import (
    AUTOPLAY,
    DEFAULT,
    MAX_NEXT_TRACKS,
    MAX_PREV_TRACKS,
    ConnectState,
    PlayStatus,
    StateContext,
    StateError,
    is_delimiter,
    page_url_to_uri,
)


def uri(n: int) -> str:
    return "spotify:track:" + gid_to_base62(n.to_bytes(16, "big"))


def make_context(count: int, ctx_uri: str = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", start: int = 0) -> context_pb2.Context:
    ctx = context_pb2.Context()
    ctx.uri = ctx_uri
    ctx.url = f"context://{ctx_uri}"
    page = ctx.pages.add()
    for i in range(start, start + count):
        t = page.tracks.add()
        t.uri = uri(i)
        t.uid = f"uid{i}"
    return ctx


def new_state() -> ConnectState:
    return ConnectState("Test", "ab" * 20, "session")


def loaded(count: int, index: int = 0) -> ConnectState:
    s = new_state()
    s.update_context(make_context(count), DEFAULT)
    s.set_active_context(DEFAULT)
    s.set_current_track(index)
    s.reset_playback_to_position(index)
    return s


def uris(tracks) -> list[str]:
    return [t.uri for t in tracks if not is_delimiter(t)]


def test_initial_state_is_inactive_with_sane_defaults():
    s = new_state()
    assert not s.is_active
    assert s.player.playback_speed == 1.0
    assert s.player.session_id == "session"
    assert s.device_info.capabilities.can_be_player
    assert not s.has_track


def test_load_context_sets_current_prev_and_next():
    s = loaded(20, index=5)
    assert s.current_uri == uri(5)
    assert s.player.index.track == 5
    assert uris(s.player.prev_tracks) == [uri(i) for i in range(0, 5)]
    assert uris(s.player.next_tracks) == [uri(i) for i in range(6, 20)]
    assert s.player.context_uri == "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
    assert s.player.track.metadata["context_index"] == "5"


def test_windows_are_bounded():
    s = loaded(300, index=150)
    assert len(s.player.prev_tracks) == MAX_PREV_TRACKS
    assert len(s.player.next_tracks) == MAX_NEXT_TRACKS
    assert uris(s.player.prev_tracks)[-1] == uri(149)
    assert uris(s.player.next_tracks)[0] == uri(151)


def test_next_walks_the_whole_context_in_order():
    s = loaded(200)
    seen = [s.current_uri]
    while s.next_track():
        seen.append(s.current_uri)
    assert seen == [uri(i) for i in range(200)]
    assert not s.has_track


def test_prev_after_next_returns_to_previous_track():
    s = loaded(10)
    assert s.next_track() and s.next_track()
    assert s.current_uri == uri(2)
    assert s.prev_track()
    assert s.current_uri == uri(1)
    assert s.player.index.track == 1
    assert uris(s.player.next_tracks)[0] == uri(2)
    assert s.prev_track()
    assert s.current_uri == uri(0)
    assert not s.prev_track()


def test_repeat_context_wraps_around():
    s = loaded(3)
    s.handle_set_repeat_context(True)
    assert any(is_delimiter(t) for t in s.player.next_tracks)
    seen = [s.current_uri]
    for _ in range(7):
        assert s.next_track()
        seen.append(s.current_uri)
    assert seen == [uri(i % 3) for i in range(8)]


def test_queue_is_played_before_the_context_and_keeps_position():
    s = loaded(5)
    q = player_pb2.ProvidedTrack()
    q.uri = uri(100)
    s.add_to_queue(q)
    q.uri = uri(101)
    s.add_to_queue(q)
    assert uris(s.player.next_tracks)[:3] == [uri(100), uri(101), uri(1)]
    assert [t.provider for t in s.player.next_tracks[:2]] == ["queue", "queue"]
    assert s.player.next_tracks[0].uid != s.player.next_tracks[1].uid

    order = []
    while s.next_track():
        order.append(s.current_uri)
    assert order == [uri(100), uri(101), uri(1), uri(2), uri(3), uri(4)]


def test_queued_tracks_survive_loading_new_upcoming_tracks():
    s = loaded(5)
    q = player_pb2.ProvidedTrack()
    q.uri = uri(100)
    s.add_to_queue(q)
    s.clear_next_tracks()
    assert uris(s.player.next_tracks) == [uri(100)]


def test_shuffle_keeps_current_track_and_plays_every_track_once():
    s = loaded(50, index=7)
    s.handle_shuffle(True)
    assert s.shuffling_context
    assert s.current_uri == uri(7)
    order = [s.current_uri]
    while s.next_track():
        order.append(s.current_uri)
    assert sorted(order) == sorted(uri(i) for i in range(50))
    assert order != [uri(i) for i in range(50)]


def test_unshuffle_restores_order_around_current_track():
    s = loaded(30, index=3)
    s.handle_shuffle(True)
    for _ in range(4):
        s.next_track()
    current = s.current_uri
    s.handle_shuffle(False)
    assert not s.shuffling_context
    assert s.current_uri == current
    original = [uri(i) for i in range(30)]
    position = original.index(current)
    assert uris(s.player.next_tracks) == original[position + 1 :]
    assert [t.uri for t in s.get_context(DEFAULT).tracks] == original


def test_state_context_shuffle_is_reversible():
    ctx = StateContext(tracks=[player_pb2.ProvidedTrack(uri=uri(i)) for i in range(40)])
    ctx.shuffle(12345, uri(9))
    assert ctx.tracks[0].uri == uri(9)
    assert [t.uri for t in ctx.tracks] != [uri(i) for i in range(40)]
    ctx.unshuffle()
    assert [t.uri for t in ctx.tracks] == [uri(i) for i in range(40)]


def test_autoplay_context_continues_after_default_context():
    s = loaded(2)
    auto = make_context(3, "spotify:station:playlist:x", start=500)
    s.update_context(auto, AUTOPLAY)
    s.fill_up_next_tracks()
    order = []
    while s.next_track():
        order.append((s.current_uri, s.player.track.provider))
    assert order == [
        (uri(1), "context"),
        (uri(500), "autoplay"),
        (uri(501), "autoplay"),
        (uri(502), "autoplay"),
    ]
    assert s.active_context == AUTOPLAY


def test_unavailable_tracks_are_skipped():
    s = loaded(5)
    s.mark_unavailable(uri(1))
    s.mark_unavailable(uri(3))
    order = []
    while s.next_track():
        order.append(s.current_uri)
    assert order == [uri(2), uri(4)]


def test_repeat_with_everything_unavailable_does_not_loop_forever():
    s = loaded(2)
    s.set_repeat_context(True)
    s.mark_unavailable(uri(0))
    s.mark_unavailable(uri(1))
    assert len(s.player.next_tracks) <= MAX_NEXT_TRACKS


def test_set_queue_replaces_upcoming_tracks():
    s = loaded(5)
    a = player_pb2.ProvidedTrack(uri=uri(42))
    a.metadata["is_queued"] = "true"
    b = player_pb2.ProvidedTrack(uri=uri(3), provider="context")
    s.handle_set_queue([a, b], [])
    assert uris(s.player.next_tracks) == [uri(42), uri(3)]
    assert s.player.next_tracks[0].provider == "queue"
    assert s.player.queue_revision


def test_preview_next_uri_and_repeat_track():
    s = loaded(3)
    assert s.preview_next_uri() == uri(1)
    s.set_repeat_track(True)
    assert s.preview_next_uri() == uri(0)
    # skipping while repeating a track turns repeat-track off
    assert s.next_track()
    assert not s.repeat_track and s.current_uri == uri(1)


def test_status_flags():
    s = loaded(3)
    s.set_status(PlayStatus("playing"))
    assert s.player.is_playing and not s.player.is_paused and not s.player.is_buffering
    assert s.is_playing() and s.player.playback_speed == 1.0
    assert list(s.player.restrictions.disallow_resuming_reasons) == ["not_paused"]

    s.set_status(PlayStatus("paused", position_ms=100))
    assert s.player.is_playing and s.player.is_paused and s.player.is_buffering
    assert not s.is_playing() and s.player.playback_speed == 0.0
    assert list(s.player.restrictions.disallow_pausing_reasons) == ["not_playing"]

    s.set_status(PlayStatus("loading_play"))
    assert s.player.is_buffering and not s.player.is_paused


def test_position_bookkeeping():
    s = loaded(3)
    s.update_position(1000, 50_000)
    s.update_position_in_relation(52_500)
    assert s.player.position_as_of_timestamp == 3500
    assert s.player.timestamp == 52_500


def test_empty_context_is_rejected():
    s = new_state()
    ctx = context_pb2.Context(uri="spotify:album:x")
    ctx.pages.add()
    with pytest.raises(StateError):
        s.update_context(ctx, DEFAULT)


def test_extra_pages_with_tracks_are_appended_and_page_urls_returned():
    s = new_state()
    ctx = make_context(2)
    second = ctx.pages.add()
    second.tracks.add().uri = uri(50)
    third = ctx.pages.add()
    third.page_url = "hm://artistplaycontext/v1/page/spotify/album/5LFzwirfFwBKXJQGfwmiMY/km_artist"
    remaining = s.update_context(ctx, DEFAULT)
    assert [t.uri for t in s.get_context(DEFAULT).tracks] == [uri(0), uri(1), uri(50)]
    assert s.get_context(DEFAULT).tracks[2].metadata["context_index"] == "2"
    assert remaining == ["spotify:album:5LFzwirfFwBKXJQGfwmiMY"]


def test_page_url_to_uri():
    assert page_url_to_uri("hm://artistplaycontext/v1/page/spotify/album/abc/km_artist") == "spotify:album:abc"
    assert page_url_to_uri("hm://something/else") == ""


def test_gid_only_context_tracks_become_track_uris():
    s = new_state()
    ctx = context_pb2.Context(uri="spotify:album:x")
    page = ctx.pages.add()
    page.tracks.add().gid = (7).to_bytes(16, "big")
    s.update_context(ctx, DEFAULT)
    assert s.get_context(DEFAULT).tracks[0].uri == uri(7)


def make_transfer(
    current: int, paused: bool = False, shuffle: bool = False, repeat: bool = False
) -> transfer_state_pb2.TransferState:
    t = transfer_state_pb2.TransferState()
    t.options.shuffling_context = shuffle
    t.options.repeating_context = repeat
    t.playback.timestamp = 1_000_000
    t.playback.position_as_of_timestamp = 30_000
    t.playback.is_paused = paused
    t.playback.current_track.uri = uri(current)
    t.playback.current_track.uid = f"uid{current}"
    t.current_session.context.uri = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
    t.current_session.play_origin.feature_identifier = "playlist"
    return t


def test_transfer_positions_state_on_the_transferred_track():
    s = new_state()
    transfer = make_transfer(4, repeat=True)
    s.set_track(s.current_track_from_transfer(transfer))
    s.update_context(make_context(10), DEFAULT)
    s.handle_initial_transfer(transfer, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    s.finish_transfer(transfer)
    assert s.current_uri == uri(4)
    assert s.player.index.track == 4
    assert uris(s.player.prev_tracks) == [uri(i) for i in range(4)]
    assert uris(s.player.next_tracks)[:5] == [uri(i) for i in range(5, 10)]
    assert s.repeat_context
    assert s.player.play_origin.feature_identifier == "playlist"
    assert s.player.context_uri == "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"


def test_transfer_with_queue_keeps_queued_tracks_first():
    s = new_state()
    transfer = make_transfer(2)
    q = transfer.queue.tracks.add()
    q.uri = uri(77)
    s.set_track(s.current_track_from_transfer(transfer))
    s.update_context(make_context(6), DEFAULT)
    s.handle_initial_transfer(transfer, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    s.finish_transfer(transfer)
    assert s.current_uri == uri(2)
    assert uris(s.player.next_tracks)[:2] == [uri(77), uri(3)]


def test_transfer_of_track_missing_from_context_keeps_playing_it():
    s = new_state()
    transfer = make_transfer(999)
    s.set_track(s.current_track_from_transfer(transfer))
    s.update_context(make_context(4), DEFAULT)
    s.handle_initial_transfer(transfer, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    s.finish_transfer(transfer)
    assert s.current_uri == uri(999)
    assert uris(s.player.next_tracks) == [uri(i) for i in range(4)]


def test_transfer_with_shuffle_keeps_current_and_covers_context():
    s = new_state()
    transfer = make_transfer(3, shuffle=True)
    s.set_track(s.current_track_from_transfer(transfer))
    s.update_context(make_context(12), DEFAULT)
    s.handle_initial_transfer(transfer, "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
    s.finish_transfer(transfer)
    assert s.current_uri == uri(3)
    assert s.shuffling_context
    order = [s.current_uri]
    while s.next_track():
        order.append(s.current_uri)
    assert sorted(order) == sorted(uri(i) for i in range(12))
    assert len(order) == 12


def test_reset_clears_playback_but_keeps_session_id():
    s = loaded(3)
    s.set_active(True)
    s.reset()
    assert not s.is_active and not s.has_track
    assert s.player.session_id == "session"
    assert len(s.player.next_tracks) == 0
