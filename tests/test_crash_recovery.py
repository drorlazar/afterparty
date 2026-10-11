"""Kill the installer and the uninstaller at every checkpoint (os._exit, like a power cut); the next run must finish
the job, and --purge must then leave the home folder exactly as it was."""
import json
import os
import subprocess
import sys

import pytest

from cleanup.installer import edits, owned
from tests.conftest import REPO, fake_app_builder, make_installer, snapshot

ARGS = ("--yes", "--hygiene-rule", "yes")
ARGS_APP = ARGS + ("--app", "yes")


def _pretend_user(ins):
    (ins.home / ".claude/settings.json").write_text(
        json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/mine.sh"}]}]}}, indent=2) + "\n")
    (ins.home / ".claude/CLAUDE.md").write_text("# Mine\n")


def _labels(ins, *args, script="install.sh", extra=None):
    log = ins.tmp / "checkpoints.log"
    if log.exists():
        log.unlink()
    r = ins.run(*args, script=script, extra=dict(extra or {}, AFTERPARTY_TEST_CHECKPOINTS=str(log)))
    assert r.returncode == 0, r.stdout + r.stderr
    return [line.split(" ", 1)[1] for line in log.read_text().splitlines()]


EXCHANGE = "exchanged "  # fsops.swap_owned's exchange/verification window: its own tests below (G0 S4)


def _named(labels):
    """The named checkpoints (every step's own), without the exchange window inside each owned write."""
    return [n for n, label in enumerate(labels, 1) if not label.startswith(EXCHANGE)]


def _count(ins, *args, script="install.sh", extra=None):
    return len(_named(_labels(ins, *args, script=script, extra=extra)))


def test_crash_at_every_checkpoint_of_an_app_upgrade(tmp_path):
    """An installed app being replaced by a new build, killed at every point: the rerun finishes the upgrade
    (never "an Afterparty.app this installer didn't put there"), and --purge still restores the baseline."""
    def mac(name):
        ins = make_installer(tmp_path / name)
        _pretend_user(ins)
        return (ins, {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(ins.tmp, "1"))},
                {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(ins.tmp, "2"))})
    probe, v1, v2 = mac("probe")
    assert probe.run(*ARGS_APP, extra=v1).returncode == 0
    for at in _named(_labels(probe, *ARGS_APP, extra=v2)):
        ins, v1, v2 = mac("upgrade-%d" % at)
        base = snapshot(ins.home)
        assert ins.run(*ARGS_APP, extra=v1).returncode == 0
        r = ins.run(*ARGS_APP, extra=dict(v2, AFTERPARTY_TEST_CRASH_AT=str(at)))
        assert r.returncode == 99, (at, r.stdout, r.stderr)
        r = ins.run(*ARGS_APP, extra=v2)
        assert r.returncode == 0, (at, r.stdout, r.stderr)
        exe = ins.home / "Applications/Afterparty.app/Contents/MacOS/Afterparty"
        assert "afterparty 2" in exe.read_text(), at
        assert ins.run("--yes", "--purge", script="uninstall.sh", extra=v2).returncode == 0, at
        assert snapshot(ins.home) == base, at


