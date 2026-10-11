import json
import sys
import os
import shutil
import time

import pytest

from cleanup.plan import executor as X
from cleanup.plan import manifest as M
from tests.test_manifest import fake_classify, tree


def fake_trash(tmp):
    bin_ = tmp / ".Trash"; bin_.mkdir(exist_ok=True)
    def trash(paths):
        out = {}
        for p in paths:
            dst = bin_ / os.path.basename(p)
            shutil.move(p, dst); out[p] = str(dst)
        return out
    return trash


def run(tmp, paths, **kw):
    m = M.build(paths, classify_fn=fake_classify)
    j = tmp / "journal.jsonl"
    kw.setdefault("allowed_roots", [str(tmp)])
    return m, j, X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp), journal=str(j), **kw)


def test_smart_split_executes(tmp_path):
    t = tree(tmp_path)
    m, j, r = run(tmp_path, [str(t / "p/node_modules"), str(t / "doc.txt")])
    assert not (t / "p/node_modules").exists() and not (t / "doc.txt").exists()
    assert [os.path.basename(x["path"]) for x in r["deleted"]] == ["node_modules"]
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == ["doc.txt"]
    assert (tmp_path / ".Trash/doc.txt").exists()


def test_revalidation_skips_changed_targets(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules"), str(t / "doc.txt"), str(t / "p/renders")], classify_fn=fake_classify)
    time.sleep(0.02)
    (t / "p/node_modules/new.js").write_text("added after review")          # folder mtime changes
    os.remove(t / "doc.txt"); (t / "doc.txt").write_text("replaced")          # inode changes
    shutil.rmtree(t / "p/renders/v1"); os.symlink(t / "p/renders/master", t / "p/renders/v1")  # swapped for a symlink
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["deleted"] == [] and r["trashed"] == []
    reasons = " | ".join(x["reason"] for x in r["skipped"])
    assert "changed since review" in reasons and "replaced" in reasons
    assert (t / "p/node_modules").exists() and (t / "p/renders/master/f.png").exists()


def test_permanent_requires_regenerable_at_execute_time(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules")], classify_fn=fake_classify)
    def now_review(path, **k):
        return {"class": "review", "action": "trash", "rule": "x", "reason": "changed rules"}
    r = X.execute(m, classify_fn=now_review, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["deleted"] == [] and "verdict changed" in r["skipped"][0]["reason"]


def test_keep_list_added_after_review_wins(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    r = X.execute(m, keep=[str(t)], classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["trashed"] == [] and "keep list" in r["skipped"][0]["reason"]


def test_journal_written_before_result(tmp_path):
    t = tree(tmp_path)
    m, j, r = run(tmp_path, [str(t / "p/node_modules"), str(t / "doc.txt")])
    lines = [json.loads(l) for l in j.read_text().splitlines()]
    order = [os.path.basename(x["path"]) for x in M.targets(m)]  # actions follow manifest order
    got = [(os.path.basename(l["path"]), l["result"]) for l in lines if l.get("event") == "action"]
    assert got == [(n, "deleted" if n == "node_modules" else "trashed") for n in order]
    assert lines[0]["event"] == "start" and lines[-1]["event"] == "end"


def test_stop_halts_remaining(tmp_path):
    import threading
    t = tree(tmp_path)
    stop = threading.Event(); stop.set()
    m, j, r = run(tmp_path, [str(t / "p/node_modules"), str(t / "doc.txt")], stop=stop)
    assert r["deleted"] == [] and r["trashed"] == [] and (t / "doc.txt").exists()


# ---------------- final-review regressions ----------------
def test_symlink_is_never_trashed_or_followed(tmp_path):
    # Review C1: Finder resolves aliases — a staged link must never send its *target* to the Trash.
    t = tree(tmp_path)
    os.symlink(t / "p/renders", t / "shortcut")
    m, j, r = run(tmp_path, [str(t / "shortcut")])
    assert r["trashed"] == [] and r["deleted"] == []
    assert "symlink" in r["skipped"][0]["reason"]
    assert (t / "p/renders/master/f.png").exists() and os.path.islink(t / "shortcut")


def test_trash_target_rechecked_right_before_finder(tmp_path):
    # Review I2: a trash target replaced after its validation but before the Finder call must be skipped.
    t = tree(tmp_path)
    (t / "b.txt").write_text("b")
    m = M.build([str(t / "doc.txt"), str(t / "b.txt")], classify_fn=fake_classify)

    def sneaky(path, **k):
        if path.endswith("b.txt"):
            os.remove(t / "doc.txt"); (t / "doc.txt").write_text("a different file now")
        return fake_classify(path, **k)
    r = X.execute(m, classify_fn=sneaky, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == ["b.txt"]
    assert (t / "doc.txt").read_text() == "a different file now"


def test_file_rewritten_in_place_is_skipped(tmp_path):
    # Review I3: same inode, new content → changed since review.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    time.sleep(0.02)
    with open(t / "doc.txt", "a") as f:
        f.write(" — edited after review")
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["trashed"] == [] and "changed since review" in r["skipped"][0]["reason"]


def test_deep_change_inside_permanent_folder_is_skipped(tmp_path):
    # Review I4: a file saved deep inside node_modules after review must stop the permanent delete.
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules")], classify_fn=fake_classify)
    (t / "p/node_modules/x/new.js").write_bytes(b"z" * 20000)   # only x's mtime changes
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["deleted"] == [] and "changed since review" in r["skipped"][0]["reason"]
    assert (t / "p/node_modules").exists()


def test_permanent_delete_stays_inside_allowed_roots(tmp_path):
    # Review I7: permanent deletes only inside the allowed folders (the user's home).
    t = tree(tmp_path)
    other = tmp_path / "elsewhere"; other.mkdir()
    m, j, r = run(tmp_path, [str(t / "p/node_modules")], allowed_roots=[str(other)])
    assert r["deleted"] == [] and "outside" in r["skipped"][0]["reason"]


def test_partial_finder_batch_is_recorded_honestly(tmp_path):
    # Review I10: items Finder moved before failing must be journaled as trashed, not failed.
    t = tree(tmp_path)
    (t / "b.txt").write_text("b")
    bin_ = tmp_path / ".Trash"; bin_.mkdir()

    moved = []

    def dies_halfway(paths):
        shutil.move(paths[0], bin_ / os.path.basename(paths[0])); moved.append(os.path.basename(paths[0]))
        raise RuntimeError("osascript killed")
    m = M.build([str(t / "doc.txt"), str(t / "b.txt")], classify_fn=fake_classify)
    j = tmp_path / "j.jsonl"
    r = X.execute(m, classify_fn=fake_classify, trash_fn=dies_halfway, journal=str(j), allowed_roots=[str(tmp_path)])
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == moved
    assert [os.path.basename(x["path"]) for x in r["failed"]] == [n for n in ("b.txt", "doc.txt") if n not in moved]
    assert any(json.loads(l).get("event") == "attempt" for l in j.read_text().splitlines())


def test_trash_output_parsing_survives_newlines(monkeypatch):
    # Review I9: a newline in a name must not shift every following trash path.
    import types
    from cleanup.plan import trash as T
    names = ["/u/a\nb.txt", "/u/c.txt"]
    out = "\x1e".join(["/Users/u/.Trash/a\nb.txt", "/Users/u/.Trash/c.txt"])
    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr=""))
    assert T.move_to_trash(names) == {"/u/a\nb.txt": "/Users/u/.Trash/a\nb.txt", "/u/c.txt": "/Users/u/.Trash/c.txt"}


# ---------------- Codex deletion-path review ----------------
def test_permanent_folder_with_protected_descendant_is_skipped(tmp_path):
    # Codex C2: a regenerable folder that contains a git repo (or a kept path) is never wiped wholesale.
    t = tree(tmp_path)
    (t / "p/node_modules/x/repo/.git").mkdir(parents=True)
    m, j, r = run(tmp_path, [str(t / "p/node_modules")])
    assert r["deleted"] == [] and "contains" in r["skipped"][0]["reason"]
    assert (t / "p/node_modules/x/repo/.git").exists()


def test_keep_list_is_reloaded_per_action(tmp_path):
    # Codex C3: Protect clicked while a cleanup runs must still win.
    t = tree(tmp_path)
    (t / "b.txt").write_text("b")
    m = M.build([str(t / "b.txt"), str(t / "doc.txt")], classify_fn=fake_classify)
    keep = []

    def keep_now():
        keep.append(str(t / "doc.txt"))  # protected right after the first check
        return list(keep)
    r = X.execute(m, keep=keep_now, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)])
    assert "doc.txt" not in [os.path.basename(x["path"]) for x in r["trashed"]]
    assert (t / "doc.txt").exists()


def test_verdict_drift_is_skipped(tmp_path):
    # Codex I7: the full verdict recorded at review must still hold at execute time.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    def now_cloud(path, **k):
        return {"class": "review", "action": "none", "rule": "cloud", "reason": "synced"}
    r = X.execute(m, classify_fn=now_cloud, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["trashed"] == [] and "verdict changed" in r["skipped"][0]["reason"]


def test_relative_or_bogus_trash_result_is_not_trusted(tmp_path):
    # Codex I11: a malformed Finder answer must never become an undo source.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    def weird(paths):
        os.remove(paths[0]); return {paths[0]: "b"}
    r = X.execute(m, classify_fn=fake_classify, trash_fn=weird, journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["trashed"][0]["trash_path"] is None


# ---------------- user overrides at execution ----------------
def test_forced_item_ignores_rules_but_not_identity(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/renders/v2"), str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "p/renders/v2"): "trash", str(t / "doc.txt"): "trash"},
                          keep=[], home=str(tmp_path))
    with open(t / "doc.txt", "a") as f:
        f.write(" changed")          # identity check still applies to forced items
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == ["v2"]
    assert "changed since review" in r["skipped"][0]["reason"]
    log = [json.loads(l) for l in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert any(l.get("override") and l["path"].endswith("v2") for l in log if l.get("event") == "action")


def test_forced_still_blocked_by_keep_list_and_roots(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "doc.txt"): "permanent"}, keep=[], home=str(tmp_path))
    r = X.execute(m, keep=[str(t / "doc.txt")], classify_fn=fake_classify, trash_fn=fake_trash(tmp_path),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert r["deleted"] == [] and "keep list" in r["skipped"][0]["reason"]
    r2 = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j2.jsonl"),
                   allowed_roots=[str(tmp_path / "elsewhere")], home=str(tmp_path))
    assert r2["deleted"] == [] and "outside" in r2["skipped"][0]["reason"]


def test_stop_does_not_trash_the_pending_batch(tmp_path):
    # Review I4: after Stop, nothing else moves — including trash items already queued for Finder.
    import threading
    t = tree(tmp_path)
    m = M.build([str(t / "p/renders/v2"), str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "p/renders/v2"): "trash", str(t / "doc.txt"): "trash"},
                          keep=[], home=str(tmp_path))
    stop = threading.Event()
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path), stop=stop, progress=lambda e: stop.set())
    assert r["trashed"] == [] and (t / "doc.txt").exists() and (t / "p/renders/v2").exists()
    assert all("stopped" in s["reason"] for s in r["skipped"]) and len(r["skipped"]) == 2


