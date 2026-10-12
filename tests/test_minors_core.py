"""Deferred review minors (plan-b rulings, Tasks 7 and 7A): later merge of pending protections, /api/keep and
/api/plan error mapping, the launchd plist receipt, the scan store's corrupt-database paths."""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

import pytest

from cleanup import config, fsops
from tests.test_server_plan import srv  # noqa: F401  (fixture)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(d))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (d / "config.json").write_text(json.dumps({"version": 1, "roots": ["~/Work"]}))
    return d / "config.json"


def _stuck_protect(cfg, monkeypatch, path="~/Work/client"):
    """A Protect press whose merge failed: KeepPending, the pending file left in place."""
    real = config.update

    def failing(*a, **k):
        raise config.ConfigError("disk said no")
    monkeypatch.setattr(config, "update", failing)
    with pytest.raises(config.KeepPending):
        config.add_keep(path)
    monkeypatch.setattr(config, "update", real)
    assert len(os.listdir(config.pending_dir())) == 1
    assert path not in json.loads(cfg.read_text()).get("keep", [])


# ---- Task 7: a Protect that failed to merge is merged by the next settings write
def test_the_next_settings_write_merges_a_stuck_protection_then_withdraws_it(cfg, monkeypatch):
    _stuck_protect(cfg, monkeypatch)
    out = config.update(lambda c: c.update(autopilot="clean"))
    saved = json.loads(cfg.read_text())
    assert saved["keep"] == ["~/Work/client"] and saved["autopilot"] == "clean" and out == config.validate(saved)
    assert os.listdir(config.pending_dir()) == []
    assert config.load()[0]["keep"] == ["~/Work/client"]


def test_a_no_op_write_still_merges_a_stuck_protection(cfg, monkeypatch):
    _stuck_protect(cfg, monkeypatch)
    config.update(lambda c: None)
    assert json.loads(cfg.read_text())["keep"] == ["~/Work/client"] and os.listdir(config.pending_dir()) == []


def test_a_mutate_cant_drop_a_pending_protection(cfg, monkeypatch):
    _stuck_protect(cfg, monkeypatch)
    config.update(lambda c: c.update(keep=[]))  # pending entries are folded in after mutate
    assert json.loads(cfg.read_text())["keep"] == ["~/Work/client"]


def test_a_stuck_protection_stays_when_the_write_isnt_durable(cfg, monkeypatch):
    _stuck_protect(cfg, monkeypatch)

    def no_fsync(folder):
        raise OSError("fsync failed")
    monkeypatch.setattr(config, "_fsync_dir", no_fsync)
    config.update(lambda c: None)
    assert json.loads(cfg.read_text())["keep"] == ["~/Work/client"]
    assert len(os.listdir(config.pending_dir())) == 1  # R13: withdrawn only after the folder fsync


def test_a_failed_write_leaves_the_stuck_protection_in_place(cfg, monkeypatch):
    _stuck_protect(cfg, monkeypatch)
    cfg.write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.update(lambda c: None)
    assert len(os.listdir(config.pending_dir())) == 1


def test_a_malformed_pending_file_is_never_withdrawn_and_still_refuses_deleting(cfg):
    d = config.pending_dir()
    os.makedirs(d)
    with open(os.path.join(d, "junk.json"), "w") as f:
        f.write("{}")
    config.update(lambda c: c.update(autopilot="clean"))  # other settings writes still work
    assert os.listdir(d) == ["junk.json"]
    with pytest.raises(config.ConfigError):
        config.current_keep()


# ---- Task 7: /api/keep maps settings errors to a clear 409, never a generic 500
def _post(app, route, body):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{route}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Cleanup-Key": app.token, "X-Cleanup-CSRF": app.token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_keep_with_malformed_settings_still_protects_and_says_so(srv, tmp_path):  # noqa: F811
    call, t, app = srv
    (tmp_path / "config/config.json").write_text("{not json")
    s, body = _post(app, "/api/keep", {"path": str(t / "doc.txt")})
    assert s == 409 and body["pending"] is True and "settings not updated" in body["error"]
    names = os.listdir(tmp_path / "state" / "keep-pending")
    assert len(names) == 1 and json.loads((tmp_path / "state/keep-pending" / names[0]).read_text()) == \
        {"keep": str(t / "doc.txt")}


