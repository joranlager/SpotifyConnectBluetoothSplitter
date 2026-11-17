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


def test_set_prime_adds_delay_behind_queued_audio():
    buf = RingBuffer(1000)
    buf.prime(10)
    buf.write(b"A" * 20)
    buf.set_prime(30)  # 20 more bytes of delay
    buf.write(b"X" * 5)
    assert buf.read(1000) == bytes(10) + b"A" * 20 + bytes(20) + b"X" * 5
    assert buf.prime_bytes == 30


def test_set_prime_lower_skips_ahead_by_exactly_the_difference():
    buf = RingBuffer(1000)
    buf.prime(30)
    buf.write(b"B" * 5)
    buf.set_prime(10)  # 20 bytes less delay: drop the 20 oldest bytes
    assert buf.read(1000) == bytes(10) + b"B" * 5
    assert buf.prime_bytes == 10


def test_set_prime_lower_than_what_is_queued_does_not_fail():
    buf = RingBuffer(1000)
    buf.prime(50)
    buf.read(45)
    buf.set_prime(0)
    assert buf.prime_bytes == 0
    buf.write(b"ok")
    assert buf.read(10) == b"ok"


def test_flush_restores_the_changed_delay():
    buf = RingBuffer(1000)
    buf.prime(8)
    buf.set_prime(24)
    buf.write(b"zzz")
    buf.flush()
    assert buf.read(100) == bytes(24)


def test_max_pending_caps_how_far_a_slow_speaker_can_fall_behind():
    buf = RingBuffer(10_000)
    buf.prime(20)
    buf.max_pending = 100
    buf.write(bytes(range(150)))
    assert buf.dropped_bytes == 50
    assert buf.read(1000)[-10:] == bytes(range(140, 150))  # newest audio kept


def test_max_pending_is_not_hit_in_normal_operation():
    buf = RingBuffer(10_000)
    buf.prime(20)
    buf.max_pending = 1000
    for _ in range(50):
        buf.write(b"x" * 10)
        buf.read(10)
    assert buf.dropped_bytes == 0


def test_hold_input_drops_the_source_but_not_forced_writes():
    fan = FanOut()
    buf = fan.add("a", 1000)
    fan.hold_input(True)
    fan.write(b"source")
    fan.write(b"TEST", force=True)
    assert buf.read(100) == b"TEST"
    fan.hold_input(False)
    fan.write(b"source")
    assert buf.read(100) == b"source"
