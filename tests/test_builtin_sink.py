import time

from splitter.builtin import FanOutSink
from splitter.fanout import FanOut

BYTES_PER_MS = 44100 * 4 / 1000


def test_sink_reports_pending_from_the_speakers():
    fan = FanOut()
    buf = fan.add("speaker", 1_000_000)
    buf.prime(4000)
    sink = FanOutSink(fan)
    assert sink.pending_ms() == 0
    sink.write(bytes(int(BYTES_PER_MS * 200)))
    assert 199 <= sink.pending_ms() <= 201
    buf.read(int(BYTES_PER_MS * 50))  # the speaker consumed 50 ms worth of its queue
    assert 149 <= sink.pending_ms() <= 151
    sink.flush()
    assert sink.pending_ms() == 0
    assert buf.buffered == 4000  # alignment silence is kept


def test_sink_without_speakers_still_paces_in_real_time():
    sink = FanOutSink(FanOut())
    sink.write(bytes(int(BYTES_PER_MS * 300)))
    assert 250 < sink.pending_ms() <= 300
    time.sleep(0.1)
    assert sink.pending_ms() < 230
