"""HTTP side of a Spotify session: endpoint lookup, tokens and the spclient API.

- ApResolver: asks apresolve.spotify.com for access point, dealer and spclient hosts.
- ClientToken: an app-level token every spclient/login5 request carries.
- Login5: turns the stored credentials from the access point into an access token.
- SpClient: authenticated requests (connect-state, metadata, storage, context).
"""

from __future__ import annotations

import json
import logging
import platform
import random
import threading
import time

import requests
from google.protobuf import json_format

from . import version
from .crypto import solve_hashcash
from .proto import autoplay_context_request_pb2 as autoplay_pb
from .proto import clienttoken_http_pb2 as ct_pb
from .proto import connect_pb2
from .proto import context_pb2
from .proto import context_page_pb2
from .proto import extended_metadata_pb2 as extmeta_pb
from .proto import login5_pb2
from .proto import storage_resolve_pb2

log = logging.getLogger(__name__)

TIMEOUT = 15.0
FALLBACK = {
    "accesspoint": ["ap.spotify.com:443"],
    "dealer": ["dealer.spotify.com:443"],
    "spclient": ["spclient.wg.spotify.com:443"],
}


class HttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(f"HTTP {status}{': ' + message if message else ''}")


class AuthError(Exception):
    """Token acquisition failed for a reason retrying will not fix."""


def _fill_page(page: context_page_pb2.ContextPage, data: dict) -> None:
    for key in ("page_url", "next_page_url"):
        if isinstance(data.get(key), str):
            setattr(page, key, data[key])
    for key, value in (data.get("metadata") or {}).items():
        if isinstance(value, str):
            page.metadata[key] = value
    for track_data in data.get("tracks") or []:
        if not isinstance(track_data, dict):
            continue
        track = page.tracks.add()
        for key in ("uri", "uid"):
            if isinstance(track_data.get(key), str):
                setattr(track, key, track_data[key])
        for key, value in (track_data.get("metadata") or {}).items():
            if isinstance(value, str):
                track.metadata[key] = value


def parse_page(data: dict | None) -> context_page_pb2.ContextPage:
    """Build a ContextPage from JSON, tolerating unknown or oddly typed fields."""
    page = context_page_pb2.ContextPage()
    if not isinstance(data, dict):
        return page
    try:
        json_format.ParseDict(data, page, ignore_unknown_fields=True)
        return page
    except json_format.ParseError as exc:
        log.debug("context page needs manual parsing: %s", exc)
    page = context_page_pb2.ContextPage()
    _fill_page(page, data)
    return page


def parse_context(data: dict | None) -> context_pb2.Context:
    """Build a Context from JSON, tolerating unknown or oddly typed fields."""
    ctx = context_pb2.Context()
    if not isinstance(data, dict):
        return ctx
    try:
        json_format.ParseDict(data, ctx, ignore_unknown_fields=True)
        return ctx
    except json_format.ParseError as exc:
        log.debug("context needs manual parsing: %s", exc)
    ctx = context_pb2.Context()
    for key in ("uri", "url"):
        if isinstance(data.get(key), str):
            setattr(ctx, key, data[key])
    for key, value in (data.get("metadata") or {}).items():
        if isinstance(value, str):
            ctx.metadata[key] = value
    for page_data in data.get("pages") or []:
        if isinstance(page_data, dict):
            _fill_page(ctx.pages.add(), page_data)
    return ctx


