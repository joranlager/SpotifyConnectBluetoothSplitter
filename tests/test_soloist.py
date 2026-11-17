import logging
import sys
import textwrap
from array import array

import pytest

from splitter import soloist as soloist_mod
from splitter.app import DEFAULT_OUTPUT_NOTE, Engine, _ms_to_bytes
from splitter.capture import CaptureInfo, to_stereo
from splitter.config import AppConfig, parse_config
from splitter.fanout import FanOut
from splitter.soloist import (
    ENV_KEY,
    SoloistError,
    SoloistSource,
    build_command,
    find_executable,
    load_api_key,
    redact,
)

SECRET = "sk-TEST-super-secret-key-123"
INFO = CaptureInfo(device_index=3, name="Speakers (Default Out) [Loopback]", rate=48000, channels=2)


# ----- config ------------------------------------------------------------------------


def test_soloist_is_a_valid_receiver_with_sane_defaults():
    cfg = parse_config({"receiver": "soloist"})
    assert cfg.receiver == "soloist"
    assert cfg.soloist_path == "soloist"
    assert cfg.soloist_api_key_file == ".soloist-api-key"
    assert cfg.soloist_args == []
    with pytest.raises(ValueError):
        parse_config({"receiver": "spotify"})


def test_api_key_is_never_part_of_the_config():
    from splitter.config import config_to_dict

    assert not any("key" in k and k != "soloist_api_key_file" for k in config_to_dict(AppConfig()))


# ----- finding things -------------------------------------------------------------------


def test_find_executable_prefers_configured_path_then_tools_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(soloist_mod.shutil, "which", lambda p: None)
    assert find_executable("soloist", tmp_path) is None

    tools = tmp_path / "tools" / "soloist"
    tools.mkdir(parents=True)
    exe = tools / soloist_mod._exe_names()[0]
    exe.write_text("x")
    assert find_executable("soloist", tmp_path) == str(exe)

    custom = tmp_path / "bin" / "mysoloist.exe"
    custom.parent.mkdir()
    custom.write_text("x")
    assert find_executable("bin/mysoloist.exe", tmp_path) == str(custom)
    assert find_executable(str(custom), tmp_path) == str(custom)


def test_find_executable_falls_back_to_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(soloist_mod.shutil, "which", lambda p: "C:/somewhere/soloist.exe")
    assert find_executable("soloist", tmp_path) == "C:/somewhere/soloist.exe"


def test_api_key_comes_from_env_first_then_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    assert load_api_key(".soloist-api-key", tmp_path) is None
    (tmp_path / ".soloist-api-key").write_text("  file-key \n")
    assert load_api_key(".soloist-api-key", tmp_path) == "file-key"
    monkeypatch.setenv(ENV_KEY, "env-key")
    assert load_api_key(".soloist-api-key", tmp_path) == "env-key"
    monkeypatch.setenv(ENV_KEY, "   ")
    assert load_api_key(".soloist-api-key", tmp_path) == "file-key"
    (tmp_path / "empty").write_text("\n")
    monkeypatch.delenv(ENV_KEY)
    assert load_api_key("empty", tmp_path) is None


def test_command_line_and_redaction(tmp_path):
    cmd = build_command("soloist.exe", "Living Room", SECRET, tmp_path / "d", tmp_path / "c", ["--verbose"])
    assert cmd[:5] == ["soloist.exe", "--device-name", "Living Room", "--api-key", SECRET]
    assert cmd[cmd.index("--data-dir") + 1] == str(tmp_path / "d")
    assert cmd[cmd.index("--cache-dir") + 1] == str(tmp_path / "c")
    assert cmd[-1] == "--verbose"
    shown = redact(cmd, SECRET)
    assert SECRET not in shown and "<api-key>" in shown and "Living Room" in shown


# ----- capture helpers ----------------------------------------------------------------------


def _pcm(values):
    return array("h", values).tobytes()


def test_to_stereo_handles_any_channel_count():
    stereo = _pcm([1, 2, 3, 4])
    assert to_stereo(stereo, 2) == stereo
    assert to_stereo(_pcm([5, 6, 7]), 1) == _pcm([5, 5, 6, 6, 7, 7])
    # 6 channels: keep front left/right of each frame
    six = _pcm([1, 2, 90, 90, 90, 90, 3, 4, 90, 90, 90, 90])
    assert to_stereo(six, 6) == _pcm([1, 2, 3, 4])
    # a partial trailing frame is dropped, not mangled
    assert to_stereo(_pcm([1, 2, 90, 90, 90, 90, 3]), 6) == _pcm([1, 2])
    assert to_stereo(b"", 6) == b""


def test_capture_info_output_name():
    assert INFO.output_name == "Speakers (Default Out)"


# ----- the Soloist process --------------------------------------------------------------------


class FakeCapture:
    def __init__(self, info, on_audio):
        self.info = info
        self.on_audio = on_audio
        self.active = False
        self.started = self.stopped = 0

    def start(self):
        self.active = True
        self.started += 1

    def stop(self):
        self.active = False
        self.stopped += 1


