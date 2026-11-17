import threading
import time

import pytest

from spconnect.audio import SAMPLE_RATE, Unavailable
from spconnect.player import MAX_VOLUME, NullSink, Player, volume_to_gain
from spconnect_helpers import loaded_track

A = "spotify:track:aaaaaaaaaaaaaaaaaaaaaa"
B = "spotify:track:bbbbbbbbbbbbbbbbbbbbbb"
BAD = "spotify:track:cccccccccccccccccccccc"


class FakeLoader:
    def __init__(self, seconds: float = 1.5, delay: float = 0.0) -> None:
        self.seconds = seconds
        self.delay = delay
        self.calls: list[tuple[str, int]] = []
        self.tracks = []

    def load(self, uri: str, position_ms: int = 0):
        self.calls.append((uri, position_ms))
        if self.delay:
            time.sleep(self.delay)
        if uri == BAD:
            raise Unavailable("not available")
        track = loaded_track(uri, self.seconds)
        if position_ms:
            track.seek_ms(position_ms)
        self.tracks.append(track)
        return track


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, int, dict]] = []
        self._cond = threading.Condition()

    def __call__(self, kind: str, play_id: int, data: dict) -> None:
        with self._cond:
            self.items.append((kind, play_id, data))
            self._cond.notify_all()

    def kinds(self, play_id: int | None = None) -> list[str]:
        with self._cond:
            return [k for k, pid, _ in self.items if play_id is None or pid == play_id]

    def wait_for(self, kind: str, play_id: int | None = None, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for k, pid, data in self.items:
                    if k == kind and (play_id is None or pid == play_id):
                        return data
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no '{kind}' event; got {[(k, p) for k, p, _ in self.items]}")
                self._cond.wait(remaining)


@pytest.fixture
def rig():
    made = []

    def make(loader=None):
        loader = loader or FakeLoader()
        sink, events = NullSink(), Events()
        player = Player(loader, sink, events)
        made.append(player)
        return player, loader, sink, events

    yield make
    for p in made:
        p.close()


def test_volume_curve():
    assert volume_to_gain(0) == 0.0
    assert volume_to_gain(MAX_VOLUME) == pytest.approx(1.0)
    assert 0 < volume_to_gain(MAX_VOLUME // 2) < 0.1
    assert volume_to_gain(MAX_VOLUME * 2) == pytest.approx(1.0)


def test_plays_a_track_to_the_end_in_real_time(rig):
    player, loader, sink, events = rig()
    started = time.monotonic()
    pid = player.load(A, True)
    events.wait_for("end_of_track", pid)
    elapsed = time.monotonic() - started

    kinds = events.kinds(pid)
    assert kinds[:3] == ["loading", "track_info", "playing"]
    assert "time_to_preload" in kinds
    assert kinds[-1] == "end_of_track"
    # the whole track was written, as 16-bit stereo
    assert sink.bytes_written == loader.tracks[0].frames_total * 4
    # paced by the sink, not decoded as fast as possible (1.5 s track, ~0.25 s lead)
    assert 0.9 < elapsed < 3.0


def test_load_paused_then_play(rig):
    player, _, sink, events = rig()
    pid = player.load(A, False)
    assert events.wait_for("paused", pid)["position_ms"] == 0
    time.sleep(0.2)
    assert sink.bytes_written == 0
    player.play()
    assert events.wait_for("playing", pid)["position_ms"] == 0
    events.wait_for("end_of_track", pid)


def test_pause_stops_output_and_resume_continues_from_the_same_position(rig):
    player, loader, sink, events = rig(FakeLoader(seconds=3.0))
    pid = player.load(A, True)
    events.wait_for("playing", pid)
    time.sleep(0.8)
    player.pause()
    paused_at = events.wait_for("paused", pid)["position_ms"]
    assert 500 <= paused_at <= 1100
    assert sink.pending_ms() == 0  # queued audio was dropped: silence is immediate
    written = sink.bytes_written
    time.sleep(0.3)
    assert sink.bytes_written == written
    assert abs(player.position_ms() - paused_at) < 20

    events.items.clear()
    player.play()
    resumed_at = events.wait_for("playing", pid)["position_ms"]
    assert abs(resumed_at - paused_at) <= 30  # nothing is skipped or replayed
    events.wait_for("end_of_track", pid)


def test_seek_moves_position(rig):
    player, _, _, events = rig(FakeLoader(seconds=4.0))
    pid = player.load(A, True)
    events.wait_for("playing", pid)
    player.seek(3000)
    assert abs(events.wait_for("seeked", pid)["position_ms"] - 3000) <= 30
    started = time.monotonic()
    events.wait_for("end_of_track", pid)
    assert time.monotonic() - started < 2.5  # only ~1 s was left


def test_load_at_position(rig):
    player, loader, _, events = rig(FakeLoader(seconds=3.0))
    pid = player.load(A, True, 2000)
    assert abs(events.wait_for("playing", pid)["position_ms"] - 2000) <= 30
    assert loader.calls == [(A, 2000)]


def test_new_load_supersedes_the_previous_one(rig):
    player, loader, _, events = rig(FakeLoader(seconds=1.0, delay=0.3))
    first = player.load(A, True)
    second = player.load(B, True)
    events.wait_for("end_of_track", second)
    assert "playing" not in events.kinds(first)
    assert "end_of_track" not in events.kinds(first)
    # the superseded track is closed, not leaked
    time.sleep(0.2)
    assert any(t.info.uri == A and t._source.closed for t in loader.tracks)


def test_unavailable_track_reports_an_event(rig):
    player, _, _, events = rig()
    pid = player.load(BAD, True)
    data = events.wait_for("unavailable", pid)
    assert data["uri"] == BAD
    assert "playing" not in events.kinds(pid)


def test_preloaded_track_is_used_and_playback_is_gapless(rig):
    player, loader, sink, events = rig(FakeLoader(seconds=1.5))
    first = player.load(A, True)
    events.wait_for("time_to_preload", first)
    player.preload(B)
    events.wait_for("end_of_track", first)
    pending_before = sink.pending_ms()
    second = player.load(B, True)
    events.wait_for("playing", second)
    # B was loaded once (by the preload), and the queued tail of A was not dropped
    assert loader.calls.count((B, 0)) == 1
    assert pending_before > 0
    events.wait_for("end_of_track", second)
    total_frames = sum(t.frames_total for t in loader.tracks)
    assert sink.bytes_written == total_frames * 4


def test_stop_ends_playback(rig):
    player, loader, sink, events = rig(FakeLoader(seconds=3.0))
    pid = player.load(A, True)
    events.wait_for("playing", pid)
    player.stop()
    events.wait_for("stopped", pid)
    written = sink.bytes_written
    time.sleep(0.3)
    assert sink.bytes_written == written
    assert loader.tracks[0]._source.closed
    assert "end_of_track" not in events.kinds(pid)


def test_download_failure_mid_track_reports_unavailable(rig):
    player, loader, _, events = rig(FakeLoader(seconds=20.0))
    pid = player.load(A, True)
    events.wait_for("playing", pid)
    loader.tracks[0]._source.fail = True  # every further download read now fails
    data = events.wait_for("unavailable", pid, timeout=15)
    assert "simulated download failure" in data["reason"]
    assert "end_of_track" not in events.kinds(pid)


def test_null_sink_paces_in_real_time():
    sink = NullSink()
    sink.write(bytes(SAMPLE_RATE * 4 // 2))  # half a second
    assert 400 < sink.pending_ms() <= 500
    time.sleep(0.2)
    assert 200 < sink.pending_ms() < 320
    sink.flush()
    assert sink.pending_ms() == 0
