import hashlib
import os

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from spconnect.crypto import (
    AUDIO_AES_IV,
    DiffieHellman,
    Shannon,
    audio_decrypt,
    compute_handshake_keys,
    decode_auth_blob,
    decrypt_zeroconf_blob,
    solve_hashcash,
)
from spconnect.ids import SpotifyId, base62_to_gid, gid_to_base62, is_playable_uri
from spconnect_helpers import make_add_user_params, make_auth_blob

# Produced by this implementation after it was verified against a live Spotify
# access point (the server decrypted our login and we verified its reply's MAC).
SHANNON_VECTORS = [
    ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", 0, "", "", "0aab5702"),
    ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", 1, "ab0003010203", "109725ae9142", "78098e59"),
    (
        "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100",
        258,
        "0000000000000000000000000000000000",
        "ddfb233b6add594c5e9d64c64ad900cdd6",
        "46f38d39",
    ),
    (
        "5370636f6e6e656374207465737420766563746f72206b65792031323334353637",
        65536,
        "48656c6c6f2c2053686f6e6e6f6e21",
        "9ab85e46ce9d21ee1e04f12365ad38",
        "a2ba0a8f",
    ),
]


@pytest.mark.parametrize("key,nonce,plain,cipher,mac", SHANNON_VECTORS)
def test_shannon_known_vectors(key, nonce, plain, cipher, mac):
    s = Shannon(bytes.fromhex(key))
    s.nonce_u32(nonce)
    assert s.encrypt(bytes.fromhex(plain)).hex() == cipher
    assert s.finish(4).hex() == mac


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 5, 7, 8, 9, 31, 32, 33, 255, 1000])
def test_shannon_roundtrip_with_header_split(length):
    """The receiver decrypts a 3-byte header first, then the payload: same result."""
    key = os.urandom(32)
    data = os.urandom(length)
    enc = Shannon(key)
    enc.nonce_u32(5)
    cipher = enc.encrypt(data)
    mac = enc.finish(4)

    dec = Shannon(key)
    dec.nonce_u32(5)
    plain = dec.decrypt(cipher[:3]) + dec.decrypt(cipher[3:])
    assert plain == data
    assert dec.finish(4) == mac


def test_shannon_arbitrary_split_points_agree():
    key = os.urandom(32)
    data = os.urandom(50)
    whole = Shannon(key)
    whole.nonce_u32(9)
    expected = whole.encrypt(data), whole.finish(4)
    for a in range(0, 12):
        for b in range(a, 20):
            s = Shannon(key)
            s.nonce_u32(9)
            out = s.encrypt(data[:a]) + s.encrypt(data[a:b]) + s.encrypt(data[b:])
            assert (out, s.finish(4)) == expected, (a, b)


def test_shannon_mac_detects_tampering_and_wrong_nonce():
    key = os.urandom(32)
    enc = Shannon(key)
    enc.nonce_u32(0)
    cipher = bytearray(enc.encrypt(b"\xab\x00\x05hello"))
    mac = enc.finish(4)

    cipher[4] ^= 1
    dec = Shannon(key)
    dec.nonce_u32(0)
    dec.decrypt(bytes(cipher))
    assert dec.finish(4) != mac

    cipher[4] ^= 1
    dec = Shannon(key)
    dec.nonce_u32(1)
    dec.decrypt(bytes(cipher))
    assert dec.finish(4) != mac


def test_shannon_nonce_resets_the_stream():
    s = Shannon(b"k" * 32)
    s.nonce_u32(3)
    first = s.encrypt(b"same input bytes")
    s.finish(4)
    s.nonce_u32(3)
    assert s.encrypt(b"same input bytes") == first


def test_diffie_hellman_both_sides_agree():
    a, b = DiffieHellman(), DiffieHellman()
    assert a.shared_secret(b.public_key_bytes()) == b.shared_secret(a.public_key_bytes())


def test_handshake_keys_have_expected_sizes_and_depend_on_transcript():
    challenge, send_key, recv_key = compute_handshake_keys(b"secret", b"packets")
    assert (len(challenge), len(send_key), len(recv_key)) == (20, 32, 32)
    assert send_key != recv_key
    assert compute_handshake_keys(b"secret", b"other")[0] != challenge


def test_zeroconf_blob_roundtrip():
    device_id = os.urandom(20).hex()
    device = DiffieHellman()
    auth_data = os.urandom(137)  # longer than 127 to exercise the two-byte length
    params = make_add_user_params("someuser", device_id, device.public_key_bytes(), 1, auth_data)

    import base64

    decrypted = decrypt_zeroconf_blob(
        base64.b64decode(params["blob"]), base64.b64decode(params["clientKey"]), device
    )
    assert decrypted is not None
    auth_type, decoded = decode_auth_blob("someuser", decrypted, device_id)
    assert auth_type == 1
    assert decoded == auth_data


def test_zeroconf_blob_with_bad_mac_is_rejected():
    import base64

    device = DiffieHellman()
    params = make_add_user_params("u", "ab" * 20, device.public_key_bytes(), 1, b"data")
    blob = bytearray(base64.b64decode(params["blob"]))
    blob[20] ^= 0xFF
    assert decrypt_zeroconf_blob(bytes(blob), base64.b64decode(params["clientKey"]), device) is None
    assert decrypt_zeroconf_blob(b"short", b"\x02", device) is None


def test_auth_blob_is_bound_to_device_and_user():
    blob = make_auth_blob("alice", "11" * 20, 1, b"credential-bytes")
    assert decode_auth_blob("alice", blob, "11" * 20) == (1, b"credential-bytes")
    try:
        wrong = decode_auth_blob("alice", blob, "22" * 20)
    except (ValueError, IndexError):
        wrong = None
    assert wrong != (1, b"credential-bytes")


@pytest.mark.parametrize("length", [1, 8, 12])
def test_hashcash_solution_is_valid(length):
    context, prefix = b"login-context", os.urandom(16)
    suffix, seconds = solve_hashcash(context, prefix, length)
    assert len(suffix) == 16
    digest = hashlib.sha1(prefix + suffix).digest()
    assert int.from_bytes(digest[12:], "big") % (1 << length) == 0
    assert seconds >= 0


def test_audio_decrypt_matches_full_decryption_at_any_offset():
    key = os.urandom(16)
    plain = os.urandom(5000)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(AUDIO_AES_IV)).encryptor()
    cipher = encryptor.update(plain) + encryptor.finalize()
    for offset, n in [(0, 5000), (1, 100), (15, 17), (16, 16), (17, 3000), (4999, 1), (160, 0x30)]:
        assert audio_decrypt(key, cipher[offset : offset + n], offset) == plain[offset : offset + n]


def test_base62_roundtrip_and_padding():
    assert gid_to_base62(bytes(16)) == "0" * 22
    assert base62_to_gid("0" * 22) == bytes(16)
    for _ in range(50):
        gid = os.urandom(16)
        assert base62_to_gid(gid_to_base62(gid)) == gid
        assert len(gid_to_base62(gid)) == 22


def test_spotify_id_parsing():
    sid = SpotifyId.from_uri("spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    assert sid.kind == "track"
    assert sid.uri == "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
    assert len(sid.gid) == 16
    with pytest.raises(ValueError):
        SpotifyId.from_uri("spotify:local:artist:album:title:123")
    assert is_playable_uri("spotify:episode:4uLU6hMCjMI75M1A2tKUQC")
    assert not is_playable_uri("spotify:album:4uLU6hMCjMI75M1A2tKUQC")
    assert not is_playable_uri("spotify:delimiter")
