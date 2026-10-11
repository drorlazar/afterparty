"""Fix round 2 (Plan A re-review): halts are terminal, delegated commands honour protections, every Trash move gets
the full preflight, and engine items never take finished files."""
import os
import subprocess

import pytest

from cleanup.engine.core import Item
from cleanup.plan import bridge
from cleanup.plan import executor as X
from cleanup.plan import manifest as M
from tests.test_executor import fake_trash as exec_trash
from tests.test_manifest import fake_classify, tree


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def no_real_uv(monkeypatch):
    """Never ask the host's uv where its cache is (tests that need an answer set one)."""
    if hasattr(bridge, "_uv_cache_dir"):
        monkeypatch.setattr(bridge, "_uv_cache_dir", lambda: None)


def counting_trash(tmp, calls):
    inner = bridge.test_trash(str(tmp / "trashroot"))

    def trash(paths):
        calls.append(list(paths))
        return inner(paths)
    return trash


def raise_on(n):
    """A keep callable whose n-th read fails once (settings broke), then reads fine again."""
    calls = []

    def keep():
        calls.append(1)
        if len(calls) == n:
            raise ValueError("settings became invalid")
        return []
    return keep


# ---------------- 1. a halt is terminal for the whole run ----------------
def test_halt_during_trash_flush_stops_later_permanent_deletes(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt"), str(t / "p/node_modules")], classify_fn=fake_classify)
    assert [x["mode"] for x in M.targets(m)] == ["trash", "permanent"]
    # reads: 1 = doc.txt check, 2 = node_modules check, 3 = the Trash flush before node_modules (fails once)
    r = X.execute(m, keep=raise_on(3), classify_fn=fake_classify, trash_fn=exec_trash(tmp_path),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path),
                  open_fn=lambda: [])
    assert r["deleted"] == [] and r["trashed"] == [] and r["halted"] == X.SETTINGS_CHANGED
    assert (t / "doc.txt").exists() and (t / "p/node_modules").exists()
    assert sorted(os.path.basename(s["path"]) for s in r["skipped"]) == ["doc.txt", "node_modules"]
    assert all(s["reason"] == X.SETTINGS_CHANGED for s in r["skipped"])


def test_halt_in_a_full_trash_batch_stops_everything_after(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "BATCH", 1)
    for n in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / n).write_text(n)
    m = M.build([str(tmp_path / n) for n in ("a.txt", "b.txt", "c.txt")], classify_fn=fake_classify)
    calls = []
    trash = exec_trash(tmp_path)
    r = X.execute(m, keep=raise_on(2), classify_fn=fake_classify,  # 1 = a check, 2 = a's flush (fails once)
                  trash_fn=lambda ps: calls.append(ps) or trash(ps), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path), open_fn=lambda: [])
    assert calls == [] and r["trashed"] == [] and r["halted"] == X.SETTINGS_CHANGED
    assert len(r["skipped"]) == 3


