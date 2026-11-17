"""Zeroconf discovery: makes the device show up in Spotify's device picker.

Two parts:

- an mDNS advertisement of `_spotify-connect._tcp` (via python-zeroconf)
- a small HTTP server the Spotify app talks to:
    GET  ?action=getInfo   -> device description and our DH public key
    POST  action=addUser   -> encrypted credentials of the user who picked us
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import ifaddr
from zeroconf import ServiceInfo, Zeroconf

from . import version
from .ap import Credentials
from .crypto import DiffieHellman, decode_auth_blob, decrypt_zeroconf_blob

log = logging.getLogger(__name__)

SERVICE_TYPE = "_spotify-connect._tcp.local."
MAX_BODY = 64 * 1024

# Called with the credentials of a user who selected this device. Returns True
# if the login succeeded. May block for a few seconds.
LoginCallback = Callable[[Credentials], bool]


def local_ipv4_addresses() -> list[str]:
    result = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                result.append(ip.ip)
    return result


class Discovery:
    def __init__(
        self,
        device_name: str,
        device_id: str,
        on_login: LoginCallback,
        device_type: str = "Speaker",
        port: int = 0,
    ) -> None:
        self.device_name = device_name
        self.device_id = device_id
        self.device_type = device_type
        self._on_login = on_login
        self._dh = DiffieHellman()
        self._lock = threading.Lock()
        self._active_user = ""
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None

        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="zeroconf-http", daemon=True)

    # ----- lifecycle ---------------------------------------------------------

    def start(self, advertise: bool = True) -> None:
        """Start the HTTP server and, unless advertise is False, the mDNS advertisement."""
        self._thread.start()
        if not advertise:
            return
        addresses = local_ipv4_addresses()
        if not addresses:
            log.warning("no network address found; the device will not be discoverable")
        host = socket.gethostname().split(".")[0] or "spconnect"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.device_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=self.port,
            properties={"VERSION": "1.0", "CPath": "/"},
            server=f"{host}.local.",
        )
        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._info, allow_name_change=True)
        log.info(
            "advertising '%s' as a Spotify Connect device on %s port %d",
            self.device_name,
            ", ".join(addresses) or "(no address)",
            self.port,
        )

    def close(self) -> None:
        if self._zeroconf is not None:
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
                self._zeroconf.close()
            except Exception as exc:
                log.debug("error closing zeroconf: %s", exc)
            self._zeroconf = None
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as exc:
            log.debug("error closing discovery server: %s", exc)

    def set_active_user(self, username: str) -> None:
        with self._lock:
            self._active_user = username

    # ----- request handling ----------------------------------------------------

    def get_info(self) -> dict:
        with self._lock:
            active_user = self._active_user
        return {
            "status": 101,
            "statusString": "OK",
            "spotifyError": 0,
            "version": "2.9.0",
            "deviceID": self.device_id,
            "deviceType": self.device_type,
            "remoteName": self.device_name,
            "publicKey": base64.b64encode(self._dh.public_key_bytes()).decode(),
            "brandDisplayName": "spconnect",
            "modelDisplayName": "spconnect-python",
            "libraryVersion": version.__version__,
            "resolverVersion": "1",
            "groupStatus": "NONE",
            "tokenType": "default",
            "clientID": version.CLIENT_ID,
            "productID": 0,
            "scope": "streaming",
            "availability": "",
            "supported_drm_media_formats": [],
            "supported_capabilities": 1,
            "accountReq": "PREMIUM",
            "activeUser": active_user,
            "aliases": [],
        }

    def add_user(self, params: dict[str, str]) -> tuple[int, dict]:
        """Returns (http_status, json_body)."""
        username = params.get("userName", "")
        blob_b64 = params.get("blob", "")
        client_key_b64 = params.get("clientKey", "")
        if not username or not blob_b64 or not client_key_b64:
            return 400, {"status": 102, "spotifyError": 0, "statusString": "Bad Request"}
        try:
            blob = base64.b64decode(blob_b64)
            client_key = base64.b64decode(client_key_b64)
        except ValueError:
            return 400, {"status": 102, "spotifyError": 0, "statusString": "Bad Request"}

        decrypted = decrypt_zeroconf_blob(blob, client_key, self._dh)
        if decrypted is None:
            log.warning("zeroconf login for %r failed: MAC mismatch", username)
            return 200, {"status": 102, "spotifyError": 1, "statusString": "ERROR-MAC"}
        try:
            auth_type, auth_data = decode_auth_blob(username, decrypted, self.device_id)
        except Exception as exc:
            log.warning("zeroconf login for %r failed: bad blob (%s)", username, exc)
            return 400, {"status": 102, "spotifyError": 0, "statusString": "Bad Request"}

        log.info("zeroconf login request from user %s", username)
        credentials = Credentials(username=username, auth_type=auth_type, auth_data=auth_data)
        try:
            accepted = self._on_login(credentials)
        except Exception:
            log.exception("login callback failed")
            accepted = False
        if not accepted:
            return 503, {"status": 102, "spotifyError": 0, "statusString": "Service Unavailable"}
        self.set_active_user(username)
        return 200, {"status": 101, "spotifyError": 0, "statusString": "OK"}

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        discovery = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("zeroconf http %s - %s", self.address_string(), fmt % args)

            def _reply(self, status: int, body: dict | None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def _params(self) -> dict[str, str]:
                params = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= MAX_BODY:
                    body = self.rfile.read(length).decode("utf-8", "replace")
                    params.update({k: v[0] for k, v in parse_qs(body).items()})
                return params

            def do_GET(self) -> None:  # noqa: N802
                if self._params().get("action") == "getInfo":
                    self._reply(200, discovery.get_info())
                else:
                    self._reply(404, None)

            def do_POST(self) -> None:  # noqa: N802
                params = self._params()
                if params.get("action") == "addUser":
                    status, body = discovery.add_user(params)
                    self._reply(status, body)
                else:
                    self._reply(404, None)

        return Handler
