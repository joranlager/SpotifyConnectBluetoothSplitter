"""Configuration loading, validation and saving (JSON file)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample, S16LE
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
# Only Spotify's own clients are supported; nothing here speaks Spotify's protocol or
# touches its DRM, we only route the sound the official client plays.
# "soloist":   Spotify's official headless client (Soloist). It plays on the Windows
#              default output and we capture that with WASAPI loopback.
# "headless":  the official Spotify desktop client, run headless on Linux by the
#              `spotify-headless` Rust tool (headless/), which writes PCM to stdout.
VALID_RECEIVERS = ("soloist", "headless")
DEFAULT_RECEIVER = "headless" if sys.platform.startswith("linux") else "soloist"
# Receivers that earlier versions offered. They are no longer supported.
REMOVED_RECEIVERS = ("builtin", "librespot")


def bytes_per_second(sample_rate: int) -> int:
    """Bytes per second of stereo S16LE audio at the given sample rate."""
    return sample_rate * CHANNELS * SAMPLE_WIDTH


@dataclass
class SpeakerConfig:
    match: str  # case-insensitive substring of the output device name
    delay_ms: float = 0.0  # extra manual delay to align this speaker
    volume: float = 1.0  # 0.0 - 1.0, applied live
    muted: bool = False


@dataclass
class AppConfig:
    device_name: str = "Bluetooth Splitter"
    receiver: str = DEFAULT_RECEIVER
    soloist_path: str = "soloist"  # executable; also searched in ./tools/soloist next to the config
    soloist_args: list[str] = field(default_factory=list)  # extra Soloist command-line flags
    # File holding the Soloist API key. The SOLOIST_API_KEY environment variable wins
    # over it. The key is a credential: it is never stored in this config.
    soloist_api_key_file: str = ".soloist-api-key"
    # receiver "headless": the spotify-headless tool (also searched in ./tools/spotify-headless
    # and ./headless/target/release next to the config) and the official Spotify client it runs.
    headless_path: str = "spotify-headless"
    headless_args: list[str] = field(default_factory=list)  # extra flags for spotify-headless
    spotify_bin: str = "spotify"  # the official client's executable
    # Folder for the client's config (the saved login), cache and logs. Default:
    # ".spotify-headless-data" next to the config file. It holds a login: keep it private.
    headless_data_dir: str | None = None
    headless_vnc_port: int = 5900  # loopback VNC port of the one-time setup session
    buffer_seconds: float = 2.0
    auto_latency_compensation: bool = True
    autostart: bool = True  # start streaming when the app launches
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    speakers: list[SpeakerConfig] = field(default_factory=list)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def parse_config(data: dict) -> AppConfig:
    _require(isinstance(data, dict), "config must be a JSON object")
    cfg = AppConfig(
        device_name=str(data.get("device_name", "Bluetooth Splitter")).strip() or "Bluetooth Splitter",
        receiver=str(data.get("receiver", DEFAULT_RECEIVER)),
        soloist_path=str(data.get("soloist_path", "soloist")).strip() or "soloist",
        soloist_args=[str(a) for a in data.get("soloist_args", [])],
        soloist_api_key_file=str(data.get("soloist_api_key_file", ".soloist-api-key")).strip()
        or ".soloist-api-key",
        headless_path=str(data.get("headless_path", "spotify-headless")).strip() or "spotify-headless",
        headless_args=[str(a) for a in data.get("headless_args", [])],
        spotify_bin=str(data.get("spotify_bin", "spotify")).strip() or "spotify",
        headless_data_dir=(str(data.get("headless_data_dir") or "").strip() or None),
        headless_vnc_port=int(data.get("headless_vnc_port", 5900)),
        buffer_seconds=float(data.get("buffer_seconds", 2.0)),
        auto_latency_compensation=bool(data.get("auto_latency_compensation", True)),
        autostart=bool(data.get("autostart", True)),
        web_host=str(data.get("web_host", "127.0.0.1")),
        web_port=int(data.get("web_port", 8080)),
        speakers=[],
    )
    _require(
        cfg.receiver not in REMOVED_RECEIVERS,
        f"receiver {cfg.receiver!r} is no longer supported; use one of {VALID_RECEIVERS} (Spotify's own clients)",
    )
    _require(cfg.receiver in VALID_RECEIVERS, f"receiver must be one of {VALID_RECEIVERS}")
    _require(0.2 <= cfg.buffer_seconds <= 30, "buffer_seconds must be between 0.2 and 30")
    _require(1 <= cfg.web_port <= 65535, "web_port must be between 1 and 65535")
    _require(1 <= cfg.headless_vnc_port <= 65535, "headless_vnc_port must be between 1 and 65535")
    seen: set[str] = set()
    for item in data.get("speakers", []):
        match = str(item.get("match", "")).strip()
        _require(bool(match), "every speaker needs a non-empty 'match'")
        _require(match.lower() not in seen, f"duplicate speaker match {match!r}")
        seen.add(match.lower())
        delay = float(item.get("delay_ms", 0.0))
        _require(0 <= delay <= 5000, "delay_ms must be between 0 and 5000")
        volume = float(item.get("volume", 1.0))
        _require(0.0 <= volume <= 1.0, "volume must be between 0 and 1")
        cfg.speakers.append(
            SpeakerConfig(
                match=match,
                delay_ms=delay,
                volume=volume,
                muted=bool(item.get("muted", False)),
            )
        )
    return cfg


def config_to_dict(cfg: AppConfig) -> dict:
    return asdict(cfg)


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as fh:
        return parse_config(json.load(fh))


def save_config(cfg: AppConfig, path: str | Path) -> None:
    """Write atomically so a crash mid-save never leaves a broken file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config_to_dict(cfg), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
