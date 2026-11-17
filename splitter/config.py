"""Configuration loading, validation and saving (JSON file)."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # bytes per sample, S16LE
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
VALID_BITRATES = (96, 160, 320)
# "builtin":   the pure-Python receiver in this repo (spconnect). No extra install.
# "librespot": an external librespot executable writing PCM to stdout.
VALID_RECEIVERS = ("builtin", "librespot")


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
    receiver: str = "builtin"
    librespot_path: str = "librespot"  # only used when receiver is "librespot"
    librespot_args: list[str] = field(default_factory=list)
    bitrate: int = 320
    cache_dir: str | None = None
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
        receiver=str(data.get("receiver", "builtin")),
        librespot_path=str(data.get("librespot_path", "librespot")),
        librespot_args=[str(a) for a in data.get("librespot_args", [])],
        bitrate=int(data.get("bitrate", 320)),
        cache_dir=data.get("cache_dir") or None,
        buffer_seconds=float(data.get("buffer_seconds", 2.0)),
        auto_latency_compensation=bool(data.get("auto_latency_compensation", True)),
        autostart=bool(data.get("autostart", True)),
        web_host=str(data.get("web_host", "127.0.0.1")),
        web_port=int(data.get("web_port", 8080)),
        speakers=[],
    )
    _require(cfg.receiver in VALID_RECEIVERS, f"receiver must be one of {VALID_RECEIVERS}")
    _require(cfg.bitrate in VALID_BITRATES, f"bitrate must be one of {VALID_BITRATES}")
    _require(0.2 <= cfg.buffer_seconds <= 30, "buffer_seconds must be between 0.2 and 30")
    _require(1 <= cfg.web_port <= 65535, "web_port must be between 1 and 65535")
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
