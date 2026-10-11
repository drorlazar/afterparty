"""Final review fixes I4 (no job restart during a cleanup), M3 (tab indentation), M4 (unreadable folders in --purge), M5 (ensure_server's label). Every test runs in a temp home with stubs."""
import fcntl
import json
import os
import subprocess
import sys

import pytest

from tests.conftest import names, snapshot

AP = "io.github.drorlazar.afterparty"


# ---- I4: install / update / uninstall never stop a job while a cleanup runs
def _run_lock(installer):
    d = installer.home / ".local/state/afterparty"
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / "run.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _bootouts(installer):
    return [l for l in installer.log.read_text().splitlines() if l.startswith("bootout")] if installer.log.exists() else []


def test_a_reinstall_leaves_running_unchanged_jobs_alone(installer):
    assert installer.run("--yes").returncode == 0
    installer.log.write_text("")
    r = installer.run("--yes", "--reuse-answers")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _bootouts(installer) == []                        # a "same" job that is loaded keeps running
    assert {p.name for p in installer.launchd.iterdir()} >= {AP + ".weekly", AP + ".spacecheck"}


def test_a_reinstall_starts_an_unchanged_job_that_isnt_loaded(installer):
    assert installer.run("--yes").returncode == 0
    (installer.launchd / (AP + ".weekly")).unlink()          # launchd forgot it (e.g. booted out by hand)
    assert installer.run("--yes", "--reuse-answers").returncode == 0
    assert (installer.launchd / (AP + ".weekly")).exists()


def test_install_refuses_to_restart_jobs_while_a_cleanup_runs(installer):
    assert installer.run("--yes").returncode == 0
    before = snapshot(installer.home)
    installer.log.write_text("")
    fd = _run_lock(installer)
    try:
        r = installer.run("--yes", "--reuse-answers")
    finally:
        os.close(fd)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "cleanup is running" in r.stderr and _bootouts(installer) == []
    assert names(snapshot(installer.home)) == names(before)


def test_a_job_restart_holds_the_cleanup_run_lock(tmp_path, monkeypatch):
    from cleanup.installer import jobs, main as inst
    from cleanup.plan import bridge
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    started = []
    monkeypatch.setattr(jobs, "start", lambda lbl, path: started.append(lbl) or (True, "loaded"))
    monkeypatch.setattr(jobs, "is_loaded", lambda lbl: True)
    do = inst._job_do(AP + ".weekly", str(tmp_path / "w.plist"), "x", None, "update")
    monkeypatch.setattr(inst.owned, "put", lambda *a, **k: None)
    with bridge.lock(str(tmp_path / "state")):
        with pytest.raises(bridge.Busy):
            do(lambda **f: None)
    assert started == []
    do(lambda **f: None)
    assert started == [AP + ".weekly"]


def test_a_job_written_but_not_restarted_is_restarted_on_the_next_run(installer, monkeypatch):
    """A run that wrote a job's new plist but stopped before restarting it (bridge.Busy or a crash) leaves the record
    pending; the next run reads the plist as "same" but must still restart it: launchd runs the old definition."""
    from cleanup.installer import jobs, main as inst
    from cleanup.plan import bridge
    assert installer.run("--yes").returncode == 0
    for k, v in installer.env.items():                          # the rest runs in-process in the same pretend Mac
        monkeypatch.setenv(k, v)
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_INSTALL_LOCK_FD"):
        monkeypatch.delenv(k, raising=False)
    render = jobs.render
    monkeypatch.setattr(jobs, "render", lambda name, **k: render(name, **k).replace(
        "<plist", "<!-- new definition -->\n<plist", 1) if name == "weekly" else render(name, **k))
    put = inst.owned.put

    def put_then_busy(path, build, prior):
        put(path, build, prior)
        raise bridge.Busy("a cleanup started")                 # stopped after writing, before the restart
    monkeypatch.setattr(inst.owned, "put", put_then_busy)
    argv = ["install", "--python", sys.executable, "--yes", "--reuse-answers"]
    assert inst.main(argv) == inst.BUSY
    plist = installer.home / "Library/LaunchAgents" / (AP + ".weekly.plist")
    assert "new definition" in plist.read_text()
    monkeypatch.setattr(inst.owned, "put", put)
    start = jobs.start

    def busy(lbl, path):
        raise bridge.Busy("a cleanup started")
    monkeypatch.setattr(jobs, "start", busy)
    assert inst.main(argv) == inst.BUSY                        # a restart refused mid-run keeps the record pending
    monkeypatch.setattr(jobs, "start", start)
    installer.log.write_text("")
    assert inst.main(argv) == inst.OK
    calls = installer.log.read_text().splitlines()
    assert any(c.startswith("bootout") and c.endswith(AP + ".weekly") for c in calls), calls
    assert any(c.startswith("bootstrap") and c.endswith(AP + ".weekly.plist") for c in calls), calls
    assert not any(AP + ".spacecheck" in c for c in calls if c.startswith(("bootout", "bootstrap"))), calls
    installer.log.write_text("")
    assert inst.main(argv) == inst.OK                          # done now: a third run leaves it alone
    assert _bootouts(installer) == []


