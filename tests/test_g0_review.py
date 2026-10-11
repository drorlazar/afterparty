"""Adversarial scenarios from the Codex G0 review of Plan B."""
import glob
import json
import os

import pytest

from cleanup import fsops
from cleanup.installer import edits


def _all_bytes(root):
    out = []
    for d, _dirs, files in os.walk(str(root)):
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                with open(p, "rb") as fh:
                    out.append(fh.read())
    return out


# ---- M1: a read failure after the exchange never deletes the displaced (maybe foreign) copy
def _fail_reads_of(monkeypatch, pred):
    real = fsops._bytes

    def flaky(p):
        if pred(p):
            raise OSError(5, "Input/output error")
        return real(p)
    monkeypatch.setattr(fsops, "_bytes", flaky)


def test_m1_edit_keeps_a_concurrent_write_when_the_displaced_copy_cant_be_read(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text("ORIGINAL")

    def user_writes():  # lands after our read, right before the exchange
        target.write_text("USER-CONCURRENT")
        _fail_reads_of(monkeypatch, lambda p: os.path.basename(p) == "new"
                       and os.path.basename(os.path.dirname(p)).startswith(edits.STAGE_PREFIX))

    with pytest.raises(edits.EditConflict) as e:
        edits.edit(str(target), lambda t: "OURS", before_check=user_writes)
    assert b"USER-CONCURRENT" in _all_bytes(tmp_path)  # never deleted
    assert "kept" in str(e.value)


def test_m1_write_owned_keeps_the_displaced_copy_when_it_cant_be_read(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"THEIRS")
    _fail_reads_of(monkeypatch, lambda p: os.path.basename(p).startswith(".afterparty-"))
    with pytest.raises(fsops.Conflict) as e:
        fsops.write_owned(str(target), b"OURS", b"WHAT-WE-READ")
    assert e.value.kept and open(e.value.kept, "rb").read() == b"THEIRS"


def test_m1_edit_never_deletes_a_stage_holding_anything_unexpected(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("ORIGINAL")

    def boom():
        stage = glob.glob(str(tmp_path / (edits.STAGE_PREFIX + "*")))[0]
        with open(os.path.join(stage, "someone-elses"), "w") as f:
            f.write("UNKNOWN")
        raise OSError(28, "No space left on device")

    with pytest.raises(edits.EditConflict) as e:
        edits.edit(str(target), lambda t: "OURS", before_check=boom)
    assert b"UNKNOWN" in _all_bytes(tmp_path) and edits.STAGE_PREFIX in str(e.value)
    assert target.read_text() == "ORIGINAL"


def test_m1_edit_still_clears_its_own_stage(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("ORIGINAL")
    assert edits.edit(str(target), lambda t: "OURS") is True
    assert target.read_text() == "OURS"
    assert not glob.glob(str(tmp_path / (edits.STAGE_PREFIX + "*")))


# ---- M2: the app's completion receipt is the sum of what we built and placed, never a live read afterwards
def test_m2_app_receipt_never_adopts_a_file_added_after_placement(tmp_path, monkeypatch):
    from cleanup.installer import main as inst, owned
    from tests.conftest import fake_app_builder
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AFTERPARTY_APP_DEST", str(tmp_path / "apps"))
    monkeypatch.setenv("AFTERPARTY_TEST_APP_BUILDER", str(fake_app_builder(tmp_path)))
    (tmp_path / "apps").mkdir()
    real_put = owned.put

    def put_then_user_adds(dst, build, prior, before_place=None):
        r = real_put(dst, build, prior, before_place)
        with open(os.path.join(dst, "Contents", "user-notes.txt"), "w") as f:
            f.write("mine")  # a concurrent user file, right after placement
        return r
    monkeypatch.setattr(owned, "put", put_then_user_adds)
    step = inst.plan_app(owned.Manifest(str(tmp_path / "m.json")), adopt=False)
    intents = []
    extra = step.do(lambda **f: intents.append(f))
    dst = str(tmp_path / "apps" / "Afterparty.app")
    assert extra["sum"] == intents[0]["sum"]           # exactly the bundle we built
    assert extra["sum"] != fsops.tree_sum(dst)          # never the live tree with the user's file in it


def test_m2_put_returns_the_sum_of_the_staged_copy(tmp_path):
    from cleanup.installer import owned
    dst = tmp_path / "x"
    got = owned.put(str(dst), lambda p: open(p, "w").write("ours"), None)
    assert got == fsops.text_sum("ours")


# ---- M3: receipts are certified and parsed from one read
def test_m3_trusted_receipts_come_from_the_bytes_that_were_certified(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    folder = tmp_path / "state"
    folder.mkdir()
    fsops.note_written(str(folder / "ours.txt"), b"ours")
    rf = folder / fsops.RECEIPTS
    forged = {"user-file.txt": fsops.text_sum("user data"), "ours.txt": fsops.text_sum("ours")}
    real_open = open
    seen = {"n": 0}

    def racing_open(p, *a, **k):
        f = real_open(p, *a, **k)
        if str(p) == str(rf):
            seen["n"] += 1
            if seen["n"] == 1:  # an un-certified replacement lands right after the first read opened
                tmp = folder / "forged.tmp"
                tmp.write_text(json.dumps(forged))
                os.replace(str(tmp), str(rf))
        return f
    monkeypatch.setattr(fsops, "open", racing_open, raising=False)
    assert inst.trusted_receipts(str(folder)) == {"ours.txt": fsops.text_sum("ours")}


# ---- S3: purge keeps the install record when a Trash move failed, so a rerun can finish
def test_s3_purge_keeps_the_record_when_a_trash_move_fails_and_a_rerun_finishes(installer):
    assert installer.run("--yes").returncode == 0
    mine = installer.home / ".local/state/afterparty/my-notes.txt"
    mine.write_text("mine")
    trash = installer.tmp / "trash" / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    os.chmod(str(trash), 0o500)  # the Trash refuses every move
    try:
        r = installer.run("--yes", "--purge", script="uninstall.sh")
    finally:
        os.chmod(str(trash), 0o700)
    assert r.returncode != 0 and mine.read_text() == "mine"
    assert (installer.home / ".local/state/afterparty/install-manifest.json").exists(), r.stdout + r.stderr
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not mine.exists() and not (installer.home / ".local/state/afterparty").exists()


# ---- M4: the python sidecar is never written over a file Afterparty doesn't own
@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(d))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return d


def test_m4_save_stops_at_an_unowned_python_file_and_leaves_it(cdir):
    from cleanup import config
    cdir.mkdir()
    (cdir / "python").write_text("my own notes, not Afterparty's\n")
    with pytest.raises(config.ConfigError, match="python"):
        config.save({"python": "/usr/bin/python3"})
    assert (cdir / "python").read_text() == "my own notes, not Afterparty's\n"
    assert not (cdir / "config.json").exists()


def test_m4_update_stops_at_an_unowned_python_file_and_leaves_it(cdir):
    from cleanup import config
    cdir.mkdir()
    (cdir / "python").write_text("mine\n")
    with pytest.raises(config.ConfigError):
        config.update(lambda c: c.update(python="/usr/bin/python3"))
    assert (cdir / "python").read_text() == "mine\n"


def test_m4_our_own_sidecar_is_still_replaced_and_a_matching_one_is_fine(cdir):
    from cleanup import config
    config.save({"python": "/usr/bin/python3"})
    config.save({"python": "/opt/py/bin/python3"})
    assert (cdir / "python").read_text() == "/opt/py/bin/python3\n"
    os.unlink(str(cdir / fsops.RECEIPTS))
    config.save({"python": "/opt/py/bin/python3", "roots": ["~/x"]})  # already exactly what we'd write: no stop
    assert config.load()[0]["roots"] == ["~/x"]


# ================= round 2 (Codex recheck) =================
# ---- NEW1: teardown recovery never deletes a record that isn't exactly the one we moved out
def test_new1_a_modified_record_in_a_crashed_teardown_is_never_deleted(installer, tmp_path):
    from tests.conftest import make_installer
    from tests.test_crash_recovery import ARGS, _labels
    probe = make_installer(tmp_path / "probe")
    assert probe.run(*ARGS).returncode == 0
    at = _labels(probe, "--yes", "--purge", script="uninstall.sh").index("install record moved out") + 1
    assert installer.run(*ARGS).returncode == 0
    r = installer.run("--yes", "--purge", script="uninstall.sh", extra={"AFTERPARTY_TEST_CRASH_AT": str(at)})
    assert r.returncode == 99, r.stdout + r.stderr
    (staged,) = [os.path.join(d, f) for d, _, fs in os.walk(str(installer.home)) for f in fs
                 if f == "install-manifest.json" and edits.STAGE_PREFIX in d]
    d = json.loads(open(staged).read())
    d["private"] = "someone's added notes"                               # nonce kept, content changed
    with open(staged, "w") as f:
        f.write(json.dumps(d, indent=1, sort_keys=True) + "\n")
    installer.run("--yes", "--purge", script="uninstall.sh")
    assert any(b"someone's added notes" in b for b in _all_bytes(installer.home)), "the modified record was deleted"


# ---- NEW2: an incomplete walk never authorizes a teardown deletion
def _flaky_scandir(monkeypatch, target):
    real, seen = os.scandir, {"n": 0}

    def scandir(p="."):
        if isinstance(p, (str, bytes, os.PathLike)) and os.fspath(p) == str(target) and seen["n"] == 0:
            seen["n"] += 1
            raise PermissionError(13, "Permission denied", str(target))
        return real(p)
    monkeypatch.setattr(os, "scandir", scandir)


def test_new2_only_chain_refuses_a_subtree_it_couldnt_list(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    a = tmp_path / "a"
    (a / "b").mkdir(parents=True)
    (a / "b" / "rec").write_text("r")
    (a / "x").mkdir()
    (a / "x" / "user.txt").write_text("the user's")
    _flaky_scandir(monkeypatch, a / "x")
    assert inst._only_chain(str(a), {str(a), str(a / "b")}, str(a / "b" / "rec")) is False


def test_new2_sweep_never_deletes_a_teardown_stage_it_couldnt_list_completely(tmp_path, monkeypatch):
    rel = os.path.join(".local", "state", "afterparty")
    stage = edits.make_stage(str(tmp_path), "teardown", rel=rel, manifest="install-manifest.json")
    nonce = os.path.basename(stage)[len(edits.STAGE_PREFIX):]
    os.makedirs(os.path.join(stage, rel))
    rec = json.dumps({"stage": nonce}).encode()
    edits.update_mark(stage, record_sum=fsops.data_sum(rec))
    with open(os.path.join(stage, rel, "install-manifest.json"), "wb") as f:
        f.write(rec)
    hidden = os.path.join(stage, ".local", "state")
    with open(os.path.join(hidden, "user.txt"), "w") as f:
        f.write("the user's")
    _flaky_scandir(monkeypatch, hidden)
    edits.sweep(str(tmp_path))
    assert os.path.exists(os.path.join(hidden, "user.txt"))


# ---- S3 round 2: a Trash failure whose restore is blocked keeps the install record
def test_s3b_a_blocked_restore_after_a_trash_failure_marks_the_purge_unfinished(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    monkeypatch.setenv("AFTERPARTY_TEST_TRASH_DIR", str(tmp_path / "t"))
    created = tmp_path / "ours"
    created.mkdir()
    f = created / "notes.txt"
    f.write_text("x")
    p = inst.Purge([str(created)])

    def refusing_trash(paths):
        f.write_text("someone's new file")  # a replacement lands at the original path meanwhile
        return {q: "ERR:no Trash" for q in paths}
    p.trash = refusing_trash
    p.remove(str(f), lambda s: False)
    assert p.trash_failed and f.read_text() == "someone's new file"
    assert any("kept" in k for k in p.kept)


# ================= round 3 (Codex recheck 2) =================
def _record_stage(parent, rel=os.path.join(".local", "state", "afterparty"), manifest="install-manifest.json",
                  record=True):
    stage = edits.make_stage(str(parent), "teardown", rel=rel, manifest=manifest)
    nonce = os.path.basename(stage)[len(edits.STAGE_PREFIX):]
    rec = (json.dumps({"stage": nonce}) + "\n").encode()
    edits.update_mark(stage, record_sum=fsops.data_sum(rec))
    if record:
        os.makedirs(os.path.join(stage, rel))
        with open(os.path.join(stage, rel, manifest), "wb") as f:
            f.write(rec)
    return stage


# ---- P1-1: a malformed marker never makes the removal reach outside the stage
@pytest.mark.parametrize("rel,manifest", [("../../precious", "notes.txt"), ("../../precious", "install-manifest.json"),
                                          (".local/../../../precious", "install-manifest.json"),
                                          ("ABS", "install-manifest.json"), (".local/state/afterparty", "../x")])
def test_p1_1_a_marker_naming_paths_outside_the_stage_deletes_nothing(tmp_path, rel, manifest):
    parent = tmp_path / "parent"
    parent.mkdir()
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "notes.txt").write_text("the user's")
    (precious / "install-manifest.json").write_text("the user's")
    if rel == "ABS":
        rel = str(precious)
    stage = edits.make_stage(str(parent), "teardown", rel=rel, manifest=manifest)  # marker only, no record
    edits.sweep(str(parent))
    assert (precious / "notes.txt").read_text() == "the user's"
    assert (precious / "install-manifest.json").read_text() == "the user's"
    assert os.path.isdir(stage)                                          # preserved, nothing deleted


# ---- P1-2: a record changed after its checksum was checked is never deleted
def test_p1_2_a_record_changed_after_validation_is_never_deleted(tmp_path, monkeypatch):
    stage = _record_stage(tmp_path)
    rec = os.path.join(stage, ".local", "state", "afterparty", "install-manifest.json")
    real = edits._bootstrap_ok

    def then_edit(s, d):
        ok = real(s, d)
        with open(rec, "a") as f:
            f.write("someone's private notes\n")
        return ok
    monkeypatch.setattr(edits, "_bootstrap_ok", then_edit)
    assert edits.sweep(str(tmp_path)) == []
    assert any(b"someone's private notes" in b for b in _all_bytes(tmp_path))


def test_p1_2_an_unchanged_record_stage_is_still_cleared(tmp_path):
    _record_stage(tmp_path)
    assert edits.sweep(str(tmp_path)) == ["teardown"] and os.listdir(str(tmp_path)) == []


# ---- S-1: a teardown removal that fails is reported as unfinished (non-zero exit)
def test_s1_a_failed_teardown_removal_is_unfinished(installer, monkeypatch):
    from cleanup.installer import main as inst
    from tests.test_crash_recovery import ARGS
    assert installer.run(*ARGS).returncode == 0
    for k, v in installer.env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AFTERPARTY_INSTALL_LOCK_FD", raising=False)
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR"):  # the installer's HOME is the tmp one: use its defaults
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(edits, "remove_record_stage", lambda stage, d: False)
    rc = inst.uninstall(purge=True, yes=True)
    assert rc == inst.FAILED
