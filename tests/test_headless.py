import json
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from array import array
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from splitter import headless as hl
from splitter.app import Engine
from splitter.config import AppConfig, config_to_dict, parse_config, save_config
from splitter.fanout import FanOut
from splitter.headless import (
    HeadlessError,
    HeadlessSetup,
    HeadlessSource,
    build_run_command,
    build_setup_command,
    data_dir,
    find_executable,
    login_data_found,
)
from splitter.web import make_handler


@pytest.fixture(autouse=True)
def linux_and_spotify(monkeypatch):
    """Pretend to be on Linux with an installed Spotify, so the tests run anywhere."""
    monkeypatch.setattr(hl, "SUPPORTED", True)
    real_which = hl.shutil.which
    monkeypatch.setattr(hl.shutil, "which", lambda p: "/usr/bin/spotify" if p == "spotify" else real_which(p))


# ----- config ------------------------------------------------------------------------------------


def test_headless_is_a_valid_receiver_with_sane_defaults():
    cfg = parse_config({"receiver": "headless"})
    assert cfg.receiver == "headless"
    assert cfg.headless_path == "spotify-headless"
    assert cfg.spotify_bin == "spotify"
    assert cfg.headless_args == []
    assert cfg.headless_data_dir is None
    assert cfg.headless_vnc_port == 5900


def test_headless_settings_round_trip_and_are_validated():
    cfg = parse_config(
        {
            "headless_path": " /opt/sh ",
            "spotify_bin": "/snap/bin/spotify",
            "headless_args": ["--resolution", "800x600x24"],
            "headless_data_dir": "client",
            "headless_vnc_port": 5911,
        }
    )
    assert parse_config(config_to_dict(cfg)) == cfg
    assert cfg.headless_path == "/opt/sh"
    assert parse_config({"headless_data_dir": "  "}).headless_data_dir is None
    for port in (0, 70000):
        with pytest.raises(ValueError, match="headless_vnc_port"):
            parse_config({"headless_vnc_port": port})


# ----- finding things --------------------------------------------------------------------------------


def test_find_executable_search_order(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.shutil, "which", lambda p: None)
    assert find_executable("spotify-headless", tmp_path) is None

    name = hl._exe_names()[0]
    built = tmp_path / "headless" / "target" / "release" / name
    built.parent.mkdir(parents=True)
    built.write_text("x")
    assert find_executable("spotify-headless", tmp_path) == str(built)

    tools = tmp_path / "tools" / "spotify-headless" / name
    tools.parent.mkdir(parents=True)
    tools.write_text("x")
    assert find_executable("spotify-headless", tmp_path) == str(tools)  # tools/ wins over the build folder

    custom = tmp_path / "bin" / "mine"
    custom.parent.mkdir()
    custom.write_text("x")
    assert find_executable("bin/mine", tmp_path) == str(custom)  # relative to the config folder
    assert find_executable(str(custom), tmp_path) == str(custom)


def test_find_executable_falls_back_to_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.shutil, "which", lambda p: "/usr/local/bin/spotify-headless")
    assert find_executable("spotify-headless", tmp_path) == "/usr/local/bin/spotify-headless"


def test_data_dir_default_relative_and_absolute(tmp_path):
    assert data_dir(AppConfig(), tmp_path) == tmp_path / ".spotify-headless-data"
    assert data_dir(AppConfig(headless_data_dir="client"), tmp_path) == tmp_path / "client"
    assert data_dir(AppConfig(headless_data_dir=str(tmp_path / "abs")), tmp_path / "other") == tmp_path / "abs"


def test_login_data_heuristic(tmp_path):
    assert login_data_found(tmp_path) is False
    spotify_cfg = tmp_path / "config" / "spotify"
    (spotify_cfg / "Users").mkdir(parents=True)
    assert login_data_found(tmp_path) is False  # an empty Users folder is not a login
    (spotify_cfg / "prefs").write_text('app.autostart-configured=true\n')
    assert login_data_found(tmp_path) is False
    (spotify_cfg / "prefs").write_text('autologin.canonical_username="someone"\n')
    assert login_data_found(tmp_path) is True
    (spotify_cfg / "prefs").unlink()
    (spotify_cfg / "Users" / "someone-user").mkdir()
    assert login_data_found(tmp_path) is True


# ----- command lines ------------------------------------------------------------------------------------