def _json_object(body: bytes) -> dict:
    if not body:
        raise LookupError("empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise LookupError(f"response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LookupError("response is not a JSON object")
    return data


def new_http_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = version.user_agent()
    return session


# ----- endpoint resolution -----------------------------------------------------


class ApResolver:
    URL = "https://apresolve.spotify.com/?type=accesspoint&type=dealer&type=spclient"

    def __init__(self, http: requests.Session) -> None:
        self._http = http
        self._lock = threading.Lock()
        self._data: dict[str, list[str]] = {}
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        try:
            resp = self._http.get(self.URL, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("apresolve failed (%s), using fallback endpoints", exc)
            data = {}
        merged = {}
        for kind, fallback in FALLBACK.items():
            hosts = [h for h in data.get(kind, []) if isinstance(h, str)]
            merged[kind] = hosts + [h for h in fallback if h not in hosts]
        self._data = merged
        self._fetched_at = time.monotonic()

    def get(self, kind: str) -> list[str]:
        with self._lock:
            if not self._data or time.monotonic() - self._fetched_at > 3600:
                self._refresh()
            return list(self._data[kind])


# ----- client token ---------------------------------------------------------------


class ClientToken:
    URL = "https://clienttoken.spotify.com/v1/clienttoken"

    def __init__(self, http: requests.Session, device_id: str) -> None:
        self._http = http
        self._device_id = device_id
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def _post(self, message) -> ct_pb.ClientTokenResponse:
        resp = self._http.post(
            self.URL,
            data=message.SerializeToString(),
            headers={"Accept": "application/x-protobuf"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise HttpError(resp.status_code, "client token request failed")
        out = ct_pb.ClientTokenResponse()
        out.ParseFromString(resp.content)
        return out

    def _build_request(self) -> ct_pb.ClientTokenRequest:
        req = ct_pb.ClientTokenRequest()
        req.request_type = ct_pb.REQUEST_CLIENT_DATA_REQUEST
        data = req.client_data
        data.client_version = version.SPOTIFY_SEMANTIC_VERSION
        data.client_id = version.CLIENT_ID
        sdk = data.connectivity_sdk_data
        sdk.device_id = self._device_id
        system = platform.system().lower()
        machine = platform.machine().lower()
        if system == "windows":
            win = sdk.platform_specific_data.desktop_windows
            try:
                build = int(platform.version().split(".")[-1])
            except ValueError:
                build = 21370
            pe, image = {
                "amd64": (34404, 34404),
                "x86_64": (34404, 34404),
                "arm64": (43620, 452),
            }.get(machine, (332, 332))
            win.os_version = 10
            win.os_build = build
            win.platform_id = 2
            win.unknown_value_6 = 9
            win.image_file_machine = image
            win.pe_machine = pe
            win.unknown_value_10 = True
        elif system == "darwin":
            mac = sdk.platform_specific_data.desktop_macos
            mac.system_version = platform.mac_ver()[0] or "0"
            mac.hw_model = "iMac21,1"
            mac.compiled_cpu_type = machine
        else:
            linux = sdk.platform_specific_data.desktop_linux
            linux.system_name = "Linux"
            linux.system_release = platform.release() or "0"
            linux.system_version = platform.version() or "0"
            linux.hardware = machine
        return req

    def get(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token

            request = self._build_request()
            response = self._post(request)
            for _ in range(3):
                if response.response_type == ct_pb.RESPONSE_GRANTED_TOKEN_RESPONSE:
                    break
                if response.response_type != ct_pb.RESPONSE_CHALLENGES_RESPONSE:
                    raise AuthError(f"unknown client token response type {response.response_type}")
                challenges = response.challenges
                if not challenges.challenges:
                    raise AuthError("client token challenge response had no challenges")
                params = challenges.challenges[0].evaluate_hashcash_parameters
                suffix, _ = solve_hashcash(b"", bytes.fromhex(params.prefix), params.length)
                answer = ct_pb.ClientTokenRequest()
                answer.request_type = ct_pb.REQUEST_CHALLENGE_ANSWERS_REQUEST
                answer.challenge_answers.state = challenges.state
                item = answer.challenge_answers.answers.add()
                item.ChallengeType = ct_pb.CHALLENGE_HASH_CASH
                item.hash_cash.suffix = suffix.hex().upper()
                response = self._post(answer)
            else:
                raise AuthError("could not solve the client token challenge")

            granted = response.granted_token
            if not granted.token:
                raise AuthError("client token response contained no token")
            lifetime = granted.refresh_after_seconds if granted.refresh_after_seconds > 0 else 7200
            self._token = granted.token
            self._expires_at = time.monotonic() + lifetime - 10
            log.debug("obtained client token (valid %d s)", lifetime)
            return self._token


# ----- login5 ---------------------------------------------------------------------------


class Login5:
    URL = "https://login5.spotify.com/v3/login"
    MAX_TRIES = 3

    def __init__(self, http: requests.Session, client_token: ClientToken, device_id: str) -> None:
        self._http = http
        self._client_token = client_token
        self._device_id = device_id
        self._lock = threading.Lock()
        self._username = ""
        self._stored = b""
        self._token: str | None = None
        self._expires_at = 0.0

    def set_credentials(self, username: str, stored_credential: bytes) -> None:
        with self._lock:
            self._username = username
            self._stored = stored_credential
            self._token = None

    def _post(self, message: login5_pb2.LoginRequest) -> login5_pb2.LoginResponse:
        resp = self._http.post(
            self.URL,
            data=message.SerializeToString(),
            headers={
                "Accept": "application/x-protobuf",
                "Client-Token": self._client_token.get(),
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise HttpError(resp.status_code, "login5 request failed")
        out = login5_pb2.LoginResponse()
        out.ParseFromString(resp.content)
        return out

    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.monotonic() < self._expires_at:
                return self._token
            if not self._stored:
                raise AuthError("no stored credentials to obtain an access token with")

            request = login5_pb2.LoginRequest()
            request.client_info.client_id = version.CLIENT_ID
            request.client_info.device_id = self._device_id
            request.stored_credential.username = self._username
            request.stored_credential.data = self._stored

            response = self._post(request)
            for attempt in range(1, self.MAX_TRIES + 1):
                which = response.WhichOneof("response")
                if which == "ok":
                    ok = response.ok
                    lifetime = ok.access_token_expires_in if ok.access_token_expires_in > 0 else 3600
                    self._token = ok.access_token
                    self._expires_at = time.monotonic() + lifetime - 30
                    if ok.stored_credential:
                        self._stored = ok.stored_credential
                    log.debug("obtained access token (valid %d s)", lifetime)
                    return self._token
                if which == "error":
                    if response.error in (login5_pb2.TIMEOUT, login5_pb2.TOO_MANY_ATTEMPTS):
                        time.sleep(3)
                    else:
                        name = login5_pb2.LoginError.Name(response.error)
                        raise AuthError(f"login5 refused the credentials: {name}")
                elif which == "challenges":
                    for challenge in response.challenges.challenges:
                        kind = challenge.WhichOneof("challenge")
                        if kind == "code":
                            raise AuthError("login5 asked for a code challenge, which is not supported")
                        if kind != "hashcash":
                            continue
                        suffix, took = solve_hashcash(
                            response.login_context, challenge.hashcash.prefix, challenge.hashcash.length
                        )
                        solution = request.challenge_solutions.solutions.add()
                        solution.hashcash.suffix = suffix
                        solution.hashcash.duration.seconds = int(took)
                        solution.hashcash.duration.nanos = int((took % 1) * 1e9)
                    request.login_context = response.login_context
                if attempt == self.MAX_TRIES:
                    break
                response = self._post(request)
            raise AuthError(f"login5 did not succeed after {self.MAX_TRIES} attempts")


# ----- spclient --------------------------------------------------------------------------


class SpClient:
    """Authenticated HTTP API. Thread-safe."""

    def __init__(
        self,
        http: requests.Session,
        resolver: ApResolver,
        login5: Login5,
        client_token: ClientToken,
        device_id: str,
    ) -> None:
        self._http = http
        self._resolver = resolver
        self._login5 = login5
        self._client_token = client_token
        self._device_id = device_id
        self._lock = threading.Lock()
        self._hosts: list[str] = []
        self._host_index = 0
        self.country = ""  # set once the access point reports it

    def _base_url(self) -> str:
        with self._lock:
            if not self._hosts:
                self._hosts = self._resolver.get("spclient")
                self._host_index = 0
            return f"https://{self._hosts[self._host_index % len(self._hosts)]}"

    def _next_host(self) -> None:
        with self._lock:
            self._host_index += 1

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        metrics: bool = True,
        salt: bool = True,
        tries: int = 3,
        ok: tuple[int, ...] = (200,),
    ) -> bytes:
        """Send a request to the spclient. Returns the response body or raises."""
        last: Exception | None = None
        force_token = False
        for attempt in range(tries):
            url = self._base_url() + endpoint
            if metrics and "product=0" not in url:
                url += f"{'&' if '?' in url else '?'}product=0&country={self.country}"
            if salt and "salt=" not in url:
                url += f"{'&' if '?' in url else '?'}salt={random.getrandbits(32)}"

            hdrs = dict(headers or {})
            hdrs["Authorization"] = f"Bearer {self._login5.access_token(force=force_token)}"
            force_token = False
            try:
                hdrs["Client-Token"] = self._client_token.get()
            except (requests.RequestException, HttpError, AuthError) as exc:
                log.warning("continuing without a client token: %s", exc)

            try:
                resp = self._http.request(method, url, headers=hdrs, data=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last = exc
                log.debug("spclient %s %s failed: %s", method, endpoint, exc)
                self._next_host()
                time.sleep(min(0.5 * (attempt + 1), 2.0))
                continue

            if resp.status_code in ok:
                return resp.content
            last = HttpError(resp.status_code, resp.text[:200])
            if resp.status_code == 401:
                force_token = True
                continue
            if resp.status_code in (500, 502, 503, 504):
                time.sleep(min(0.5 * (attempt + 1), 2.0))
                continue
            break
        assert last is not None
        raise last

    # ----- connect state ------------------------------------------------------

    def put_connect_state(
        self, connection_id: str, request: connect_pb2.PutStateRequest
    ) -> connect_pb2.Cluster:
        body = self.request(
            "PUT",
            f"/connect-state/v1/devices/{self._device_id}",
            headers={
                "X-Spotify-Connection-Id": connection_id,
                "Content-Type": "application/x-protobuf",
            },
            body=request.SerializeToString(),
        )
        cluster = connect_pb2.Cluster()
        if body:
            cluster.ParseFromString(body)
        return cluster

    def put_connect_state_inactive(self, connection_id: str, notify: bool = False) -> None:
        self.request(
            "PUT",
            f"/connect-state/v1/devices/{self._device_id}/inactive?notify={'true' if notify else 'false'}",
            headers={"X-Spotify-Connection-Id": connection_id},
            ok=(200, 204),
        )

    def delete_connect_state(self) -> None:
        self.request("DELETE", f"/connect-state/v1/devices/{self._device_id}", ok=(200, 204), tries=1)

    # ----- metadata -----------------------------------------------------------

    def extended_metadata(self, uri: str, kind: int, message):
        """Fetch one extension for one entity and parse it into `message`."""
        req = extmeta_pb.BatchedEntityRequest()
        entity = req.entity_request.add()
        entity.entity_uri = uri
        entity.query.add().extension_kind = kind
        body = self.request(
            "POST",
            "/extended-metadata/v0/extended-metadata",
            headers={"Content-Type": "application/x-protobuf"},
            body=req.SerializeToString(),
        )
        resp = extmeta_pb.BatchedExtensionResponse()
        resp.ParseFromString(body)
        for array in resp.extended_metadata:
            if array.extension_kind != kind:
                continue
            for data in array.extension_data:
                if data.entity_uri and data.entity_uri != uri:
                    continue
                if data.header.status_code not in (0, 200):
                    raise HttpError(data.header.status_code, f"extended metadata for {uri}")
                message.ParseFromString(data.extension_data.value)
                return message
        raise LookupError(f"no extended metadata of kind {kind} for {uri}")

    # ----- storage ------------------------------------------------------------

    def resolve_audio_storage(self, file_id: bytes) -> list[str]:
        """Return CDN URLs for an audio file."""
        body = self.request("GET", f"/storage-resolve/files/audio/interactive/{file_id.hex()}")
        resp = storage_resolve_pb2.StorageResolveResponse()
        resp.ParseFromString(body)
        if resp.result != storage_resolve_pb2.StorageResolveResponse.CDN:
            name = storage_resolve_pb2.StorageResolveResponse.Result.Name(resp.result)
            raise LookupError(f"audio storage is not available from a CDN ({name})")
        if not resp.cdnurl:
            raise LookupError("storage resolve returned no CDN urls")
        return list(resp.cdnurl)

    # ----- contexts -----------------------------------------------------------

    def get_context(self, uri: str) -> context_pb2.Context:
        body = self.request("GET", f"/context-resolve/v1/{uri}", metrics=False, salt=False)
        return parse_context(_json_object(body))

    def get_autoplay_context(self, context_uri: str, recent_track_uris: list[str]) -> context_pb2.Context:
        req = autoplay_pb.AutoplayContextRequest()
        req.context_uri = context_uri
        req.recent_track_uri.extend(recent_track_uris)
        body = self.request(
            "POST",
            "/context-resolve/v1/autoplay",
            headers={"Content-Type": "application/x-protobuf"},
            body=req.SerializeToString(),
            metrics=False,
            salt=False,
        )
        return parse_context(_json_object(body))

    def get_page(self, page_url: str) -> context_page_pb2.ContextPage:
        """Fetch a context page from an hm:// url."""
        if not page_url.startswith("hm://"):
            raise ValueError(f"not an hm:// url: {page_url}")
        body = self.request(
            "GET", "/" + page_url[len("hm://") :], headers={"Accept": "application/json"}
        )
        return parse_page(_json_object(body))
