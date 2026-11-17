"""Sync test: a click every second, sent to all speakers together.

Bluetooth speakers add different, unreported delays. Played through every
speaker at once, a click that sounds doubled shows the speakers are out of sync;
the delay of the speaker that is early is then raised until it sounds like one
click.

The clicks go through each speaker's own alignment, exactly like music does, so
what you hear is what music gets. The source (Soloist or the built-in receiver)
is muted while the test runs.
"""

from __future__ import annotations

import math
import struct
import threading
import time

from .fanout import FanOut

CLICK_MS = 12
CLICK_HZ = 1800
PIECE_S = 0.05  # written in small pieces, paced in real time, like a live source


def make_click_second(rate: int) -> bytes:
    """One second of stereo S16LE audio: a short sharp tick, then silence."""
    frames = rate
    click_frames = max(1, rate * CLICK_MS // 1000)
    samples = bytearray(frames * 4)
    for i in range(click_frames):
        t = i / rate
        envelope = math.exp(-t / 0.003)  # fast decay: a tick, not a beep
        value = int(0.6 * 32767 * envelope * math.sin(2 * math.pi * CLICK_HZ * t))
        struct.pack_into("<hh", samples, i * 4, value, value)
    return bytes(samples)


def run_sync_test(fan: FanOut, rate: int, seconds: int, stop: threading.Event) -> None:
    """Play `seconds` clicks on every speaker, then hand the speakers back to the source."""
    second = make_click_second(rate)
    piece_bytes = int(rate * PIECE_S) * 4
    pieces = [second[i : i + piece_bytes] for i in range(0, len(second), piece_bytes)]
    fan.hold_input(True)
    try:
        fan.flush()  # restart every speaker from its own alignment delay
        t0 = time.monotonic()
        index = 0
        for _ in range(seconds):
            for piece in pieces:
                fan.write(piece, force=True)
                index += 1
                if stop.wait(max(0.0, t0 + index * PIECE_S - time.monotonic())):
                    return
        # let the last click play out
        deadline = time.monotonic() + 2.0
        while (fan.pending_bytes() or 0) > 0 and time.monotonic() < deadline:
            if stop.wait(0.05):
                return
    finally:
        fan.hold_input(False)
        fan.flush()