def test_run_command_streams_pcm_on_stdout(tmp_path):
    cfg = AppConfig(spotify_bin="/snap/bin/spotify", headless_args=["--spotify-arg", "--disable-gpu"])
    cmd = build_run_command("/x/spotify-headless", cfg, tmp_path)
    assert cmd[:2] == ["/x/spotify-headless", "run"]
    assert cmd[cmd.index("--pcm-out") + 1] == "-"
    assert cmd[cmd.index("--rate") + 1] == "44100"  # what the fan-out and speakers expect
    assert cmd[cmd.index("--data-dir") + 1] == str(tmp_path)
    assert cmd[cmd.index("--log-dir") + 1] == str(tmp_path / "logs")
    assert cmd[cmd.index("--spotify-bin") + 1] == "/snap/bin/spotify"
    assert cmd[-2:] == ["--spotify-arg", "--disable-gpu"]
    assert "--vnc-port" not in cmd


def test_setup_command_has_vnc_and_no_capture(tmp_path):
    cmd = build_setup_command("/x/spotify-headless", AppConfig(headless_vnc_port=5911), tmp_path)
    assert cmd[cmd.index("--vnc-port") + 1] == "5911"
    assert "--pcm-out" not in cmd
    assert cmd[cmd.index("--data-dir") + 1] == str(tmp_path)  # the same folder, so the login is shared


# ----- the streaming source ---------------------------------------------------------------------------------


@pytest.fixture
def make_source(tmp_path, monkeypatch):
    """HeadlessSource objects whose spotify-headless is a tiny Python script."""
    made = []

    def make(script: str, **cfg_kwargs):
        stub = tmp_path / "stub.py"
        stub.write_text(textwrap.dedent(script))
        exe = tmp_path / "spotify-headless-stub"
        exe.write_text("x")
        real_popen = hl.subprocess.Popen
        monkeypatch.setattr(
            hl.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, str(stub), *cmd[1:]], **kw)
        )
        cfg_kwargs.setdefault("headless_path", str(exe))
        cfg = AppConfig(receiver="headless", **cfg_kwargs)
        fan = FanOut()
        fan.add("a", 1 << 20)
        source = HeadlessSource(cfg, fan, tmp_path)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()


STAYS_RUNNING = """
    import sys, time
    print("args:", sys.argv[1:], file=sys.stderr, flush=True)
    time.sleep(60)
"""


def _pcm(values):
    return array("h", values).tobytes()


def test_missing_tool_and_wrong_platform_are_clear_errors(make_source, monkeypatch):
    monkeypatch.setattr(hl.shutil, "which", lambda p: None)
    source = make_source(STAYS_RUNNING, headless_path="does-not-exist")
    with pytest.raises(HeadlessError, match="spotify-headless was not found"):
        source.start()
    monkeypatch.setattr(hl, "SUPPORTED", False)
    with pytest.raises(HeadlessError, match="only runs on Linux"):
        make_source(STAYS_RUNNING).start()


def test_pcm_from_stdout_reaches_the_fan_out_unchanged(make_source):
    payload = _pcm(range(-2000, 2000, 7)) * 4
    source = make_source(
        f"""
        import sys, time
        sys.stdout.buffer.write({payload!r})
        sys.stdout.buffer.flush()
        time.sleep(60)
        """
    )
    source.start()
    buf = source._fan.get("a")
    deadline = time.monotonic() + 5
    while buf.buffered < len(payload) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert buf.read(len(payload)) == payload
    assert source._fan.bytes_in == len(payload)
    assert source.running and source.exit_error() is None


def test_tool_is_started_with_the_streaming_arguments_and_stops_cleanly(make_source, tmp_path):
    source = make_source(STAYS_RUNNING, headless_args=["--verbose"])
    source.start()
    log_text = source.log_path.read_text()
    assert "--pcm-out" in log_text and "--rate" in log_text and "'44100'" in log_text
    assert "--verbose" in log_text
    assert source.data_dir == tmp_path / ".spotify-headless-data"

    proc = source._proc
    source.stop()
    assert proc.poll() is not None and not source.running
    source.stop()  # idempotent


def test_early_exit_reports_the_end_of_the_log(make_source):
    source = make_source('import sys; print("Xvfb not found", file=sys.stderr); sys.exit(1)')
    with pytest.raises(HeadlessError) as err:
        source.start()
    assert "code 1" in str(err.value) and "Xvfb not found" in str(err.value)
    assert not source.running


def test_dying_later_is_noticed(make_source):
    source = make_source("import time; time.sleep(2.6)")
    source.start()
    assert source.running and source.exit_error() is None
    source._proc.wait(timeout=10)
    assert not source.running
    assert "exited" in source.exit_error()