def test_uninstall_refuses_while_a_cleanup_runs_and_removes_nothing(installer):
    assert installer.run("--yes").returncode == 0
    before = snapshot(installer.home)
    installer.log.write_text("")
    fd = _run_lock(installer)
    try:
        r = installer.run("--yes", script="uninstall.sh")
    finally:
        os.close(fd)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "cleanup is running" in r.stderr and _bootouts(installer) == []
    assert names(snapshot(installer.home)) == names(before)
    assert installer.run("--yes", script="uninstall.sh").returncode == 0   # works once it has finished


def test_update_refuses_before_fetching_while_a_cleanup_runs(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    from cleanup.plan import bridge
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AFTERPARTY_INSTALL_LOCK_FD", raising=False)
    git = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    bare, copy = tmp_path / "o.git", tmp_path / "copy"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=git)
    subprocess.run(["git", "clone", "-q", str(bare), str(copy)], check=True, env=git, capture_output=True)
    subprocess.run(["git", "-C", str(copy), "commit", "-q", "--allow-empty", "-m", "1"], check=True, env=git)
    subprocess.run(["git", "-C", str(copy), "push", "-q", "origin", "HEAD:main"], check=True, env=git)
    seen = []
    with bridge.lock(str(tmp_path / "state")):
        rc = inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: seen.append(r) or 0)
    assert rc == inst.BUSY and seen == []


# ---- M3: a tab-indented settings.json stays tab-indented
def test_settings_hook_keeps_tab_indentation(tmp_path):
    from cleanup.installer import settings_hook
    p = tmp_path / "settings.json"
    p.write_text('{\n\t"model": "opus",\n\t"hooks": {}\n}\n')
    settings_hook.add(str(p), settings_hook.entry("/x/afterparty-session-end"))
    text = p.read_text()
    assert '\n\t"model": "opus"' in text and '\n\t\t"SessionEnd"' in text and "\n  " not in text
    assert settings_hook.remove(str(p), settings_hook.entry("/x/afterparty-session-end")) == "removed"
    assert p.read_text() == '{\n\t"model": "opus"\n}\n'


def test_settings_hook_keeps_space_indentation_width(tmp_path):
    from cleanup.installer import settings_hook
    p = tmp_path / "settings.json"
    p.write_text('{\n    "model": "opus"\n}\n')
    settings_hook.add(str(p), settings_hook.entry("/x/afterparty-session-end"))
    assert '\n    "model": "opus"' in p.read_text() and "\t" not in p.read_text()


# ---- M4: an unreadable folder during --purge is a clear message and a retryable exit, never a traceback
def test_purge_lists_an_unreadable_folder_and_exits_non_zero(installer):
    assert installer.run("--yes").returncode == 0
    locked = installer.home / ".local/state/afterparty/private"
    locked.mkdir()
    (locked / "f").write_text("x")
    locked.chmod(0)
    try:
        r = installer.run("--yes", "--purge", script="uninstall.sh")
    finally:
        locked.chmod(0o700)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "Traceback" not in r.stderr and "couldn't read" in (r.stdout + r.stderr)
    assert (locked / "f").read_text() == "x"                              # left alone
    assert not (installer.home / ".local/bin/afterparty").exists()       # the entry points are gone


# ---- M5: ensure_server boots out only our own server job
def _ensure_stops_after_bootout(monkeypatch, state_path):
    from cleanup import launcher
    seen = []
    monkeypatch.setattr(launcher, "_bootout", lambda label: seen.append(label) or (True, "stopped"))

    class Halt(Exception):
        pass

    def halt(s):
        raise Halt()
    monkeypatch.setattr(launcher, "_stop_stale", halt)
    with pytest.raises(Halt):
        launcher.ensure_server(open_browser=False, python=sys.executable, state_path=str(state_path))
    return seen


@pytest.mark.parametrize("label", ["com.example.other", 7, ["x"]])
def test_ensure_server_never_boots_out_a_job_the_state_file_names_but_isnt_ours(tmp_path, monkeypatch, label):
    state_path = tmp_path / "server.json"
    state_path.write_text(json.dumps({"mode": "launchd", "label": label, "port": 1, "token": "t"}))
    assert _ensure_stops_after_bootout(monkeypatch, state_path) == []


def test_ensure_server_boots_out_our_own_stale_job(tmp_path, monkeypatch):
    from cleanup import launcher
    state_path = tmp_path / "server.json"
    state_path.write_text(json.dumps({"mode": "launchd", "label": launcher.SERVER_LABEL, "port": 1, "token": "t"}))
    assert _ensure_stops_after_bootout(monkeypatch, state_path) == [launcher.SERVER_LABEL]
