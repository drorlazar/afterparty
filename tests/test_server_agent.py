import json
import threading
import urllib.error
import urllib.request

import pytest

from cleanup.agent.cache import Cache
from cleanup.scanner.scan import scan
from cleanup.scanner.store import Store
from cleanup.server.app import serve
from tests.test_agent import Done, good


@pytest.fixture
def ag_off(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / "disk/proj/build").mkdir(parents=True); monkeypatch.setenv("HOME", str(home))
    (home / "disk/proj/build/a.o").write_bytes(b"x" * 1000)
    store = Store(str(tmp_path / "db.sqlite")); store.save(scan(str(home / "disk"), skip=()))
    server, app = serve(port=0, store=store, roots=(str(home / "disk"),), scan_skip=())
    app.agent_cache = Cache(str(tmp_path / "agentcache"))
    calls = []

    def run(cmd, **kw):
        calls.append(kw["cwd"])
        return Done(json.dumps({"is_error": False, "total_cost_usd": 0.02, "structured_output": good(
            proposed=[{"path": str(home / "disk/proj/build"), "action": "delete", "reason": "build output"}])}))
    app.agent_run_fn = run
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-Cleanup-Key": app.token, "X-Cleanup-CSRF": app.token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield call, home / "disk", app, calls
    server.shutdown()


@pytest.fixture
def ag(ag_off):
    """The same server with Ask Claude already turned on (the user's consent given)."""
    assert ag_off[0]("/api/agent/enable", {"consent": True})[0] == 200
    return ag_off


def test_preview_shows_what_will_be_sent(ag):
    call, disk, app, calls = ag
    s, d = call("/api/agent/preview", {"path": str(disk / "proj")})
    assert s == 200 and d["context"]["names"] == ["build"] and d["prompt_chars"] > 100 and calls == []


def test_ask_answers_then_serves_from_cache(ag):
    call, disk, app, calls = ag
    s, d = call("/api/agent/ask", {"path": str(disk / "proj")})
    assert s == 200 and d["ok"] and d["cached"] is False and d["asked"] == str(disk / "proj")
    assert d["answer"]["proposed"][0]["path"] == str(disk / "proj/build") and calls == [str(disk / "proj")]
    s, d = call("/api/agent/ask", {"path": str(disk / "proj")})
    assert d["cached"] is True and len(calls) == 1
    s, d = call("/api/agent/ask", {"path": str(disk / "proj"), "refresh": True})
    assert d["cached"] is False and len(calls) == 2


def test_ask_errors_are_cards_not_cached(ag):
    call, disk, app, calls = ag
    app.agent_run_fn = lambda cmd, **kw: Done("garbage")
    s, d = call("/api/agent/ask", {"path": str(disk / "proj")})
    assert s == 200 and d["ok"] is False and "unreadable" in d["error"]
    assert app.agent_cache.get(str(disk / "proj"), None) is None


def test_ask_rejects_bad_paths_and_limits_concurrency(ag):
    call, disk, app, calls = ag
    assert call("/api/agent/ask", {"path": "relative/x"})[0] == 400
    assert call("/api/agent/ask", {"path": str(disk / "nope")})[0] == 404
    app.agent_slots = threading.BoundedSemaphore(1); app.agent_slots.acquire()
    assert call("/api/agent/ask", {"path": str(disk / "proj")})[0] == 429
import time as _time


def get(app, path):
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", headers={"X-Cleanup-Key": app.token})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_mole_routes(ag, tmp_path):
    call, disk, app, calls = ag
    app.mole_state = str(tmp_path / "mole.json")
    app.mole_available = lambda: True
    started = threading.Event()

    def fake_refresh(state, keep=(), **kw):
        started.set(); _time.sleep(0.3)
        res = {"finished": 1.0, "items": [{"path": str(disk / "proj/build"), "size": 1, "category": "Project artifacts",
                                           "source": "purge", "rules": {"class": "unknown"}, "agree": True, "stageable": True}],
               "errors": []}
        json.dump(res, open(state, "w")); return res
    app.mole_refresh_fn = fake_refresh
    assert get(app, "/api/mole")["finished"] is None
    assert call("/api/mole/refresh", {})[0] == 202
    started.wait(2)
    assert call("/api/mole/refresh", {})[0] == 409 and get(app, "/api/mole")["running"] is True
    _time.sleep(0.6)
    m = get(app, "/api/mole")
    assert m["running"] is False and m["items"][0]["path"].endswith("build") and not m["items"][0].get("gone")
    app.mole_available = lambda: False
    assert call("/api/mole/refresh", {})[0] == 404