def test_status_tracks_audible_audio_and_hints_at_a_missing_login(make_source, monkeypatch, tmp_path):
    source = make_source(STAYS_RUNNING)
    first = source.status()
    assert first["state"] == "waiting" and first["playing"] is False
    assert "No saved Spotify login" in first["error"]

    source._on_audio(_pcm([0, 0, 1, -1] * 100))  # digital silence
    assert source.status()["playing"] is False
    source._on_audio(_pcm([0, 3000, -3000, 0] * 100))
    status = source.status()
    assert status["playing"] is True and status["state"] == "active" and status["error"] is None
    assert source._fan.bytes_in == 2 * 400 * 2  # silence is passed on too: the speakers stay fed

    monkeypatch.setattr(hl.time, "monotonic", lambda: source._last_audible + 10)
    assert source.status()["playing"] is False
    (tmp_path / ".spotify-headless-data" / "config" / "spotify" / "Users" / "me-user").mkdir(parents=True)
    assert source.status()["error"] is None  # a saved login: no hint


# ----- the setup session --------------------------------------------------------------------------------------


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    stub = tmp_path / "stub.py"
    exe = tmp_path / "spotify-headless-stub"
    exe.write_text("x")
    real_popen = hl.subprocess.Popen
    monkeypatch.setattr(
        hl.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, str(stub), *cmd[1:]], **kw)
    )
    cfg = AppConfig(receiver="headless", headless_path=str(exe), headless_vnc_port=5933)
    setup = HeadlessSetup()

    def script(text):
        stub.write_text(textwrap.dedent(text))

    script(STAYS_RUNNING)
    yield setup, cfg, script
    setup.stop()


def test_setup_runs_the_tool_with_vnc_and_stops(setup_env, tmp_path):
    setup, cfg, _ = setup_env
    assert setup.status(cfg, tmp_path)["running"] is False
    setup.start(cfg, tmp_path)
    status = setup.status(cfg, tmp_path)
    assert status["running"] is True and status["vnc_port"] == 5933 and status["error"] is None
    assert status["tool_path"] == cfg.headless_path and status["spotify_found"] is True
    assert status["data_dir"] == str(tmp_path / ".spotify-headless-data")
    log_text = (tmp_path / ".spotify-headless-data" / "setup.log").read_text()
    assert "--vnc-port" in log_text and "'5933'" in log_text and "--pcm-out" not in log_text

    proc = setup._proc
    setup.start(cfg, tmp_path)  # already running: nothing happens
    assert setup._proc is proc
    setup.stop()
    assert proc.poll() is not None and setup.status(cfg, tmp_path)["running"] is False
    setup.stop()  # idempotent


def test_setup_start_failures_are_reported(setup_env, tmp_path, monkeypatch):
    setup, cfg, script = setup_env
    script('import sys; print("x11vnc not found", file=sys.stderr); sys.exit(1)')
    with pytest.raises(HeadlessError, match="x11vnc not found"):
        setup.start(cfg, tmp_path)
    assert setup.running() is False

    monkeypatch.setattr(hl.shutil, "which", lambda p: None)
    with pytest.raises(HeadlessError, match="Spotify client was not found"):
        setup.start(cfg, tmp_path)
    cfg.headless_path = "nope"
    with pytest.raises(HeadlessError, match="spotify-headless was not found"):
        setup.start(cfg, tmp_path)
    monkeypatch.setattr(hl, "SUPPORTED", False)
    with pytest.raises(HeadlessError, match="only runs on Linux"):
        setup.start(cfg, tmp_path)


def test_setup_that_dies_by_itself_keeps_its_reason(setup_env, tmp_path):
    setup, cfg, script = setup_env
    script("import time; time.sleep(2.6)")
    setup.start(cfg, tmp_path)
    assert setup.status(cfg, tmp_path)["running"] is True
    setup._proc.wait(timeout=10)
    status = setup.status(cfg, tmp_path)
    assert status["running"] is False and "exited" in status["error"]


# ----- engine -----------------------------------------------------------------------------------------------------


class FakeSetup:
    def __init__(self, running=False):
        self._running = running
        self.stopped = 0

    def running(self):
        return self._running

    def stop(self):
        self.stopped += 1
        self._running = False


def test_engine_refuses_to_stream_while_the_setup_session_runs(tmp_path):
    engine = Engine(parse_config({"receiver": "headless", "speakers": [{"match": "JBL"}]}), tmp_path / "c.json")
    engine._setup = FakeSetup(running=True)
    assert engine.start() is False
    status = engine.status()
    assert status["state"] == "error" and "setup is running" in status["last_error"]


def test_engine_refuses_setup_while_streaming(tmp_path):
    engine = Engine(parse_config({"receiver": "headless"}), tmp_path / "c.json")
    engine._st.source = SimpleNamespace(running=True)
    with pytest.raises(ValueError, match="Stop the splitter first"):
        engine.setup_start()


def test_engine_turns_setup_errors_into_value_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(hl.shutil, "which", lambda p: None)
    engine = Engine(parse_config({"receiver": "headless"}), tmp_path / "c.json")
    with pytest.raises(ValueError, match="spotify-headless was not found"):
        engine.setup_start()


