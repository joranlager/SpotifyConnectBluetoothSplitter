import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from splitter.app import Engine
from splitter.config import load_config, parse_config, save_config
from splitter.web import make_handler


def _cfg(**extra):
    data = {"speakers": [{"match": "JBL"}, {"match": "Sony", "delay_ms": 20}]}
    data.update(extra)
    return parse_config(data)


@pytest.fixture
def server(tmp_path):
    cfg_path = tmp_path / "config.json"
    save_config(_cfg(), cfg_path)
    engine = Engine(load_config(cfg_path), cfg_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, engine, cfg_path
    httpd.shutdown()
    httpd.server_close()


def _req(base, method, path, body=None, content_type="application/json", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if content_type and data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def test_index_served_with_security_headers(server):
    base, *_ = server
    code, body, headers = _req(base, "GET", "/")
    assert code == 200
    assert b"<title>" in body
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_status_reports_stopped_engine_with_configured_speakers(server):
    base, *_ = server
    code, body, _ = _req(base, "GET", "/api/status")
    status = json.loads(body)
    assert code == 200
    assert status["state"] == "stopped"
    assert [s["match"] for s in status["speakers"]] == ["JBL", "Sony"]
    assert all(s["connected"] is False for s in status["speakers"])


def test_patch_speaker_volume_and_mute_persist(server):
    base, engine, cfg_path = server
    code, body, _ = _req(base, "PATCH", "/api/speaker", {"match": "JBL", "volume": 0.4, "muted": True})
    assert code == 200
    spk = next(s for s in json.loads(body)["status"]["speakers"] if s["match"] == "JBL")
    assert spk["volume"] == pytest.approx(0.4)
    assert spk["muted"] is True
    saved = load_config(cfg_path)
    assert saved.speakers[0].volume == pytest.approx(0.4)
    assert saved.speakers[0].muted is True


def test_patch_unknown_speaker_is_404(server):
    base, *_ = server
    code, body, _ = _req(base, "PATCH", "/api/speaker", {"match": "Nope", "volume": 0.5})
    assert code == 404
    assert "unknown speaker" in json.loads(body)["error"]


def test_patch_rejects_unknown_fields_and_bad_types(server):
    base, *_ = server
    code, _, _ = _req(base, "PATCH", "/api/speaker", {"match": "JBL", "evil": 1})
    assert code == 400
    code, _, _ = _req(base, "PATCH", "/api/speaker", {"match": "JBL", "volume": "loud"})
    assert code == 400


def test_mutation_requires_json_content_type(server):
    base, *_ = server
    code, body, _ = _req(base, "POST", "/api/control", {"action": "start"}, content_type="text/plain")
    assert code == 400
    assert "application/json" in json.loads(body)["error"]


def test_control_rejects_unknown_action(server):
    base, *_ = server
    code, _, _ = _req(base, "POST", "/api/control", {"action": "explode"})
    assert code == 400


def test_put_config_validates_and_saves(server):
    base, _, cfg_path = server
    cfg = json.loads(_req(base, "GET", "/api/config")[1])
    cfg["device_name"] = "Living Room"
    cfg["bitrate"] = 160
    code, body, _ = _req(base, "PUT", "/api/config", cfg)
    assert code == 200
    assert load_config(cfg_path).device_name == "Living Room"

    cfg["bitrate"] = 999
    code, body, _ = _req(base, "PUT", "/api/config", cfg)
    assert code == 400
    assert "bitrate" in json.loads(body)["error"]
    assert load_config(cfg_path).bitrate == 160


def test_put_config_rejects_duplicate_speakers(server):
    base, *_ = server
    cfg = json.loads(_req(base, "GET", "/api/config")[1])
    cfg["speakers"].append({"match": "jbl"})
    code, _, _ = _req(base, "PUT", "/api/config", cfg)
    assert code == 400


def test_invalid_json_body_is_400(server):
    base, *_ = server
    code, _, _ = _req(base, "PUT", "/api/config", raw=b"{not json")
    assert code == 400


def test_engine_update_speaker_persists_delay(server):
    _, engine, cfg_path = server
    engine.update_speaker("Sony", delay_ms=35)
    assert load_config(cfg_path).speakers[1].delay_ms == 35


def test_engine_rejects_out_of_range_delay(server):
    _, engine, _ = server
    with pytest.raises(ValueError):
        engine.update_speaker("Sony", delay_ms=9999)


def test_engine_start_without_speakers_reports_error(tmp_path):
    engine = Engine(parse_config({}), tmp_path / "c.json")
    assert engine.start() is False
    status = engine.status()
    assert status["state"] == "error"
    assert "No speakers" in status["last_error"]


def test_rejected_requests_still_get_their_reply(server):
    """A refused request must be answered, not have its connection reset."""
    base, *_ = server
    for _ in range(40):
        code, _, _ = _req(base, "POST", "/api/control", {"action": "start"}, content_type="text/plain")
        assert code == 400
        code, _, _ = _req(base, "POST", "/api/nowhere", {"x": "y" * 2000})
        assert code == 404


def test_oversized_body_is_refused(server):
    base, *_ = server
    for size in (70 * 1024, 300 * 1024):
        for _ in range(10):
            code, _, _ = _req(base, "PUT", "/api/config", raw=b"x" * size)
            assert code == 413


def test_transport_control_without_a_session_is_a_clean_error(server):
    base, *_ = server
    code, body, _ = _req(base, "POST", "/api/control", {"action": "next"})
    assert code == 400
    assert "not available" in json.loads(body)["error"]


def test_status_names_the_receiver(server):
    base, *_ = server
    status = json.loads(_req(base, "GET", "/api/status")[1])
    assert status["receiver"] == "builtin"
    assert status["spotify"] is None


def test_sync_test_without_a_running_splitter_is_a_clean_error(server):
    base, *_ = server
    code, body, _ = _req(base, "POST", "/api/control", {"action": "sync_test"})
    assert code == 400
    assert "Start the splitter" in json.loads(body)["error"]


def test_delay_patch_is_persisted_without_a_running_stream(server):
    base, engine, cfg_path = server
    code, _, _ = _req(base, "PATCH", "/api/speaker", {"match": "JBL", "delay_ms": 180})
    assert code == 200
    assert load_config(cfg_path).speakers[0].delay_ms == 180
    code, _, _ = _req(base, "PATCH", "/api/speaker", {"match": "JBL", "delay_ms": 9000})
    assert code == 400
