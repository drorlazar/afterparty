"""Regression probes from the final whole-branch review."""
import json
import os
import shutil
from pathlib import Path

import pytest

from tests.conftest import age, seq, set_config


@pytest.fixture
def core_env(env, monkeypatch, tmp_path):
    """Run the engine in-process against the isolated env (HOME, config, state and Trash redirected)."""
    from cleanup.engine import core
    for k in ("HOME", "AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_TEST_TRASH_DIR",
              "AFTERPARTY_NO_NOTIFY", "DISKCLEAN_CODE_SIGN_DIR", "DISKCLEAN_TEMP_DIR", "DISKCLEAN_CACHE_DIR"):
        monkeypatch.setenv(k, env[k])
    st = Path(env["AFTERPARTY_STATE_DIR"])
    monkeypatch.setattr(core, "STATE", str(st))
    monkeypatch.setattr(core, "LEDGER", str(st / "sessions.jsonl"))
    monkeypatch.setattr(core, "HOME", env["HOME"])
    monkeypatch.setattr(core, "SEQ_MIN_BYTES", 1024)
    monkeypatch.setattr(core, "SEQ_MIN_FILES", 30)
    core.FAILED.clear()
    proj = Path(env["HOME"]) / "Projects"
    proj.mkdir(exist_ok=True)
    return core, proj


def _trashed(env):
    root = Path(env["AFTERPARTY_TEST_TRASH_DIR"]) / ".Trash"
    return sorted(p.name.split("-", 1)[1] for p in root.iterdir()) if root.exists() else []


# ---------------- MUST-FIX 3: the CLI executes exactly what was reviewed ----------------
def test_tidy_replacement_during_confirmation_is_not_trashed(core_env, env, monkeypatch):
    core, proj = core_env
    task = proj / "task"
    shot = seq(task / "renders" / "shot_v1")
    seen = []

    def confirm(prompt):  # while the user reads the prompt, the reviewed folder is swapped for a new one
        seen.append(prompt)
        shutil.rmtree(shot)
        seq(shot)
        return True
    monkeypatch.setattr(core, "confirm", confirm)
    core.main(["tidy", str(task), "--apply"])
    assert seen and shot.exists() and len(list(shot.iterdir())) == 40
    assert _trashed(env) == []


# ---------------- MUST-FIX 4: protection added during confirmation wins ----------------
def test_keep_added_during_confirmation_protects(core_env, env, monkeypatch):
    from cleanup import config
    core, proj = core_env
    task = proj / "task"
    shot = seq(task / "renders" / "shot_v1")

    def confirm(prompt):
        config.add_keep(str(shot))
        return True
    monkeypatch.setattr(core, "confirm", confirm)
    core.main(["tidy", str(task), "--apply"])
    assert shot.exists() and _trashed(env) == []


def test_tier4_answer_applies_to_what_was_shown(core_env, env, monkeypatch):
    core, proj = core_env
    f = Path(env["HOME"]) / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("reviewed")
    allowed = [os.path.realpath(env["HOME"] + "/Downloads")]

    def answer(prompt):  # the file is replaced while the question is on screen
        f.unlink()
        f.write_text("a different file")
        return "y"
    monkeypatch.setattr("builtins.input", answer)
    core.ask_tier4([core.Item(4, "personal", str(f), "old", size=1, disposition="trash")], [], allowed)
    assert f.read_text() == "a different file" and _trashed(env) == []


# ---------------- MUST-FIX 6: retention settings are real ----------------
DAY = 86400


def _deps_and_scratch(env, days_old):
    import time
    proj = Path(env["HOME"]) / "Projects" / "app"
    nm = proj / "node_modules"
    nm.mkdir(parents=True)
    (proj / "package.json").write_text("{}")
    run = proj / ".scratch" / "run1"
    run.mkdir(parents=True)
    (run / "a.png").write_text("a")
    age(proj, time.time() - days_old * DAY)
    return nm, run