def test_keep_refused_by_the_settings_is_a_409_with_the_reason(srv, monkeypatch):  # noqa: F811
    call, t, app = srv

    def refuse(path, cfg_path=None):
        raise config.ConfigError("a blank path can't be protected")
    monkeypatch.setattr(config, "add_keep", refuse)
    s, body = _post(app, "/api/keep", {"path": str(t / "doc.txt")})
    assert s == 409 and "blank path" in body["error"] and "nothing changed" in body["error"]


# ---- Task 7A: /api/plan's Conflict branch
def test_plan_whose_file_changed_while_saved_is_a_409(srv, monkeypatch):  # noqa: F811
    from cleanup.server import app as server_app
    call, t, app = srv

    def conflict(m):
        raise fsops.Conflict("someone else wrote it")
    monkeypatch.setattr(server_app.plan_store, "save_manifest", conflict)
    s, body = _post(app, "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert s == 409 and "nothing was replaced" in body["error"] and "someone else wrote it" in body["error"]


# ---- Task 7A: the launchd plist is private and receipted
def test_start_launchd_writes_a_private_receipted_plist(tmp_path, monkeypatch):
    import types
    from cleanup import launcher
    monkeypatch.setattr(launcher, "_bootout", lambda label: (True, "stopped"))
    monkeypatch.setattr(launcher.jobs, "run", lambda *a: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    launcher._start_launchd(sys.executable, 50999, "tok", str(tmp_path), "com.example.test")
    plist = tmp_path / "com.example.test.plist"
    assert plist.stat().st_mode & 0o777 == 0o600
    assert fsops.receipts(str(tmp_path))["com.example.test.plist"] == fsops.file_sum(str(plist))


# ---- Task 7A: scan store, corrupt database
def test_corrupt_db_that_cant_be_moved_aside_is_reported_and_left_alone(tmp_path, monkeypatch):
    from cleanup.scanner import store
    db = tmp_path / "db.sqlite"
    db.write_bytes(b"this is not a database" * 100)
    monkeypatch.setattr(store.fsops, "rename_exclusive", lambda a, b: "conflict")
    with pytest.raises(sqlite3.DatabaseError):
        store.Store(str(db))
    assert db.read_bytes() == b"this is not a database" * 100
    assert [p.name for p in tmp_path.iterdir()] == ["db.sqlite"]


def test_the_connection_to_a_corrupt_db_is_closed_before_it_is_moved_aside(tmp_path, monkeypatch):
    from cleanup.scanner import store
    db = tmp_path / "db.sqlite"
    db.write_bytes(b"this is not a database" * 100)
    opened, real = [], sqlite3.connect
    monkeypatch.setattr(store.sqlite3, "connect", lambda *a, **k: opened.append(real(*a, **k)) or opened[-1])
    moved = []
    real_move = store.fsops.rename_exclusive

    def move(a, b):
        with pytest.raises(sqlite3.ProgrammingError):  # already closed when we move the file
            opened[0].execute("SELECT 1")
        moved.append(b)
        return real_move(a, b)
    monkeypatch.setattr(store.fsops, "rename_exclusive", move)
    s = store.Store(str(db))
    assert len(opened) == 2 and moved and s.latest() is None


def test_a_stale_server_is_stopped_only_when_its_port_matches_the_record(monkeypatch):
    import subprocess
    from cleanup import launcher
    killed = []
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: killed.append(pid))
    cmd = "/usr/bin/python3 -B -m cleanup.cli serve --live --port 50123\n"
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=cmd, stderr=""))
    launcher._stop_stale({"pid": 4242, "port": 50999})  # someone else's server on another port
    launcher._stop_stale({"pid": 4242})  # no recorded port: can't prove it's ours
    launcher._stop_stale({"pid": 4242, "port": 5012})  # a prefix of the real port isn't a match
    assert killed == []
    launcher._stop_stale({"pid": 4242, "port": 50123})
    assert killed == [4242]
