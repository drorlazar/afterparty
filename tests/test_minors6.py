import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from cleanup.agent import runner as R
from cleanup.agent import schema as A

WEB = Path(__file__).resolve().parents[1] / "web/js"


def test_agent_unreadable_folder_is_a_card(tmp_path):
    ctx = {"path": str(tmp_path / "gone"), "kind": "dir", "size": 0, "rules": {}, "top_children": [], "names": [],
           "names_truncated": False}
    r = R.ask(ctx, run_fn=lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("cwd")))
    assert r["ok"] is False and "can't open that folder" in r["error"]


def test_sanitize_under_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"; (real / "proj/build").mkdir(parents=True)
    os.symlink(real, tmp_path / "alias")
    t = str(tmp_path / "alias/proj")
    ans = {"verdict": "review", "confidence": 1, "summary": "", "evidence": [],
           "proposed": [{"path": t + "/build", "action": "delete", "reason": "x"}]}
    assert [p["path"] for p in A.sanitize(ans, t)["proposed"]] == [t + "/build"]


def test_agent_refresh_link_prevents_default():
    assert "preventDefault" in (WEB / "agent.js").read_text()


def test_review_offers_only_trash_overrides():
    # Final review MUST-FIX 1: no "delete anyway → permanently" anywhere (whole apps included).
    js = (WEB / "review.js").read_text()
    assert 'forced[path] = "permanent"' not in js and "Move to the Trash anyway" in js


def test_sanitize_keeps_the_target_itself(tmp_path):
    # Review I4: "delete this whole folder" must not be dropped as "a symlink".
    t = tmp_path / "proj"; t.mkdir()
    ans = {"verdict": "safe-to-delete", "confidence": 1, "summary": "", "evidence": [],
           "proposed": [{"path": str(t), "action": "delete", "reason": "build"}]}
    assert [p["path"] for p in A.sanitize(ans, str(t))["proposed"]] == [str(t)]