@pytest.fixture
def source_factory(tmp_path, monkeypatch):
    """Builds SoloistSource objects whose 'soloist' is a tiny Python script."""
    monkeypatch.delenv(ENV_KEY, raising=False)
    (tmp_path / ".soloist-api-key").write_text(SECRET)
    monkeypatch.setattr(soloist_mod, "LoopbackCapture", FakeCapture)
    made = []

    def make(script: str, *, stub_exe: bool = True, **cfg_kwargs):
        stub = tmp_path / "stub.py"
        stub.write_text(textwrap.dedent(script))
        real_popen = soloist_mod.subprocess.Popen
        if stub_exe:
            exe = tmp_path / "soloist-stub.exe"
            exe.write_text("x")
            monkeypatch.setattr(
                soloist_mod.subprocess,
                "Popen",
                lambda cmd, **kw: real_popen([sys.executable, str(stub), *cmd[1:]], **kw),
            )
            cfg_kwargs.setdefault("soloist_path", str(exe))
        cfg = AppConfig(receiver="soloist", device_name="Test Device", **cfg_kwargs)
        source = SoloistSource(cfg, FanOut(), tmp_path, INFO)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()


STAYS_RUNNING = """
    import sys, time
    print("args:", sys.argv[1:], flush=True)
    time.sleep(60)
"""


def test_start_reports_a_missing_executable(source_factory, monkeypatch):
    monkeypatch.setattr(soloist_mod.shutil, "which", lambda p: None)
    source = source_factory(STAYS_RUNNING, stub_exe=False, soloist_path="does-not-exist")
    with pytest.raises(SoloistError, match="not found"):
        source.start()
    assert source.capture.started == 0  # nothing was opened


def test_start_reports_a_missing_api_key(source_factory, tmp_path, monkeypatch):
    (tmp_path / ".soloist-api-key").unlink()
    monkeypatch.delenv(ENV_KEY, raising=False)
    source = source_factory(STAYS_RUNNING)
    with pytest.raises(SoloistError) as err:
        source.start()
    assert ENV_KEY in str(err.value) and ".soloist-api-key" in str(err.value)
    assert SECRET not in str(err.value)


def test_early_exit_explains_an_expired_build(source_factory):
    source = source_factory("import sys; sys.exit(10)")
    with pytest.raises(SoloistError, match="expired"):
        source.start()
    assert source.capture.stopped >= 1 and not source.capture.active
    assert not source.running


def test_early_exit_includes_the_end_of_soloist_log(source_factory):
    source = source_factory('import sys; print("invalid api key supplied"); sys.exit(3)')
    with pytest.raises(SoloistError) as err:
        source.start()
    assert "code 3" in str(err.value) and "invalid api key supplied" in str(err.value)


def test_soloist_runs_with_the_expected_arguments_and_stops_cleanly(source_factory, tmp_path, caplog):
    source = source_factory(STAYS_RUNNING, soloist_args=["--verbose"])
    with caplog.at_level(logging.DEBUG):
        source.start()
    assert source.running and source.capture.active
    assert not any(SECRET in r.getMessage() for r in caplog.records)  # the key is never logged

    log_text = (tmp_path / ".soloist-data" / "soloist.log").read_text()
    assert "--device-name" in log_text and "Test Device" in log_text
    assert SECRET in log_text  # Soloist itself did receive the key (this is its own output)

    proc = source._proc
    source.stop()
    assert proc.poll() is not None
    assert not source.running and source.capture.stopped >= 1
    source.stop()  # idempotent


def test_soloist_dying_later_is_noticed(source_factory):
    source = source_factory("import time; time.sleep(2.6)")
    source.start()
    assert source.running and source.exit_error() is None
    source._proc.wait(timeout=10)
    assert not source.running
    assert "exited" in source.exit_error()


def test_status_reflects_audible_audio_only(source_factory, monkeypatch):
    source = source_factory(STAYS_RUNNING)
    assert source.status() == {"state": "waiting", "username": "", "error": None, "playing": False, "track": None}
    source._on_audio(_pcm([0, 0, 1, -1] * 100))  # digital silence: Windows sends this while idle
    assert source.status()["playing"] is False
    source._on_audio(_pcm([0, 3000, -3000, 0] * 100))
    assert source.status()["playing"] is True and source.status()["state"] == "active"
    # all captured audio, silent or not, is passed on so speakers stay fed
    assert source._fan.bytes_in == 2 * 400 * 2
    monkeypatch.setattr(soloist_mod.time, "monotonic", lambda: source._last_audible + 10)
    assert source.status()["playing"] is False


# ----- engine integration -----------------------------------------------------------------------


class FakeStream:
    instances: list = []

    def __init__(self, name, index, buffer, hostapi="", samplerate=44100):
        self.name, self.index, self.buffer, self.samplerate = name, index, buffer, samplerate
        self.volume, self.muted, self.latency = 1.0, False, 0.05
        self.underruns = 0
        self.started = self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class FakeSource:
    def __init__(self, error=None):
        self.error = error
        self.running = True
        self.stopped = False

    def start(self):
        if self.error:
            raise self.error

    def stop(self):
        self.stopped = True
        self.running = False

    def status(self):
        return {"state": "waiting", "username": "", "error": None, "playing": False, "track": None}


