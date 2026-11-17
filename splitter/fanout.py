"""Thread-safe PCM buffers and a fan-out that copies one stream to many sinks.

The Spotify Connect source produces a single stream of raw PCM. Every Bluetooth
speaker gets its own RingBuffer; FanOut.write() copies each incoming chunk into
all of them. Buffers are bounded: when a speaker falls behind, its oldest audio
is dropped so latency cannot grow without limit.

Speaker alignment: each buffer starts with `prime` bytes of silence. A speaker
with less output latency gets more, so every speaker plays the same audio at the
same moment. flush() restores exactly that amount of silence, which keeps the
speakers aligned across pauses, seeks and track changes.
"""

from __future__ import annotations

import threading


class RingBuffer:
    """Bounded FIFO byte buffer. Overflow drops the oldest bytes."""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity = capacity_bytes
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._prime = 0
        # True while running dry is expected (nothing written yet, or just flushed).
        self._dry = True
        self.dropped_bytes = 0
        self.underruns = 0  # times the buffer ran dry in the middle of a stream

    @property
    def capacity(self) -> int:
        with self._lock:
            return self._capacity

    @property
    def buffered(self) -> int:
        with self._lock:
            return len(self._buf)

    @property
    def pending(self) -> int:
        """Bytes of real audio queued, not counting the alignment silence."""
        with self._lock:
            return max(0, len(self._buf) - self._prime)

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            self._dry = False
            overflow = len(self._buf) - self._capacity
            if overflow > 0:
                del self._buf[:overflow]
                self.dropped_bytes += overflow

    def read(self, n: int) -> bytes:
        """Return up to n bytes. Returns fewer (possibly none) on underrun."""
        with self._lock:
            take = min(n, len(self._buf))
            out = bytes(self._buf[:take])
            del self._buf[:take]
            if take < n and not self._dry:
                self._dry = True
                self.underruns += 1
            return out

    def prime(self, n: int) -> None:
        """Queue n bytes of silence ahead of the stream and grow capacity to fit.

        Used for latency alignment: a speaker with less inherent latency is
        primed with more silence so all speakers play the same audio together.
        """
        if n <= 0:
            return
        with self._lock:
            self._capacity += n
            self._prime += n
            self._buf += bytes(n)

    def flush(self) -> None:
        """Drop queued audio, keeping only the alignment silence."""
        with self._lock:
            self._buf = bytearray(self._prime)
            self._dry = True


class FanOut:
    """Copies every written chunk into each registered RingBuffer."""

    def __init__(self) -> None:
        self._sinks: dict[str, RingBuffer] = {}
        self._lock = threading.Lock()
        self.bytes_in = 0  # total bytes received from the source

    def add(self, name: str, capacity_bytes: int) -> RingBuffer:
        with self._lock:
            if name in self._sinks:
                raise ValueError(f"sink {name!r} already registered")
            buf = RingBuffer(capacity_bytes)
            self._sinks[name] = buf
            return buf

    def get(self, name: str) -> RingBuffer:
        with self._lock:
            return self._sinks[name]

    def remove(self, name: str) -> None:
        with self._lock:
            self._sinks.pop(name, None)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._sinks)

    def _buffers(self) -> list[RingBuffer]:
        with self._lock:
            return list(self._sinks.values())

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self.bytes_in += len(chunk)
            sinks = list(self._sinks.values())
        for buf in sinks:
            buf.write(chunk)

    def pending_bytes(self) -> int | None:
        """Audio queued in the emptiest buffer, or None when there are no sinks."""
        buffers = self._buffers()
        if not buffers:
            return None
        return min(buf.pending for buf in buffers)

    def flush(self) -> None:
        for buf in self._buffers():
            buf.flush()