def test_crash_at_every_checkpoint_with_the_app_too(tmp_path):
    probe = make_installer(tmp_path / "probe")
    _pretend_user(probe)
    for at in _named(_labels(probe, *ARGS_APP, extra={"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(probe.tmp))})):
        ins = make_installer(tmp_path / ("app-%d" % at))
        _pretend_user(ins)
        app = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(ins.tmp))}
        base = snapshot(ins.home)
        r = ins.run(*ARGS_APP, extra=dict(app, AFTERPARTY_TEST_CRASH_AT=str(at)))
        assert r.returncode == 99, (at, r.stdout, r.stderr)
        r = ins.run(*ARGS_APP, extra=app)
        assert r.returncode == 0, (at, r.stdout, r.stderr)     # never "an Afterparty.app this installer didn't put there"
        assert ins.run("--yes", "--purge", script="uninstall.sh", extra=app).returncode == 0, at
        assert snapshot(ins.home) == base, at


def test_crash_at_every_checkpoint_then_rerun_and_purge_restores_the_baseline(tmp_path):
    probe = make_installer(tmp_path / "probe")
    _pretend_user(probe)
    named = _named(_labels(probe, *ARGS))
    assert len(named) >= 20
    for at in named:
        ins = make_installer(tmp_path / ("install-%d" % at))
        _pretend_user(ins)
        base = snapshot(ins.home)
        r = ins.run(*ARGS, extra={"AFTERPARTY_TEST_CRASH_AT": str(at)})
        assert r.returncode == 99, (at, r.stdout, r.stderr)
        r = ins.run(*ARGS)
        assert r.returncode == 0, (at, r.stdout, r.stderr)
        assert ins.run("--yes", "--purge", script="uninstall.sh").returncode == 0, at
        assert snapshot(ins.home) == base, at


def test_crash_during_uninstall_then_rerun_restores_the_baseline(tmp_path):
    probe = make_installer(tmp_path / "probe")
    _pretend_user(probe)
    assert probe.run(*ARGS).returncode == 0
    labels = _labels(probe, "--yes", "--purge", script="uninstall.sh")
    named = _named(labels)
    assert len(named) >= 10
    for at in named:
        ins = make_installer(tmp_path / ("uninstall-%d" % at))
        _pretend_user(ins)
        base = snapshot(ins.home)
        assert ins.run(*ARGS).returncode == 0
        r = ins.run("--yes", "--purge", script="uninstall.sh", extra={"AFTERPARTY_TEST_CRASH_AT": str(at)})
        assert r.returncode == 99, (at, r.stdout, r.stderr)
        if labels[at - 1] == "install record removed":   # its very last step: nothing is left to finish
            assert snapshot(ins.home) == base, at
            continue
        r = ins.run("--yes", "--purge", script="uninstall.sh")
        assert r.returncode == 0, (at, r.stdout, r.stderr)
        assert snapshot(ins.home) == base, at


def test_g0_s4_crash_inside_every_exchange_window_of_an_install(tmp_path):
    """Killed right after swap_owned's atomic exchange, before the displaced copy is verified and removed (every
    owned write: the record, receipts, stage markers, the user's edited files): the rerun finishes and --purge
    restores the baseline."""
    probe = make_installer(tmp_path / "probe")
    _pretend_user(probe)
    points = [n for n, label in enumerate(_labels(probe, *ARGS), 1) if label.startswith(EXCHANGE)]
    assert len(points) >= 10
    for at in points:
        ins = make_installer(tmp_path / ("x-install-%d" % at))
        _pretend_user(ins)
        base = snapshot(ins.home)
        r = ins.run(*ARGS, extra={"AFTERPARTY_TEST_CRASH_AT": str(at)})
        assert r.returncode == 99, (at, r.stdout, r.stderr)
        r = ins.run(*ARGS)
        assert r.returncode == 0, (at, r.stdout, r.stderr)
        r = ins.run("--yes", "--purge", script="uninstall.sh")
        assert r.returncode == 0, (at, r.stdout, r.stderr)
        assert snapshot(ins.home) == base, at


def test_g0_s4_crash_inside_every_exchange_window_and_the_final_record_removal_of_a_purge(tmp_path):
    probe = make_installer(tmp_path / "probe")
    _pretend_user(probe)
    assert probe.run(*ARGS).returncode == 0
    labels = _labels(probe, "--yes", "--purge", script="uninstall.sh")
    final = ("before removing the install record", "install record marked for removal", "install record moved out",
             "install record removed")
    points = [n for n, label in enumerate(labels, 1) if label.startswith(EXCHANGE) or label in final]
    assert all(label in labels for label in final) and len(points) >= 10
    for at in points:
        ins = make_installer(tmp_path / ("x-uninstall-%d" % at))
        _pretend_user(ins)
        base = snapshot(ins.home)
        assert ins.run(*ARGS).returncode == 0
        r = ins.run("--yes", "--purge", script="uninstall.sh", extra={"AFTERPARTY_TEST_CRASH_AT": str(at)})
        assert r.returncode == 99, (at, labels[at - 1], r.stdout, r.stderr)
        if labels[at - 1] == "install record removed":   # its very last step: nothing is left to finish
            assert snapshot(ins.home) == base, at
            continue
        r = ins.run("--yes", "--purge", script="uninstall.sh")
        assert r.returncode == 0, (at, labels[at - 1], r.stdout, r.stderr)
        assert snapshot(ins.home) == base, (at, labels[at - 1])


# ---- the recovery pieces on their own (Task 8 left these tests to this task)
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_INSTALL_LOCK_FD",
              "AFTERPARTY_TEST_CRASH_AT", "AFTERPARTY_TEST_CHECKPOINTS"):
        monkeypatch.delenv(k, raising=False)
    return h


