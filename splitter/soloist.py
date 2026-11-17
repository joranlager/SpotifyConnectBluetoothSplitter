"""Spotify Soloist: the official headless Spotify Connect client.

Soloist shows up in the Spotify app as a Connect device and plays on the Windows
default output. We run it as a child process and capture its sound with WASAPI
loopback (see capture.py), then fan that out to the speakers.

Soloist needs an API key from the Spotify for Developers dashboard (Premium
account). The key is read from the SOLOIST_API_KEY environment variable or from
a key file next to the config, never from config.json, and it is never logged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from array import array
from pathlib import Path

from .capture import CaptureInfo, LoopbackCapture
from .config import AppConfig
from .fanout import FanOut

log = logging.getLogger(__name__)

ENV_KEY = "SOLOIST_API_KEY"
EXIT_BUILD_EXPIRED = 10  # documented: Soloist builds expire 90 days after release
STARTUP_CHECK_S = 2.0
AUDIBLE_PEAK = 8  # int16 sample magnitude below which we call it silence
ACTIVE_WINDOW_S = 3.0


class SoloistError(RuntimeError):
    """Soloist could not be started."""


def _exe_names() -> tuple[str, ...]:
    return ("soloist.exe",) if sys.platform == "win32" else ("soloist",)


def find_executable(path: str, base_dir: Path) -> str | None:
    """Locate the Soloist executable.

    Tries, in order: the configured path (absolute, or relative to the config's
    folder), tools/soloist next to the config, then the system PATH.
    """
    candidate = Path(path)
    options: list[Path] = []
    if candidate.is_absolute():
        options.append(candidate)
    else:
        options.append(base_dir / candidate)
    for name in _exe_names():
        options.append(base_dir / "tools" / "soloist" / name)
    for option in options:
        if option.is_file():
            return str(option)
    return shutil.which(path)


def load_api_key(key_file: str, base_dir: Path) -> str | None:
    """The API key from the environment, else from the key file. None if neither is set."""
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        return env
    candidate = Path(key_file)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def build_command(exe: str, device_name: str, api_key: str, data_dir: Path, cache_dir: Path, extra: list[str]) -> list[str]:
    return [
        exe,
        "--device-name", device_name,
        "--api-key", api_key,
        "--data-dir", str(data_dir),
        "--cache-dir", str(cache_dir),
        *extra,
    ]


def redact(cmd: list[str], api_key: str) -> str:
    """The command line as a string with the API key hidden, safe to log."""
    return " ".join("<api-key>" if part == api_key else part for part in cmd)


def _tail(path: Path, lines: int = 8) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _peak(chunk: bytes) -> int:
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) & ~1])
    return max(max(samples), -min(samples)) if samples else 0


class SoloistSource:
    """Runs Soloist and the loopback capture. Same interface as the other sources."""

    def __init__(self, cfg: AppConfig, fan: FanOut, base_dir: Path, capture_info: CaptureInfo) -> None:
        self._cfg = cfg
        self._fan = fan
        self._base_dir = base_dir
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self._last_audible = 0.0
        self.capture = LoopbackCapture(capture_info, self._on_audio)
        self.data_dir = base_dir / ".soloist-data"

    # ----- audio from the capture ------------------------------------------------

    def _on_audio(self, chunk: bytes) -> None:
        if _peak(chunk) > AUDIBLE_PEAK:
            self._last_audible = time.monotonic()
        self._fan.write(chunk)

    # ----- lifecycle ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self.capture.active

    def start(self) -> None:
        cfg = self._cfg
        exe = find_executable(cfg.soloist_path, self._base_dir)
        if exe is None:
            raise SoloistError(
                f"Soloist was not found ({cfg.soloist_path}). Download it from Spotify's Soloist "
                "page and put it in the tools/soloist folder, or set its path in Advanced settings."
            )
        api_key = load_api_key(cfg.soloist_api_key_file, self._base_dir)
        if api_key is None:
            raise SoloistError(
                f"No Soloist API key. Create one on the Spotify for Developers dashboard, then set the "
                f"{ENV_KEY} environment variable or save the key in the file {cfg.soloist_api_key_file} "
                f"next to the config."
            )
        cache_dir = self.data_dir / "cache"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_command(exe, cfg.device_name, api_key, self.data_dir, cache_dir, cfg.soloist_args)

        log_path = self.data_dir / "soloist.log"
        self._log_file = open(log_path, "wb")  # noqa: SIM115 - closed in stop()
        self.capture.start()
        try:
            log.info("starting Soloist: %s", redact(cmd, api_key))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            deadline = time.monotonic() + STARTUP_CHECK_S
            while time.monotonic() < deadline:
                code = self._proc.poll()
                if code is not None:
                    raise SoloistError(self._exit_message(code, log_path))
                time.sleep(0.1)
        except BaseException:
            self.stop()
            raise

    @staticmethod
    def _exit_message(code: int, log_path: Path) -> str:
        if code == EXIT_BUILD_EXPIRED:
            return (
                "This Soloist build has expired (builds last 90 days). "
                "Download a newer one from Spotify's Soloist page."
            )
        detail = _tail(log_path)
        message = f"Soloist exited right after starting (code {code})."
        return f"{message} {detail}" if detail else message

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self.capture.stop()
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None

    # ----- status ------------------------------------------------------------------

    def exit_error(self) -> str | None:
        """Why Soloist stopped, once it has (so the web UI can say so)."""
        proc = self._proc
        if proc is None:
            return None
        code = proc.poll()
        if code is None:
            return None
        return self._exit_message(code, self.data_dir / "soloist.log")

    def status(self) -> dict:
        playing = time.monotonic() - self._last_audible < ACTIVE_WINDOW_S
        return {
            "state": "active" if playing else "waiting",
            "username": "",
            "error": None,
            "playing": playing,
            "track": None,
        }
