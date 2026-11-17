"""Engine: owns the Spotify Connect source and the per-speaker outputs.

The engine can be started, stopped and reconfigured at runtime (used by the web
UI). All public methods are thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import SAMPLE_RATE, AppConfig, SpeakerConfig, bytes_per_second, save_config
from .fanout import FanOut
from .librespot import LibrespotSource, build_command

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_NOTE = (
    "This is the Windows default output, which the app listens to, so playing to it "
    "would echo. In Windows Sound settings, pick a different default output (or set the "
    "output of Soloist alone to another device), or choose another speaker."
)


def _ms_to_bytes(ms: float, rate: int = SAMPLE_RATE) -> int:
    frame = 4  # 2 channels * 2 bytes
    n = int(bytes_per_second(rate) * ms / 1000.0)
    return n - n % frame


@dataclass
class _Runtime:
    match: str
    device_name: str
    hostapi: str
    stream: object  # outputs.SpeakerStream (imported lazily)
    alignment_ms: float = 0.0


@dataclass
class _State:
    fan: FanOut | None = None
    source: object | None = None  # LibrespotSource, BuiltinSource or SoloistSource
    rate: int = SAMPLE_RATE  # sample rate of the audio flowing through the fan-out
    speakers: list[_Runtime] = field(default_factory=list)
    started_at: float | None = None
    last_error: str | None = None


class Engine:
    def __init__(self, cfg: AppConfig, config_path: str | Path | None = None) -> None:
        self._cfg = cfg
        self._config_path = Path(config_path) if config_path else None
        self._lock = threading.RLock()
        self._st = _State()
        self._skipped: dict[str, str] = {}  # match -> reason, from the last start

    # ----- lifecycle -------------------------------------------------------

    @property
    def config(self) -> AppConfig:
        return self._cfg

    def list_outputs(self):
        """The audio outputs Windows has now. Rescans devices unless we are streaming."""
        from .outputs import list_outputs

        with self._lock:
            return list_outputs(refresh=self._st.source is None)

    def running(self) -> bool:
        with self._lock:
            return self._st.source is not None and self._st.source.running

    def start(self) -> bool:
        from .outputs import SpeakerStream, find_output, list_outputs

        with self._lock:
            if self._st.source is not None:
                return True
            cfg = self._cfg
            self._st = _State()
            self._skipped = {}
            if not cfg.speakers:
                self._st.last_error = "No speakers configured. Add one in Settings."
                return False

            # The audio format. The built-in receiver and librespot deliver 44.1 kHz;
            # Soloist plays on the Windows default output, so we capture that and the
            # pipeline runs at the capture's rate.
            rate = SAMPLE_RATE
            capture = None
            if cfg.receiver == "soloist":
                from .capture import CaptureError, probe_loopback

                try:
                    capture = probe_loopback()
                except CaptureError as exc:
                    self._st.last_error = f"Cannot capture the default output: {exc}"
                    log.error("%s", self._st.last_error)
                    return False
                rate = capture.rate
                log.info("capturing %s at %d Hz", capture.name, rate)

            # No stream of ours is open here, so it is safe to rescan: speakers that
            # connected since the last start would otherwise be missing or misnumbered.
            outputs = list_outputs(refresh=True)
            fan = FanOut()
            capacity = _ms_to_bytes(cfg.buffer_seconds * 1000, rate)
            runtimes: list[_Runtime] = []
            for spk in cfg.speakers:
                try:
                    info = find_output(spk.match, outputs)
                except LookupError as exc:
                    self._skipped[spk.match] = str(exc)
                    log.warning("skipping speaker: %s", exc)
                    continue
                if capture is not None and info.name.lower() == capture.output_name.lower():
                    self._skipped[spk.match] = DEFAULT_OUTPUT_NOTE
                    log.warning("skipping %s: it is the default output being captured", info.name)
                    continue
                buf = fan.add(spk.match, capacity)
                try:
                    stream = SpeakerStream(spk.match, info.index, buf, info.hostapi, rate)
                except Exception as exc:  # PortAudio errors vary by driver
                    self._skipped[spk.match] = f"could not open: {exc}"
                    log.warning("could not open %s: %s", info.name, exc)
                    fan.remove(spk.match)
                    continue
                stream.volume = spk.volume
                stream.muted = spk.muted
                runtimes.append(_Runtime(spk.match, info.name, info.hostapi, stream))

            if not runtimes:
                self._st.last_error = "None of the configured speakers are connected."
                return False

            # Latency alignment: the slowest speaker sets the pace; faster speakers
            # get silence prepended so all speakers play the same audio together.
            max_latency_ms = max(r.stream.latency * 1000 for r in runtimes)
            spk_by_match = {s.match: s for s in cfg.speakers}
            for r in runtimes:
                manual = spk_by_match[r.match].delay_ms
                extra = manual
                if cfg.auto_latency_compensation:
                    extra += max_latency_ms - r.stream.latency * 1000
                r.alignment_ms = extra
                fan.get(r.match).prime(_ms_to_bytes(extra, rate))

            for r in runtimes:
                r.stream.start()

            try:
                if cfg.receiver == "builtin":
                    from .builtin import BuiltinSource

                    source = BuiltinSource(cfg, fan, self._receiver_state_path())
                elif cfg.receiver == "soloist":
                    from .soloist import SoloistSource

                    source = SoloistSource(cfg, fan, self._base_dir(), capture)
                else:
                    source = LibrespotSource(build_command(cfg), on_audio=fan.write)
                source.start()
            except Exception as exc:  # missing executable, port in use, missing package ...
                for r in runtimes:
                    r.stream.close()
                if cfg.receiver == "builtin":
                    message = f"Could not start the Spotify Connect receiver: {exc}"
                elif cfg.receiver == "soloist":
                    message = str(exc)
                else:
                    message = (
                        f"Could not start librespot ({cfg.librespot_path}): {exc}. "
                        "Install it or set librespot_path in Advanced settings."
                    )
                self._st = _State(last_error=message)
                log.error("%s", message)
                return False
            self._st.fan = fan
            self._st.source = source
            self._st.rate = rate
            self._st.speakers = runtimes
            self._st.started_at = time.monotonic()
            log.info("streaming to %d speaker(s)", len(runtimes))
            return True

    def stop(self) -> None:
        with self._lock:
            st = self._st
            if st.source is not None:
                st.source.stop()
            for r in st.speakers:
                try:
                    r.stream.close()
                except Exception as exc:  # pragma: no cover - device gone
                    log.warning("error closing %s: %s", r.match, exc)
            self._st = _State(last_error=st.last_error)

    def restart(self) -> bool:
        with self._lock:
            self.stop()
            return self.start()

    def check_health(self) -> None:
        """Record an error if the receiver exited while we think it is running."""
        with self._lock:
            source = self._st.source
            if source is not None and not source.running:
                reason = source.exit_error() if hasattr(source, "exit_error") else None
                self.stop()
                self._st.last_error = reason or "The Spotify receiver stopped. Check the console for details."

    def _base_dir(self) -> Path:
        """Folder of the config file; where keys, data and tools are looked for."""
        return self._config_path.resolve().parent if self._config_path else Path.cwd()

    def _receiver_state_path(self) -> Path | None:
        """Where the built-in receiver keeps its device id and saved login."""
        if self._config_path is None:
            return None
        return self._config_path.with_name(".spconnect-state.json")

    def control(self, action: str) -> None:
        """Transport control from the web UI: play, pause, next or prev."""
        if action not in ("play", "pause", "next", "prev"):
            raise ValueError("unknown control action")
        with self._lock:
            source = self._st.source
        if source is None or not hasattr(source, "control"):
            raise ValueError("playback control is not available")
        source.control(action)

    # ----- configuration ----------------------------------------------------

    def _persist(self) -> None:
        if self._config_path is not None:
            save_config(self._cfg, self._config_path)

    def update_config(self, cfg: AppConfig) -> bool:
        """Replace the configuration, save it, and restart if currently streaming."""
        with self._lock:
            was_running = self._st.source is not None
            self._cfg = cfg
            self._persist()
            if was_running:
                return self.restart()
            return True

    def update_speaker(self, match: str, **changes) -> SpeakerConfig:
        """Change one speaker. volume/muted apply live; delay_ms restarts the stream."""
        with self._lock:
            spk = next((s for s in self._cfg.speakers if s.match == match), None)
            if spk is None:
                raise KeyError(match)
            needs_restart = False
            if "volume" in changes:
                spk.volume = min(1.0, max(0.0, float(changes["volume"])))
            if "muted" in changes:
                spk.muted = bool(changes["muted"])
            if "delay_ms" in changes:
                delay = float(changes["delay_ms"])
                if not 0 <= delay <= 5000:
                    raise ValueError("delay_ms must be between 0 and 5000")
                spk.delay_ms = delay
                needs_restart = True
            for r in self._st.speakers:
                if r.match == match:
                    r.stream.volume = spk.volume
                    r.stream.muted = spk.muted
            self._persist()
            if needs_restart and self._st.source is not None:
                self.restart()
            return spk

    # ----- monitoring -------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            st = self._st
            running = st.source is not None and st.source.running
            live = {r.match: r for r in st.speakers}
            speakers = []
            for spk in self._cfg.speakers:
                r = live.get(spk.match)
                entry = {
                    "match": spk.match,
                    "delay_ms": spk.delay_ms,
                    "volume": spk.volume,
                    "muted": spk.muted,
                    "connected": r is not None,
                    "device_name": None,
                    "hostapi": None,
                    "latency_ms": None,
                    "alignment_ms": None,
                    "buffered_ms": None,
                    "underruns": None,
                    "dropped_bytes": None,
                    "note": self._skipped.get(spk.match),
                }
                if r is not None:
                    buf = r.stream.buffer
                    entry.update(
                        device_name=r.device_name,
                        hostapi=r.hostapi,
                        latency_ms=round(r.stream.latency * 1000, 1),
                        alignment_ms=round(r.alignment_ms, 1),
                        buffered_ms=round(buf.buffered * 1000 / bytes_per_second(st.rate), 1),
                        underruns=r.stream.underruns,
                        dropped_bytes=buf.dropped_bytes,
                        note=None,
                    )
                speakers.append(entry)
            spotify = None
            if running and hasattr(st.source, "status"):
                spotify = st.source.status()
            return {
                "state": "running" if running else ("error" if st.last_error else "stopped"),
                "receiver": self._cfg.receiver,
                "spotify": spotify,
                "device_name": self._cfg.device_name,
                "uptime_s": round(time.monotonic() - st.started_at) if running and st.started_at else 0,
                "last_error": st.last_error,
                "bytes_in": st.fan.bytes_in if st.fan else 0,
                "speakers": speakers,
            }


def _health_loop(engine: Engine, stop: threading.Event) -> None:
    while not stop.wait(1.0):
        engine.check_health()


def run_engine_with_web(cfg: AppConfig, config_path: str | Path, web: bool = True) -> int:
    """Start the engine (if autostart) and serve the web UI until Ctrl+C."""
    from .web import serve

    engine = Engine(cfg, config_path)
    if cfg.autostart:
        engine.start()
        err = engine.status()["last_error"]
        if err:
            log.warning("%s", err)
    stop = threading.Event()
    threading.Thread(target=_health_loop, args=(engine, stop), daemon=True).start()
    try:
        if web:
            serve(engine, cfg.web_host, cfg.web_port)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        stop.set()
        engine.stop()
    return 0
