"""Access point connection: handshake, Shannon framing, login and audio keys.

The access point (AP) is Spotify's long-lived TCP service. We use it to log in
(which yields reusable credentials), to learn the account's country and product
attributes, and to request per-file AES keys for audio decryption.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import version
from .crypto import DiffieHellman, Shannon, compute_handshake_keys, verify_server_signature
from .proto import authentication_pb2 as auth_pb
from .proto import keyexchange_pb2 as kx_pb

log = logging.getLogger(__name__)

# packet types
PING = 0x04
REQUEST_KEY = 0x0C
AES_KEY = 0x0D
AES_KEY_ERROR = 0x0E
COUNTRY_CODE = 0x1B
PONG = 0x49
PONG_ACK = 0x4A
PRODUCT_INFO = 0x50
LOGIN = 0xAB
AP_WELCOME = 0xAC
AUTH_FAILURE = 0xAD

MAX_HANDSHAKE_PACKET = 64 * 1024

INITIAL_PING_TIMEOUT = 20.0
PING_TIMEOUT = 80.0  # 60 s expected + 20 s margin
PONG_DELAY = 60.0
PONG_ACK_TIMEOUT = 20.0

_LOGIN_ERRORS = {
    0x0: "protocol error",
    0x2: "try another access point",
    0x5: "bad connection id",
    0x9: "travel restriction",
    0xB: "premium account required",
    0xC: "bad credentials",
    0xD: "could not validate credentials",
    0xE: "account exists",
    0xF: "extra verification required",
    0x10: "invalid app key",
    0x11: "application banned",
}
TRY_ANOTHER_AP = 0x2


class ApError(Exception):
    """Connection-level failure (network, handshake, protocol)."""


class LoginError(ApError):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"login failed: {_LOGIN_ERRORS.get(code, f'error {code}')}")


class AudioKeyError(ApError):
    """The access point refused or did not deliver an audio key."""


@dataclass(frozen=True)
class Credentials:
    username: str | None
    auth_type: int
    auth_data: bytes


def _platform() -> int:
    system = platform.system().lower()
    machine = platform.machine().lower()
    is64 = machine in ("amd64", "x86_64", "arm64", "aarch64")
    if system == "windows":
        return kx_pb.PLATFORM_WIN32_X86_64 if is64 else kx_pb.PLATFORM_WIN32_X86
    if system == "darwin":
        return kx_pb.PLATFORM_OSX_X86_64 if is64 else kx_pb.PLATFORM_OSX_X86
    if machine.startswith(("arm", "aarch")):
        return kx_pb.PLATFORM_LINUX_ARM
    return kx_pb.PLATFORM_LINUX_X86_64 if is64 else kx_pb.PLATFORM_LINUX_X86


def _os_and_cpu() -> tuple[int, int]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_ = {
        "windows": auth_pb.OS_WINDOWS,
        "darwin": auth_pb.OS_OSX,
        "linux": auth_pb.OS_LINUX,
        "freebsd": auth_pb.OS_FREEBSD,
    }.get(system, auth_pb.OS_UNKNOWN)
    if machine in ("amd64", "x86_64"):
        cpu = auth_pb.CPU_X86_64
    elif machine in ("x86", "i386", "i686"):
        cpu = auth_pb.CPU_X86
    elif machine.startswith(("arm", "aarch")):
        cpu = auth_pb.CPU_ARM
    else:
        cpu = auth_pb.CPU_UNKNOWN
    return os_, cpu


class AccessPoint:
    """One authenticated connection to an access point."""

    def __init__(self, address: str, device_id: str, connect_timeout: float = 10.0) -> None:
        host, _, port = address.rpartition(":")
        self._host = host
        self._port = int(port)
        self._device_id = device_id
        self._connect_timeout = connect_timeout

        self._sock: socket.socket | None = None
        self._send_cipher: Shannon | None = None
        self._recv_cipher: Shannon | None = None
        self._send_nonce = 0
        self._recv_nonce = 0
        self._send_lock = threading.Lock()

        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self._keepalive: threading.Thread | None = None
        self._ka_lock = threading.Lock()
        self._ka_state = "expect_ping"
        self._ka_deadline = 0.0

        self._key_lock = threading.Lock()
        self._key_seq = 0
        self._key_pending: dict[int, tuple[threading.Event, list]] = {}

        self._country_event = threading.Event()
        self.country: str = ""
        self.attributes: dict[str, str] = {}
        self.on_closed = None  # callable() invoked once when the connection dies

    # ----- raw socket helpers ------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        chunks = []
        remaining = n
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ApError("connection closed by access point")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # ----- handshake -----------------------------------------------------------

    def connect(self) -> None:
        """Open the TCP connection and complete the encrypted handshake."""
        try:
            self._sock = socket.create_connection((self._host, self._port), self._connect_timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._handshake()
        except (OSError, ApError) as exc:
            self.close()
            if isinstance(exc, ApError):
                raise
            raise ApError(f"could not connect to {self._host}:{self._port}: {exc}") from exc

    def _handshake(self) -> None:
        assert self._sock is not None
        dh = DiffieHellman()

        hello = kx_pb.ClientHello()
        hello.build_info.product = kx_pb.PRODUCT_CLIENT
        hello.build_info.product_flags.append(kx_pb.PRODUCT_FLAG_NONE)
        hello.build_info.platform = _platform()
        hello.build_info.version = version.SPOTIFY_VERSION
        hello.cryptosuites_supported.append(kx_pb.CRYPTO_SUITE_SHANNON)
        hello.login_crypto_hello.diffie_hellman.gc = dh.public_key_bytes()
        hello.login_crypto_hello.diffie_hellman.server_keys_known = 1
        hello.client_nonce = os.urandom(16)
        hello.padding = b"\x1e"
        body = hello.SerializeToString()

        packet = b"\x00\x04" + struct.pack(">I", 2 + 4 + len(body)) + body
        self._sock.sendall(packet)
        accumulator = bytearray(packet)

        header = self._recv_exact(4)
        accumulator += header
        (size,) = struct.unpack(">I", header)
        if size < 4 or size > MAX_HANDSHAKE_PACKET:
            raise ApError(f"invalid handshake packet size {size}")
        data = self._recv_exact(size - 4)
        accumulator += data

        response = kx_pb.APResponseMessage()
        response.ParseFromString(data)
        if response.HasField("login_failed"):
            raise LoginError(response.login_failed.error_code)
        challenge = response.challenge.login_crypto_challenge.diffie_hellman
        if not challenge.gs:
            raise ApError("access point sent no key exchange challenge")
        if not verify_server_signature(challenge.gs, challenge.gs_signature):
            raise ApError("access point signature verification failed")

        shared = dh.shared_secret(challenge.gs)
        hmac_challenge, send_key, recv_key = compute_handshake_keys(shared, bytes(accumulator))

        reply = kx_pb.ClientResponsePlaintext()
        reply.login_crypto_response.diffie_hellman.hmac = hmac_challenge
        reply.pow_response.SetInParent()
        reply.crypto_response.SetInParent()
        body = reply.SerializeToString()
        self._sock.sendall(struct.pack(">I", 4 + len(body)) + body)

        self._send_cipher = Shannon(send_key)
        self._recv_cipher = Shannon(recv_key)
        self._send_nonce = 0
        self._recv_nonce = 0

    # ----- framed packets ------------------------------------------------------

    def send(self, cmd: int, payload: bytes) -> None:
        if len(payload) > 0xFFFF:
            raise ApError("payload too large")
        with self._send_lock:
            if self._sock is None or self._send_cipher is None:
                raise ApError("access point is not connected")
            self._send_cipher.nonce_u32(self._send_nonce)
            self._send_nonce += 1
            data = self._send_cipher.encrypt(bytes([cmd]) + struct.pack(">H", len(payload)) + payload)
            mac = self._send_cipher.finish(4)
            try:
                self._sock.sendall(data + mac)
            except OSError as exc:
                raise ApError(f"send failed: {exc}") from exc

    def _recv_packet(self) -> tuple[int, bytes]:
        assert self._recv_cipher is not None
        header = self._recv_exact(3)
        self._recv_cipher.nonce_u32(self._recv_nonce)
        self._recv_nonce += 1
        header = self._recv_cipher.decrypt(header)
        cmd = header[0]
        (size,) = struct.unpack(">H", header[1:3])
        payload = self._recv_cipher.decrypt(self._recv_exact(size)) if size else b""
        mac = self._recv_exact(4)
        if self._recv_cipher.finish(4) != mac:
            raise ApError("packet MAC mismatch")
        return cmd, payload

    # ----- login -----------------------------------------------------------------

    def authenticate(self, credentials: Credentials) -> Credentials:
        """Log in. Returns reusable credentials on success, raises LoginError otherwise."""
        assert self._sock is not None
        os_, cpu = _os_and_cpu()
        msg = auth_pb.ClientResponseEncrypted()
        if credentials.username:
            msg.login_credentials.username = credentials.username
        msg.login_credentials.typ = credentials.auth_type
        msg.login_credentials.auth_data = credentials.auth_data
        msg.system_info.cpu_family = cpu
        msg.system_info.os = os_
        msg.system_info.system_information_string = version.SYSTEM_INFO
        msg.system_info.device_id = self._device_id
        msg.version_string = version.VERSION_STRING

        self._sock.settimeout(self._connect_timeout)
        try:
            self.send(LOGIN, msg.SerializeToString())
            cmd, payload = self._recv_packet()
        except OSError as exc:
            self.close()
            raise ApError(f"login failed: {exc}") from exc
        except ApError:
            self.close()
            raise

        if cmd == AP_WELCOME:
            welcome = auth_pb.APWelcome()
            welcome.ParseFromString(payload)
            self._sock.settimeout(None)
            return Credentials(
                username=welcome.canonical_username,
                auth_type=welcome.reusable_auth_credentials_type,
                auth_data=welcome.reusable_auth_credentials,
            )
        self.close()
        if cmd == AUTH_FAILURE:
            failed = kx_pb.APLoginFailed()
            failed.ParseFromString(payload)
            raise LoginError(failed.error_code)
        raise ApError(f"unexpected packet 0x{cmd:02x} in reply to login")

    # ----- background dispatch ------------------------------------------------

    def start(self) -> None:
        """Start receiving packets. Call after authenticate()."""
        with self._ka_lock:
            self._ka_state = "expect_ping"
            self._ka_deadline = time.monotonic() + INITIAL_PING_TIMEOUT
        self._reader = threading.Thread(target=self._read_loop, name="ap-reader", daemon=True)
        self._keepalive = threading.Thread(target=self._keepalive_loop, name="ap-keepalive", daemon=True)
        self._reader.start()
        self._keepalive.start()

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                cmd, payload = self._recv_packet()
                try:
                    self._dispatch(cmd, payload)
                except Exception:
                    log.exception("error handling access point packet 0x%02x", cmd)
        except (ApError, OSError) as exc:
            if not self._closed.is_set():
                log.warning("access point connection lost: %s", exc)
        finally:
            self._shutdown(notify=True)

    def _dispatch(self, cmd: int, payload: bytes) -> None:
        if cmd == PING:
            with self._ka_lock:
                self._ka_state = "pending_pong"
                self._ka_deadline = time.monotonic() + PONG_DELAY
        elif cmd == PONG_ACK:
            with self._ka_lock:
                self._ka_state = "expect_ping"
                self._ka_deadline = time.monotonic() + PING_TIMEOUT
        elif cmd == COUNTRY_CODE:
            self.country = payload.decode("utf-8", "replace")
            log.info("account country: %s", self.country)
            self._country_event.set()
        elif cmd == PRODUCT_INFO:
            self.attributes = _parse_product_info(payload)
            log.debug("received %d product attributes", len(self.attributes))
        elif cmd in (AES_KEY, AES_KEY_ERROR):
            self._on_key(cmd, payload)
        else:
            log.debug("ignoring access point packet 0x%02x (%d bytes)", cmd, len(payload))

    def _keepalive_loop(self) -> None:
        while not self._closed.wait(1.0):
            send_pong = False
            with self._ka_lock:
                if time.monotonic() < self._ka_deadline:
                    continue
                state = self._ka_state
                if state == "pending_pong":
                    self._ka_state = "expect_pong_ack"
                    self._ka_deadline = time.monotonic() + PONG_ACK_TIMEOUT
                    send_pong = True
            if send_pong:
                try:
                    self.send(PONG, b"\x00\x00\x00\x00")
                except ApError as exc:
                    log.warning("could not send pong: %s", exc)
                    self._shutdown(notify=True)
                    return
            else:
                log.warning("access point keep-alive timed out (%s)", state)
                self._shutdown(notify=True)
                return

    def wait_for_country(self, timeout: float = 5.0) -> str:
        self._country_event.wait(timeout)
        return self.country

    # ----- audio keys -----------------------------------------------------------

    def request_audio_key(self, track_gid: bytes, file_id: bytes, timeout: float = 5.0) -> bytes:
        """Ask the access point for the AES key of an audio file."""
        event = threading.Event()
        result: list = []
        with self._key_lock:
            seq = self._key_seq
            self._key_seq = (self._key_seq + 1) & 0xFFFFFFFF
            self._key_pending[seq] = (event, result)
        try:
            self.send(REQUEST_KEY, file_id + track_gid + struct.pack(">IH", seq, 0))
            if not event.wait(timeout):
                raise AudioKeyError("audio key request timed out")
        finally:
            with self._key_lock:
                self._key_pending.pop(seq, None)
        value = result[0]
        if isinstance(value, Exception):
            raise value
        return value

    def _on_key(self, cmd: int, payload: bytes) -> None:
        if len(payload) < 4:
            return
        (seq,) = struct.unpack(">I", payload[:4])
        with self._key_lock:
            pending = self._key_pending.get(seq)
        if pending is None:
            log.debug("audio key reply for unknown sequence %d", seq)
            return
        event, result = pending
        if cmd == AES_KEY and len(payload) >= 20:
            result.append(payload[4:20])
        else:
            code = payload[4:6].hex() if len(payload) >= 6 else "?"
            result.append(AudioKeyError(f"access point refused the audio key (code {code})"))
        event.set()

    # ----- shutdown ---------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _shutdown(self, notify: bool) -> None:
        already = self._closed.is_set()
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._key_lock:
            pending = list(self._key_pending.values())
        for event, result in pending:
            if not result:
                result.append(AudioKeyError("access point connection closed"))
            event.set()
        if notify and not already and self.on_closed is not None:
            try:
                self.on_closed()
            except Exception:
                log.exception("on_closed callback failed")

    def close(self) -> None:
        self._shutdown(notify=False)


def _parse_product_info(payload: bytes) -> dict[str, str]:
    """ProductInfo is a small XML document: <products><product><k>v</k>...</product></products>."""
    attrs: dict[str, str] = {}
    try:
        root = ET.fromstring(payload.decode("utf-8", "replace"))
    except ET.ParseError as exc:
        log.warning("could not parse product info: %s", exc)
        return attrs
    for element in root.iter():
        if len(element) == 0 and element.text is not None:
            attrs[element.tag] = element.text
    return attrs