def _crash_bootstrap(home):
    """Run owned.bootstrap in a child that dies at "bootstrap staged" (its second checkpoint: the first is the
    exchange of the marker update that records the record's checksum); return the stage."""
    code = "import sys; sys.path.insert(0, %r); from cleanup.installer import owned; owned.bootstrap()" % str(REPO)
    r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, AFTERPARTY_TEST_CRASH_AT="2"))
    assert r.returncode == 99
    (stage,) = [home / n for n in os.listdir(home) if n.startswith(edits.STAGE_PREFIX)]
    return stage


def test_a_crashed_bootstrap_is_cleared_and_the_next_one_succeeds(home):
    _crash_bootstrap(home)
    assert not (home / ".local").exists()
    edits.sweep(str(home))
    assert os.listdir(home) == []
    assert owned.bootstrap() and (home / ".local/state/afterparty" / owned.MANIFEST).exists()


@pytest.mark.parametrize("tamper", ["extra file", "extra folder", "foreign record"])
def test_a_bootstrap_stage_holding_anything_else_is_left_alone(home, tamper):
    stage = _crash_bootstrap(home)
    record = stage / ".local/state/afterparty" / owned.MANIFEST
    if tamper == "extra file":
        (stage / ".local/state/mine.txt").write_text("the user's")
    elif tamper == "extra folder":
        (stage / ".local/elsewhere").mkdir()
    else:
        record.write_text(record.read_text().replace('"stage": "', '"stage": "x'))
    edits.sweep(str(home))
    assert stage.is_dir() and record.exists()            # not ours as the marker describes it: a person decides


def test_take_if_sends_a_copy_that_fails_its_check_to_the_trash(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("changed")
    trash = tmp_path / "Trash"
    trash.mkdir()

    def mover(paths):
        out = {}
        for p in paths:
            dst = trash / os.path.basename(p)
            os.rename(p, dst)
            out[p] = str(dst)
        return out
    landing = owned.take_if(str(f), lambda s: False, trash=mover)
    assert landing == str(trash / "notes.txt") and (trash / "notes.txt").read_text() == "changed"
    assert os.listdir(tmp_path) == ["Trash"]              # nothing at the path, no staging folder left


def test_take_if_puts_the_copy_back_when_the_trash_refuses_it(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("changed")
    assert owned.take_if(str(f), lambda s: False, trash=lambda paths: {p: "ERR:no Trash" for p in paths}) == "restored"
    assert f.read_text() == "changed" and os.listdir(tmp_path) == ["notes.txt"]


def test_take_if_deletes_only_the_checked_copy(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("ours")
    assert owned.take_if(str(f), lambda s: open(s).read() == "ours", trash=lambda paths: {}) == "gone"
    assert os.listdir(tmp_path) == []
