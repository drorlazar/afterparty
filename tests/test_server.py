import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from cleanup.scanner.store import Store
from cleanup.server.app import serve


@pytest.fixture
def api(tmp_path):
    root = tmp_path / "disk"
    (root / "a/b").mkdir(parents=True)
    (root / "a/b/f.bin").write_bytes(b"x" * 40_000)
    (root / "odd name ✦ שלום").mkdir()
    (root / "odd name ✦ שלום/new\nline.txt").write_bytes(b"y" * 9000)
    store = Store(str(tmp_path / "db.sqlite"))
    server, app = serve(port=0, store=store, roots=(str(root),), scan_skip=())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, key=app.token, csrf=app.token, raw=False):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if key:
            req.add_header("X-Cleanup-Key", key)
        if csrf and method == "POST":
            req.add_header("X-Cleanup-CSRF", csrf)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data or b"null"))
        except urllib.error.HTTPError as e:
            return e.code, None

    def wait_scan():
        for _ in range(200):
            s, st = call("GET", "/api/status")
            if not st["scanning"]:
                return st
            time.sleep(0.05)
        raise AssertionError("scan did not finish")

    yield call, wait_scan, root, app
    server.shutdown()


def test_auth_required(api):
    call, *_ = api
    assert call("GET", "/api/status", key=None)[0] == 401
    assert call("GET", "/api/status", key="wrong")[0] == 401
    assert call("GET", "/api/status")[0] == 200


def test_post_requires_csrf(api):
    call, *_ = api
    assert call("POST", "/api/scan", body={}, csrf=None)[0] == 401


def test_api_without_snapshot(api):
    call, *_ = api
    s, st = call("GET", "/api/status")
    assert st["scan"] is None
    assert call("GET", "/api/tree?path=/")[0] == 404


def test_scan_then_tree_list_classify_hidden(api):
    call, wait_scan, root, _ = api
    assert call("POST", "/api/scan", body={})[0] == 202
    st = wait_scan()
    assert st["scan"]["status"] == "done"
    s, t = call("GET", f"/api/tree?path={urllib.request.quote(str(root))}&depth=2")
    assert s == 200 and t["size"] > 0
    names = [c["name"] for c in t["children"]]
    assert "a" in names
    s, ls = call("GET", f"/api/list?path={urllib.request.quote(str(root / 'a/b'))}")
    assert s == 200 and ls["entries"][0]["name"] == "f.bin"
    s, v = call("GET", f"/api/classify?path={urllib.request.quote(str(root / 'a'))}")
    assert s == 200 and v["path"] == str(root / "a") and v["evidence"]  # tmp lives under /private → "system"
    s, h = call("GET", "/api/hidden")
    assert s == 200 and "unattributed" in h


def test_api_tree_handles_odd_names(api):
    call, wait_scan, root, _ = api
    call("POST", "/api/scan", body={}); wait_scan()
    odd = root / "odd name ✦ שלום"
    s, ls = call("GET", f"/api/list?path={urllib.request.quote(str(odd))}")
    assert s == 200 and ls["entries"][0]["name"] == "new\nline.txt"
    s, t = call("GET", f"/api/tree?path={urllib.request.quote(str(odd))}&depth=1")
    assert s == 200 and t["name"] == "odd name ✦ שלום"


def test_bad_paths_rejected(api):
    call, *_ = api
    for q in ("relative", "/tmp/../etc", "%00"):
        assert call("GET", f"/api/list?path={q}")[0] == 400


def test_second_scan_rejected(api):
    call, wait_scan, _, app = api
    gate = threading.Event()
    real = app.scan_fn

    def slow_scan(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    app.scan_fn = slow_scan
    assert call("POST", "/api/scan", body={})[0] == 202
    assert call("POST", "/api/scan", body={})[0] == 409
    gate.set()
    wait_scan()


def test_scan_events_stream(api):
    call, wait_scan, root, app = api
    assert call("POST", "/api/scan", body={})[0] == 202
    s, raw = call("GET", "/api/scan/events", raw=True)
    events = [json.loads(l[6:]) for l in raw.decode().splitlines() if l.startswith("data: ")]
    assert events[-1]["state"] == "done"


def test_index_key_serves_page_with_cookie(api):
    # The key is traded for a Strict cookie on the page response itself (no redirect: a redirect started
    # from another site — e.g. the native app's first page — would drop a SameSite=Strict cookie).
    import http.client
    call, _, _, app = api
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=5)
    conn.request("GET", f"/?key={app.token}")
    r = conn.getresponse(); body = r.read()
    assert r.status == 200 and b"Afterparty" in body
    cookie = r.getheader("Set-Cookie")
    assert f"cleanup_key={app.token}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert r.getheader("Referrer-Policy") == "no-referrer"
    assert f'name="csrf" content="{app.token}"'.encode() in body and b"{{CSRF}}" not in body
    conn.request("GET", "/", headers={"Cookie": cookie.split(";")[0]})
    r = conn.getresponse(); r.read()
    assert r.status == 200


def test_errors_return_json_not_dropped_connections(api, tmp_path):
    # Review I3: OS errors must map to HTTP errors instead of killing the connection.
    call, *_ = api
    f = tmp_path / "plain.txt"; f.write_text("x")
    assert call("GET", f"/api/list?path={urllib.request.quote(str(f))}")[0] == 400
    locked = tmp_path / "locked"; locked.mkdir(); locked.chmod(0)
    try:
        assert call("GET", f"/api/list?path={urllib.request.quote(str(locked))}")[0] == 403
    finally:
        locked.chmod(0o700)


def test_broken_db_is_503_not_dropped(api):
    import sqlite3
    call, _, _, app = api
    def boom(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    app.store.latest = boom
    assert call("GET", "/api/status")[0] == 503