def test_forced_trash_respects_allowed_roots(tmp_path):
    # Review I1: forced TRASH re-checks the allowed roots too, not only permanent.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "doc.txt"): "trash"}, keep=[], home=str(tmp_path))
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path / "elsewhere")], home=str(tmp_path))
    assert r["trashed"] == [] and "outside" in r["skipped"][0]["reason"]


def test_failed_forced_action_is_journaled_as_override(tmp_path):
    # Review I5: every journal line about a forced item says so — including failures and the start summary.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "doc.txt"): "trash"}, keep=[], home=str(tmp_path))
    X.execute(m, classify_fn=fake_classify, trash_fn=lambda paths: {p: "ERR:Trash said no" for p in paths},
              journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path))
    log = [json.loads(l) for l in (tmp_path / "j.jsonl").read_text().splitlines()]
    fail = [l for l in log if l.get("result") == "failed"][0]
    assert fail["override"] is True and fail["rule_reason"]
    assert any(l["event"] == "override" and l["path"].endswith("doc.txt") for l in log)
    assert (t / "doc.txt").exists()


def test_forced_permanent_target_is_never_executed(tmp_path):
    # Final review MUST-FIX 1: even a hand-made manifest with a forced permanent target never deletes it.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    m["items"][0].update(mode="permanent", forced=True, rule_reason="personal")
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert r["deleted"] == [] and r["trashed"] == [] and (t / "doc.txt").exists()
    assert "Trash" in r["skipped"][0]["reason"]


