"""The web UI is a single-page app: index.html plus ES modules under /static/, served by
the Python backend next to its JSON API."""

import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from splitter import web as web_mod
from splitter.app import Engine
from splitter.config import parse_config
from splitter.web import CONTENT_TYPES, STATIC_ROOT, make_handler, resolve_static

JS_FILES = sorted(STATIC_ROOT.rglob("*.js"))


@pytest.fixture
def base(tmp_path):
    engine = Engine(parse_config({"receiver": "soloist"}), tmp_path / "config.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


# ----- the shell ---------------------------------------------------------------------------


def test_index_is_the_app_shell_with_security_headers(base):
    for path in ("/", "/index.html"):
        code, body, headers = get(base, path)
        html = body.decode()
        assert code == 200 and headers["Content-Type"].startswith("text/html")
        assert "<title>" in html and 'id="view"' in html
        assert '<link rel="stylesheet" href="/static/app.css">' in html
        assert '<script type="module" src="/static/app.js"></script>' in html
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        csp = headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp


def test_index_has_no_inline_script_or_style(base):
    """The CSP forbids them, so they would silently not work."""
    html = get(base, "/")[1].decode()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<style" not in html and 'style="' not in html


def test_the_views_do_not_use_inline_style_attributes():
    """style="..." inside markup is blocked by the CSP (el.style.x = ... is fine)."""
    offenders = [p.name for p in JS_FILES if re.search(r'style\s*=\s*"', p.read_text(encoding="utf-8"))]
    assert offenders == []


# ----- static files ------------------------------------------------------------------------------


def test_assets_are_served_with_the_right_content_type(base):
    code, body, headers = get(base, "/static/app.js")
    assert code == 200 and headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert b"startRouter" in body
    code, _, headers = get(base, "/static/views/dashboard.js")
    assert code == 200 and headers["Content-Type"].startswith("text/javascript")
    code, _, headers = get(base, "/static/app.css")
    assert code == 200 and headers["Content-Type"].startswith("text/css")
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/static/nope.js",  # does not exist
        "/static/views",  # a directory
        "/static/",  # nothing asked for
        "/static/..%2Fweb.py",  # escaping the folder, encoded
        "/static/%2e%2e/web.py",
        "/static/views/../../web.py",
        "/static//etc/passwd",  # absolute path
        "/static/..\\web.py",  # backslashes
        "/static/app.js%00.css",  # NUL byte
        "/static/views/%2e%2e/%2e%2e/config.py",
    ],
)
def test_static_never_escapes_its_folder_or_serves_other_types(base, path):
    code, body, _ = get(base, path)
    assert code == 404, path
    assert b"SpotifyBTSplitter" not in body and b"parse_config" not in body


def test_static_only_serves_whitelisted_file_types(tmp_path, monkeypatch):
    (tmp_path / "ok.js").write_text("export {}")
    (tmp_path / "secret.py").write_text("password = 1")
    (tmp_path / "data.json").write_text("{}")
    monkeypatch.setattr(web_mod, "STATIC_ROOT", tmp_path)
    assert resolve_static("/static/ok.js") == (tmp_path.resolve() / "ok.js", CONTENT_TYPES[".js"])
    assert resolve_static("/static/secret.py") is None
    assert resolve_static("/static/data.json") is None
    assert resolve_static("/other/ok.js") is None


def test_static_cannot_be_written_to_and_api_is_not_under_it(base):
    request = urllib.request.Request(base + "/static/app.js", data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request, timeout=5)
    assert err.value.code == 404
    assert get(base, "/api/status")[0] == 200
    assert get(base, "/static/api/status")[0] == 404


# ----- the app itself ----------------------------------------------------------------------------


def test_every_import_in_the_app_resolves_to_a_served_file(base):
    imports = re.compile(r'''(?:from|import)\s*\(?\s*["'](\.{1,2}/[^"']+)["']''')
    assert JS_FILES, "no UI modules found"
    for source in JS_FILES:
        for target in imports.findall(source.read_text(encoding="utf-8")):
            resolved = (source.parent / target).resolve()
            assert resolved.is_file(), f"{source.name} imports missing {target}"
            url = "/static/" + resolved.relative_to(STATIC_ROOT.resolve()).as_posix()
            assert get(base, url)[0] == 200, url


def test_the_router_has_a_view_for_every_tab(base):
    html = get(base, "/")[1].decode()
    tabs = re.findall(r'data-route="([^"]+)"', html)
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    routes = re.findall(r'path:\s*"([^"]+)"', app_js)
    assert tabs and sorted(tabs) == sorted(routes)
    for tab in tabs:
        assert f'href="#{tab}"' in html


def test_the_views_use_only_endpoints_the_backend_has():
    source = "".join(p.read_text(encoding="utf-8") for p in JS_FILES)
    used = set(re.findall(r'"(/api/[a-z_]+)"', source))
    served = set(re.findall(r'"(/api/[a-z_]+)"', Path(web_mod.__file__).read_text(encoding="utf-8")))
    assert used and used <= served, used - served
