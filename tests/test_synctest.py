import struct
import threading
import time

import pytest

from splitter.synctest import PIECE_S, make_click_second, run_sync_test


@pytest.mark.parametrize("rate", [44100, 48000, 8000])
def test_click_second_is_one_second_of_stereo_with_a_tick_at_the_start(rate):
    data = make_click_second(rate)
    assert len(data) == rate * 4
    samples = struct.unpack(f"<{rate * 2}h", data)
    assert max(abs(v) for v in samples[: rate // 50 * 2]) > 5000
    assert all(v == 0 for v in samples[rate // 20 * 2 :])
    assert samples[0::2] == samples[1::2]


class RecordingFan:
    def __init__(self):
        self.events = []
        self.writes = []

    def hold_input(self, hold):
        self.events.append(("hold", hold))

    def flush(self):
        self.events.append(("flush",))

    def write(self, chunk, force=False):
        self.writes.append((time.monotonic(), force, any(chunk)))

    def pending_bytes(self):
        return 0


def test_sync_test_clicks_once_per_second_paced_in_real_time():
    fan = RecordingFan()
    run_sync_test(fan, 8000, 2, threading.Event())
    assert fan.events[0] == ("hold", True) and fan.events[1] == ("flush",)
    assert fan.events[-2:] == [("hold", False), ("flush",)]
    per_second = round(1 / PIECE_S)
    assert len(fan.writes) == 2 * per_second
    assert all(force for _, force, _ in fan.writes)
    assert [i for i, w in enumerate(fan.writes) if w[2]] == [0, per_second]
    assert fan.writes[-1][0] - fan.writes[0][0] > 1.7


def test_sync_test_stops_promptly_and_still_releases_the_source():
    fan = RecordingFan()
    stop = threading.Event()
    thread = threading.Thread(target=run_sync_test, args=(fan, 8000, 30, stop))
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert fan.events[-2:] == [("hold", False), ("flush",)]
