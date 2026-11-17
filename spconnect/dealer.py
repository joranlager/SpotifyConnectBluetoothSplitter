"""Dealer: the websocket over which Spotify pushes messages and remote commands.

Two kinds of traffic arrive:

- messages  (fire-and-forget): connection id, cluster updates, volume, logout ...
- requests  (need a reply):    player commands such as play, pause, transfer
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import websocket

from . import version

log = logging.getLogger(__name__)

PING_INTERVAL = 30.0
PONG_TIMEOUT = 15.0
RECV_TIMEOUT = 2.0
RECONNECT_DELAYS = (1, 2, 5, 10, 10, 30)


@dataclass
class Message:
    uri: str
    headers: dict[str, str]
    payload: bytes | None = None  # binary payload (after base64 + gzip)
    json_payload: object | None = None  # for the few messages carrying plain JSON


@dataclass
class Request:
    ident: str
    key: str
    payload: dict
    _reply: Callable[[bool], None] = field(repr=False, default=lambda ok: None)

    def reply(self, success: bool) -> None:
        self._reply(success)


def _decode(headers: dict[str, str], data: bytes) -> bytes:
    if headers.get("Transfer-Encoding") == "gzip":
        return gzip.decompress(data)
    return data


def parse_message(raw: dict) -> Message:
    headers = dict(raw.get("headers") or {})
    msg = Message(uri=raw.get("uri", ""), headers=headers)
    payloads = raw.get("payloads") or []
    if not payloads:
        return msg
    if len(payloads) > 1:
        raise ValueError(f"dealer message with {len(payloads)} payloads")
    value = payloads[0]
    if isinstance(value, str):
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            msg.json_payload = value  # plain text / JSON carried as a string
            return msg
        msg.payload = _decode(headers, data)
    elif isinstance(value, list):
        msg.payload = _decode(headers, bytes(value))
    else:
        msg.json_payload = value
    return msg


def parse_request_payload(raw: dict) -> dict:
    headers = dict(raw.get("headers") or {})
    compressed = (raw.get("payload") or {}).get("compressed", "")
    data = _decode(headers, base64.b64decode(compressed))
    return json.loads(data.decode("utf-8"))


class Dealer:
    def __init__(
        self,
        get_hosts: Callable[[], list[str]],
        get_token: Callable[[bool], str],
    ) -> None:
        self._get_hosts = get_hosts
        self._get_token = get_token
        self._listeners: list[tuple[str, Callable[[Message], None]]] = []
        self._handlers: dict[str, Callable[[Request], None]] = {}
        self._ws: websocket.WebSocket | None = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._awaiting_pong = False
        self.on_lost: Callable[[], None] | None = None  # gave up reconnecting

    def listen(self, uri_prefix: str, callback: Callable[[Message], None]) -> None:
        self._listeners.append((uri_prefix, callback))

    def handle(self, ident: str, callback: Callable[[Request], None]) -> None:
        self._handlers[ident] = callback

    # ----- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Connect (raises on failure) and start the receive thread."""
        self._connect(force_token=False)
        self._thread = threading.Thread(target=self._run, name="dealer", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _connect(self, force_token: bool) -> None:
        token = self._get_token(force_token)
        last: Exception | None = None
        for host in self._get_hosts()[:3]:
            try:
                ws = websocket.create_connection(
                    f"wss://{host}/?access_token={token}",
                    timeout=10,
                    header=[f"User-Agent: {version.user_agent()}"],
                )
                ws.settimeout(RECV_TIMEOUT)
                self._ws = ws
                self._awaiting_pong = False
                log.info("dealer connected to %s", host)
                return
            except Exception as exc:  # websocket-client raises many types
                last = exc
                log.debug("dealer connection to %s failed: %s", host, exc)
        raise ConnectionError(f"could not connect to any dealer: {last}")

    def _send(self, obj: dict) -> None:
        with self._send_lock:
            ws = self._ws
            if ws is None:
                raise ConnectionError("dealer is not connected")
            ws.send(json.dumps(obj, separators=(",", ":")))

    def _run(self) -> None:
        last_ping = time.monotonic()
        while not self._closed.is_set():
            try:
                now = time.monotonic()
                if self._awaiting_pong and now - last_ping >= PONG_TIMEOUT:
                    raise ConnectionError("dealer did not answer the last ping")
                if now - last_ping >= PING_INTERVAL:
                    self._send({"type": "ping"})
                    self._awaiting_pong = True
                    last_ping = now
                try:
                    raw = self._ws.recv() if self._ws is not None else None
                except websocket.WebSocketTimeoutException:
                    continue
                if raw is None or raw == "":
                    raise ConnectionError("dealer closed the connection")
                if isinstance(raw, bytes):
                    continue
                self._on_frame(raw)
            except Exception as exc:
                if self._closed.is_set():
                    break
                log.warning("dealer connection lost: %s", exc)
                if not self._reconnect():
                    break
                last_ping = time.monotonic()
        if not self._closed.is_set() and self.on_lost is not None:
            self._closed.set()
            try:
                self.on_lost()
            except Exception:
                log.exception("dealer on_lost callback failed")

    def _reconnect(self) -> bool:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for delay in RECONNECT_DELAYS:
            if self._closed.wait(delay):
                return False
            try:
                self._connect(force_token=True)
                return True
            except Exception as exc:
                log.warning("dealer reconnect failed: %s", exc)
        return False

    # ----- incoming frames ----------------------------------------------------

    def _on_frame(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("dealer sent invalid JSON")
            return
        kind = data.get("type")
        if kind == "pong":
            self._awaiting_pong = False
        elif kind == "message":
            self._on_message(data)
        elif kind == "request":
            self._on_request(data)
        elif kind != "ping":
            log.debug("unknown dealer frame type: %s", kind)

    def _on_message(self, data: dict) -> None:
        uri = data.get("uri", "")
        targets = [cb for prefix, cb in self._listeners if uri.startswith(prefix)]
        if not targets:
            log.debug("ignoring dealer message %s", uri)
            return
        try:
            msg = parse_message(data)
        except Exception as exc:
            log.warning("could not decode dealer message %s: %s", uri, exc)
            return
        for cb in targets:
            try:
                cb(msg)
            except Exception:
                log.exception("dealer message handler failed for %s", uri)

    def _on_request(self, data: dict) -> None:
        ident = data.get("message_ident", "")
        key = data.get("key", "")

        def reply(success: bool) -> None:
            try:
                self._send({"type": "reply", "key": key, "payload": {"success": bool(success)}})
            except Exception as exc:
                log.warning("could not reply to dealer request: %s", exc)

        handler = self._handlers.get(ident)
        if handler is None:
            log.warning("no handler for dealer request %s", ident)
            reply(False)
            return
        try:
            payload = parse_request_payload(data)
        except Exception as exc:
            log.warning("could not decode dealer request %s: %s", ident, exc)
            reply(False)
            return
        try:
            handler(Request(ident=ident, key=key, payload=payload, _reply=reply))
        except Exception:
            log.exception("dealer request handler failed for %s", ident)
            reply(False)
