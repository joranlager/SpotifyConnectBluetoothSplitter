"""spconnect: a Spotify Connect receiver written in Python.

    from spconnect import Receiver, ReceiverConfig, NullSink

    receiver = Receiver(ReceiverConfig(device_name="Living Room"), NullSink())
    receiver.start()      # the device now shows up in Spotify's device picker

Audio is delivered to a `Sink` (signed 16-bit little-endian, stereo, 44.1 kHz).

Protocol knowledge is based on the MIT-licensed librespot project
(https://github.com/librespot-org/librespot). Requires a Spotify Premium account.
"""

from .audio import CHANNELS, SAMPLE_RATE
from .player import NullSink, Sink
from .receiver import Receiver, ReceiverConfig
from .version import __version__

__all__ = [
    "CHANNELS",
    "SAMPLE_RATE",
    "NullSink",
    "Receiver",
    "ReceiverConfig",
    "Sink",
    "__version__",
]
