import json
import os
from pathlib import Path

import pytest

from cleanup.engine.core import Item
from cleanup.plan import bridge
from cleanup.plan.undo import undo


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def fake_trash(tmp):
    return bridge.test_trash(str(tmp / "trashroot"))


def _run(items, home, tmp_path, trash_fn=None):
    return bridge.run(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                      trash_fn=trash_fn or fake_trash(tmp_path))


def _events(res):
    return [json.loads(l) for l in Path(res["journal"]).read_text().splitlines()]


def test_permanent_rebuildable_is_deleted_and_journaled(home, tmp_path):
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    (nm / "a.js").write_text("x")
    res = _run([Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT)], home, tmp_path)
    assert not nm.exists()
    assert [d["path"] for d in res["deleted"]] == [str(nm)]
    assert any(e.get("result") == "deleted" for e in _events(res))


def test_permanent_claim_without_a_rule_goes_to_trash(home, tmp_path):
    stuff = home / "Projects" / "app" / "stuff"
    stuff.mkdir(parents=True)
    res = _run([Item(1, "node_modules", str(stuff), "wrong", disposition=bridge.PERMANENT)], home, tmp_path)
    assert res["deleted"] == []
    assert [t["path"] for t in res["trashed"]] == [str(stuff)]


def test_trash_then_undo_restores(home, tmp_path):
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("zip")
    res = _run([Item(4, "personal", str(f), "old", disposition=bridge.TRASH)], home, tmp_path)
    assert not f.exists() and res["trash_bytes"] > 0
    out = undo(res["journal"])
    assert [r["path"] for r in out["restored"]] == [str(f)] and f.read_text() == "zip"


def test_forced_trash_overrides_latest_frames_keep(home, tmp_path):
    d = home / "Projects" / "film" / "renders" / "shot_v4"
    d.mkdir(parents=True)
    for i in range(3):
        (d / f"f{i:04}.png").write_text("p")
    res = _run([Item(3, "frames-latest", str(d), "latest", disposition=bridge.TRASH, force_trash=True)], home, tmp_path)
    assert not d.exists() and [t["path"] for t in res["trashed"]] == [str(d)]


def test_trash_failure_leaves_files(home, tmp_path):
    f = home / "Downloads" / "keepme.zip"
    f.parent.mkdir()
    f.write_text("m")
    res = _run([Item(4, "personal", str(f), "old", disposition=bridge.TRASH)], home, tmp_path,
               trash_fn=lambda paths: {p: "ERR:Finder said no" for p in paths})
    assert f.exists() and res["deleted"] == [] and len(res["failed"]) == 1


UV_PRUNE = ["env", "UV_LOCK_TIMEOUT=10", "uv", "cache", "prune"]


def _exec(items, home, tmp_path, command_fn, running_fn=lambda tool: False):
    prep = bridge.prepare(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    return bridge.execute(prep, keep=[], trash_fn=fake_trash(tmp_path), command_fn=command_fn, running_fn=running_fn)


def test_delegated_commands_are_journaled(home, tmp_path):
    calls = []

    def fake(argv):
        calls.append(argv)
        return (0, "") if argv[0] == "env" else (1, "lots of output\npnpm exploded")
    ok = Item(1, "cache", "uv cache", "prune", disposition=bridge.DELEGATED, command=UV_PRUNE)
    bad = Item(1, "cache", "pnpm store", "prune", disposition=bridge.DELEGATED, command=["pnpm", "store", "prune"])
    res = _exec([ok, bad], home, tmp_path, fake)
    assert calls == [UV_PRUNE, ["pnpm", "store", "prune"]]
    assert [d["path"] for d in res["delegated"]] == ["uv cache"]
    assert "pnpm exploded" in res["failed"][0]["reason"]
    ev = [e for e in _events(res) if e["event"] == "delegated"]
    assert [e["result"] for e in ev] == ["ok", "failed"]
    assert ev[0]["argv"] == UV_PRUNE and ev[0]["exit"] == 0 and ev[1]["exit"] == 1 and "pnpm exploded" in ev[1]["stderr"]


# ---------------- final review MUST-FIX 8: delegated = allowlisted commands only ----------------
def test_delegated_runs_only_allowlisted_commands(home, tmp_path):
    calls = []
    fake = lambda argv: (calls.append(argv), (0, ""))[1]
    items = [Item(1, "cache", "evil", "x", disposition=bridge.DELEGATED, command=["rm", "-rf", str(home)]),
             Item(1, "cache", "uv full", "x", disposition=bridge.DELEGATED, command=["uv", "cache", "clean", "--force"]),
             Item(4, "model", "ollama:bad", "x", disposition=bridge.DELEGATED, command=["ollama", "rm", "x; rm -rf /"]),
             Item(4, "model", "ollama:ok", "x", disposition=bridge.DELEGATED, command=["ollama", "rm", "llama3:8b"]),
             Item(1, "cache", "callable", "x", disposition=bridge.DELEGATED)]
    res = _exec(items, home, tmp_path, fake)
    assert calls == [["ollama", "rm", "llama3:8b"]]
    assert sorted(f["path"] for f in res["failed"]) == ["callable", "evil", "ollama:bad", "uv full"]
    assert all(f["reason"] == "not an allowed command" for f in res["failed"])


def test_tier1_prune_refused_while_its_tool_runs(home, tmp_path):
    calls = []
    fake = lambda argv: (calls.append(argv), (0, ""))[1]
    it = Item(1, "cache", "uv cache", "prune", disposition=bridge.DELEGATED, command=UV_PRUNE)
    res = _exec([it], home, tmp_path, fake, running_fn=lambda tool: tool == "uv")
    assert calls == [] and "uv is running" in res["failed"][0]["reason"]


def test_precheck_failure_skips_item(home, tmp_path):
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)

    def nope():
        raise RuntimeError("in use")

    res = _run([Item(1, "node_modules", str(nm), "x", disposition=bridge.PERMANENT, precheck=nope)], home, tmp_path)
    assert nm.exists() and res["failed"][0]["reason"] == "in use"
    ev = [e for e in _events(res) if e["event"] == "precheck"]
    assert ev and ev[0]["result"] == "failed" and ev[0]["reason"] == "in use" and ev[0]["path"] == str(nm)


