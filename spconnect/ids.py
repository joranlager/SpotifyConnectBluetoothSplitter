"""Spotify identifiers: 16-byte gids, base62 ids and spotify: URIs."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_URI_RE = re.compile(r"^spotify:([a-z]+):([0-9a-zA-Z]{21,22})$")

PLAYABLE_TYPES = ("track", "episode")


def gid_to_base62(gid: bytes) -> str:
    if len(gid) != 16:
        raise ValueError(f"gid must be 16 bytes, got {len(gid)}")
    n = int.from_bytes(gid, "big")
    out = []
    while n:
        n, rem = divmod(n, 62)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out)).rjust(22, "0")


def base62_to_gid(b62: str) -> bytes:
    n = 0
    for c in b62:
        if c not in _INDEX:
            raise ValueError(f"invalid base62 id: {b62!r}")
        n = n * 62 + _INDEX[c]
    if n >= 1 << 128:
        raise ValueError(f"base62 id out of range: {b62!r}")
    return n.to_bytes(16, "big")


@dataclass(frozen=True)
class SpotifyId:
    kind: str  # "track", "episode", ...
    gid: bytes

    @property
    def base62(self) -> str:
        return gid_to_base62(self.gid)

    @property
    def uri(self) -> str:
        return f"spotify:{self.kind}:{self.base62}"

    @property
    def hex(self) -> str:
        return self.gid.hex()

    @classmethod
    def from_uri(cls, uri: str) -> "SpotifyId":
        m = _URI_RE.match(uri)
        if not m:
            raise ValueError(f"not a simple spotify uri: {uri!r}")
        return cls(m.group(1), base62_to_gid(m.group(2)))

    @classmethod
    def from_gid(cls, kind: str, gid: bytes) -> "SpotifyId":
        if len(gid) != 16:
            raise ValueError("gid must be 16 bytes")
        return cls(kind, bytes(gid))

    def __str__(self) -> str:
        return self.uri


def is_playable_uri(uri: str) -> bool:
    m = _URI_RE.match(uri)
    return bool(m) and m.group(1) in PLAYABLE_TYPES
