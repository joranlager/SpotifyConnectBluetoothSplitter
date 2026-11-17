"""Spotify Connect receiver: runs librespot and reads its decoded PCM output.

librespot (https://github.com/librespot-org/librespot) handles Spotify Connect
discovery, authentication and decoding. We run it with the pipe backend and read
raw S16LE / 44.1 kHz / stereo audio from its stdout.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable

from .config import AppConfig

log = logging.getLogger(__name__)

CHUNK_BYTES = 4096


def build_command(cfg: AppConfig) -> list[str]:
    cmd = [
        cfg.librespot_path,
        "--name", cfg.device_name,
        "--device-type", "speaker",
        "--backend", "pipe",
        "--device", "-",  # write PCM to stdout
        "--format", "S16",
        "--bitrate", str(cfg.bitrate),
    ]
    if cfg.cache_dir:
        cmd += ["--cache", cfg.cache_dir]
    cmd += cfg.librespot_args
    return cmd


class LibrespotSource:
    """Starts librespot and forwards each PCM chunk to on_audio."""

    def __init__(self, cmd: list[str], on_audio: Callable[[bytes], None]) -> None:
        self._cmd = cmd
        self._on_audio = on_audio
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        log.info("starting librespot: %s", " ".join(self._cmd))
        # stderr is inherited so librespot logs (pairing, errors) show in the console.
        self._proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        self._thread = threading.Thread(target=self._pump, name="librespot-pump", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        while True:
            chunk = stream.read1(CHUNK_BYTES)
            if not chunk:
                break
            self._on_audio(chunk)
        log.warning("librespot audio stream ended")

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._proc = None