@pytest.fixture
def engine_env(monkeypatch, tmp_path):
    from splitter import capture, outputs

    FakeStream.instances = []
    out = lambda i, n: outputs.OutputInfo(index=i, name=n, hostapi="Windows WASAPI", max_channels=2)  # noqa: E731
    devices = [out(1, "Speakers (Default Out)"), out(2, "Speakers (Sonos Move)")]
    refreshed = []
    monkeypatch.setattr(outputs, "list_outputs", lambda refresh=False: (refreshed.append(refresh), devices)[1])
    holder_refreshed = refreshed
    monkeypatch.setattr(outputs, "SpeakerStream", FakeStream)
    monkeypatch.setattr(capture, "probe_loopback", lambda: INFO)
    holder = {"refreshed": holder_refreshed}

    def make_engine(speakers, source=None):
        cfg = parse_config({"receiver": "soloist", "speakers": [{"match": m} for m in speakers]})
        engine = Engine(cfg, tmp_path / "config.json")
        src = source or FakeSource()
        monkeypatch.setattr(soloist_mod, "SoloistSource", lambda *a, **k: src)
        holder["source"] = src
        return engine

    return make_engine, holder


def test_engine_runs_the_pipeline_at_the_capture_rate(engine_env):
    make_engine, _ = engine_env
    engine = make_engine(["Sonos Move"])
    assert engine.start() is True
    stream = FakeStream.instances[0]
    assert stream.samplerate == 48000
    status = engine.status()
    assert status["state"] == "running" and status["receiver"] == "soloist"
    assert status["spotify"]["state"] == "waiting"
    # buffer depth is reported in real time at the capture rate
    stream.buffer.write(bytes(48000 * 4 // 2))
    assert engine.status()["speakers"][0]["buffered_ms"] == pytest.approx(500, abs=2)
    engine.stop()


def test_buffer_sizes_follow_the_sample_rate():
    assert _ms_to_bytes(1000, 44100) == 44100 * 4
    assert _ms_to_bytes(1000, 48000) == 48000 * 4
    assert _ms_to_bytes(10, 48000) % 4 == 0  # always whole frames


def test_default_output_speaker_is_skipped_to_avoid_an_echo(engine_env):
    make_engine, _ = engine_env
    engine = make_engine(["Default Out", "Sonos Move"])
    assert engine.start() is True
    assert [s.name for s in FakeStream.instances] == ["Sonos Move"]
    by_match = {s["match"]: s for s in engine.status()["speakers"]}
    assert by_match["Default Out"]["connected"] is False
    assert by_match["Default Out"]["note"] == DEFAULT_OUTPUT_NOTE
    assert by_match["Sonos Move"]["connected"] is True
    engine.stop()


def test_only_speaker_being_the_default_output_gives_a_clear_failure(engine_env):
    make_engine, _ = engine_env
    engine = make_engine(["Default Out"])
    assert engine.start() is False
    status = engine.status()
    assert status["state"] == "error"
    assert status["speakers"][0]["note"] == DEFAULT_OUTPUT_NOTE


def test_soloist_start_failure_is_reported_and_speakers_are_released(engine_env):
    make_engine, _ = engine_env
    engine = make_engine(["Sonos Move"], FakeSource(error=SoloistError("No Soloist API key. Create one...")))
    assert engine.start() is False
    assert engine.status()["last_error"] == "No Soloist API key. Create one..."
    assert all(s.closed for s in FakeStream.instances)


def test_engine_reports_why_soloist_stopped(engine_env):
    make_engine, holder = engine_env
    engine = make_engine(["Sonos Move"])
    engine.start()
    holder["source"].running = False
    holder["source"].exit_error = lambda: "This Soloist build has expired (builds last 90 days)."
    engine.check_health()
    status = engine.status()
    assert status["state"] == "error" and "expired" in status["last_error"]
    assert holder["source"].stopped


def test_capture_failure_is_reported(engine_env, monkeypatch):
    from splitter import capture

    make_engine, _ = engine_env

    def boom():
        raise capture.CaptureError("WASAPI is not available on this system")

    monkeypatch.setattr(capture, "probe_loopback", boom)
    engine = make_engine(["Sonos Move"])
    assert engine.start() is False
    assert "Cannot capture the default output" in engine.status()["last_error"]
    assert FakeStream.instances == []


def test_start_rescans_devices_so_newly_connected_speakers_work(engine_env):
    make_engine, holder = engine_env
    engine = make_engine(["Sonos Move"])
    assert engine.start() is True
    assert holder["refreshed"] == [True]  # rescanned once, before any stream was opened
    engine.stop()


def test_listing_outputs_rescans_only_when_nothing_is_streaming(engine_env):
    make_engine, holder = engine_env
    engine = make_engine(["Sonos Move"])
    engine.list_outputs()
    assert holder["refreshed"][-1] is True  # idle: safe to rescan
    engine.start()
    holder["refreshed"].clear()
    engine.list_outputs()
    assert holder["refreshed"] == [False]  # streaming: a rescan would break the open streams
    engine.stop()
