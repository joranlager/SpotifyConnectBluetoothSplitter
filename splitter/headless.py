"""The official Spotify desktop client, run headless by the `spotify-headless` Rust tool.

`headless/` in this repository is a small Rust program that runs Spotify's own
desktop client on a virtual display, routes its sound to a virtual audio sink and
writes that sound to its stdout as raw PCM (signed 16-bit little-endian, stereo,
44.1 kHz). This module runs that program and reads the PCM from its stdout, like
`soloist.py` does for Soloist's capture. Nothing is decrypted here: the client plays
and DRM-decodes the music itself, we only route the sound.

The client is a normal Spotify Connect device while it runs and is signed in, so
the phone's device list shows it (under the computer's host name, which the
client chooses itself).

Two things run the same Rust program:

* `HeadlessSource`: the streaming mode (`run --pcm-out -`), used by the engine.
* `HeadlessSetup`: the one-time setup mode. It starts the same program with a
  VNC server on the loopback so you can sign in to Spotify once in the client's
  window. The login is kept in the data folder, so later runs need no setup.

Linux only: the Rust program needs Xvfb and PulseAudio/PipeWire.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import SAMPLE_RATE, AppConfig
from .fanout import FanOut
from .soloist import ACTIVE_WINDOW_S, AUDIBLE_PEAK, _peak, _tail

log = logging.getLogger(__name__)

SUPPORTED = sys.platform.startswith("linux")
CHUNK_BYTES = 4096
STARTUP_CHECK_S = 2.0
STOP_TIMEOUT_S = 12.0  # the tool stops Xvfb and Spotify (up to 5 s each) before it exits


class HeadlessError(RuntimeError):
    """The Spotify client could not be started."""


# ----- finding things ------------------------------------------------------------------------


def _exe_names() -> tuple[str, ...]:
    return ("spotify-headless.exe",) if sys.platform == "win32" else ("spotify-headless",)


def find_executable(path: str, base_dir: Path) -> str | None:
    """Locate the spotify-headless executable.

    Tries, in order: the configured path (absolute, or relative to the config's
    folder), tools/spotify-headless and headless/target/release next to the config,
    then the system PATH.
    """
    candidate = Path(path)
    options: list[Path] = [candidate if candidate.is_absolute() else base_dir / candidate]
    for name in _exe_names():
        options.append(base_dir / "tools" / "spotify-headless" / name)
        options.append(base_dir / "headless" / "target" / "release" / name)
    for option in options:
        if option.is_file():
            return str(option)
    return shutil.which(path)


def data_dir(cfg: AppConfig, base_dir: Path) -> Path:
    """Where the client keeps its config (the login!), cache and the tool's logs."""
    if cfg.headless_data_dir:
        path = Path(cfg.headless_data_dir)
        return path if path.is_absolute() else base_dir / path
    return base_dir / ".spotify-headless-data"


def login_data_found(data: Path) -> bool:
    """Whether the client has saved a login. This is a heuristic: it looks for the
    per-user folder or the auto-login preference the desktop client writes."""
    spotify_cfg = data / "config" / "spotify"
    try:
        users = spotify_cfg / "Users"
        if users.is_dir() and any(p.is_dir() for p in users.iterdir()):
            return True
        prefs = spotify_cfg / "prefs"
        return prefs.is_file() and "autologin" in prefs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


# ----- command lines ---------------------------------------------------------------------------


def _common(exe: str, cfg: AppConfig, data: Path) -> list[str]:
    return [
        exe,
        "run",
        "--data-dir", str(data),
        "--log-dir", str(data / "logs"),
        "--spotify-bin", cfg.spotify_bin,
    ]


def build_run_command(exe: str, cfg: AppConfig, data: Path) -> list[str]:
    """Streaming mode: the PCM goes to stdout, the tool's own log lines to stderr."""
    return [*_common(exe, cfg, data), "--rate", str(SAMPLE_RATE), "--pcm-out", "-", *cfg.headless_args]


def build_setup_command(exe: str, cfg: AppConfig, data: Path) -> list[str]:
    """Setup mode: no audio capture, but a VNC server (loopback only) to sign in."""
    return [*_common(exe, cfg, data), "--vnc-port", str(cfg.headless_vnc_port), *cfg.headless_args]


def _missing_tool_message(path: str) -> str:
    return (
        f"spotify-headless was not found ({path}). Build it with `cargo build --release` in the "
        "headless folder, or set its path in Advanced settings."
    )


def _exit_message(code: int, log_path: Path) -> str:
    detail = _tail(log_path)
    message = f"spotify-headless exited (code {code})."
    return f"{message} {detail}" if detail else message


