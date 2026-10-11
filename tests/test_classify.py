import os
import time

import pytest

from cleanup.engine.classify import classify
from tests.conftest import OLD, git

H = "/Users/u"


@pytest.mark.parametrize("path,cls,rule", [
    ("/System/Library/Fonts", "protected", "system"),
    ("/usr/lib", "protected", "system"),
    ("/Library/Fonts", "protected", "system"),
    ("/Applications/Xcode.app/Contents/Developer", "protected", "app-internals"),
    ("/Applications/Adobe Premiere Pro 2026/Adobe Premiere Pro 2026.app", "review", "application"),
    (H + "/Library/Application Support/Google/Chrome/Default", "protected", "chrome"),
    (H + "/Library/Caches/com.spotify.client", "regenerable", "app-cache"),
    (H + "/Library/Application Support/Code/Cache", "regenerable", "app-cache"),
    (H + "/Library/Application Support/Claude", "protected", "app-data"),
    (H + "/proj/.git/objects", "protected", "git-internal"),
    (H + "/proj/web/node_modules", "regenerable", "dependency"),
    (H + "/.cache/uv", "regenerable", "tool-cache"),
    (H + "/proj/.worktrees/feat", "review", "worktree"),
    (H + "/proj/renders/v3", "review", "frames"),
    (H + "/Downloads/movie.mp4", "review", "downloads"),
    (H + "/Documents/taxes", "unknown", "unmatched"),
])
def test_rules(path, cls, rule):
    v = classify(path, home=H)
    assert (v["class"], v["rule"]) == (cls, rule)


def test_actions_follow_class():
    assert classify(H + "/proj/node_modules", home=H)["action"] == "permanent"
    assert classify(H + "/Downloads/x", home=H)["action"] == "trash"
    assert classify("/System/x", home=H)["action"] == "none"
    assert classify(H + "/Documents/x", home=H)["action"] == "trash"


def test_keep_list_wins(tmp_path):
    p = str(tmp_path / "node_modules")
    v = classify(p, keep=[str(tmp_path)], home=str(tmp_path))
    assert v["class"] == "protected" and v["rule"] == "keep-list"


def test_tracked_dependency_is_protected(git_repo, env, monkeypatch):
    r = git_repo()
    (r / "node_modules").mkdir(); (r / "node_modules/x.js").write_text("x")
    git(r, "add", ".", env=env); git(r, "commit", "-qm", "vendored", env=env)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    v = classify(str(r / "node_modules"), home=str(r.parent))
    assert v["class"] == "protected" and v["rule"] == "tracked"


def test_build_output_needs_git_ignore(git_repo, env, monkeypatch):
    r = git_repo()
    (r / "dist").mkdir(); (r / ".gitignore").write_text("dist/\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    assert classify(str(r / "dist"), home=str(r.parent))["class"] == "regenerable"
    (r / "build").mkdir()
    assert classify(str(r / "build"), home=str(r.parent))["class"] == "review"


def test_evidence_lists_checks():
    v = classify(H + "/proj/node_modules", mtime=time.time() - 40 * 86400, home=H)
    checks = {e["check"]: e["status"] for e in v["evidence"]}
    assert checks["not a system path"] == "passed"
    assert checks["not in keep list"] == "passed"
    assert "idle 40 days" in [e["detail"] for e in v["evidence"] if e["check"] == "idle"][0]


@pytest.mark.parametrize("path", [
    H + "/Library/Mobile Documents/com~apple~CloudDocs/Project/Caches",
    H + "/Library/CloudStorage/Dropbox/Work/Cache",
])
def test_cloud_synced_caches_are_not_regenerable(path):
    # Review I8: these are the user's synced files, not app caches.
    v = classify(path, home=H)
    assert v["class"] == "review" and v["rule"] == "cloud" and v["action"] == "none"


def test_cache_name_outside_app_folders_is_not_app_cache():
    v = classify(H + "/Library/Developer/MyProject/Cache", home=H)
    assert v["rule"] != "app-cache"


@pytest.mark.parametrize("name", ["venv", ".venv"])
def test_venv_needs_pyvenv_cfg(tmp_path, name):
    # Review I9: a folder merely named venv is not proof it's a virtualenv.
    d = tmp_path / "Documents" / name; d.mkdir(parents=True)
    assert classify(str(d), home=str(tmp_path))["class"] == "review"
    (d / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert classify(str(d), home=str(tmp_path))["class"] == "regenerable"


def test_derived_data_outside_xcode_or_repo_needs_review(tmp_path):
    d = tmp_path / "Desktop/archive/DerivedData"; d.mkdir(parents=True)
    assert classify(str(d), home=str(tmp_path))["class"] == "review"
    x = tmp_path / "Library/Developer/Xcode/DerivedData"; x.mkdir(parents=True)
    assert classify(str(x), home=str(tmp_path))["class"] == "regenerable"


def test_case_variants_cannot_dodge_protection(tmp_path):
    # Codex C4: APFS is case-insensitive — ".GIT" is ".git", "/system" is "/System".
    assert classify(H + "/proj/.GIT/objects", home=H)["class"] == "protected"
    assert classify("/system/Library/x", home=H)["class"] == "protected"
    p = str(tmp_path / "Scratch/.scratch")
    assert classify(p, keep=[str(tmp_path / "scratch")], home=str(tmp_path))["rule"] == "keep-list"


def test_dependency_rule_requires_a_real_folder(tmp_path):
    # Codex I10: a regular file named node_modules is not a dependency folder.
    f = tmp_path / "node_modules"; f.write_text("notes")
    assert classify(str(f), home=str(tmp_path))["class"] != "regenerable"


def test_scratch_run_is_regenerable_permanent(tmp_path):
    run = tmp_path / "proj" / ".scratch" / "frames-run2"
    (run / "renders").mkdir(parents=True)
    v = classify(str(run), home=str(tmp_path))
    assert (v["class"], v["action"], v["rule"]) == ("regenerable", "permanent", "scratch")


def test_stale_temp_goes_to_trash(tmp_path, monkeypatch):
    t = tmp_path / "T"
    t.mkdir()
    f = t / "old.bin"
    f.write_text("x")
    os.utime(f, (OLD, OLD))
    monkeypatch.setenv("DISKCLEAN_TEMP_DIR", str(t))
    v = classify(str(f), home=str(tmp_path), fast=True)
    assert (v["class"], v["action"], v["rule"]) == ("review", "trash", "temp-stale")
