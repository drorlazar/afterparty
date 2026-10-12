"""Deferred review minors in the installer (jobs.fill, swift_ready, app reinstall, adopted jobs, install.sh flags,
--purge on unreadable or replaced items, update's git config, the isolated re-install)."""
import json
import os
import subprocess

import pytest

from cleanup.installer import jobs
from cleanup.installer import main as inst
from tests.conftest import fake_app_builder, make_installer
from tests.test_uninstall import _commit, _git, origin_and_copy  # noqa: F401 (fixture)


def _manifest(ins):
    return json.loads((ins.home / ".local/state/afterparty/install-manifest.json").read_text())


# ---- jobs.fill: one pass
def test_fill_never_rescans_a_value():
    out = jobs.fill("<a>@PYTHON@</a><b>@HOME@</b>", {"PYTHON": "/x/@HOME@/@NOPE@/py", "HOME": "/h"})
    assert out == "<a>/x/@HOME@/@NOPE@/py</a><b>/h</b>"


def test_fill_still_refuses_an_unfilled_placeholder():
    with pytest.raises(ValueError, match="@LOG@"):
        jobs.fill("@HOME@ @LOG@", {"HOME": "/h"})


def test_render_with_a_home_that_looks_like_a_placeholder(tmp_path):
    text = jobs.render("weekly", python="/usr/bin/python3", home="/Users/@REPO@", log_dir=str(tmp_path))
    assert "/Users/@REPO@" in text and jobs.REPO in text


# ---- swift_ready
class _Runs:
    def __init__(self, codes):
        self.codes, self.calls = codes, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        code = self.codes.get(os.path.basename(cmd[0]) + " " + cmd[1], 0)
        if isinstance(code, BaseException):
            raise code
        return subprocess.CompletedProcess(cmd, code, b"", b"")


@pytest.fixture
def swift(monkeypatch, tmp_path):
    monkeypatch.delenv("AFTERPARTY_TEST_APP_BUILDER", raising=False)
    monkeypatch.setenv("AFTERPARTY_XCODE_SELECT", str(tmp_path / "xcode-select"))

    def use(codes):
        runs = _Runs(codes)
        monkeypatch.setattr(inst.subprocess, "run", runs)
        return runs
    return use


def test_swift_ready_needs_the_tools_swiftc_and_the_sdk(swift):
    runs = swift({})
    assert inst.swift_ready() is True
    assert [c[1:] for c in runs.calls] == [["-p"], ["--find", "swiftc"], ["--show-sdk-path"]]


def test_swift_ready_never_calls_xcrun_without_the_command_line_tools(swift):
    runs = swift({"xcode-select -p": 2})
    assert inst.swift_ready() is False
    assert len(runs.calls) == 1  # xcrun would open the install dialog


@pytest.mark.parametrize("codes", [{"xcrun --find": 1}, {"xcrun --show-sdk-path": 1},
                                   {"xcode-select -p": OSError("missing")},
                                   {"xcrun --find": subprocess.TimeoutExpired("xcrun", 60)}])
def test_swift_ready_is_false_when_any_part_is_missing(swift, codes):
    swift(codes)
    assert inst.swift_ready() is False


def test_swift_ready_with_the_test_builder(monkeypatch):
    monkeypatch.setenv("AFTERPARTY_TEST_APP_BUILDER", "/nonexistent")
    assert inst.swift_ready() is True


# ---- the app: a rebuild identical to the installed bundle isn't swapped in
def test_reinstall_leaves_an_identical_app_in_place(installer):
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    assert installer.run("--yes", "--app", "yes", extra=ex).returncode == 0
    app = installer.home / "Applications/Afterparty.app"
    before, item = os.stat(app).st_ino, [i for i in _manifest(installer)["items"] if i["kind"] == "app"]
    r = installer.run("--yes", "--app", "yes", extra=ex)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.stat(app).st_ino == before
    assert [i for i in _manifest(installer)["items"] if i["kind"] == "app"] == item
    ex2 = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp, version="2"))}
    assert installer.run("--yes", "--app", "yes", extra=ex2).returncode == 0
    assert os.stat(app).st_ino != before  # a different build still replaces it
    assert "afterparty 2" in (app / "Contents/MacOS/Afterparty").read_text()


