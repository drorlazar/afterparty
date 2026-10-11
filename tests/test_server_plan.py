import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest

from cleanup.scanner.scan import scan
from cleanup.scanner.store import Store
from cleanup.server.app import serve
from tests.test_executor import fake_trash
from tests.test_manifest import fake_classify, tree


@pytest.fixture
def srv(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1, "roots": [str(home / "disk")]}))
    t = tree(home / "disk")
    store = Store(str(tmp_path / "db.sqlite")); store.save(scan(str(home / "disk"), skip=()))
    server, app = serve(port=0, store=store, roots=(str(home / "disk"),), scan_skip=())
    app.trash_fn = fake_trash(tmp_path); app.classify_fn = fake_classify
    app.agent_settings_file = str(tmp_path / "config" / "agent.json")  # never the real home's
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{app.port}"

    def call(method, path, body=None, raw=False):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Cleanup-Key": app.token, "X-Cleanup-CSRF": app.token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = r.read(); return r.status, (d if raw else json.loads(d or b"null"))
        except urllib.error.HTTPError as e:
            return e.code, None
    yield call, t, app
    server.shutdown()


def test_plan_execute_undo_flow(srv):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "p/node_modules"), str(t / "doc.txt")]})
    assert s == 200 and m["totals"]["permanent"] > 0
    assert call("POST", f"/api/plan/{m['id']}/execute", {})[0] == 400           # permanent needs ack
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
    s, raw = call("GET", f"/api/plan/{m['id']}/events", raw=True)
    ev = [json.loads(l[6:]) for l in raw.decode().splitlines() if l.startswith("data: ")]
    assert ev[-1]["state"] == "done" and len(ev[-1]["result"]["deleted"]) == 1
    assert not (t / "doc.txt").exists()
    s, u = call("POST", f"/api/plan/{m['id']}/undo", {})
    assert s == 200 and len(u["restored"]) == 1 and (t / "doc.txt").exists()
    s, h = call("GET", "/api/history")
    assert h[0]["id"] == m["id"] and h[0]["executed"]


def test_plan_rejects_bad_ids_and_paths(srv):
    call, t, app = srv
    assert call("POST", "/api/plan", {"paths": ["relative"]})[0] == 400
    assert call("GET", "/api/plan/../../etc")[0] in (400, 404)
    assert call("POST", "/api/plan/zzz/execute", {"ack": True})[0] == 404


def test_scan_refused_while_executing(srv):
    # Review I8
    call, t, app = srv
    app.executing = True
    try:
        assert call("POST", "/api/scan", {})[0] == 409
    finally:
        app.executing = False


def test_forced_needs_typed_confirmation_and_activity_reports(srv):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    forced = {str(t / "doc.txt"): "permanent"}
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": forced})[0] == 400
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": forced, "confirm": "nope"})[0] == 400
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": forced, "confirm": "delete"})[0] == 202
    s, raw = call("GET", f"/api/plan/{m['id']}/events", raw=True)
    s, act = call("GET", "/api/activity")
    assert s == 200 and act["executing"] is False and act["last"]["plan"] == m["id"]
    assert not (t / "doc.txt").exists()


def test_stop_endpoint(srv):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert call("POST", f"/api/plan/{m['id']}/stop", {})[0] in (200, 409)


def test_saved_forced_plan_needs_confirm_again(srv):
    # Review I3 / Codex C1: a forced override from a rejected attempt is not saved — the next run, without
    # confirmation, executes only the plan as reviewed (no override, nothing permanent).
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    real, got = app.start_execute, []
    app.start_execute = lambda m_: False      # first attempt doesn't start (e.g. busy)
    forced = {str(t / "doc.txt"): "permanent"}
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": forced, "confirm": "delete"})[0] == 409
    app.start_execute = lambda m_: (got.append(m_), True)[1]
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
    assert not any(i.get("forced") or i["mode"] == "permanent" for i in got[0]["items"])
    app.start_execute = real


def test_bad_override_shapes_are_400(srv):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": "permanent", "confirm": "delete"})[0] == 400
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "overrides": []})[0] == 400
def test_stop_only_stops_its_own_plan(srv):
    call, t, app = srv
    app.activity = {"plan": "other", "done": 0, "total": 1, "path": "", "processed": [], "started": 0}
    app.executing = True
    try:
        assert call("POST", "/api/plan/abc/stop", {})[0] == 409
        assert not app.exec_stop.is_set()
    finally:
        app.executing = False


