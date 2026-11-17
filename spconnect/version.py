"""Version and client identification constants."""

from __future__ import annotations

import platform

__version__ = "0.1.0"

# Protocol version of the Spotify desktop client we identify as (from librespot).
SPOTIFY_VERSION = 124200290
SPOTIFY_SEMANTIC_VERSION = "1.2.52.442"
SPIRC_VERSION = "3.2.6"

# Spotify's "keymaster" client id, used by all open-source Connect receivers.
CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"

VERSION_STRING = f"spconnect-python {__version__}"
SYSTEM_INFO = f"spconnect-python-{__version__}"


def user_agent() -> str:
    system = platform.system().lower()
    name = {"windows": "Win32", "darwin": "OSX"}.get(system, "Linux")
    return f"Spotify/{SPOTIFY_VERSION} {name}/0 ({SYSTEM_INFO})"
