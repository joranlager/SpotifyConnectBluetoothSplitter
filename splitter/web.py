"""Local web server: serves the single-page UI and a small JSON API.

Endpoints
    GET   /                  the web UI
    GET   /api/status        live state and per-speaker monitoring
    GET   /api/outputs       audio output devices visible to Windows
    GET   /api/config        current configuration
    PUT   /api/config        replace configuration (saved; restarts if streaming)
    POST  /api/control       {"action": "start" | "stop" | "restart" | "play" | "pause" | "next" | "prev"}
    PATCH /api/speaker       {"match": ..., "volume"?, "muted"?, "delay_ms"?}

Mutating requests must send Content-Type: application/json. Browsers cannot send
that cross-site without a CORS preflight, which this server never grants, so
other websites cannot change the configuration. The default bind address is
127.0.0.1; set web_host to 0.0.0.0 to control it from other devices on your
network (there is no login, so only do that on a trusted network).
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import config_to_dict, parse_config

log = logging.getLogger(__name__)

UI_PATH = Path(__file__).parent / "web" / "index.html"
MAX_BODY = 64 * 1024


class _BadRequest(Exception):
    pass


def make_handler(engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SpotifyBTSplitter"

        # ----- helpers -----------------------------------------------------

        def log_message(self, fmt, *args):  # route access log to logging
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        def _take_body(self) -> bool:
            """Read the request body before answering.

            Replying while the body is still unread makes the OS reset the
            connection, and the client then never sees the reply. Returns False
            (after answering) when the body is too large to accept.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                self.close_connection = True
                self._error(413, "request body too large")
                return False
            self._body = self.rfile.read(length) if length > 0 else b""
            return True

        def _read_json(self) -> dict:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise _BadRequest("Content-Type must be application/json")
            if not self._body:
                raise _BadRequest("request body missing")
            try:
                data = json.loads(self._body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _BadRequest(f"invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise _BadRequest("JSON body must be an object")
            return data

        def _dispatch(self, fn) -> None:
            try:
                fn()
            except _BadRequest as exc:
                self._error(400, str(exc))
            except KeyError as exc:
                self._error(404, f"unknown speaker: {exc.args[0]}")
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception:  # never leak tracebacks to the browser
                log.exception("API error")
                self._error(500, "internal error; see the console log")

        # ----- routes ------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    body = UI_PATH.read_bytes()
                except OSError:
                    return self._error(500, "UI file missing")
                return self._send(200, body, "text/html; charset=utf-8")
            if path == "/api/status":
                return self._dispatch(lambda: self._json(200, engine.status()))
            if path == "/api/outputs":
                return self._dispatch(self._get_outputs)
            if path == "/api/config":
                return self._dispatch(lambda: self._json(200, config_to_dict(engine.config)))
            self._error(404, "not found")

        def _mutate(self, path: str, handler) -> None:
            if not self._take_body():
                return
            if self.path.split("?", 1)[0] != path:
                return self._error(404, "not found")
            self._dispatch(handler)

        def do_PUT(self) -> None:  # noqa: N802
            self._mutate("/api/config", self._put_config)

        def do_POST(self) -> None:  # noqa: N802
            self._mutate("/api/control", self._control)

        def do_PATCH(self) -> None:  # noqa: N802
            self._mutate("/api/speaker", self._patch_speaker)

        # ----- handlers ----------------------------------------------------

        def _get_outputs(self) -> None:
            from .outputs import list_outputs

            outputs = [
                {"index": o.index, "name": o.name, "hostapi": o.hostapi}
                for o in list_outputs()
            ]
            self._json(200, {"outputs": outputs})

        def _put_config(self) -> None:
            data = self._read_json()
            try:
                cfg = parse_config(data)
            except (TypeError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            engine.update_config(cfg)
            self._json(200, {"config": config_to_dict(engine.config), "status": engine.status()})

        def _control(self) -> None:
            action = self._read_json().get("action")
            if action == "start":
                engine.start()
            elif action == "stop":
                engine.stop()
            elif action == "restart":
                engine.restart()
            elif action in ("play", "pause", "next", "prev"):
                engine.control(action)
            else:
                raise _BadRequest("action must be start, stop, restart, play, pause, next or prev")
            self._json(200, {"status": engine.status()})

        def _patch_speaker(self) -> None:
            data = self._read_json()
            match = data.pop("match", None)
            if not isinstance(match, str) or not match:
                raise _BadRequest("'match' is required")
            allowed = {"volume", "muted", "delay_ms"}
            unknown = set(data) - allowed
            if unknown:
                raise _BadRequest(f"unknown fields: {', '.join(sorted(unknown))}")
            if "volume" in data:
                volume = data["volume"]
                if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                    raise _BadRequest("volume must be a number")
            if "delay_ms" in data:
                delay = data["delay_ms"]
                if not isinstance(delay, (int, float)) or isinstance(delay, bool):
                    raise _BadRequest("delay_ms must be a number")
            engine.update_speaker(match, **data)
            self._json(200, {"status": engine.status()})

    return Handler


def serve(engine, host: str, port: int) -> None:
    """Serve until interrupted (blocking)."""
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    server.daemon_threads = True
    log.info("web UI at http://%s:%d", "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