def test_history_must_be_a_list(ag):
    call, disk, app, calls = ag
    s, d = call("/api/agent/ask", {"path": str(disk / "proj"), "history": 5})
    assert s == 200 and d["ok"]


def test_agent_status_and_settings_routes(ag, tmp_path):
    call, disk, app, calls = ag
    app.agent_settings_file = str(tmp_path / "settings.json")
    app.agent_status_fn = lambda: {"installed": True, "path": "/x/claude", "version": "2.1", "signed_in": False,
                                   "method": None, "problem": "Sign in: run claude, then /login."}
    st = get(app, "/api/agent/status")
    assert st["installed"] and st["signed_in"] is False and st["model"] == "sonnet"
    assert call("/api/agent/settings", {"agent_model": "opus"})[0] == 200 and get(app, "/api/agent/status")["model"] == "opus"
    assert call("/api/agent/settings", {"agent_model": "nope"})[0] == 400


def test_ask_when_claude_missing_is_a_setup_card(ag):
    call, disk, app, calls = ag
    app.agent_claude_fn = lambda: None
    s, d = call("/api/agent/ask", {"path": str(disk / "proj"), "refresh": True})
    assert s == 200 and d["ok"] is False and d.get("setup") is True and calls == []


def test_ask_without_question_uses_the_default_question(ag, tmp_path):
    call, disk, app, calls = ag
    app.agent_settings_file = str(tmp_path / "s.json")
    from cleanup.agent import setup as S
    S.save_settings(app.agent_settings_file, {"agent_default_question": "Which parts can go?"})
    seen = []
    app.agent_run_fn = lambda cmd, **kw: (seen.append(cmd[-1]), Done(json.dumps({"is_error": False, "structured_output": good()})))[1]
    s, d = call("/api/agent/ask", {"path": str(disk / "proj"), "refresh": True})
    assert d["ok"] and "Question: Which parts can go?" in seen[0] and d["question"] == "Which parts can go?"


def test_agent_off_until_consent(ag_off):
    call, disk, app, calls = ag_off
    for route in ("/api/agent/preview", "/api/agent/ask"):
        s, d = call(route, {"path": str(disk / "proj")})
        assert s == 403 and d["error"] == "agent-off"
    assert get(app, "/api/agent/status")["enabled"] is False
    assert call("/api/agent/enable", {"consent": False})[0] == 400
    assert call("/api/agent/enable", {})[0] == 400
    assert call("/api/agent/preview", {"path": str(disk / "proj")})[0] == 403
    s, d = call("/api/agent/enable", {"consent": True, "read_files": False})
    assert s == 200 and d == {"enabled": True, "read_files": False}
    assert call("/api/agent/preview", {"path": str(disk / "proj")})[0] == 200
    assert calls == []


def test_ask_is_names_only_unless_read_files_allowed(ag_off):
    call, disk, app, calls = ag_off
    cmds = []
    run = app.agent_run_fn
    app.agent_run_fn = lambda cmd, **kw: (cmds.append(cmd), run(cmd, **kw))[1]
    call("/api/agent/enable", {"consent": True})
    assert call("/api/agent/ask", {"path": str(disk / "proj"), "refresh": True})[0] == 200
    assert "Read" not in cmds[-1][cmds[-1].index("--tools") + 1].split(",")
    assert call("/api/agent/enable", {"consent": True, "read_files": True})[1]["read_files"] is True
    assert call("/api/agent/ask", {"path": str(disk / "proj"), "refresh": True})[0] == 200
    assert "Read" in cmds[-1][cmds[-1].index("--tools") + 1].split(",")
