"""Cryptographic primitives used by the Spotify protocols.

- Shannon: the stream cipher + MAC that frames access point packets.
- DiffieHellman: key exchange for the access point and for zeroconf login.
- Blob decryption for zeroconf credentials.
- Hashcash solver for login5 / client-token challenges.
- AES-CTR helper for audio files.

The Shannon implementation follows the reference algorithm (as used by
librespot's MIT-licensed `shannon` crate).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MASK = 0xFFFFFFFF
_N = 16
_INITKONST = 0x6996C53A
_KEYP = 13


def _rotl(w: int, x: int) -> int:
    return ((w << x) | (w >> (32 - x))) & _MASK


def _sbox1(w: int) -> int:
    w ^= _rotl(w, 5) | _rotl(w, 7)
    w ^= _rotl(w, 19) | _rotl(w, 22)
    return w & _MASK


def _sbox2(w: int) -> int:
    w ^= _rotl(w, 7) | _rotl(w, 22)
    w ^= _rotl(w, 5) | _rotl(w, 19)
    return w & _MASK


class Shannon:
    """Shannon stream cipher with integrated MAC."""

    def __init__(self, key: bytes) -> None:
        self.R = [0] * _N
        self.CRC = [0] * _N
        self.initR = [0] * _N
        self.konst = _INITKONST
        self.sbuf = 0
        self.mbuf = 0
        self.nbuf = 0

        self.R[0] = 1
        self.R[1] = 1
        for i in range(2, _N):
            self.R[i] = (self.R[i - 1] + self.R[i - 2]) & _MASK

        self._loadkey(key)
        self.konst = self.R[0]
        self.initR = list(self.R)

    def _cycle(self) -> None:
        R = self.R
        t = R[12] ^ R[13] ^ self.konst
        t = _sbox1(t) ^ _rotl(R[0], 1)
        del R[0]
        R.append(t)
        t = _sbox2(R[2] ^ R[15])
        R[0] ^= t
        self.sbuf = t ^ R[8] ^ R[12]

    def _diffuse(self) -> None:
        for _ in range(_N):
            self._cycle()

    def _loadkey(self, key: bytes) -> None:
        for i in range(0, len(key), 4):
            word = key[i : i + 4].ljust(4, b"\x00")
            self.R[_KEYP] ^= struct.unpack("<I", word)[0]
            self._cycle()
        self.R[_KEYP] ^= len(key) & _MASK
        self._cycle()
        self.CRC = list(self.R)
        self._diffuse()
        for i in range(_N):
            self.R[i] ^= self.CRC[i]

    def nonce(self, nonce: bytes) -> None:
        self.R = list(self.initR)
        self.konst = _INITKONST
        self._loadkey(nonce)
        self.konst = self.R[0]
        self.nbuf = 0

    def nonce_u32(self, n: int) -> None:
        self.nonce(struct.pack(">I", n & _MASK))

    def _macfunc(self, i: int) -> None:
        crc = self.CRC
        t = crc[0] ^ crc[2] ^ crc[15] ^ i
        del crc[0]
        crc.append(t)
        self.R[_KEYP] ^= i

    def _process(self, data: bytes, encrypt: bool) -> bytes:
        buf = bytearray(data)
        pos = 0
        n = len(buf)

        # finish a partially processed word left over from a previous call
        if self.nbuf != 0:
            while self.nbuf != 0 and pos < n:
                shift = 32 - self.nbuf
                b = buf[pos]
                if encrypt:
                    self.mbuf ^= b << shift
                    buf[pos] = b ^ ((self.sbuf >> shift) & 0xFF)
                else:
                    b ^= (self.sbuf >> shift) & 0xFF
                    buf[pos] = b
                    self.mbuf ^= b << shift
                pos += 1
                self.nbuf -= 8
            if self.nbuf != 0:
                return bytes(buf)  # still not a whole word
            self._macfunc(self.mbuf & _MASK)

        # whole words
        end_words = pos + ((n - pos) & ~3)
        while pos < end_words:
            self._cycle()
            (t,) = struct.unpack_from("<I", buf, pos)
            if encrypt:
                self._macfunc(t)
                t ^= self.sbuf
            else:
                t ^= self.sbuf
                self._macfunc(t)
            struct.pack_into("<I", buf, pos, t)
            pos += 4

        # trailing bytes
        if pos < n:
            self._cycle()
            self.mbuf = 0
            self.nbuf = 32
            while pos < n:
                shift = 32 - self.nbuf
                b = buf[pos]
                if encrypt:
                    self.mbuf ^= b << shift
                    buf[pos] = b ^ ((self.sbuf >> shift) & 0xFF)
                else:
                    b ^= (self.sbuf >> shift) & 0xFF
                    buf[pos] = b
                    self.mbuf ^= b << shift
                pos += 1
                self.nbuf -= 8
        return bytes(buf)

    def encrypt(self, data: bytes) -> bytes:
        return self._process(data, True)

    def decrypt(self, data: bytes) -> bytes:
        return self._process(data, False)

    def finish(self, length: int = 4) -> bytes:
        """Finalise the MAC for everything processed since the last nonce."""
        if self.nbuf != 0:
            self._macfunc(self.mbuf & _MASK)
        self._cycle()
        self.R[_KEYP] ^= _INITKONST ^ ((self.nbuf << 3) & _MASK)
        self.nbuf = 0
        for i in range(_N):
            self.R[i] ^= self.CRC[i]
        self._diffuse()
        out = bytearray()
        while len(out) < length:
            self._cycle()
            out += struct.pack("<I", self.sbuf)
        return bytes(out[:length])


# ----- Diffie-Hellman -------------------------------------------------------

_DH_PRIME = int.from_bytes(
    bytes.fromhex(
        "ffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74"
        "020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f1437"
        "4fe1356d6d51c245e485b576625e7ec6f44c42e9a63a3620ffffffffffffffff"
    ),
    "big",
)
_DH_GENERATOR = 2


def _int_to_bytes(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")


class DiffieHellman:
    def __init__(self, private_key: bytes | None = None) -> None:
        self._private = int.from_bytes(private_key or os.urandom(95), "big")
        self._public = pow(_DH_GENERATOR, self._private, _DH_PRIME)

    def public_key_bytes(self) -> bytes:
        return _int_to_bytes(self._public)

    def shared_secret(self, remote_key: bytes) -> bytes:
        return _int_to_bytes(pow(int.from_bytes(remote_key, "big"), self._private, _DH_PRIME))


# ----- access point server key ----------------------------------------------

_SERVER_KEY = int.from_bytes(
    bytes.fromhex(
        "ace0460bffc230aff46bfec3bfbf863da191c6cc336c93a14fb3b01612acac6a"
        "f180e7f614d9429dbe2e346643e362d2327a1a0d923baedd1402b18155056104"
        "d52c96a44c1ecc024ad4b20c001f17edc22fc43521c8f0cbaed2add72b0f9db3"
        "c5321a2afe59f35a0dac68f1fa621efb2c8d0cb7392d9247e3d7351a6dbd24c2"
        "ae255b88ffab73298a0bcccd0c58673189e8bd3480784a5fc96b899d956bfc86"
        "d74f33a6781796c9c32d0d32a5abcd0527e2f710a39613c42f99c027bfed049c"
        "3c275804b6b219f9c12f02e94863eca1b642a09d4825f8b39dd0e86af9484da1"
        "c2ba863042ea9db3086c190e48b39d66eb0006a25aeea11b13873cd719e655bd"
    ),
    "big",
)


def verify_server_signature(gs: bytes, signature: bytes) -> bool:
    """Check the access point's RSA signature over its DH public key."""
    public_key = rsa.RSAPublicNumbers(65537, _SERVER_KEY).public_key()
    try:
        public_key.verify(signature, gs, padding.PKCS1v15(), hashes.SHA1())
        return True
    except Exception:
        return False