def test_rejected_execute_does_not_persist_overrides(srv):
    # Codex C1: a 409'd execute must not leave A forced for the next attempt.
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt"), str(t / "p/renders/v1")]})
    real, got = app.start_execute, []
    app.start_execute = lambda m_: False
    a, b = str(t / "doc.txt"), str(t / "p/renders/v1")
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": {a: "permanent"}, "confirm": "delete"})[0] == 409
    app.start_execute = lambda m_: (got.append(m_), True)[1]
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True, "forced": {b: "permanent"}, "confirm": "delete"})[0] == 202
    modes = {i["path"]: (i["mode"], bool(i.get("forced"))) for i in got[0]["items"]}
    assert modes[b] == ("trash", True) and modes[a][1] is False and modes[a][0] != "permanent"
    app.start_execute = real


def test_foreign_host_header_is_rejected(srv):
    # DNS-rebinding defence: only 127.0.0.1/localhost Host headers are served.
    import http.client
    call, t, app = srv
    c = http.client.HTTPConnection("127.0.0.1", app.port)
    c.request("GET", "/api/status", headers={"Host": "evil.example:80", "X-Cleanup-Key": app.token})
    assert c.getresponse().status == 403


def test_started_cleanup_is_reported_started_even_if_saving_fails(srv, monkeypatch):
    # Codex re-check: never answer 500 "couldn't start" for a cleanup that is already running.
    call, t, app = srv
    from cleanup.plan import store as plan_store
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    real_start = app.start_execute
    app.start_execute = lambda m_: True
    def boom(m_):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(plan_store, "save_manifest", boom)
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
    app.start_execute = real_start


@pytest.mark.parametrize("mode", ["malformed", "missing"])
def test_execute_refused_without_valid_config(srv, tmp_path, mode):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert s == 200
    cfg = tmp_path / "config/config.json"
    if mode == "malformed":
        cfg.write_text("{broken")
    else:
        cfg.unlink()
    s, body = call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})
    assert s == 409
    assert app.exec_error and ("invalid" in app.exec_error or "isn't set up yet" in app.exec_error)
    assert (t / "doc.txt").exists()


def test_execute_refused_by_live_guard(srv):
    call, t, app = srv
    app.guard_fn = lambda: "engine has uncommitted edits"
    assert app.start_execute({"id": "abc123", "items": []}) is False
    assert "uncommitted" in app.exec_error


def test_execute_refused_while_cli_runs(srv):
    from cleanup import config
    from cleanup.plan import bridge
    call, t, app = srv
    with bridge.lock(config.state_dir()):
        assert app.start_execute({"id": "abc124", "items": []}) is False
    assert "Another Afterparty cleanup is running" in app.exec_error


def test_execute_releases_lock_when_finished(srv):
    call, t, app = srv
    for i in range(2):
        s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
        assert s == 200
        assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
        deadline = time.time() + 20
        while app.executing and time.time() < deadline:
            time.sleep(0.05)
        assert not app.executing and app.exec_lock is None
        shutil.rmtree(t / "doc.txt", ignore_errors=True)
        (t / "doc.txt").write_text("x")
    assert "Another Afterparty" not in (app.exec_error or "")


def test_execute_releases_lock_on_error(srv):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert s == 200
    app.classify_fn = lambda *a, **k: 1 / 0
    assert app.start_execute(m)
    deadline = time.time() + 20
    while app.executing and time.time() < deadline:
        time.sleep(0.05)
    assert app.exec_lock is None
    app.classify_fn = fake_classify
    from cleanup import config
    from cleanup.plan import bridge
    with bridge.lock(config.state_dir()):
        pass


def test_read_keep_refuses_a_missing_config_for_deleting(srv, tmp_path):
    # Final review MUST-FIX 4: a config removed mid-run never turns into "no protections".
    from cleanup import config
    from cleanup.server import app as appmod
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    (tmp_path / "config/config.json").unlink()
    with pytest.raises(config.ConfigError):
        appmod.read_keep(strict=True)
    app.exec_events[m["id"]] = []
    app._run_execute(m)
    final = app.exec_events[m["id"]][-1]
    assert (t / "doc.txt").exists() and final["result"]["trashed"] == []
    assert final["result"]["skipped"][0]["reason"].startswith("settings changed")