def test_close_stops_the_setup_session_too(tmp_path):
    engine = Engine(parse_config({}), tmp_path / "c.json")
    engine._setup = FakeSetup(running=True)
    engine.close()
    assert engine._setup.stopped == 1


class FakeStream:
    instances: list = []

    def __init__(self, name, index, buffer, hostapi="", samplerate=44100):
        self.name, self.buffer, self.samplerate = name, buffer, samplerate
        self.volume, self.muted, self.latency, self.underruns = 1.0, False, 0.05, 0
        self.closed = False
        FakeStream.instances.append(self)

    def start(self):
        pass

    def close(self):
        self.closed = True


class FakeSource:
    def __init__(self, error=None):
        self.error, self.running = error, True

    def start(self):
        if self.error:
            raise self.error

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "waiting", "username": "", "error": None, "playing": False, "track": None}


@pytest.fixture
def stream_env(monkeypatch, tmp_path):
    from splitter import outputs

    FakeStream.instances = []
    device = outputs.OutputInfo(index=2, name="Speakers (Sonos Move)", hostapi="ALSA", max_channels=2)
    monkeypatch.setattr(outputs, "list_outputs", lambda refresh=False: [device])
    monkeypatch.setattr(outputs, "SpeakerStream", FakeStream)

    def make(source):
        monkeypatch.setattr(hl, "HeadlessSource", lambda *a, **k: source)
        cfg = parse_config({"receiver": "headless", "speakers": [{"match": "Sonos Move"}]})
        return Engine(cfg, tmp_path / "config.json")

    return make


def test_engine_streams_the_headless_source_at_44100(stream_env):
    engine = stream_env(FakeSource())
    assert engine.start() is True
    assert FakeStream.instances[0].samplerate == 44100
    status = engine.status()
    assert status["state"] == "running" and status["receiver"] == "headless"
    assert status["spotify"]["state"] == "waiting"
    engine.stop()


def test_engine_reports_headless_start_errors_verbatim_and_releases_speakers(stream_env):
    engine = stream_env(FakeSource(error=HeadlessError("spotify-headless was not found (x).")))
    assert engine.start() is False
    assert engine.status()["last_error"] == "spotify-headless was not found (x)."
    assert all(s.closed for s in FakeStream.instances)


# ----- web API ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    cfg_path = tmp_path / "config.json"
    save_config(parse_config({"receiver": "headless", "speakers": [{"match": "JBL"}]}), cfg_path)
    from splitter.config import load_config

    engine = Engine(load_config(cfg_path), cfg_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", engine
    engine.close()
    httpd.shutdown()
    httpd.server_close()


def _req(base, method, path, body=None, content_type="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_get_setup_reports_the_state(server):
    base, _ = server
    code, body = _req(base, "GET", "/api/setup")
    assert code == 200
    assert set(body) == {
        "supported", "tool_path", "spotify_found", "login_data_found", "running", "vnc_port", "data_dir", "error",
    }
    assert body["running"] is False and body["vnc_port"] == 5900


def test_setup_start_failure_is_a_clean_400(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(hl.shutil, "which", lambda p: None)
    code, body = _req(base, "POST", "/api/setup", {"action": "start"})
    assert code == 400 and "spotify-headless was not found" in body["error"]


def test_setup_start_and_finish_over_the_api(server, tmp_path, monkeypatch):
    base, engine = server
    stub = tmp_path / "stub.py"
    stub.write_text(textwrap.dedent(STAYS_RUNNING))
    exe = tmp_path / "spotify-headless-stub"
    exe.write_text("x")
    engine.config.headless_path = str(exe)
    real_popen = hl.subprocess.Popen
    monkeypatch.setattr(
        hl.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, str(stub), *cmd[1:]], **kw)
    )

    code, body = _req(base, "POST", "/api/setup", {"action": "start"})
    assert code == 200 and body["setup"]["running"] is True
    assert body["status"]["state"] in ("stopped", "error")

    code, body = _req(base, "POST", "/api/setup", {"action": "stop"})
    assert code == 200 and body["setup"]["running"] is False


def test_setup_rejects_bad_requests(server):
    base, _ = server
    assert _req(base, "POST", "/api/setup", {"action": "explode"})[0] == 400
    assert _req(base, "POST", "/api/setup", {"action": "start"}, content_type="text/plain")[0] == 400
    assert _req(base, "POST", "/api/nowhere", {"action": "start"})[0] == 404


def test_the_ui_offers_the_setup(server):
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        html = resp.read().decode()
    for needle in ('data-v="headless"', 'id="setupSection"', "/api/setup", "headless_vnc_port"):
        assert needle in html