def test_bridge_halt_in_first_pass_starts_no_later_pass(home, tmp_path):
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    a = home / "Downloads" / "a.zip"
    a.parent.mkdir()
    a.write_text("a")
    items = [Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT),
             Item(4, "personal", str(a), "old", disposition=bridge.TRASH)]
    prep = bridge.prepare(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    assert len(prep.manifests) == 2
    calls = []
    res = bridge.execute(prep, keep=raise_on(1), trash_fn=counting_trash(tmp_path, calls), open_fn=lambda: [])
    assert calls == [] and res["deleted"] == [] and res["trashed"] == [] and res["halted"] == bridge.SETTINGS_CHANGED
    assert nm.exists() and a.exists()
    assert {s["path"] for s in res["skipped"]} == {str(nm), str(a)}


def test_bridge_halt_during_commands_runs_nothing_after(home, tmp_path):
    a = home / "Downloads" / "a.zip"
    a.parent.mkdir()
    a.write_text("a")
    items = [Item(4, "personal", str(home / ".cache" / "uv"), "full", disposition=bridge.DELEGATED,
                  command=["uv", "cache", "clean", "--force"]),
             Item(4, "personal", str(home / ".npm" / "_cacache"), "full", disposition=bridge.DELEGATED,
                  command=["npm", "cache", "clean", "--force"]),
             Item(4, "personal", str(a), "old", disposition=bridge.TRASH)]
    prep = bridge.prepare(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    ran, calls = [], []
    res = bridge.execute(prep, keep=raise_on(1), trash_fn=counting_trash(tmp_path, calls),
                         command_fn=lambda argv: ran.append(argv) or (0, ""), running_fn=lambda tool: False,
                         open_fn=lambda: [])
    assert ran == [] and calls == [] and res["halted"] == bridge.SETTINGS_CHANGED and a.exists()


# ---------------- 2. delegated commands honour current protections ----------------
def test_protecting_uv_cache_after_prepare_stops_the_clean(home, tmp_path):
    uv = home / ".cache" / "uv"
    uv.mkdir(parents=True)
    it = Item(4, "personal", str(uv), "full uv cache", disposition=bridge.DELEGATED,
              command=["uv", "cache", "clean", "--force"])
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    assert prep.commands == [it]
    ran = []
    res = bridge.execute(prep, keep=lambda: [str(uv)], command_fn=lambda argv: ran.append(argv) or (0, ""),
                         running_fn=lambda tool: False)
    assert ran == [] and res["delegated"] == []
    assert "keep list" in res["skipped"][0]["reason"]


@pytest.mark.parametrize("argv,kept_rel", [
    (["pnpm", "store", "prune"], "Library/pnpm/store"),
    (["ollama", "rm", "llama3:8b"], ".ollama/models"),
    (["env", "HOMEBREW_NO_AUTO_UPDATE=1", "brew", "cleanup", "-s", "--prune=all"], "Library/Caches/Homebrew/x"),
])
def test_delegated_command_refused_when_its_store_is_kept(home, tmp_path, argv, kept_rel):
    tier = 4 if argv[0] == "ollama" else 1
    it = Item(tier, "cache", "pnpm store" if argv[0] == "pnpm" else "ollama:llama3:8b", "x",
              disposition=bridge.DELEGATED, command=argv)
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    ran = []
    res = bridge.execute(prep, keep=lambda: [str(home / kept_rel)], command_fn=lambda a: ran.append(a) or (0, ""),
                         running_fn=lambda tool: False)
    assert ran == [] and "keep list" in res["skipped"][0]["reason"]


def test_unprotected_delegated_command_still_runs(home, tmp_path):
    it = Item(4, "personal", str(home / ".cache" / "uv"), "full", disposition=bridge.DELEGATED,
              command=["uv", "cache", "clean", "--force"])
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    ran = []
    res = bridge.execute(prep, keep=lambda: [str(home / "Projects")], command_fn=lambda a: ran.append(a) or (0, ""),
                         running_fn=lambda tool: False)
    assert ran == [["uv", "cache", "clean", "--force"]] and len(res["delegated"]) == 1


# ---------------- 3. full preflight at the moment of every Trash move ----------------
def test_target_opened_after_queueing_is_not_trashed(tmp_path):
    (tmp_path / "doc.txt").write_text("hello")
    m = M.build([str(tmp_path / "doc.txt")], classify_fn=fake_classify)
    reads = []

    def opened():  # nothing open at the queueing check; the file is open by the time the batch is flushed
        reads.append(1)
        return [] if len(reads) == 1 else [str(tmp_path / "doc.txt")]
    calls = []
    trash = exec_trash(tmp_path)
    r = X.execute(m, keep=[], classify_fn=fake_classify, trash_fn=lambda ps: calls.append(ps) or trash(ps),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path), open_fn=opened)
    assert len(reads) >= 2 and calls == [] and (tmp_path / "doc.txt").exists()
    assert "open in" in r["skipped"][0]["reason"]


def test_target_tracked_after_queueing_is_not_trashed(tmp_path):
    (tmp_path / "doc.txt").write_text("hello")
    m = M.build([str(tmp_path / "doc.txt")], classify_fn=fake_classify)
    seen = []

    def tracked(path):
        seen.append(path)
        return len(seen) > 1
    calls = []
    trash = exec_trash(tmp_path)
    r = X.execute(m, keep=[], classify_fn=fake_classify, trash_fn=lambda ps: calls.append(ps) or trash(ps),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path),
                  open_fn=lambda: [], tracked_fn=tracked)
    assert calls == [] and (tmp_path / "doc.txt").exists() and "tracked by git" in r["skipped"][0]["reason"]