def test_map_items_outside_roots_only_go_to_the_trash(srv, tmp_path):
    # Final review MUST-FIX 7: the map app may pick anything in home, but outside your project folders (and the
    # fixed off-root list) nothing is deleted for good — it goes to the Trash instead.
    call, t, app = srv
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1, "roots": [str(tmp_path / "home/elsewhere")]}))
    (tmp_path / "home/elsewhere").mkdir()
    s, m = call("POST", "/api/plan", {"paths": [str(t / "p/node_modules")]})
    assert s == 200 and m["items"][0]["mode"] == "trash" and m["totals"]["permanent"] == 0
    assert call("POST", f"/api/plan/{m['id']}/execute", {})[0] == 202  # nothing permanent: no ack needed
    call("GET", f"/api/plan/{m['id']}/events", raw=True)
    assert not (t / "p/node_modules").exists() and (tmp_path / ".Trash" / "node_modules").exists()


def test_map_execute_downgrades_even_a_saved_permanent_plan(srv, tmp_path):
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "p/node_modules")]})
    assert m["items"][0]["mode"] == "permanent"  # inside roots: normal rules
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1, "roots": []}))
    got = []
    app.start_execute = lambda m_: (got.append(m_), True)[1]
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
    assert got[0]["items"][0]["mode"] == "trash"


# ---------------- final review MUST-FIX 10: one journal home, undo serialized ----------------
def test_app_journals_live_in_the_afterparty_state_folder(srv, tmp_path):
    from cleanup.plan import store as plan_store
    call, t, app = srv
    assert plan_store.journal_path("abc123").startswith(str(tmp_path / "state" / "journal") + "/")
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert (tmp_path / "state" / "plans" / (m["id"] + ".json")).exists()


def test_cli_undo_finds_an_app_cleanup(srv, tmp_path):
    from cleanup import commands
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert call("POST", f"/api/plan/{m['id']}/execute", {})[0] == 202
    call("GET", f"/api/plan/{m['id']}/events", raw=True)
    assert not (t / "doc.txt").exists()
    assert commands.main(["undo"]) == 0 and (t / "doc.txt").read_text() == "hello"


def test_undo_waits_for_a_running_cleanup(srv, tmp_path):
    from cleanup import commands
    from cleanup.plan import bridge
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    assert call("POST", f"/api/plan/{m['id']}/execute", {})[0] == 202
    call("GET", f"/api/plan/{m['id']}/events", raw=True)
    with bridge.lock(str(tmp_path / "state")):
        assert call("POST", f"/api/plan/{m['id']}/undo", {})[0] == 409
        assert commands.main(["undo"]) == 3
    assert not (t / "doc.txt").exists()


def test_lock_released_when_the_worker_cannot_start(srv, tmp_path, monkeypatch):
    # Final review SHOULD-FIX: a thread that fails to start must not leave the run lock held.
    from cleanup.plan import bridge
    from cleanup.server import app as appmod
    call, t, app = srv
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})

    class NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(appmod.threading, "Thread", NoThread)
    assert app.start_execute(m) is False and app.executing is False and app.exec_error
    with bridge.lock(str(tmp_path / "state")):
        pass  # free again


def test_started_cleanup_is_reported_started_even_if_the_plan_save_conflicts(srv, monkeypatch):
    call, t, app = srv
    from cleanup import fsops
    from cleanup.plan import store as plan_store
    s, m = call("POST", "/api/plan", {"paths": [str(t / "doc.txt")]})
    real_start = app.start_execute
    app.start_execute = lambda m_: True
    def boom(m_):
        raise fsops.Conflict("changed")
    monkeypatch.setattr(plan_store, "save_manifest", boom)
    assert call("POST", f"/api/plan/{m['id']}/execute", {"ack": True})[0] == 202
    app.start_execute = real_start


def test_a_settings_conflict_is_a_409_not_a_500(srv, monkeypatch):
    call, t, app = srv
    from cleanup import fsops
    def boom(*a, **k):
        raise fsops.Conflict("changed")
    monkeypatch.setattr(fsops, "replace_owned", boom)
    assert "/.config/afterparty/" not in app.agent_settings_file      # a temp file, never the real home's
    assert call("POST", "/api/agent/settings", {"agent_model": "sonnet"})[0] == 409
