"""Regressions for an independent full-app review."""
import json
import os
import threading

import pytest

from cleanup.agent import runner as R
from cleanup.engine.classify import classify
from cleanup.engine.core import kept
from cleanup.plan import executor as X
from cleanup.plan import manifest as M
from cleanup.plan.undo import undo
from tests.test_executor import fake_trash
from tests.test_manifest import fake_classify, tree


def ex(m, tmp, **kw):
    kw.setdefault("allowed_roots", [str(tmp)])
    return X.execute(m, classify_fn=fake_classify, trash_fn=fake_trash(tmp), journal=str(tmp / "j.jsonl"), **kw)


# C2 — ordinary (rule-based) actions get the hard floor too
def test_ordinary_trash_outside_allowed_roots_is_refused(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    r = ex(m, tmp_path, allowed_roots=[str(tmp_path / "elsewhere")])
    assert r["trashed"] == [] and "outside" in r["skipped"][0]["reason"] and (t / "doc.txt").exists()


def test_home_itself_is_never_trashed(tmp_path):
    home = tmp_path / "h"; tree(home)
    m = M.build([str(home)], classify_fn=fake_classify)
    r = ex(m, tmp_path, home=str(home))
    assert r["trashed"] == [] and home.exists()


def test_app_internals_protected_anywhere(tmp_path):
    home = tmp_path / "h"; nm = home / "Downloads/Foo.APP/Contents/node_modules"; nm.mkdir(parents=True)
    assert classify(str(nm), home=str(home))["class"] == "protected"


# C3 — a parent of protected account data can't be forced
def test_parent_of_account_data_is_below_the_floor(tmp_path):
    h = tmp_path / "h"
    (h / "Library/Application Support/MobileSync").mkdir(parents=True); (h / "Library/Caches/x").mkdir(parents=True)
    assert M.override_floor(str(h / "Library/Application Support"), keep=(), home=str(h))
    assert M.override_floor(str(h / "Library/Caches/x"), keep=(), home=str(h)) is None


# C4 — a permanent folder whose contents were swapped (same sizes) is refused
def test_permanent_folder_with_swapped_contents_is_refused(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules")], classify_fn=fake_classify)
    new = t / "p/node_modules/x/new.js"; new.write_bytes(b"z" * 5000)
    os.replace(new, t / "p/node_modules/x/a.js")          # same size, different file
    r = ex(m, tmp_path)
    assert r["deleted"] == [] and (t / "p/node_modules").exists()


# C5 — Unicode-equivalent names can't dodge the keep list
def test_keep_list_matches_nfd_and_nfc():
    assert kept("/tmp/x/café/a", ["/tmp/x/café"])


# C7 — no git in the agent allowlist (core.fsmonitor runs repo-configured programs)
def test_agent_allowlist_has_no_git():
    assert not any("git" in t for t in R.ALLOWED_TOOLS)


# I1 — Stop / Protect right before the final Trash batch still win
def test_stop_after_last_item_blocks_final_batch(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    stop = threading.Event()
    r = ex(m, tmp_path, stop=stop, progress=lambda e: stop.set())
    assert r["trashed"] == [] and (t / "doc.txt").exists()


def test_protect_after_queueing_blocks_trash(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    keep = []
    r = ex(m, tmp_path, keep=lambda: keep, progress=lambda e: keep.append(str(t / "doc.txt")))
    assert r["trashed"] == [] and (t / "doc.txt").exists()


# I2 — an attempt record is on disk before every irreversible step
def test_attempt_journaled_before_permanent_delete(tmp_path, monkeypatch):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules")], classify_fn=fake_classify)
    seen = []

    def spy(p):
        seen.append([json.loads(l) for l in (tmp_path / "j.jsonl").read_text().splitlines()])
    monkeypatch.setattr(X, "rmtree", spy)
    ex(m, tmp_path)
    assert any(e.get("event") == "attempt" and e.get("path") == str(t / "p/node_modules") for e in seen[0])


# I3 — undo only moves back the object CleanUp trashed
def test_undo_refuses_a_different_file_with_the_same_trash_name(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    r = ex(m, tmp_path)
    tp = r["trashed"][0]["trash_path"]
    os.remove(tp); open(tp, "w").write("someone else's file")      # Trash emptied, another doc.txt trashed later
    u = undo(str(tmp_path / "j.jsonl"))
    assert u["restored"] == [] and not (t / "doc.txt").exists()


# I4 — the server's agent runs use the process-group runner
def test_server_agent_uses_run_group(tmp_path):
    from cleanup.scanner.store import Store
    from cleanup.server.app import App
    assert App.__init__.__code__.co_names.count("run_group") >= 1


# minor — a compressed file with another hard link frees nothing
def test_compressed_hard_link_is_zero(tmp_path):
    import subprocess
    from cleanup.engine import apfs
    src = tmp_path / "s.txt"; src.write_bytes(b"compress me " * 200_000)
    dst = tmp_path / "d.txt"; subprocess.run(["ditto", "--hfsCompression", str(src), str(dst)], check=True)
    if not os.lstat(dst).st_flags & 0x20:
        pytest.skip("not compressed")
    os.link(dst, tmp_path / "d2.txt")
    assert apfs.private_size(str(dst)) == 0


# Codex re-check: C4 partial — contents changed during the preceding Trash batch
def test_deep_change_during_trash_batch_blocks_permanent(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt"), str(t / "p/node_modules")], classify_fn=fake_classify)
    inner = fake_trash(tmp_path)

    def trash_and_tamper(paths):
        out = inner(paths)
        new = t / "p/node_modules/x/new.js"; new.write_bytes(b"z" * 5000); os.replace(new, t / "p/node_modules/x/a.js")
        return out
    r = X.execute(m, classify_fn=fake_classify, trash_fn=trash_and_tamper, journal=str(tmp_path / "j.jsonl"),
                  allowed_roots=[str(tmp_path)])
    assert r["deleted"] == [] and (t / "p/node_modules").exists()


# Codex re-check: I3 partial — journals from before identity tracking can't prove what's in the Trash
def test_undo_refuses_legacy_entries_without_identity(tmp_path):
    t = tree(tmp_path)
    (tmp_path / ".Trash").mkdir(); tp = tmp_path / ".Trash/doc.txt"; tp.write_text("something")
    j = tmp_path / "old.jsonl"
    j.write_text(json.dumps({"event": "action", "path": str(t / "restored.txt"), "mode": "trash", "result": "trashed",
                             "trash_path": str(tp)}) + "\n")
    u = undo(str(j))
    assert u["restored"] == [] and len(u["refused"]) == 1 and tp.exists()
