import json
import os
import signal
import sys

import pytest

from cleanup import launcher


def test_start_then_reuse(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    state = tmp_path / "server.json"
    url1 = launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
    s1 = json.loads(state.read_text())
    try:
        assert url1.startswith(f"http://127.0.0.1:{s1['port']}/?key=")
        url2 = launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
        s2 = json.loads(state.read_text())
        assert url2 == url1 and s2["pid"] == s1["pid"]
    finally:
        os.kill(s1["pid"], signal.SIGTERM)


def test_restarts_when_recorded_server_is_dead(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"pid": 999999, "port": 1, "token": "dead"}))
    url = launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
    s = json.loads(state.read_text())
    try:
        assert s["pid"] != 999999 and s["token"] != "dead" and str(s["port"]) in url
    finally:
        os.kill(s["pid"], signal.SIGTERM)


def test_server_python_prefers_project_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "cfg"))  # never the real installed interpreter
    # The scanner is ~2.3x faster on the venv's Python 3.11 than on /usr/bin/python3 3.9.
    fake = tmp_path / ".venv/bin/python"; fake.parent.mkdir(parents=True); fake.write_text("#!/bin/sh\n"); fake.chmod(0o755)
    assert launcher.server_python(repo=str(tmp_path)) == str(fake)
    assert launcher.server_python(repo=str(tmp_path / "none")) == sys.executable


def _cmdline(pid):
    import subprocess
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout


def test_token_not_in_process_args_and_state_is_private(tmp_path, env, monkeypatch):
    # Review I6: every local user can read `ps`; the token must not be in argv.
    import stat as st
    monkeypatch.setenv("HOME", env["HOME"])
    state = tmp_path / "server.json"
    launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
    s = json.loads(state.read_text())
    try:
        assert s["token"] not in _cmdline(s["pid"])
        assert st.S_IMODE(os.stat(state).st_mode) == 0o600
    finally:
        os.kill(s["pid"], signal.SIGTERM)


def test_stale_recorded_server_is_stopped_before_respawn(tmp_path, env, monkeypatch):
    # Review I7: an old server whose token no longer matches must not be left running.
    import subprocess
    monkeypatch.setenv("HOME", env["HOME"])
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "cleanup.cli", "serve"])
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"pid": old.pid, "port": 1, "token": "stale"}))
    launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
    s = json.loads(state.read_text())
    try:
        assert old.wait(timeout=5) is not None
    finally:
        os.kill(s["pid"], signal.SIGTERM)
        if old.poll() is None:
            old.kill()


def test_launchd_bootstrap_retries_then_reports_clearly(tmp_path, monkeypatch):
    # Review I9: bootstrap right after bootout can fail transiently; a persistent failure must be a clear error.
    import subprocess
    import types
    import pytest as _pytest
    calls = {"boot": 0}

    def fake_run(cmd, **kw):
        if cmd[1:2] == ["bootstrap"]:
            calls["boot"] += 1
            ok = calls["boot"] >= 2 and not fake_run.always_fail
            return types.SimpleNamespace(returncode=0 if ok else 5, stdout="", stderr="" if ok else "Bootstrap failed: 5: Input/output error")
        if cmd[1:2] == ["print"]:
            return types.SimpleNamespace(returncode=113, stdout="", stderr="not found")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    fake_run.always_fail = False
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    launcher._start_launchd(sys.executable, 50999, "tok", str(tmp_path), "com.example.test")
    assert calls["boot"] == 2
    fake_run.always_fail = True; calls["boot"] = 0
    with _pytest.raises(RuntimeError, match="Input/output error"):
        launcher._start_launchd(sys.executable, 50999, "tok", str(tmp_path), "com.example.test")


# ---- stop_server: the state file never chooses what is stopped
def _stops(monkeypatch):
    seen = {"bootout": [], "kill": []}
    monkeypatch.setattr(launcher, "_bootout", lambda label: seen["bootout"].append(label) or (True, "stopped"))
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: seen["kill"].append(pid))
    return seen


@pytest.mark.parametrize("label", ["com.example.other", 7, None, ["x"]])
def test_stop_server_never_boots_out_a_job_the_state_file_names_but_isnt_ours(tmp_path, monkeypatch, label):
    seen = _stops(monkeypatch)
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "launchd", "label": label}))
    assert launcher.stop_server(state_path=str(state)) == (True, "")
    assert seen["bootout"] == []


def test_stop_server_boots_out_our_own_job(tmp_path, monkeypatch):
    seen = _stops(monkeypatch)
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "launchd", "label": launcher.SERVER_LABEL}))
    assert launcher.stop_server(state_path=str(state)) == (True, "stopped")
    assert seen["bootout"] == [launcher.SERVER_LABEL]


@pytest.mark.parametrize("pid", [0, -1, 1, "123", True])
def test_stop_server_never_signals_a_process_group_or_a_bogus_pid(tmp_path, monkeypatch, pid):
    seen = _stops(monkeypatch)
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "spawn", "pid": pid}))
    assert launcher.stop_server(state_path=str(state)) == (True, "")
    assert seen["kill"] == []


def test_stop_server_signals_only_a_process_that_is_our_server(tmp_path, monkeypatch):
    seen = _stops(monkeypatch)
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "spawn", "pid": os.getpid()}))   # alive, but pytest, not our server
    assert launcher.stop_server(state_path=str(state)) == (True, "")
    assert seen["kill"] == []
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: launcher.subprocess.CompletedProcess(
        a, 0, "/usr/bin/python3 -B -m cleanup.cli serve --live --port 1\n", ""))
    assert launcher.stop_server(state_path=str(state)) == (True, "")
    assert seen["kill"] == [os.getpid()]


def test_a_failed_start_reports_why_with_this_starts_server_log(tmp_path, env, monkeypatch):
    # CI only shows the exception: it must carry the exit status and what this start wrote to server.log
    monkeypatch.setenv("HOME", env["HOME"])
    state = tmp_path / "server.json"
    (tmp_path / "server.log").write_text("an older run's line\n")
    bad = tmp_path / "python"
    bad.write_text("#!/bin/sh\necho 'Traceback: ImportError: no module named boom' >&2\nexit 3\n")
    bad.chmod(0o755)
    with pytest.raises(RuntimeError) as e:
        launcher.ensure_server(open_browser=False, state_path=str(state), python=str(bad), wait=10)
    msg = str(e.value)
    assert "exited with status 3" in msg and "no module named boom" in msg and "older run" not in msg
    assert str(tmp_path / "server.log") in msg and not state.exists()
