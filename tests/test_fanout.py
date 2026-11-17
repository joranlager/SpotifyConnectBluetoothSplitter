import threading

from splitter.fanout import FanOut, RingBuffer


def test_write_then_read_returns_same_bytes():
    buf = RingBuffer(100)
    buf.write(b"abcd")
    assert buf.read(4) == b"abcd"
    assert buf.buffered == 0


def test_read_underrun_returns_fewer_bytes():
    buf = RingBuffer(100)
    buf.write(b"ab")
    assert buf.read(10) == b"ab"
    assert buf.read(10) == b""


def test_overflow_drops_oldest_bytes():
    buf = RingBuffer(4)
    buf.write(b"abcd")
    buf.write(b"ef")
    assert buf.buffered == 4
    assert buf.dropped_bytes == 2
    assert buf.read(4) == b"cdef"


def test_oversized_single_write_keeps_newest_capacity_bytes():
    buf = RingBuffer(3)
    buf.write(b"abcdef")
    assert buf.read(10) == b"def"
    assert buf.dropped_bytes == 3


def test_prime_inserts_silence_and_grows_capacity():
    buf = RingBuffer(4)
    buf.prime(2)
    assert buf.capacity == 6
    buf.write(b"wxyz")
    assert buf.read(6) == b"\x00\x00wxyz"


def test_fanout_copies_every_chunk_to_every_sink():
    fan = FanOut()
    a = fan.add("a", 100)
    b = fan.add("b", 100)
    fan.write(b"1234")
    fan.write(b"56")
    assert a.read(100) == b"123456"
    assert b.read(100) == b"123456"


def test_fanout_reads_do_not_affect_other_sinks():
    fan = FanOut()
    a = fan.add("a", 100)
    b = fan.add("b", 100)
    fan.write(b"xyz")
    a.read(3)
    assert b.read(3) == b"xyz"


def test_removed_sink_stops_receiving():
    fan = FanOut()
    a = fan.add("a", 100)
    fan.remove("a")
    fan.write(b"data")
    assert a.buffered == 0
    assert fan.names() == []


def test_duplicate_sink_name_rejected():
    fan = FanOut()
    fan.add("a", 10)
    try:
        fan.add("a", 10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_concurrent_writes_and_reads_keep_totals_consistent():
    fan = FanOut()
    buf = fan.add("a", 10_000_000)
    chunk = b"\x01" * 512
    n_writes = 500

    def writer():
        for _ in range(n_writes):
            fan.write(chunk)

    t = threading.Thread(target=writer)
    t.start()
    total = 0
    while t.is_alive() or buf.buffered:
        total += len(buf.read(1024))
    t.join()
    total += len(buf.read(10_000_000))
    assert total == 512 * n_writes


def test_pending_excludes_alignment_silence():
    buf = RingBuffer(100)
    buf.prime(10)
    assert buf.buffered == 10 and buf.pending == 0
    buf.write(b"x" * 30)
    assert buf.pending == 30
    buf.read(25)  # 10 bytes of silence + 15 of audio
    assert buf.pending == 5


def test_flush_keeps_only_the_alignment_silence():
    buf = RingBuffer(100)
    buf.prime(8)
    buf.write(b"audio-data")
    buf.read(3)
    buf.flush()
    assert buf.buffered == 8 and buf.pending == 0
    assert buf.read(100) == bytes(8)


def test_flush_restores_alignment_between_speakers():
    """After a flush, new audio reaches each speaker after exactly its own delay."""
    fan = FanOut()
    near, far = fan.add("near", 1000), fan.add("far", 1000)
    near.prime(40)  # the speaker with less latency waits longer
    far.prime(8)
    fan.write(b"A" * 100)
    near.read(70)
    far.read(20)  # the buffers have drifted apart
    fan.flush()
    fan.write(b"B" * 10)
    assert near.read(1000) == bytes(40) + b"B" * 10
    assert far.read(1000) == bytes(8) + b"B" * 10


def test_underruns_count_mid_stream_gaps_only():
    buf = RingBuffer(100)
    assert buf.read(10) == b""  # nothing written yet: not an underrun
    assert buf.underruns == 0
    buf.write(b"12345")
    buf.read(10)  # ran dry while playing
    buf.read(10)  # still dry: the same gap, counted once
    assert buf.underruns == 1
    buf.write(b"12345")
    buf.flush()  # pause/seek: draining afterwards is expected
    buf.read(10)
    assert buf.underruns == 1
    buf.write(b"abc")
    buf.read(10)
    assert buf.underruns == 2


def test_fanout_pending_is_the_emptiest_buffer():
    fan = FanOut()
    assert fan.pending_bytes() is None
    a, b = fan.add("a", 1000), fan.add("b", 1000)
    a.prime(50)
    fan.write(b"x" * 100)
    # Both speakers played 60 bytes; "a" keeps its 50-byte lead as extra queue depth.
    a.read(60)
    b.read(60)
    assert (a.pending, b.pending) == (40, 40)
    b.read(10)  # one speaker ran ahead
    assert fan.pending_bytes() == 30