# ---- an adopted job is loaded in the same run
def test_an_adopted_job_is_loaded_in_that_run(installer):
    assert installer.run("--yes").returncode == 0
    agents = installer.home / "Library/LaunchAgents"
    saved = {p.name: p.read_bytes() for p in agents.glob("io.github.drorlazar.afterparty.*.plist")}
    assert len(saved) == 2
    assert installer.run("--yes", script="uninstall.sh").returncode == 0
    agents.mkdir(parents=True, exist_ok=True)
    for name, data in saved.items():
        (agents / name).write_bytes(data)              # identical copies nobody recorded, not loaded
    assert not any((installer.launchd / n[:-len(".plist")]).exists() for n in saved)
    r = installer.run("--yes", "--adopt")
    assert r.returncode == 0, r.stdout + r.stderr
    for name in saved:
        assert (installer.launchd / name[:-len(".plist")]).exists(), name
    jobs_ = [i for i in _manifest(installer)["items"] if i["kind"] == "job"]
    assert len(jobs_) == 2 and all(i.get("adopted") and i["state"] == "done" for i in jobs_)


# ---- install.sh --python=/x
def test_install_sh_takes_the_python_equals_form(installer):
    r = installer.run("--dry-run", "--python=/nonexistent/python3")
    assert r.returncode == 69, r.stdout + r.stderr
    assert "install plan" not in r.stdout


# ---- --purge: a recorded folder that is now a file or a symlink isn't ours
def test_a_dir_receipt_whose_path_is_no_longer_a_folder_isnt_ours(tmp_path):
    real, f, link = tmp_path / "real", tmp_path / "file", tmp_path / "link"
    real.mkdir()
    f.write_text("x")
    link.symlink_to(real)
    items = [{"kind": "dir", "path": str(p), "state": s} for p, s in ((real, "done"), (f, "pending"),
                                                                      (link, "pending"), (tmp_path / "gone", "done"))]
    items.append({"kind": "link", "path": str(tmp_path / "x"), "state": "done"})
    assert inst._created_dirs(items) == {str(real)}


# ---- --purge: an unreadable file is never a traceback part-way through
def test_purge_with_an_unreadable_receipted_file_finishes_cleanly(tmp_path):
    ins = make_installer(tmp_path)
    assert ins.run("--yes").returncode == 0
    cfg = ins.home / ".config/afterparty/config.json"
    cfg.chmod(0)
    try:
        r = ins.run("--yes", "--purge", script="uninstall.sh")
    finally:
        for d, ds, fs in os.walk(tmp_path):
            for n in ds + fs:
                p = os.path.join(d, n)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
    assert "Traceback" not in r.stderr, r.stderr
    assert not [p for p in tmp_path.rglob("*") if ".afterparty-stage-" in str(p)]
    trashed = list((tmp_path / "trash").rglob("*config.json"))
    assert trashed or cfg.exists()  # never deleted unverified: in the Trash (listed) or left in place
    assert "config.json" in r.stdout + r.stderr


def test_file_is_check_fails_closed_on_an_unreadable_file(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    want = inst.fsops.file_sum(str(p))
    assert inst._file_is(want)(str(p)) and not inst._file_is(None)(str(p))
    p.chmod(0)
    try:
        assert inst._file_is(want)(str(p)) is False
    finally:
        p.chmod(0o600)


# ---- update: only the repository's own git config
def test_update_ignores_url_insteadof_in_the_global_git_config(origin_and_copy, tmp_path, monkeypatch):  # noqa: F811
    bare, seed, copy = origin_and_copy
    evil = tmp_path / "evil.git"
    _git("clone", "-q", "--bare", str(bare), str(evil))
    other = tmp_path / "other"
    _git("clone", "-q", str(evil), str(other))
    _commit(other, "evil", "not from the official repo")
    _commit(seed, "2", "two")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text('[url "%s"]\n\tinsteadOf = %s\n' % (evil, bare))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: 0) == 0
    assert (copy / "a.txt").read_text() == "2"


def test_reinstall_runs_isolated(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(inst.config, "load", lambda: ({"python": "/py"}, True))
    monkeypatch.setattr(inst.subprocess, "run", run)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    assert inst._reinstall("/repo", 7) == 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["/py", "-I", "-B"] and cmd[3] == "-c" and cmd[5:8] == ["/repo", "cleanup.installer", "install"]
    assert seen["env"]["AFTERPARTY_INSTALL_LOCK_FD"] == "7"