def test_report_only_items_are_ignored(home, tmp_path):
    f = home / "Projects" / "note.txt"
    f.write_text("n")
    res = _run([Item(4, "info", str(f), "info")], home, tmp_path)
    assert f.exists() and res["owner"] == {}


def test_lock_refuses_second_run(tmp_path):
    with bridge.lock(str(tmp_path)):
        with pytest.raises(bridge.Busy, match="Another Afterparty cleanup is running"):
            with bridge.lock(str(tmp_path)):
                pass


def test_default_trash_uses_test_seam(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERPARTY_TEST_TRASH_DIR", str(tmp_path / "tt"))
    f = tmp_path / "a.txt"
    f.write_text("a")
    out = bridge.default_trash()([str(f)])
    assert "/.Trash/" in out[str(f)] and not f.exists()


def test_trash_item_on_git_worktree_is_never_removed(home, tmp_path):
    wt = home / "Projects" / "app" / ".worktrees" / "feat"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /nowhere\n")
    res = _run([Item(2, "worktree", str(wt), "idle", disposition=bridge.TRASH)], home, tmp_path)
    assert wt.exists() and res["deleted"] == [] and res["trashed"] == []


def test_force_trash_never_overrides_protected(home, tmp_path):
    d = home / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
    d.mkdir(parents=True)
    (d / "Cookies").write_text("c")
    res = _run([Item(3, "x", str(d), "forced", disposition=bridge.TRASH, force_trash=True)], home, tmp_path)
    assert d.exists() and res["trashed"] == [] and res["deleted"] == []


# ---------------- final review MUST-FIX 3/4: approval is bound to reviewed identities; current settings win ----------------
def test_prepare_has_no_side_effects_and_shows_totals(home, tmp_path):
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("zip")
    prep = bridge.prepare([Item(4, "personal", str(f), "old", disposition=bridge.TRASH)], keep=[],
                          allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    assert prep.totals["trash"] > 0 and prep.has_work()
    assert not (tmp_path / "state").exists() and f.exists()


def test_replacement_during_confirmation_is_skipped(home, tmp_path):
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("zip")
    prep = bridge.prepare([Item(4, "personal", str(f), "old", disposition=bridge.TRASH)], keep=[],
                          allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    f.unlink()
    f.write_text("a new file that nobody reviewed")  # appears while the user is confirming
    res = bridge.execute(prep, keep=[], trash_fn=fake_trash(tmp_path))
    assert f.exists() and res["trashed"] == [] and "since review" in res["skipped"][0]["reason"]


def test_invalid_settings_mid_run_stop_everything(home, tmp_path):
    a = home / "Downloads" / "a.zip"
    a.parent.mkdir()
    a.write_text("a")
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    items = [Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT),
             Item(4, "personal", str(a), "old", disposition=bridge.TRASH)]
    prep = bridge.prepare(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))

    def broken():
        raise ValueError("settings file is missing")
    res = bridge.execute(prep, keep=broken, trash_fn=fake_trash(tmp_path))
    assert nm.exists() and a.exists() and res["deleted"] == [] and res["trashed"] == []
    assert len(res["skipped"]) == 2 and all(s["reason"] == bridge.SETTINGS_CHANGED for s in res["skipped"])


# ---------------- final review MUST-FIX 9: no ancestor conflicts across passes ----------------
def test_permanent_parent_never_swallows_a_trash_child(home, tmp_path):
    # The review's overlap probe: a permanent parent plus a Trash child inside it.
    nm = home / "Projects" / "app" / "node_modules"
    child = nm / "pkg" / "notes.txt"
    child.parent.mkdir(parents=True)
    child.write_text("mine")
    calls = []
    trash = fake_trash(tmp_path)

    def counting(paths):
        calls.append(list(paths))
        return trash(paths)
    res = _run([Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT),
                Item(4, "personal", str(child), "old", disposition=bridge.TRASH)], home, tmp_path, trash_fn=counting)
    assert res["deleted"] == [] and calls, res
    assert [t["path"] for t in res["trashed"]] == [str(nm)]  # the parent went to the Trash, taking the child along
    out = undo(res["journal"])
    assert child.read_text() == "mine" and len(out["restored"]) == 1