def test_lsof_failure_at_flush_means_nothing_is_trashed(tmp_path):
    (tmp_path / "doc.txt").write_text("hello")
    m = M.build([str(tmp_path / "doc.txt")], classify_fn=fake_classify)
    reads = []
    calls = []
    trash = exec_trash(tmp_path)
    r = X.execute(m, keep=[], classify_fn=fake_classify, trash_fn=lambda ps: calls.append(ps) or trash(ps),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path),
                  open_fn=lambda: reads.append(1) or ([] if len(reads) == 1 else None))
    assert calls == [] and "can't tell which files are open" in r["skipped"][0]["reason"]


def test_flush_moves_the_rest_when_one_target_fails(tmp_path):
    for n in ("a.txt", "b.txt"):
        (tmp_path / n).write_text(n)
    m = M.build([str(tmp_path / "a.txt"), str(tmp_path / "b.txt")], classify_fn=fake_classify)
    reads = []

    def opened():
        reads.append(1)
        return [str(tmp_path / "a.txt")] if len(reads) > 2 else []
    r = X.execute(m, keep=[], classify_fn=fake_classify, trash_fn=exec_trash(tmp_path),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path), open_fn=opened)
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == ["b.txt"]
    assert [os.path.basename(x["path"]) for x in r["skipped"]] == ["a.txt"]


# ---------------- 4. no finished-file exemption for engine items ----------------
def test_dependency_folder_holding_a_video_is_refused(home, tmp_path):
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    (nm / "final.mp4").write_bytes(b"v")
    res = bridge.run([Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT)], keep=[],
                     allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                     trash_fn=counting_trash(tmp_path, []), open_fn=lambda: [])
    assert nm.exists() and (nm / "final.mp4").exists() and res["deleted"] == []
    assert "finished files" in res["skipped"][0]["reason"]


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid"))


def test_worktree_with_an_untracked_video_is_not_removed(home, tmp_path):
    from cleanup.engine.core import worktree_status
    repo = home / "Projects" / "app"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    wt = repo / ".worktrees" / "feat"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    (wt / "final.mp4").write_bytes(b"v")
    ok, why = worktree_status(str(wt))
    assert not ok and "uncommitted" in why
    res = bridge.run([Item(2, "worktree", str(wt), "idle", disposition=bridge.REMOVE)], keep=[],
                     allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                     trash_fn=counting_trash(tmp_path, []), open_fn=lambda: [])
    assert (wt / "final.mp4").exists() and res["deleted"] == []


# ---------------- finished-file rule per class ----------------
def test_expired_scratch_run_with_notes_and_models_is_cleaned(home, tmp_path):
    run_ = home / "Projects" / "app" / ".scratch" / "run1"
    run_.mkdir(parents=True)
    (run_ / "notes.md").write_text("probe notes")
    (run_ / "probe.glb").write_bytes(b"g")
    it = Item(1, ".scratch", str(run_), "expired", disposition=bridge.PERMANENT, precheck=lambda: [str(run_)])
    res = bridge.run([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                     trash_fn=counting_trash(tmp_path, []), open_fn=lambda: [])
    assert [d["path"] for d in res["deleted"]] == [str(run_)] and not run_.exists()


def test_whole_scratch_folder_has_no_extension_rule(home, tmp_path):
    sc = home / "Projects" / "app" / ".scratch"
    (sc / "run1").mkdir(parents=True)
    (sc / "run1" / "clip.mp4").write_bytes(b"v")
    tidy = Item(1, ".scratch", str(sc), "scratch (tidy)", disposition=bridge.PERMANENT)
    assert bridge._deliverable_why({"path": str(sc), "mode": "permanent", "rule": "dependency"}, tidy) is None


def test_frames_folder_holding_notes_is_kept(home, tmp_path):
    fr = home / "Projects" / "app" / "renders" / "v1"
    fr.mkdir(parents=True)
    (fr / "f.png").write_bytes(b"p")
    (fr / "readme.md").write_text("shot notes")
    t = {"path": str(fr), "mode": "trash", "rule": "frames"}
    assert "finished files" in bridge._deliverable_why(t, Item(3, "frames", str(fr), "old", disposition=bridge.TRASH))
    res = bridge.run([Item(3, "frames", str(fr), "old", disposition=bridge.TRASH)], keep=[],
                     allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                     trash_fn=counting_trash(tmp_path, []), open_fn=lambda: [])
    assert res["trashed"] == [] and (fr / "readme.md").exists()


# ================ Round 3 ================
# ---------------- R3.1 Stop is re-checked before every irreversible action ----------------
class _Stop:
    def __init__(self):
        self.flag = False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True


def test_stop_during_a_nonempty_flush_is_never_followed_by_a_permanent_delete(tmp_path, monkeypatch):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt"), str(t / "p/node_modules")], classify_fn=fake_classify)
    stop, reads, removed = _Stop(), [], []
    monkeypatch.setattr(X, "rmtree", lambda p: removed.append(p))

    def opened():  # 1, 2 = queueing checks; 3 = the flush's fresh lsof, while the user presses Stop
        reads.append(1)
        if len(reads) == 3:
            stop.set()
        return []
    r = X.execute(m, keep=[], classify_fn=fake_classify, trash_fn=exec_trash(tmp_path), stop=stop,
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path), open_fn=opened)
    assert removed == [] and r["deleted"] == [] and (t / "p/node_modules").exists()
    assert {os.path.basename(s["path"]): s["reason"] for s in r["skipped"]} == {
        "doc.txt": "stopped by you", "node_modules": "stopped by you"}