def _terminate(proc: subprocess.Popen) -> None:
    """Ask the tool to stop (it then stops Xvfb and Spotify), kill it if it hangs."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ----- streaming mode ----------------------------------------------------------------------------


class HeadlessSource:
    """Runs spotify-headless and forwards its PCM to the fan-out. Same interface as
    the other sources (start, stop, running, exit_error, status)."""

    def __init__(self, cfg: AppConfig, fan: FanOut, base_dir: Path) -> None:
        self._cfg = cfg
        self._fan = fan
        self._base_dir = base_dir
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._log_file = None
        self._last_audible = 0.0
        self.data_dir = data_dir(cfg, base_dir)

    @property
    def log_path(self) -> Path:
        return self.data_dir / "headless.log"

    # ----- audio ---------------------------------------------------------------------------------

    def _on_audio(self, chunk: bytes) -> None:
        if _peak(chunk) > AUDIBLE_PEAK:
            self._last_audible = time.monotonic()
        self._fan.write(chunk)

    def _pump(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        assert stream is not None
        while True:
            chunk = stream.read1(CHUNK_BYTES)
            if not chunk:
                break
            self._on_audio(chunk)
        log.warning("spotify-headless audio stream ended")

    # ----- lifecycle -----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if not SUPPORTED:
            raise HeadlessError("The Spotify client receiver only runs on Linux.")
        exe = find_executable(self._cfg.headless_path, self._base_dir)
        if exe is None:
            raise HeadlessError(_missing_tool_message(self._cfg.headless_path))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_run_command(exe, self._cfg, self.data_dir)
        self._log_file = open(self.log_path, "wb")  # noqa: SIM115 - closed in stop()
        try:
            log.info("starting spotify-headless: %s", " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._log_file
            )
            self._thread = threading.Thread(target=self._pump, args=(self._proc,), name="headless-pump", daemon=True)
            self._thread.start()
            deadline = time.monotonic() + STARTUP_CHECK_S
            while time.monotonic() < deadline:
                code = self._proc.poll()
                if code is not None:
                    raise HeadlessError(_exit_message(code, self.log_path))
                time.sleep(0.1)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            _terminate(proc)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None

    # ----- status --------------------------------------------------------------------------------

    def exit_error(self) -> str | None:
        """Why the tool stopped, once it has (so the web UI can say so)."""
        proc = self._proc
        if proc is None:
            return None
        code = proc.poll()
        return None if code is None else _exit_message(code, self.log_path)

    def status(self) -> dict:
        playing = time.monotonic() - self._last_audible < ACTIVE_WINDOW_S
        hint = None
        if not playing and not login_data_found(self.data_dir):
            hint = (
                "No saved Spotify login was found yet. If this device does not show up in Spotify, "
                "open Settings and run the Spotify client setup to sign in."
            )
        return {
            "state": "active" if playing else "waiting",
            "username": "",
            "error": hint,
            "playing": playing,
            "track": None,
        }


# ----- setup mode ---------------------------------------------------------------------------------


class HeadlessSetup:
    """The one-time setup session: the client on a virtual display with a loopback VNC
    server, so the user can sign in. Only one process of the tool may use the client's
    config folder at a time, so the engine refuses to stream while this runs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self._error: str | None = None

    @staticmethod
    def _log_path(data: Path) -> Path:
        return data / "setup.log"

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, cfg: AppConfig, base_dir: Path) -> None:
        with self._lock:
            if self.running():
                return
            self._cleanup()
            self._error = None
            if not SUPPORTED:
                raise HeadlessError("The Spotify client setup only runs on Linux.")
            exe = find_executable(cfg.headless_path, base_dir)
            if exe is None:
                raise HeadlessError(_missing_tool_message(cfg.headless_path))
            if shutil.which(cfg.spotify_bin) is None:
                raise HeadlessError(
                    f"The Spotify client was not found ({cfg.spotify_bin}). Install Spotify, "
                    "or set its path in Advanced settings."
                )
            data = data_dir(cfg, base_dir)
            data.mkdir(parents=True, exist_ok=True)
            cmd = build_setup_command(exe, cfg, data)
            self._log_file = open(self._log_path(data), "wb")  # noqa: SIM115 - closed in _cleanup()
            try:
                log.info("starting the Spotify client setup: %s", " ".join(cmd))
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._log_file
                )
                deadline = time.monotonic() + STARTUP_CHECK_S
                while time.monotonic() < deadline:
                    code = self._proc.poll()
                    if code is not None:
                        raise HeadlessError(_exit_message(code, self._log_path(data)))
                    time.sleep(0.1)
            except BaseException:
                self._cleanup()
                raise

    def stop(self) -> None:
        with self._lock:
            self._cleanup()

    def _cleanup(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            _terminate(proc)
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None

    def status(self, cfg: AppConfig, base_dir: Path) -> dict:
        with self._lock:
            data = data_dir(cfg, base_dir)
            if self._proc is not None and self._proc.poll() is not None:
                # It died by itself: keep the reason, so the UI can show it.
                self._error = _exit_message(self._proc.returncode, self._log_path(data))
                self._cleanup()
            return {
                "supported": SUPPORTED,
                "tool_path": find_executable(cfg.headless_path, base_dir),
                "spotify_found": shutil.which(cfg.spotify_bin) is not None,
                "login_data_found": login_data_found(data),
                "running": self.running(),
                "vnc_port": cfg.headless_vnc_port,
                "data_dir": str(data),
                "error": self._error,
            }