def test_configured_retention_is_used(env, run_cli):
    from tests.conftest import manifest
    nm, run = _deps_and_scratch(env, 20)
    run_cli("--tier", "1")  # defaults 3/14: both offered
    paths = {r[4] for r in manifest(env, "last-scoped-report")}
    assert str(nm) in paths and str(run) in paths
    set_config(env, scratch_days=30, idle_days=30)
    run_cli("--tier", "1")
    paths = {r[4] for r in manifest(env, "last-scoped-report")}
    assert str(nm) not in paths and str(run) not in paths
    r = run_cli("--apply", "--yes", "--tier", "1")
    assert r.returncode == 0 and nm.exists() and run.exists()


def test_scratch_run_used_after_discovery_is_kept(core_env, env):
    import time
    from cleanup.plan import bridge
    core, proj = core_env
    run = proj / "app" / ".scratch" / "run1"
    run.mkdir(parents=True)
    (run / "a.png").write_text("a")
    age(run)
    items = core.tier1([], [str(run.parent)], 14)
    assert [i.path for i in items] == [str(run)]
    (run / "b.png").write_text("new work")  # an agent writes into it after the scan found it stale
    res = bridge.run(items, keep=[], allowed_roots=[str(proj)], state=core.STATE)
    assert run.exists() and res["deleted"] == []


def test_scratch_only_matches_real_run_folders(core_env, env, tmp_path):
    core, proj = core_env
    scr = proj / "app" / ".scratch"
    (scr / "a" / "b").mkdir(parents=True)
    (scr / "notes.txt").write_text("n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, scr / "link")
    age(scr)
    paths = [i.path for i in core.tier1([], [str(scr)], 14)]
    assert paths == [str(scr / "a")]  # not the file, not the symlink, not a/b
    from cleanup.engine.classify import classify
    assert classify(str(scr / "a" / "b"), home=env["HOME"])["rule"] != "scratch"


def test_tracked_scratch_run_is_protected(env, run_cli, git_repo):
    from tests.conftest import git
    repo = git_repo("home/Projects/repo")
    run = repo / ".scratch" / "run1"
    run.mkdir(parents=True)
    (run / "keep.png").write_text("k")
    git(repo, "add", "-f", ".scratch/run1/keep.png", env=env)
    git(repo, "commit", "-q", "-m", "tracked scratch", env=env)
    age(repo)
    r = run_cli("--apply", "--yes", "--tier", "1")
    assert r.returncode == 0 and (run / "keep.png").exists()


# ---------------- MUST-FIX 7: configured roots are the boundary ----------------
OUTSIDE = "That folder isn't inside your Afterparty project folders. Add it to roots in"


def test_tidy_outside_roots_is_refused(env, run_cli):
    other = Path(env["HOME"]) / "Documents" / "task"
    seq(other / "renders" / "shot_v1")
    for args in ((), ("--apply", "--yes")):
        r = run_cli("tidy", other, *args)
        assert r.returncode == 2 and OUTSIDE in r.stderr and env["AFTERPARTY_CONFIG_DIR"] in r.stderr
    assert len(list((other / "renders" / "shot_v1").iterdir())) == 40


def test_root_flag_cannot_widen_a_deleting_run(env, run_cli):
    b = Path(env["HOME"]) / "Documents" / "b"
    (b / "node_modules" / "x").mkdir(parents=True)
    (b / "package.json").write_text("{}")
    age(b)
    r = run_cli("--root", b, "--tier", "1", "--apply", "--yes")
    assert r.returncode == 2 and OUTSIDE in r.stderr and (b / "node_modules").exists()
    assert run_cli("--root", b, "--tier", "1").returncode == 0  # a report may look anywhere


def test_off_root_list_includes_codex_worktrees(tmp_path):
    from cleanup.engine import core
    assert os.path.realpath(str(tmp_path) + "/.codex/worktrees") in core.off_root_allowed(str(tmp_path))
