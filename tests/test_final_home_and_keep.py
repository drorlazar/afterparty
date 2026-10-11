"""Final review fixes I1 (no test writes into the real home) and M2 (a Protect that can't merge). Every test runs in a temp home with stubs."""
import json

import pytest

from tests.conftest import home_fingerprint

AP = "io.github.drorlazar.afterparty"


# ---- I1: no test (and no import) writes into the real home folder
def test_agent_settings_follow_the_config_folder_at_call_time(tmp_path, monkeypatch):
    from cleanup.agent import setup
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "c"))
    assert setup.settings_file() == str(tmp_path / "c" / "agent.json")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "agent.json").write_text(json.dumps({"agent_model": "opus"}))
    assert setup.load_settings()["agent_model"] == "opus"
    setup.save_settings(None, {"agent_model": "haiku"})
    assert json.loads((tmp_path / "c" / "agent.json").read_text())["agent_model"] == "haiku"


def test_the_map_server_keeps_agent_settings_in_the_config_folder_it_starts_with(tmp_path, monkeypatch):
    from cleanup.scanner.store import Store
    from cleanup.server.app import serve
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "s"))
    server, app = serve(port=0, store=Store(str(tmp_path / "db.sqlite")), roots=(str(tmp_path),), scan_skip=())
    try:
        assert app.agent_settings_file == str(tmp_path / "c" / "agent.json")
    finally:
        server.server_close()


def test_the_real_home_guard_notices_afterparty_files_appearing(tmp_path):
    h = tmp_path / "h"
    (h / ".claude").mkdir(parents=True)
    (h / ".claude/settings.json").write_text('{"hooks": {}}\n')
    base = home_fingerprint(h)
    assert home_fingerprint(h) == base
    for change in (lambda: (h / ".config/afterparty").mkdir(parents=True),
                   lambda: (h / ".config/afterparty/agent.json").write_text("{}"),
                   lambda: (h / ".local/state/afterparty").mkdir(parents=True),
                   lambda: (h / "Library/LaunchAgents").mkdir(parents=True),
                   lambda: (h / "Library/LaunchAgents" / (AP + ".weekly.plist")).write_text("x"),
                   lambda: (h / ".claude/settings.json").write_text('{"x": "~/.local/bin/afterparty-session-end"}\n')):
        change()
        now = home_fingerprint(h)
        assert now != base
        base = now
    (h / "Library/LaunchAgents/com.example.other.plist").write_text("not ours")
    assert home_fingerprint(h) == base                       # someone else's job isn't Afterparty's business


def test_the_home_guard_ignores_a_real_installs_runtime_files_but_nothing_else(tmp_path):
    """An installed Afterparty's own jobs and server keep writing logs, the session ledger, server state, the scan
    database, the agent cache and the run lock while the suite runs; those never trip the guard. Settings, the install
    record, a pending protection or anything new still does. (A synthetic home, never the real one.)"""
    h = tmp_path / "h"
    st = h / ".local/state/afterparty"
    (st / "logs").mkdir(parents=True)
    (h / ".config/afterparty").mkdir(parents=True)
    (h / ".config/afterparty/config.json").write_text("{}")
    (st / "install.json").write_text("{}")
    base = home_fingerprint(h)
    for change in (lambda: (st / "logs/weekly.log").write_text("ran"),
                   lambda: (st / "logs/weekly.log").write_text("ran again, longer"),
                   lambda: (st / "sessions.jsonl").write_text("{}\n"),
                   lambda: (st / "server.json").write_text("{}"),
                   lambda: (st / "server.log").write_text("up"),
                   lambda: (st / (AP + ".server.plist")).write_text("x"),
                   lambda: (st / ".afterparty-receipts.json").write_text("{}"),
                   lambda: (st / "scan.db").write_bytes(b"db"),
                   lambda: (st / "scan.db-wal").write_bytes(b"wal"),
                   lambda: (st / "agent/ab").mkdir(parents=True),
                   lambda: (st / "agent/ab/answer.json").write_text("{}"),
                   lambda: (st / "thumbs").mkdir(),
                   lambda: (st / "mole.json").write_text("{}"),
                   lambda: (st / "run.lock").write_text("")):
        change()
        assert home_fingerprint(h) == base, change
    for change in (lambda: (h / ".config/afterparty/config.json").write_text('{"roots": []}'),
                   lambda: (st / "install.json").write_text('{"items": []}'),
                   lambda: (st / "keep-pending").write_text("/x"),
                   lambda: (st / "journal").mkdir(),
                   lambda: (st / "logs.txt").write_text("not the logs folder"),
                   lambda: (st / "agent.json").write_text("not the agent cache")):
        change()
        now = home_fingerprint(h)
        assert now != base, change
        base = now


def test_the_home_guard_tolerates_files_vanishing_mid_walk(tmp_path, monkeypatch):
    import tests.conftest as conftest
    st = tmp_path / "h/.local/state/afterparty"
    st.mkdir(parents=True)
    (st / "gone").write_text("x")
    real = conftest.os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith("gone"):
            raise FileNotFoundError(p)
        return real(p, *a, **k)
    monkeypatch.setattr(conftest.os, "lstat", lstat)
    fp = home_fingerprint(tmp_path / "h")
    assert not any("gone" in str(e[0]) for e in fp)


# ---- M2: a Protect press that couldn't update the settings says it is still protecting
@pytest.fixture
def keep_srv(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from cleanup.scanner.store import Store
    from cleanup.server.app import serve
    home = tmp_path / "home"
    (home / "disk" / "thing").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1, "roots": [str(home / "disk")]}))
    server, app = serve(port=0, store=Store(str(tmp_path / "db.sqlite")), roots=(str(home / "disk"),), scan_skip=())
    app.agent_settings_file = str(tmp_path / "agent.json")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(body):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/keep", data=json.dumps(body).encode(),
                                     method="POST", headers={"X-Cleanup-Key": app.token, "X-Cleanup-CSRF": app.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
    yield call, home
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("error", ["config", "os", "conflict"])
def test_a_protect_that_cant_merge_is_a_409_and_stays_pending(keep_srv, monkeypatch, error):
    from cleanup import config, fsops
    call, home = keep_srv
    exc = {"config": config.ConfigError("bad"), "os": OSError(28, "No space left on device"),
           "conflict": fsops.Conflict("changed")}[error]

    def boom(mutate, path=None, **k):
        raise exc
    monkeypatch.setattr(config, "update", boom)
    status, body = call({"path": str(home / "disk" / "thing")})
    assert status == 409 and body.get("pending") is True, body
    assert "protected (pending)" in body["error"] and "settings not updated" in body["error"]
    assert str(home / "disk" / "thing") in config.current_keep()        # the pending entry keeps protecting