def test_stop_after_an_empty_flush_is_never_followed_by_a_permanent_delete(tmp_path, monkeypatch):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules")], classify_fn=fake_classify)
    stop, reads, removed = _Stop(), [], []
    monkeypatch.setattr(X, "rmtree", lambda p: removed.append(p))

    def keep():  # 1 = queueing check; any later read happens after it was queued: the user presses Stop then
        reads.append(1)
        if len(reads) >= 2:
            stop.set()
        return []
    r = X.execute(m, keep=keep, classify_fn=fake_classify, trash_fn=exec_trash(tmp_path), stop=stop,
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path),
                  open_fn=lambda: [])
    assert removed == [] and r["deleted"] == [] and r["skipped"][0]["reason"] == "stopped by you"


def test_stop_before_a_delegated_command_runs_nothing(home, tmp_path):
    items = [Item(4, "personal", str(home / ".cache" / "uv"), "full", disposition=bridge.DELEGATED,
                  command=["uv", "cache", "clean", "--force"]),
             Item(4, "personal", str(home / ".npm" / "_cacache"), "full", disposition=bridge.DELEGATED,
                  command=["npm", "cache", "clean", "--force"])]
    prep = bridge.prepare(items, keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    stop, ran = _Stop(), []

    def cmd(argv):
        ran.append(argv)
        stop.set()  # Stop pressed while the first command runs
        return 0, ""
    res = bridge.execute(prep, keep=[], command_fn=cmd, running_fn=lambda tool: False, stop=stop)
    assert ran == [["uv", "cache", "clean", "--force"]]
    assert [s["reason"] for s in res["skipped"]] == ["stopped by you"]


# ---------------- R3.2 delegated stores resolve to the tool's effective location ----------------
def test_uv_store_follows_xdg_cache_home(home, tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(bridge, "_uv_cache_dir", lambda: None)  # uv not installed: env and defaults decide
    it = Item(4, "personal", str(home / ".cache" / "uv"), "full", disposition=bridge.DELEGATED,
              command=["uv", "cache", "clean", "--force"])
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    ran = []
    res = bridge.execute(prep, keep=lambda: [str(xdg / "uv")], command_fn=lambda a: ran.append(a) or (0, ""),
                         running_fn=lambda tool: False)
    assert ran == [] and "keep list" in res["skipped"][0]["reason"]


def test_uv_store_prefers_uvs_own_answer(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "custom-uv"
    monkeypatch.setattr(bridge, "_uv_cache_dir", lambda: str(elsewhere))
    assert str(elsewhere) in bridge.command_paths(
        Item(4, "personal", "x", "x", disposition=bridge.DELEGATED, command=["uv", "cache", "clean", "--force"]),
        str(home))


def test_uv_store_unknown_refuses_the_command(home, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_uv_cache_dir", lambda: bridge.UNRESOLVED)  # uv is installed but can't say
    it = Item(4, "personal", str(home / ".cache" / "uv"), "full", disposition=bridge.DELEGATED,
              command=["uv", "cache", "clean", "--force"])
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    ran = []
    res = bridge.execute(prep, keep=lambda: [str(home / "Projects")], command_fn=lambda a: ran.append(a) or (0, ""),
                         running_fn=lambda tool: False)
    assert ran == [] and "can't tell where" in res["skipped"][0]["reason"]


@pytest.mark.parametrize("tool,argv,var,sub", [
    ("npm", ["npm", "cache", "clean", "--force"], "NPM_CONFIG_CACHE", ""),
    ("npm", ["npm", "cache", "clean", "--force"], "npm_config_cache", ""),
    ("brew", ["env", "HOMEBREW_NO_AUTO_UPDATE=1", "brew", "cleanup", "-s", "--prune=all"], "HOMEBREW_CACHE", ""),
    ("pnpm", ["pnpm", "store", "prune"], "PNPM_STORE_DIR", ""),
    ("pnpm", ["pnpm", "store", "prune"], "XDG_DATA_HOME", "pnpm"),
    ("ollama", ["ollama", "rm", "llama3:8b"], "OLLAMA_MODELS", ""),
])
def test_store_env_overrides_are_honoured(home, tmp_path, monkeypatch, tool, argv, var, sub):
    loc = tmp_path / "moved"
    monkeypatch.setenv(var, str(loc))
    paths = bridge.command_paths(Item(4, "x", "x", "x", disposition=bridge.DELEGATED, command=argv), str(home))
    assert os.path.normpath(str(loc / sub)) in [os.path.normpath(p) for p in paths]


def test_relative_store_override_refuses(home, monkeypatch):
    monkeypatch.setenv("HOMEBREW_CACHE", "relative/cache")
    it = Item(1, "cache", "x", "x", disposition=bridge.DELEGATED,
              command=["env", "HOMEBREW_NO_AUTO_UPDATE=1", "brew", "cleanup", "-s", "--prune=all"])
    assert bridge.command_paths(it, str(home)) is None


# ---------------- R3.3 prechecks run fresh at dispatch ----------------
def test_precheck_turning_unsafe_after_queueing_keeps_target_out_of_the_trash(home, tmp_path):
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir()
    f.write_text("zip")
    calls = []

    def precheck():  # prepare = 1, queueing check = 2, the flush right before the move = 3 (now unsafe)
        calls.append(1)
        return [str(f)] if len(calls) <= 2 else []
    it = Item(4, "personal", str(f), "old", disposition=bridge.TRASH, precheck=precheck)
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    moved = []
    res = bridge.execute(prep, keep=[], trash_fn=counting_trash(tmp_path, moved), open_fn=lambda: [])
    assert len(calls) >= 3 and moved == [] and f.exists()
    assert "changed since review" in res["skipped"][0]["reason"]


def test_precheck_turning_unsafe_before_a_permanent_delete_stops_it(home, tmp_path, monkeypatch):
    from cleanup.plan import executor
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    calls, removed = [], []
    monkeypatch.setattr(executor, "rmtree", lambda p: removed.append(p))

    def precheck():  # prepare = 1, queueing check = 2, the last look before the delete = 3 (now unsafe)
        calls.append(1)
        return [str(nm)] if len(calls) <= 2 else []
    it = Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT, precheck=precheck)
    prep = bridge.prepare([it], keep=[], allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home))
    res = bridge.execute(prep, keep=[], trash_fn=counting_trash(tmp_path, []), open_fn=lambda: [])
    assert removed == [] and res["deleted"] == [] and "changed since review" in res["skipped"][0]["reason"]


# ---------------- R3.4 finished-file policy follows the engine item's class ----------------
def test_personal_trash_item_classified_as_cache_still_keeps_notes_and_models(home, tmp_path):
    hf = home / ".cache" / "huggingface"
    (hf / "hub").mkdir(parents=True)
    (hf / "hub" / "notes.md").write_text("my notes")
    (hf / "hub" / "character.glb").write_bytes(b"g")
    assert bridge.classify(str(hf), keep=[], home=str(home))["rule"] == "tool-cache"
    moved = []
    res = bridge.run([Item(4, "personal", str(hf), "HuggingFace models", disposition=bridge.TRASH)], keep=[],
                     allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
                     trash_fn=counting_trash(tmp_path, moved), open_fn=lambda: [])
    assert moved == [] and hf.exists() and "finished files" in res["skipped"][0]["reason"]


def test_permanent_claim_on_a_non_rebuildable_folder_gets_the_full_list(home, tmp_path):
    d = home / "Projects" / "app" / "stuff"
    d.mkdir(parents=True)
    (d / "notes.md").write_text("n")
    it = Item(1, "node_modules", str(d), "wrong", disposition=bridge.PERMANENT)
    t = {"path": str(d), "mode": "trash", "rule": "unmatched"}
    assert "finished files" in bridge._deliverable_why(t, it)


# ================ Round 4: one last-moment gate before every irreversible call ================
class _Injector:
    """Counts every hook call in a run (keep reads, Stop reads, lsof, git, prechecks, classify, running checks, the
    uv store lookup); at call number `at` it presses Stop or protects every target, then lets that call answer."""

    def __init__(self, kind, at, protect):
        self.kind, self.at, self.protect = kind, at, protect
        self.clock, self.calls, self.injected, self.effects = 0, 0, None, []
        self.flag, self.keep = False, []

    def hook(self, name="?"):
        self.clock += 1
        if name in ("realpath", "lstat") and self.kind == "keep" and getattr(self, "gate_keep_read", False):
            # Inside the gate, after its settings read: normalising the keep entries just read. A keep entry added
            # now lands after the settings read: the same irreducible boundary as below.
            return
        if name == "stop" and self.kind == "keep":
            # Reading the Stop flag is a plain memory read (no I/O). A keep entry added "during" the gate's final Stop
            # read lands after the gate read the settings: no finite sequence of reads can see it, so it's the
            # boundary, not a hook point. Every other read is one.
            return
        self.names = getattr(self, "names", []) + [name]
        self.calls += 1
        if self.calls == self.at and self.injected is None:
            self.injected = self.clock
            if self.kind == "stop":
                self.flag = True
            else:
                self.keep = list(self.protect)

    def effect(self, name):
        self.clock += 1
        self.effects.append((self.clock, name))


def _gate_run(home, tmp_path, monkeypatch, kind, at, gate_io=False, unrelated=False):
    from cleanup.engine.classify import classify as real_classify
    from cleanup.plan import executor
    f = home / "Downloads" / "old.zip"
    f.parent.mkdir(exist_ok=True)
    f.write_text("zip")
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True, exist_ok=True)
    wt = home / "Projects" / "app" / ".worktrees" / "feat"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text("gitdir: /nowhere\n")
    uv = home / ".cache" / "uv"
    uv.mkdir(parents=True, exist_ok=True)
    base_keep = [str(home / "Unrelated")] if unrelated else []
    inj = _Injector(kind, at, base_keep + [str(f), str(nm), str(wt), str(uv)])
    inj.keep = list(base_keep)
    if gate_io:  # the gate's own filesystem calls become hook points too
        real_gate, real_rp, real_lstat = executor.last_gate, os.path.realpath, os.lstat

        def gate(*a, **kw):
            inj.in_gate, inj.gate_keep_read = True, False
            try:
                return real_gate(*a, **kw)
            finally:
                inj.in_gate = inj.gate_keep_read = False

        def rp(x, *a, **kw):
            if getattr(inj, "in_gate", False):
                inj.hook("realpath")
            return real_rp(x, *a, **kw)

        def lst(x, *a, **kw):
            if getattr(inj, "in_gate", False):
                inj.hook("lstat")
            return real_lstat(x, *a, **kw)
        monkeypatch.setattr(executor, "last_gate", gate)
        monkeypatch.setattr(bridge, "last_gate", gate)
        monkeypatch.setattr(os.path, "realpath", rp)
        monkeypatch.setattr(os, "lstat", lst)

    class Stop:
        def is_set(self):
            inj.hook("stop")
            return inj.flag

    def keep():
        inj.hook("keep")
        if getattr(inj, "in_gate", False):
            inj.gate_keep_read = True
        return list(inj.keep)

    def precheck_for(p):
        def pc():
            inj.hook("precheck")
            return [p]
        return pc

    def classify_fn(path, **kw):
        inj.hook("classify")
        return real_classify(path, **kw)

    class Tracked(executor.TrackedCheck):
        def __call__(self, path):
            inj.hook("git")
            return False

    def uv_dir():
        inj.hook("uv cache dir")
        return str(uv)

    def opened():
        inj.hook("lsof")
        return []

    def running(tool):
        inj.hook("running")
        return False

    move = bridge.test_trash(str(tmp_path / "trashroot"))
    monkeypatch.setattr(executor, "TrackedCheck", Tracked)
    monkeypatch.setattr(executor, "rmtree", lambda p: inj.effect("rmtree"))
    monkeypatch.setattr(executor, "remove_worktree", lambda p: inj.effect("remove_worktree"))
    monkeypatch.setattr(bridge, "_uv_cache_dir", uv_dir)
    items = [Item(1, "cache", str(uv), "prune", disposition=bridge.DELEGATED,
                  command=["env", "UV_LOCK_TIMEOUT=10", "uv", "cache", "prune"]),
             Item(4, "personal", str(uv), "full", disposition=bridge.DELEGATED, command=["uv", "cache", "clean", "--force"]),
             Item(1, "node_modules", str(nm), "idle", disposition=bridge.PERMANENT, precheck=precheck_for(str(nm))),
             Item(2, "worktree", str(wt), "idle", disposition=bridge.REMOVE),
             Item(4, "personal", str(f), "old", disposition=bridge.TRASH, precheck=precheck_for(str(f)))]
    bridge.run(items, keep=keep, allowed_roots=[str(home)], state=str(tmp_path / "state"), home=str(home),
               classify_fn=classify_fn, trash_fn=lambda ps: inj.effect("trash") or move(ps),
               command_fn=lambda argv: inj.effect("command") or (0, ""), running_fn=running, open_fn=opened,
               stop=Stop())
    return inj


@pytest.mark.parametrize("kind", ["stop", "keep"])
def test_no_side_effect_after_stop_or_a_new_keep_entry_at_any_hook(home, tmp_path, monkeypatch, kind):
    base = _gate_run(home, tmp_path / "base", monkeypatch, kind, at=0)  # no injection: every effect happens
    assert sorted({n for _, n in base.effects}) == ["command", "remove_worktree", "rmtree", "trash"]
    assert base.calls > 20
    for at in range(1, base.calls + 1):
        h = tmp_path / f"h{at}"
        monkeypatch.setenv("HOME", str(h))
        (h / "Projects").mkdir(parents=True)
        inj = _gate_run(h, tmp_path / f"t{at}", monkeypatch, kind, at)
        late = [n for c, n in inj.effects if inj.injected is not None and c > inj.injected]
        assert late == [], f"{kind} injected at hook call {at} ({inj.names[at - 1]}): {late} still ran"


@pytest.mark.parametrize("kind", ["stop", "keep"])
def test_gate_io_never_hides_stop_or_a_new_keep_entry(home, tmp_path, monkeypatch, kind):
    # Round 5: start with an unrelated keep entry (so the gate does its realpath work) and make every realpath/lstat
    # inside the gate a hook point as well.
    base = _gate_run(home, tmp_path / "base", monkeypatch, kind, at=0, gate_io=True, unrelated=True)
    assert sorted({n for _, n in base.effects}) == ["command", "remove_worktree", "rmtree", "trash"]
    assert {"realpath", "lstat"} <= set(base.names)
    for at in range(1, base.calls + 1):
        monkeypatch.undo()
        h = tmp_path / f"h{at}"
        monkeypatch.setenv("HOME", str(h))
        monkeypatch.setattr(bridge, "_uv_cache_dir", lambda: None)
        (h / "Projects").mkdir(parents=True)
        inj = _gate_run(h, tmp_path / f"t{at}", monkeypatch, kind, at, gate_io=True, unrelated=True)
        late = [n for c, n in inj.effects if inj.injected is not None and c > inj.injected]
        assert late == [], f"{kind} injected at hook call {at} ({inj.names[at - 1]}): {late} still ran"
