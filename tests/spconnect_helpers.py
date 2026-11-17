"""Shared test helpers: synthetic audio, a fake Spotify file, fake services."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import soundfile as sf
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from spconnect.audio import SAMPLE_RATE, SPOTIFY_OGG_HEADER_END, LoadedTrack, TrackInfo
from spconnect.crypto import AUDIO_AES_IV, DiffieHellman


def sine(seconds: float, freq: float = 440.0, amplitude: float = 0.4) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    left = amplitude * np.sin(2 * np.pi * freq * t)
    right = amplitude * np.sin(2 * np.pi * freq * 1.5 * t)
    return np.stack([left, right], axis=1).astype(np.float32)


def encode_vorbis(samples: np.ndarray) -> bytes:
    """Encode to Ogg Vorbis. Written in small blocks: libsndfile overflows its
    stack on Windows when handed a large buffer for Vorbis encoding."""
    buf = io.BytesIO()
    with sf.SoundFile(buf, "w", samplerate=SAMPLE_RATE, channels=samples.shape[1], format="OGG", subtype="VORBIS") as out:
        for i in range(0, len(samples), 4096):
            out.write(samples[i : i + 4096])
    return buf.getvalue()


def spotify_file(ogg: bytes, key: bytes, track_gain_db: float = -3.0, track_peak: float = 0.9) -> bytes:
    """Wrap an Ogg stream the way Spotify stores it: 0xA7 header bytes, AES-CTR encrypted."""
    header = bytearray(SPOTIFY_OGG_HEADER_END)
    header[0:4] = b"OggS"
    header[144:160] = struct.pack("<4f", track_gain_db, track_peak, 0.0, 1.0)
    plain = bytes(header) + ogg
    encryptor = Cipher(algorithms.AES(key), modes.CTR(AUDIO_AES_IV)).encryptor()
    return encryptor.update(plain) + encryptor.finalize()


class MemorySource:
    """Stands in for CdnFile."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.size = len(data)
        self.closed = False
        self.fail = False

    def read_at(self, offset: int, n: int) -> bytes:
        if self.fail:
            raise OSError("simulated download failure")
        return self.data[offset : offset + n]

    def close(self) -> None:
        self.closed = True


def loaded_track(uri: str, seconds: float, freq: float = 440.0) -> LoadedTrack:
    """A LoadedTrack backed by in-memory, unencrypted Vorbis.

    It reads through a DecryptedStream, so setting `track._source.fail = True`
    simulates the download breaking mid-track.
    """
    from spconnect.audio import DecryptedStream

    ogg = encode_vorbis(sine(seconds, freq))
    source = MemorySource(ogg)
    stream = DecryptedStream(source, None)
    decoder = sf.SoundFile(stream)
    info = TrackInfo(uri=uri, name=uri.rsplit(":", 1)[-1], artists=["Test"], album="Album", duration_ms=int(seconds * 1000))
    return LoadedTrack(info, source, decoder, 1.0, stream)


class RangeServer:
    """Local HTTP server that serves one blob with Range support."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[str] = []
        self.fail_first = 0  # number of initial requests to answer with 503
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                rng = self.headers.get("Range", "")
                outer.requests.append(rng)
                if outer.fail_first > 0:
                    outer.fail_first -= 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = 0, len(outer.data) - 1
                if rng.startswith("bytes="):
                    a, b = rng[6:].split("-")
                    start = int(a)
                    end = min(int(b), len(outer.data) - 1) if b else end
                body = outer.data[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(outer.data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/file"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ----- the client side of zeroconf login (what the Spotify app does) ----------------


def _varint(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    return bytes([(n & 0x7F) | 0x80, n >> 7])


def make_auth_blob(username: str, device_id: str, auth_type: int, auth_data: bytes) -> bytes:
    """Build the inner (base64) credentials blob for a device id."""
    plain = b"I" + _varint(len(username)) + username.encode() + b"P" + _varint(auth_type) + b"Q"
    plain += _varint(len(auth_data)) + auth_data
    plain += b"\x00" * (-len(plain) % 16)
    data = bytearray(plain)
    for j in range(16, len(data)):  # inverse of the receiver's xor chain
        data[j] ^= data[j - 16]
    secret = hashlib.sha1(device_id.encode()).digest()
    base = hashlib.pbkdf2_hmac("sha1", secret, username.encode(), 0x100, 20)
    key = hashlib.sha1(base).digest() + struct.pack(">I", 20)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(bytes(data)) + encryptor.finalize())


def make_add_user_params(username: str, device_id: str, device_public_key: bytes, auth_type: int, auth_data: bytes) -> dict:
    """Build the addUser form fields a Spotify app would send."""
    client = DiffieHellman()
    shared = client.shared_secret(device_public_key)
    base_key = hashlib.sha1(shared).digest()[:16]
    checksum_key = hmac.new(base_key, b"checksum", hashlib.sha1).digest()
    encryption_key = hmac.new(base_key, b"encryption", hashlib.sha1).digest()[:16]
    iv = os.urandom(16)
    inner = make_auth_blob(username, device_id, auth_type, auth_data)
    encryptor = Cipher(algorithms.AES(encryption_key), modes.CTR(iv)).encryptor()
    encrypted = encryptor.update(inner) + encryptor.finalize()
    mac = hmac.new(checksum_key, encrypted, hashlib.sha1).digest()
    return {
        "action": "addUser",
        "userName": username,
        "blob": base64.b64encode(iv + encrypted + mac).decode(),
        "clientKey": base64.b64encode(client.public_key_bytes()).decode(),
        "deviceName": "Test Phone",
    }
