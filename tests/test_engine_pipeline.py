import os
from pathlib import Path

from tests.conftest import OLD, age, seq, set_config


def _trashed(env):
    d = Path(env["AFTERPARTY_TEST_TRASH_DIR"]) / ".Trash"
    return sorted(p.name.split("-", 1)[1] for p in d.iterdir()) if d.exists() else []


def _two_versions(home):
    base = home / "Projects" / "film" / "renders"
    old, new = base / "shot_v1", base / "shot_v2"
    seq(old)
    seq(new)
    return old, new


def test_old_frames_go_to_trash_not_deleted(env, run_cli):
    old, new = _two_versions(Path(env["HOME"]))
    run_cli("--apply", "--yes", "--tier", "3")
    assert not old.exists() and new.exists()
    assert "shot_v1" in _trashed(env)


def test_deliverable_inside_frame_folder_survives(env, run_cli):
    old, _ = _two_versions(Path(env["HOME"]))
    final = old / "final_cut.mp4"
    final.write_text("movie")
    age(final)
    run_cli("--apply", "--yes", "--tier", "3")
    assert final.exists()
    assert not any(p.suffix == ".png" for p in old.iterdir())


def test_trash_failure_never_deletes(env, run_cli):
    old, _ = _two_versions(Path(env["HOME"]))
    blocker = Path(env["AFTERPARTY_TEST_TRASH_DIR"])
    blocker.write_text("not a folder")  # the stand-in Trash can't be created -> every move fails
    r = run_cli("--apply", "--yes", "--tier", "3")
    assert old.exists() and len(list(old.iterdir())) >= 30
    assert "failed" in r.stdout


def test_symlinked_project_is_not_followed(env, run_cli, tmp_path):
    real = tmp_path / "elsewhere" / "app"
    (real / "node_modules").mkdir(parents=True)
    age(real / "node_modules")
    age(real)
    (Path(env["HOME"]) / "Projects").mkdir()
    os.symlink(real, Path(env["HOME"]) / "Projects" / "app")
    run_cli("--apply", "--yes", "--tier", "1")
    assert (real / "node_modules").exists()


def test_protection_added_after_report_wins(env, run_cli):
    nm = Path(env["HOME"]) / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    age(nm)
    age(nm.parent)
    run_cli()
    set_config(env, keep=[str(nm.parent)])
    run_cli("--apply", "--yes")
    assert nm.exists()


def test_active_scratch_run_survives(env, run_cli):
    proj = Path(env["HOME"]) / "Projects" / "app"
    old_run, live_run = proj / ".scratch" / "run1", proj / ".scratch" / "run2"
    old_run.mkdir(parents=True)
    live_run.mkdir()
    (old_run / "a.png").write_text("a")
    (live_run / "b.png").write_text("b")  # written just now
    age(old_run / "a.png")
    age(old_run)
    run_cli("--apply", "--yes", "--tier", "1")
    assert not old_run.exists() and live_run.exists()


def test_stale_temp_goes_to_trash(env, run_cli, tmp_path):
    t = tmp_path / "T"
    t.mkdir()
    junk = t / "old-download.bin"
    junk.write_text("j")
    age(junk)
    env["DISKCLEAN_TEMP_DIR"] = str(t)
    run_cli("--apply", "--yes", "--tier", "1")
    assert not junk.exists() and "old-download.bin" in _trashed(env)


def test_tool_prunes_are_fixed_commands(monkeypatch, tmp_path):
    from cleanup.engine import core
    from cleanup.plan import bridge
    calls = []
    monkeypatch.setattr(core, "HOME", str(tmp_path))  # never look at the real home's app caches
    monkeypatch.setattr(core, "running", lambda proc: True)  # and never call pgrep
    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(core, "run", lambda cmd, **k: (calls.append(cmd), (0, ""))[1])
    items = [i for i in core.tier1_caches() if i.path.endswith("/.cache/uv") or i.path == "pnpm store"]
    assert len(items) == 2
    for i in items:
        assert i.disposition == bridge.DELEGATED and bridge.allowed_command(i) is None
    assert [i.command for i in items] == [["env", "UV_LOCK_TIMEOUT=10", "uv", "cache", "prune"], ["pnpm", "store", "prune"]]
    assert calls == []  # building the list runs nothing


def test_off_root_list_is_exact(tmp_path):
    from cleanup.engine import core
    home = str(tmp_path)
    got = core.off_root_allowed(home)
    assert home not in got
    assert os.path.realpath(home + "/.cache/uv") in got
    assert os.path.realpath(home + "/.local/share/claude/versions") in got
    assert os.path.realpath(home + "/Downloads") in got


def test_safe_target_refuses_home_outside_roots(env):
    from cleanup.engine import core
    home = env["HOME"]
    allowed = sorted({os.path.realpath(home + "/Projects")} | set(core.off_root_allowed(home)))
    it = core.Item(1, "node_modules", home + "/Documents/x/node_modules", "x", disposition="permanent")
    assert core.safe_target(it, [], allowed) == (False, "resolves outside allowed roots")


def test_every_offered_cache_and_tier4_path_is_allowed(env, monkeypatch):
    from cleanup.engine import core
    home = env["HOME"]
    monkeypatch.setattr(core, "HOME", home)
    monkeypatch.setattr(core.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(core, "running", lambda proc: False)
    monkeypatch.setattr(core, "run", lambda *a, **k: (0, "NAME\nllama3:8b  x\n"))
    old = 1000000000
    for rel in ("Library/Caches/com.spotify.client", "Library/Caches/com.openai.codex", "Library/Caches/Codex",
                ".cache/huggingface", ".cache/mlx", "Library/Application Support/Claude/vm_bundles",
                ".cache/uv", ".npm/_cacache",
                "Library/Caches/ms-playwright/chromium-1", "Library/Caches/ms-playwright/chromium-2"):
        os.makedirs(os.path.join(home, rel), exist_ok=True)
    for rel in (".codex/sessions/a.log", ".codex/archived_sessions/b.log", "Downloads/old.bin"):
        f = os.path.join(home, rel)
        os.makedirs(os.path.dirname(f), exist_ok=True)
        Path(f).write_text("x")
        os.utime(f, (old, old))
    allowed = sorted(set(core.off_root_allowed(home)))
    items = [i for i in core.tier1_caches() + core.tier4() if i.disposition and os.path.isabs(i.path)]
    assert len(items) >= 8
    bad = [i.path for i in items if not core.safe_target(i, [], allowed)[0]]
    assert bad == []


def test_no_dead_vm_bundle_offer(env, monkeypatch):
    # Final review SHOULD-FIX: classify protects Claude's VM bundle (app data), so tier 4 must not offer it.
    from cleanup.engine import core
    home = env["HOME"]
    monkeypatch.setattr(core, "HOME", home)
    monkeypatch.setattr(core.shutil, "which", lambda n: None)
    os.makedirs(os.path.join(home, "Library/Application Support/Claude/vm_bundles"))
    assert not any(i.path.endswith("vm_bundles") for i in core.tier4())
    assert not any(p.endswith("vm_bundles") for p in core.off_root_allowed(home))
