"""Local web server: serves the single-page UI and a small JSON API.

The UI is a single-page app: this server only serves its files and a JSON API.

Endpoints
    GET   /                  the app shell (index.html); the views are routed in the browser (#/...)
    GET   /static/...        the app's ES modules and CSS (whitelisted types only)
    GET   /api/status        live state and per-speaker monitoring
    GET   /api/outputs       audio output devices visible to Windows
    GET   /api/config        current configuration
    PUT   /api/config        replace configuration (saved; restarts if streaming)
    POST  /api/control       {"action": "start" | "stop" | "restart" | "play" | "pause" | "next" | "prev" | "sync_test"}
    PATCH /api/speaker       {"match": ..., "volume"?, "muted"?, "delay_ms"?}
    GET   /api/setup         state of the Spotify client setup (receiver "headless")
    POST  /api/setup         {"action": "start" | "stop"}: the one-time sign-in session

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
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .config import config_to_dict, parse_config

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"
INDEX_PATH = WEB_ROOT / "index.html"
STATIC_ROOT = WEB_ROOT / "static"
MAX_BODY = 64 * 1024
MAX_DRAIN = 8 * 1024 * 1024  # larger refused bodies are not read, only refused

# The UI is a single-page app: index.html plus ES modules and CSS under /static/.
# Only these file types are ever served from there.
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# Everything the page needs comes from this server: no inline scripts, no other
# origins, and it may not be framed.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class _BadRequest(Exception):
    pass


def resolve_static(url_path: str) -> tuple[Path, str] | None:
    """Map a /static/... URL path to (file, content type), or None.

    Only whitelisted file types inside STATIC_ROOT are served; anything that
    resolves outside it (.., absolute paths, symlinks) is refused.
    """
    prefix = "/static/"
    if not url_path.startswith(prefix):
        return None
    rel = unquote(url_path[len(prefix):])
    if not rel or "\x00" in rel or "\\" in rel:
        return None
    content_type = CONTENT_TYPES.get(PurePosixPath(rel).suffix.lower())
    if content_type is None:
        return None
    root = STATIC_ROOT.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target, content_type


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
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            try:
                body = path.read_bytes()
            except OSError:
                return self._error(500, "UI file missing")
            self._send(200, body, content_type)

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
                # Read (and discard) the body before refusing it, or the OS resets
                # the connection and the client never sees the 413. Truly huge
                # bodies are not read at all.
                self.close_connection = True
                remaining = length if length <= MAX_DRAIN else 0
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
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
                return self._send_file(INDEX_PATH, "text/html; charset=utf-8")
            if path.startswith("/static/"):
                found = resolve_static(path)
                if found is None:
                    return self._error(404, "not found")
                return self._send_file(*found)
            if path == "/api/status":
                return self._dispatch(lambda: self._json(200, engine.status()))
            if path == "/api/outputs":
                return self._dispatch(self._get_outputs)
            if path == "/api/config":
                return self._dispatch(lambda: self._json(200, config_to_dict(engine.config)))
            if path == "/api/setup":
                return self._dispatch(lambda: self._json(200, engine.setup_status()))
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
            path = self.path.split("?", 1)[0]
            if path == "/api/setup":
                self._mutate(path, self._setup)
            else:  # anything else is answered by _mutate: /api/control, or a 404
                self._mutate("/api/control", self._control)

        def do_PATCH(self) -> None:  # noqa: N802
            self._mutate("/api/speaker", self._patch_speaker)

        # ----- handlers ----------------------------------------------------

        def _get_outputs(self) -> None:
            outputs = [
                {"index": o.index, "name": o.name, "hostapi": o.hostapi}
                for o in engine.list_outputs()
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
            elif action == "sync_test":
                engine.sync_test()
            else:
                raise _BadRequest(
                    "action must be start, stop, restart, play, pause, next, prev or sync_test"
                )
            self._json(200, {"status": engine.status()})

        def _setup(self) -> None:
            action = self._read_json().get("action")
            if action == "start":
                engine.setup_start()
            elif action == "stop":
                engine.setup_stop()
            else:
                raise _BadRequest("action must be start or stop")
            self._json(200, {"setup": engine.setup_status(), "status": engine.status()})

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
