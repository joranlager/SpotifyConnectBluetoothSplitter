"""Dealer message decoding and the zeroconf discovery server."""

import base64
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

from spconnect.ap import _parse_product_info
from spconnect.dealer import parse_message, parse_request_payload
from spconnect.discovery import Discovery
from spconnect_helpers import make_add_user_params


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_message_with_base64_payload():
    msg = parse_message({"uri": "hm://x", "headers": {"a": "b"}, "payloads": [b64(b"\x01\x02\x03")]})
    assert (msg.uri, msg.headers, msg.payload) == ("hm://x", {"a": "b"}, b"\x01\x02\x03")


def test_message_with_gzip_payload():
    raw = gzip.compress(b"cluster-bytes")
    msg = parse_message({"uri": "hm://x", "headers": {"Transfer-Encoding": "gzip"}, "payloads": [b64(raw)]})
    assert msg.payload == b"cluster-bytes"


def test_message_without_payload_keeps_headers():
    msg = parse_message({"uri": "hm://pusher/v1/connections/abc", "headers": {"Spotify-Connection-Id": "abc"}})
    assert msg.payload is None
    assert msg.headers["Spotify-Connection-Id"] == "abc"


def test_message_with_json_payload():
    msg = parse_message({"uri": "x", "payloads": [{"key": "value"}]})
    assert msg.payload is None and msg.json_payload == {"key": "value"}
    msg = parse_message({"uri": "x", "payloads": ["{not base64!}"]})
    assert msg.json_payload == "{not base64!}"


def test_message_with_several_payloads_is_rejected():
    with pytest.raises(ValueError):
        parse_message({"uri": "x", "payloads": ["YQ==", "Yg=="]})


def test_request_payload_plain_and_gzip():
    command = {"message_id": 7, "sent_by_device_id": "dev", "command": {"endpoint": "pause"}}
    data = json.dumps(command).encode()
    plain = {"payload": {"compressed": b64(data)}}
    zipped = {"headers": {"Transfer-Encoding": "gzip"}, "payload": {"compressed": b64(gzip.compress(data))}}
    assert parse_request_payload(plain) == command
    assert parse_request_payload(zipped) == command


def test_product_info_xml():
    xml = b"<products><product><type>premium</type><autoplay>1</autoplay><catalogue>premium</catalogue></product></products>"
    assert _parse_product_info(xml) == {"type": "premium", "autoplay": "1", "catalogue": "premium"}
    assert _parse_product_info(b"not xml") == {}


@pytest.fixture
def discovery():
    logins = []
    accept = {"value": True}

    def on_login(credentials):
        logins.append(credentials)
        return accept["value"]

    d = Discovery("Test Speaker", os.urandom(20).hex(), on_login)
    d.start(advertise=False)
    yield d, logins, accept
    d.close()


def get(d: Discovery, query: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{d.port}/?{query}", timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def post(d: Discovery, params: dict):
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{d.port}/", data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_get_info_describes_the_device(discovery):
    d, _, _ = discovery
    status, info = get(d, "action=getInfo")
    assert status == 200
    assert info["status"] == 101
    assert info["remoteName"] == "Test Speaker"
    assert info["deviceID"] == d.device_id
    assert info["deviceType"] == "Speaker"
    assert info["activeUser"] == ""
    assert len(base64.b64decode(info["publicKey"])) >= 90


def test_unknown_action_is_404(discovery):
    d, _, _ = discovery
    assert get(d, "action=nope")[0] == 404
    assert get(d, "")[0] == 404


def test_add_user_decrypts_credentials_and_reports_the_active_user(discovery):
    d, logins, _ = discovery
    _, info = get(d, "action=getInfo")
    secret = os.urandom(200)
    params = make_add_user_params("alice", d.device_id, base64.b64decode(info["publicKey"]), 1, secret)

    status, body = post(d, params)
    assert (status, body["status"], body["statusString"]) == (200, 101, "OK")
    assert len(logins) == 1
    assert (logins[0].username, logins[0].auth_type, logins[0].auth_data) == ("alice", 1, secret)
    assert get(d, "action=getInfo")[1]["activeUser"] == "alice"


def test_add_user_with_tampered_blob_is_refused(discovery):
    d, logins, _ = discovery
    _, info = get(d, "action=getInfo")
    params = make_add_user_params("alice", d.device_id, base64.b64decode(info["publicKey"]), 1, b"secret")
    blob = bytearray(base64.b64decode(params["blob"]))
    blob[30] ^= 0x55
    params["blob"] = b64(bytes(blob))
    status, body = post(d, params)
    assert status == 200 and body["statusString"] == "ERROR-MAC"
    assert logins == []


def test_add_user_reports_failure_when_login_is_rejected(discovery):
    d, logins, accept = discovery
    accept["value"] = False
    _, info = get(d, "action=getInfo")
    params = make_add_user_params("bob", d.device_id, base64.b64decode(info["publicKey"]), 1, b"secret")
    status, body = post(d, params)
    assert status == 503 and body["status"] == 102
    assert len(logins) == 1
    assert get(d, "action=getInfo")[1]["activeUser"] == ""


def test_add_user_with_missing_fields_is_a_bad_request(discovery):
    d, logins, _ = discovery
    status, _ = post(d, {"action": "addUser", "userName": "x"})
    assert status == 400 and logins == []
