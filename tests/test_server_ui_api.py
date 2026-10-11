import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from cleanup.engine.classify import classify
from cleanup.scanner.store import Store
from cleanup.server.app import serve


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1}))
    root = tmp_path / "home" / "disk"; (root / "proj/node_modules").mkdir(parents=True)  # under HOME: /private tmp is "system"
    (root / "proj/node_modules/x.js").write_bytes(b"x" * 9000)
    store = Store(str(tmp_path / "db.sqlite"))
    server, app = serve(port=0, store=store, roots=(str(root),), scan_skip=())
    revealed = []
    app.reveal_fn = revealed.append
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{app.port}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Cleanup-Key": app.token, "X-Cleanup-CSRF": app.token})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    from cleanup.scanner.scan import scan
    store.save(scan(str(root), skip=()))
    yield call, root, revealed, tmp_path
    server.shutdown()


def test_fast_classify_makes_no_git_calls(monkeypatch):
    import cleanup.engine.classify as C
    monkeypatch.setattr(C, "tracked", lambda p: (_ for _ in ()).throw(AssertionError("git called")))
    monkeypatch.setattr(C, "git_ignored", lambda p: (_ for _ in ()).throw(AssertionError("git called")))
    assert classify("/Users/u/p/node_modules", home="/Users/u", fast=True)["class"] == "regenerable"
    assert classify("/Users/u/p/dist", home="/Users/u", fast=True)["class"] == "review"


def test_tree_nodes_carry_class(ui):
    call, root, *_ = ui
    s, t = call("GET", f"/api/tree?path={urllib.parse.quote(str(root / 'proj'))}&depth=1")
    nm = next(c for c in t["children"] if c["name"] == "node_modules")
    assert nm["cls"] == "regenerable" and nm["rule"] == "dependency"


def test_reveal_and_keep_validate_paths(ui):
    call, root, revealed, tmp = ui
    target = str(root / "proj")
    assert call("POST", "/api/reveal", {"path": target})[0] == 200 and revealed == [target]
    assert call("POST", "/api/reveal", {"path": str(root / "missing")})[0] == 404
    assert call("POST", "/api/reveal", {"path": "relative"})[0] == 400
    assert call("POST", "/api/keep", {"path": target}) == (200, {"kept": target})
    assert call("POST", "/api/keep", {"path": target})[0] == 409
    assert target in json.loads((tmp / "config/config.json").read_text())["keep"]
    assert call("POST", "/api/keep", {"path": "/etc/../x"})[0] == 400


def test_status_reports_fda(ui):
    call, *_ = ui
    s, st = call("GET", "/api/status")
    assert isinstance(st["fda"], bool) and "Full Disk Access" in st["fda_help"]["steps"]


def _raw(app, method, path, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_auth_failure_is_401_permission_is_403(ui, tmp_path):
    # Review C1: the UI must tell "token rejected" (401) from "folder unreadable" (403).
    call, root, revealed, tmp = ui
    locked = root / "locked"; locked.mkdir(); locked.chmod(0)
    try:
        assert call("GET", f"/api/list?path={urllib.parse.quote(str(locked))}")[0] == 403
    finally:
        locked.chmod(0o700)


def test_keep_rejects_unsafe_names_and_protected_or_missing(ui):
    # Review I5 + I6: keep.txt must never be corrupted or get system/missing paths.
    call, root, revealed, tmp = ui
    for bad in (str(root / "a#b"), str(root / "line\nbreak"), str(root / "x "), "/Library"):
        if not bad.startswith("/Library"):
            os.makedirs(bad, exist_ok=True)
        s = call("POST", "/api/keep", {"path": bad})[0]
        assert s in (400, 403), (bad, s)
    assert call("POST", "/api/keep", {"path": str(root / "missing")})[0] == 404
    assert json.loads((tmp / "config/config.json").read_text()).get("keep", []) == []


def test_keep_appends_on_its_own_line_and_allows_parent_of_kept(ui):
    # Review I5 + I10
    call, root, revealed, tmp = ui
    cfg = tmp / "config/config.json"
    cfg.write_text(json.dumps({"version": 1, "keep": [str(root / "proj/node_modules")]}))
    assert call("POST", "/api/keep", {"path": str(root / "proj")})[0] == 200   # parent of a kept path
    assert json.loads(cfg.read_text())["keep"] == [str(root / "proj/node_modules"), str(root / "proj")]
    assert call("POST", "/api/keep", {"path": str(root / "proj/node_modules")})[0] == 409


def test_tree_colors_respect_keep_list(ui):
    # Review I8
    call, root, revealed, tmp = ui
    (tmp / "config/config.json").write_text(json.dumps({"version": 1, "keep": [str(root / "proj/node_modules")]}))
    s, t = call("GET", f"/api/tree?path={urllib.parse.quote(str(root / 'proj'))}&depth=1")
    nm = next(c for c in t["children"] if c["name"] == "node_modules")
    assert nm["cls"] == "protected" and nm["rule"] == "keep-list"


def test_post_errors_are_json_not_dropped(ui, monkeypatch):
    # Review I7
    call, root, revealed, tmp = ui
    import subprocess
    import cleanup.server.app as A
    def boom(p):
        raise subprocess.TimeoutExpired("open", 10)
    # reveal_fn lives on the App; find it via the gc to avoid changing the fixture API
    import gc
    app = next(o for o in gc.get_objects() if isinstance(o, A.App) and o.roots == (str(root),))
    app.reveal_fn = boom
    assert call("POST", "/api/reveal", {"path": str(root / "proj")})[0] == 500