def compute_handshake_keys(shared_secret: bytes, packets: bytes) -> tuple[bytes, bytes, bytes]:
    """Return (challenge_hmac, send_key, recv_key) for the AP handshake."""
    data = b""
    for i in range(1, 6):
        data += hmac.new(shared_secret, packets + bytes([i]), hashlib.sha1).digest()
    challenge = hmac.new(data[:0x14], packets, hashlib.sha1).digest()
    return challenge, data[0x14:0x34], data[0x34:0x54]


# ----- zeroconf credentials --------------------------------------------------


def decrypt_zeroconf_blob(blob: bytes, client_key: bytes, dh: DiffieHellman) -> bytes | None:
    """First layer: decrypt the addUser blob. Returns None on a MAC mismatch."""
    if len(blob) < 16 + 20:
        return None
    shared = dh.shared_secret(client_key)
    iv, encrypted, checksum = blob[:16], blob[16:-20], blob[-20:]
    base_key = hashlib.sha1(shared).digest()[:16]
    checksum_key = hmac.new(base_key, b"checksum", hashlib.sha1).digest()
    encryption_key = hmac.new(base_key, b"encryption", hashlib.sha1).digest()[:16]
    mac = hmac.new(checksum_key, encrypted, hashlib.sha1).digest()
    if not hmac.compare_digest(mac, checksum):
        return None
    decryptor = Cipher(algorithms.AES(encryption_key), modes.CTR(iv)).decryptor()
    return decryptor.update(encrypted) + decryptor.finalize()


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    lo = data[pos]
    pos += 1
    if lo & 0x80 == 0:
        return lo, pos
    hi = data[pos]
    return (lo & 0x7F) | (hi << 7), pos + 1


