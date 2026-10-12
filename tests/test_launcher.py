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
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "cleanup.cli", "serve", "--port", "1"])
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
    state.write_text(json.dumps({"mode": "spawn", "pid": os.getpid(), "port": 1}))   # alive, but pytest, not our server
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


# --- start diagnostics (CI: a python.org 3.12 server stayed alive, silent and unanswered for 15s) ------------------

def test_spawned_server_logs_how_far_it_got_without_the_token(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    state = tmp_path / "server.json"
    launcher.ensure_server(open_browser=False, state_path=str(state), python=sys.executable)
    s = json.loads(state.read_text())
    try:
        log = (tmp_path / "server.log").read_text()
        assert "afterparty server: starting (pid %d" % s["pid"] in log
        assert "afterparty server: listening on 127.0.0.1:%d" % s["port"] in log
        assert s["token"] not in log
    finally:
        os.kill(s["pid"], signal.SIGTERM)


def test_spawn_runs_the_interpreter_exactly_as_given_and_unbuffered(tmp_path, env, monkeypatch):
    # spawn mode never swaps in fda.real_executable's inner Python.app binary (that is for launchd jobs only)
    monkeypatch.setenv("HOME", env["HOME"])
    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw["cwd"]
        raise Stop

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    from cleanup import fda
    monkeypatch.setattr(fda, "real_executable", lambda p: pytest.fail("spawn mode must not resolve the interpreter"))
    given = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    with pytest.raises(Stop):
        launcher.ensure_server(open_browser=False, state_path=str(tmp_path / "server.json"), python=given)
    assert seen["argv"][0] == given and seen["argv"][1:5] == ["-B", "-m", "cleanup.cli", "serve"]
    assert seen["env"]["PYTHONUNBUFFERED"] == "1" and seen["env"]["CLEANUP_TOKEN"] not in " ".join(seen["argv"])
    assert seen["cwd"] == launcher.REPO


def _fake_python(tmp_path, body):
    """An 'interpreter' that records its pid and then runs `body` (sh), ignoring the server arguments."""
    py = tmp_path / "fakepy"
    py.write_text("#!/bin/sh\nfor a; do last=$a; done\necho $$ > %s\n%s\n" % (tmp_path / "fake.pid", body))
    py.chmod(0o755)
    return str(py)


def _kill_fake(tmp_path):
    try:
        os.kill(int((tmp_path / "fake.pid").read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_start_timeout_says_nothing_listens_and_samples_the_hung_process(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    py = _fake_python(tmp_path, "exec /bin/sleep 30")
    try:
        with pytest.raises(RuntimeError) as e:
            launcher.ensure_server(open_browser=False, state_path=str(tmp_path / "server.json"), python=py, wait=1)
        msg = str(e.value)
        assert "didn't answer within 1s" in msg and "isn't listening" in msg and "last check: " in msg
        assert "(it wrote nothing)" in msg
        if os.access(launcher.SAMPLE, os.X_OK):
            assert "Where the server process (pid %s) is (sample):" % (tmp_path / "fake.pid").read_text().strip() in msg
            assert "Call graph:" in msg
    finally:
        _kill_fake(tmp_path)


def test_start_timeout_tells_a_listening_but_silent_server_apart(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setattr(launcher, "STATUS_TIMEOUT", 0.3)
    monkeypatch.setattr(launcher, "_sample", lambda pid: "Call graph:\n    stuck here")
    listen = ("import socket,sys,time; s=socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(128); "
              "print('fake: listening', flush=True); time.sleep(30)")
    py = _fake_python(tmp_path, 'exec "%s" -c "%s" "$last"' % (sys.executable, listen))
    try:
        with pytest.raises(RuntimeError) as e:
            launcher.ensure_server(open_browser=False, state_path=str(tmp_path / "server.json"), python=py, wait=2)
        msg = str(e.value)
        assert "accepts connections" in msg and "fake: listening" in msg and "stuck here" in msg
    finally:
        _kill_fake(tmp_path)


def test_sample_quotes_the_call_graph_and_never_fails(tmp_path, monkeypatch):
    fake = tmp_path / "sample"
    fake.write_text("#!/bin/sh\necho 'Analysis of sampling'\necho 'Process: x'\necho 'Call graph:'\n"
                    "i=0; while [ $i -lt 100 ]; do echo \"  frame $i\"; i=$((i+1)); done\n")
    fake.chmod(0o755)
    monkeypatch.setattr(launcher, "SAMPLE", str(fake))
    out = launcher._sample(os.getpid()).splitlines()
    assert out[0] == "Call graph:" and len(out) == launcher.SAMPLE_LINES and "Process: x" not in out
    monkeypatch.setattr(launcher, "SAMPLE", str(tmp_path / "missing"))
    assert launcher._sample(os.getpid()) == ""


def test_server_plist_runs_unbuffered():
    from cleanup.installer import jobs
    assert "<key>PYTHONUNBUFFERED</key><string>1</string>" in jobs.template("server")