def test_forced_trash_still_refused_when_the_rules_say_protected(tmp_path):
    # Final review MUST-FIX 1: the executor runs the classify check for forced items too.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    m = M.apply_overrides(m, {}, forced={str(t / "doc.txt"): "trash"}, keep=[], home=str(tmp_path))
    protected = lambda path, **k: {"class": "protected", "action": "none", "rule": "system", "reason": "system"}
    r = X.execute(m, classify_fn=protected, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert r["trashed"] == [] and (t / "doc.txt").exists() and "protected" in r["skipped"][0]["reason"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Trash")
def test_move_to_trash_script_runs_and_reports_errors(tmp_path):
    # Runs the real NSFileManager script without touching the Trash: a missing path comes back as an ERR record.
    from cleanup.plan import trash as T
    gone = [str(tmp_path / "missing one"), str(tmp_path / "missing\ntwo")]
    out = T.move_to_trash(gone)
    assert set(out) == set(gone) and all(v.startswith("ERR:") and "Trash call did not answer" not in v for v in out.values())


@pytest.mark.skipif(sys.platform != "darwin" or not os.environ.get("CLEANUP_REAL_TRASH_TEST"),
                    reason="moves files to the real Trash: set CLEANUP_REAL_TRASH_TEST=1")
def test_move_to_trash_is_real_and_reports_landing_path(tmp_path):
    # Moves via NSFileManager (silent, no Finder): each item lands in a Trash folder and is left there.
    from cleanup.plan import trash as T
    f = tmp_path / "cleanup-trash-test.txt"; f.write_text("x")
    d = tmp_path / "cleanup trash dir"; (d / "sub").mkdir(parents=True)
    out = T.move_to_trash([str(f), str(d)])
    assert not f.exists() and not d.exists()
    assert "/.Trash" in out[str(f)] and "/.Trash" in out[str(d)]


def test_settings_that_break_mid_run_stop_the_rest(tmp_path):
    # Final review MUST-FIX 4: keep is re-read before every action; if it can't be read, nothing more is cleaned.
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules"), str(t / "doc.txt")], classify_fn=fake_classify)
    calls = []

    def keep():
        calls.append(1)
        if len(calls) > 1:
            raise ValueError("settings became invalid")
        return []
    r = X.execute(m, keep=keep, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path),
                  journal=str(tmp_path / "j.jsonl"), allowed_roots=[str(tmp_path)], home=str(tmp_path))
    # doc.txt (first, queued for the Trash) and node_modules: neither moves once the settings can't be read
    assert r["deleted"] == [] and r["trashed"] == [] and (t / "doc.txt").exists() and (t / "p/node_modules").exists()
    assert [s["reason"] for s in r["skipped"]] == [X.SETTINGS_CHANGED] * 2 and r["halted"] == X.SETTINGS_CHANGED


# ---------------- final review MUST-FIX 5: universal preflight ----------------
def _git(repo, *args):
    import subprocess
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_tracked_file_and_folder_with_tracked_files_are_refused(tmp_path):
    # The review's probe: a tracked final_cut.mp4 was trashed.
    repo = tmp_path / "film"
    (repo / "cuts").mkdir(parents=True)
    (repo / "cuts" / "final_cut.mp4").write_bytes(b"v" * 100)
    (repo / "loose.txt").write_text("untracked")
    _git(repo, "init", "-q")
    _git(repo, "add", "cuts/final_cut.mp4")
    _git(repo, "commit", "-q", "-m", "cut")
    m = M.build([str(repo / "cuts" / "final_cut.mp4"), str(repo / "cuts"), str(repo / "loose.txt")],
                classify_fn=fake_classify)
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert (repo / "cuts" / "final_cut.mp4").exists()
    assert [os.path.basename(x["path"]) for x in r["trashed"]] == ["loose.txt"]
    assert any("tracked by git" in s["reason"] for s in r["skipped"])


def test_folder_holding_a_nested_repo_is_refused(tmp_path):
    d = tmp_path / "stuff"
    (d / "inner").mkdir(parents=True)
    _git(d / "inner", "init", "-q")
    m = M.build([str(d)], classify_fn=fake_classify)
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path))
    assert d.exists() and r["trashed"] == [] and "git repository" in r["skipped"][0]["reason"]


def test_open_files_are_refused_and_unknown_means_in_use(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt"), str(t / "p/node_modules")], classify_fn=fake_classify)
    r = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)], home=str(tmp_path),
                  open_fn=lambda: [str(t / "doc.txt"), str(t / "p/node_modules/x/a.js")])
    assert r["deleted"] == [] and r["trashed"] == [] and all("open in" in s["reason"] for s in r["skipped"])
    r2 = X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp_path), journal=str(tmp_path / "j2.jsonl"),
                   allowed_roots=[str(tmp_path)], home=str(tmp_path), open_fn=lambda: None)
    assert r2["deleted"] == [] and r2["trashed"] == [] and len(r2["skipped"]) == 2


def test_failed_lsof_with_partial_output_means_everything_in_use(monkeypatch):
    import subprocess
    from cleanup.engine import leftovers as L
    monkeypatch.setattr(L, "_OPEN_CACHE", [0.0, None])
    monkeypatch.setattr(L.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "p1\nn/some/file\n", ""))
    assert L.open_paths(max_age=0) is None