def test_permanent_inside_a_trash_target_is_dropped(home, tmp_path):
    proj = home / "Downloads" / "proj"
    nm = proj / "node_modules"
    nm.mkdir(parents=True)
    (proj / "main.py").write_text("print(1)")
    res = _run([Item(4, "personal", str(proj), "old", disposition=bridge.TRASH),
                Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT)], home, tmp_path)
    assert res["deleted"] == [] and [t["path"] for t in res["trashed"]] == [str(proj)]


def test_worktree_holding_a_trash_target_is_not_removed(home, tmp_path, monkeypatch):
    from cleanup.plan import executor
    wt = home / "Projects" / "app" / ".worktrees" / "feat"
    shot = wt / "out" / "old.txt"
    shot.parent.mkdir(parents=True)
    shot.write_text("x")
    (wt / ".git").write_text("gitdir: /nowhere\n")
    removed = []
    monkeypatch.setattr(executor, "remove_worktree", lambda p: removed.append(p))
    res = _run([Item(2, "worktree", str(wt), "idle", disposition=bridge.REMOVE),
                Item(4, "personal", str(shot), "old", disposition=bridge.TRASH)], home, tmp_path)
    assert removed == [] and wt.exists()
    assert any("Trash" in s["reason"] and s["path"] == str(wt) for s in res["skipped"])


# ---------------- final review MUST-FIX 5: engine items never take finished files; prechecks re-run at execution ----------------
def test_engine_never_trashes_finished_files(home, tmp_path):
    dl = home / "Downloads"
    dl.mkdir()
    (dl / "final.mp4").write_bytes(b"v")
    notes = dl / "notes"
    notes.mkdir()
    (notes / "plan.md").write_text("# plan")
    (dl / "junk.bin").write_bytes(b"j")
    res = _run([Item(4, "personal", str(dl / "final.mp4"), "old", disposition=bridge.TRASH),
                Item(4, "personal", str(notes), "old", disposition=bridge.TRASH),
                Item(4, "personal", str(dl / "junk.bin"), "old", disposition=bridge.TRASH)], home, tmp_path)
    assert [os.path.basename(t["path"]) for t in res["trashed"]] == ["junk.bin"]
    assert (dl / "final.mp4").exists() and (notes / "plan.md").exists()


def test_rebuildable_folders_may_hold_docs(home, tmp_path):
    # Finished-file rule per class: README.md and models don't block dependency folders (videos still do).
    nm = home / "Projects" / "app" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "README.md").write_text("docs ship with packages")
    (nm / "model.glb").write_bytes(b"g")
    res = _run([Item(1, "node_modules", str(nm.parent), "idle", disposition=bridge.PERMANENT)], home, tmp_path)
    assert [d["path"] for d in res["deleted"]] == [str(nm.parent)]


def test_precheck_runs_again_right_before_acting(home, tmp_path):
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("zip")
    state_ = {"ok": True}
    it = Item(4, "personal", str(f), "old", disposition=bridge.TRASH,
              precheck=lambda: [str(f)] if state_["ok"] else [])
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    state_["ok"] = False  # e.g. an app opened it, or it was touched, while the user was confirming
    res = bridge.execute(prep, keep=[], trash_fn=fake_trash(tmp_path))
    assert f.exists() and res["trashed"] == [] and "no longer" in res["skipped"][0]["reason"]


def test_open_scratch_run_is_kept(home, tmp_path):
    run_ = home / "Projects" / "app" / ".scratch" / "run1"
    run_.mkdir(parents=True)
    (run_ / "frame.png").write_bytes(b"p")
    prep = bridge.prepare([Item(1, ".scratch", str(run_), "expired", disposition=bridge.PERMANENT)], keep=[],
                          allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    res = bridge.execute(prep, keep=[], trash_fn=fake_trash(tmp_path), open_fn=lambda: [str(run_ / "frame.png")])
    assert run_.exists() and res["deleted"] == [] and "open in" in res["skipped"][0]["reason"]


def test_trash_pass_never_removes_a_worktree_even_when_the_manifest_says_remove(home, tmp_path, monkeypatch):
    # Final review promoted coverage (mutation-proof): the real manifest gives this TRASH item mode "remove"; only the
    # guard in prepare() (remove -> keep in the Trash pass) stops `git worktree remove` from running.
    from cleanup.plan import executor
    from cleanup.plan import manifest as plan_manifest
    wt = home / "Projects" / "app" / ".worktrees" / "feat"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /nowhere\n")
    assert plan_manifest.build([str(wt)], home=str(home))["items"][0]["mode"] == "remove"
    removed = []
    monkeypatch.setattr(executor, "remove_worktree", lambda p: removed.append(p))
    res = _run([Item(2, "worktree", str(wt), "idle", disposition=bridge.TRASH)], home, tmp_path)
    assert removed == [] and wt.exists() and res["deleted"] == [] and res["failed"] == []