def decode_auth_blob(username: str, encrypted_blob_b64: bytes, device_id: str) -> tuple[int, bytes]:
    """Second layer: decode the blob into (authentication_type, auth_data)."""
    secret = hashlib.sha1(device_id.encode()).digest()
    base = hashlib.pbkdf2_hmac("sha1", secret, username.encode(), 0x100, 20)
    key = hashlib.sha1(base).digest() + struct.pack(">I", 20)

    data = bytearray(base64.b64decode(encrypted_blob_b64))
    whole = len(data) - len(data) % 16
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    data[:whole] = decryptor.update(bytes(data[:whole])) + decryptor.finalize()

    length = len(data)
    for i in range(length - 0x10):
        data[length - i - 1] ^= data[length - i - 0x11]

    blob = bytes(data)
    pos = 1  # skip one byte
    n, pos = _read_varint(blob, pos)
    pos += n  # skip a length-prefixed field
    pos += 1  # skip one byte
    auth_type, pos = _read_varint(blob, pos)
    pos += 1  # skip one byte
    n, pos = _read_varint(blob, pos)
    auth_data = blob[pos : pos + n]
    if len(auth_data) != n:
        raise ValueError("auth blob is truncated")
    return auth_type, auth_data


# ----- hashcash --------------------------------------------------------------


def _trailing_zero_bits(digest: bytes) -> int:
    value = int.from_bytes(digest[12:20], "big")
    if value == 0:
        return 64
    return (value & -value).bit_length() - 1


def solve_hashcash(context: bytes, prefix: bytes, length: int, timeout: float = 5.0) -> tuple[bytes, float]:
    """Find a 16-byte suffix so sha1(prefix + suffix) ends in `length` zero bits.

    Returns (suffix, seconds_taken).
    """
    start = time.monotonic()
    md = hashlib.sha1(context).digest()
    target = int.from_bytes(md[12:20], "big", signed=True)
    counter = 0
    while True:
        if counter & 0x3FF == 0 and time.monotonic() - start > timeout:
            raise TimeoutError("hashcash challenge timed out")
        suffix = struct.pack(">q", _wrap_i64(target + counter)) + struct.pack(">q", counter)
        if _trailing_zero_bits(hashlib.sha1(prefix + suffix).digest()) >= length:
            return suffix, time.monotonic() - start
        counter += 1


def _wrap_i64(n: int) -> int:
    n &= 0xFFFFFFFFFFFFFFFF
    return n - (1 << 64) if n >= (1 << 63) else n


# ----- audio ------------------------------------------------------------------

AUDIO_AES_IV = bytes.fromhex("72e067fbddcbcf77ebe8bc643f630d93")
_IV_INT = int.from_bytes(AUDIO_AES_IV, "big")


def audio_decrypt(key: bytes, data: bytes, offset: int) -> bytes:
    """Decrypt `data`, which starts at byte `offset` of an AES-128-CTR audio file."""
    block, skip = divmod(offset, 16)
    iv = ((_IV_INT + block) & ((1 << 128) - 1)).to_bytes(16, "big")
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    if skip:
        decryptor.update(b"\x00" * skip)
    return decryptor.update(data)
