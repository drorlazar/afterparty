"""Two Apple framework Python.app copies (Xcode's and the Command Line Tools'): pick the one with Full Disk Access."""
import json
import os
import sys

import pytest

from cleanup import fda
from tests.conftest import fake_launchctl
from tests.test_fda import _fake

MM = "%d.%d" % sys.version_info[:2]


def _copy(base):
    root = base / "Library" / "Frameworks" / "Python3.framework" / "Versions" / MM
    real = _fake(root / "bin" / "python3")
    inner = _fake(root / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python")
    return real, inner, root / "Resources" / "Python.app"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A Mac with Xcode (selected by xcode-select) and the Command Line Tools, and a fake launchctl. The tmp tree
    belongs to this user, so only the ownership half of the provenance check is relaxed (to this uid)."""
    tmp = tmp_path.resolve()
    dev = tmp / "Xcode.app" / "Contents" / "Developer"
    clt = tmp / "CommandLineTools"
    x_real, x_inner, x_app = _copy(dev)
    c_real, c_inner, c_app = _copy(clt)
    xs = tmp / "xcode-select"
    lc = fake_launchctl(tmp)
    monkeypatch.setenv("AFTERPARTY_XCODE_SELECT", str(xs))
    monkeypatch.setenv("AFTERPARTY_XCODE_APPS", str(tmp / "Xcode*.app"))
    monkeypatch.setenv("AFTERPARTY_CLT_DIR", str(clt))
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    monkeypatch.setenv("AFTERPARTY_TEST_TRUSTED_UID", str(os.getuid()))
    stub = _fake(tmp / "usr-bin-python3", reported=str(x_real))

    def select(folder):
        """What `xcode-select -p` prints; the stub /usr/bin/python3 runs that copy."""
        xs.write_text("#!/bin/sh\necho '%s'\n" % folder)
        xs.chmod(0o755)
        _fake(stub, reported=str(x_real if folder == dev else c_real))
    select(dev)

    def grant(*binaries):
        (lc.state / "fda-granted").write_text("".join(str(b) + "\n" for b in binaries))

    def leftovers():
        return [p.name for p in lc.state.iterdir() if p.name.startswith(fda.PROBE_LABEL)]

    def probed():
        f = lc.state / "fda-probed"
        return f.read_text().split() if f.exists() else []
    return type("Mac", (), dict(stub=stub, x_inner=x_inner, x_app=x_app, c_inner=c_inner, c_app=c_app, lc=lc,
                                dev=dev, clt=clt, tmp=tmp, select=staticmethod(select), grant=staticmethod(grant),
                                leftovers=staticmethod(leftovers), probed=staticmethod(probed)))


def test_probe_reports_access_and_always_removes_the_job(mac):
    assert fda.launchd_has_fda(str(mac.c_inner)) is False
    mac.grant(mac.c_inner)
    assert fda.launchd_has_fda(str(mac.c_inner)) is True
    log = mac.lc.log.read_text()
    assert "submit -l " + fda.PROBE_LABEL in log and "remove " + fda.PROBE_LABEL in log
    assert mac.leftovers() == []


def test_probe_timeout_and_failed_submit_are_unknown(mac):
    (mac.lc.state / "fda-hang").write_text("")
    assert fda.launchd_has_fda(str(mac.c_inner), timeout=0.3) is None
    assert mac.leftovers() == []
    (mac.lc.state / "fda-hang").unlink()
    (mac.lc.state / "fail-submit").write_text("")
    assert fda.launchd_has_fda(str(mac.c_inner)) is None


def test_the_granted_copy_is_picked(mac):
    mac.grant(mac.c_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.c_inner)
    probed = (mac.lc.state / "fda-probed").read_text().split()
    assert sorted(probed) == sorted([str(mac.x_inner), str(mac.c_inner)])
    assert mac.leftovers() == []


def test_none_or_both_granted_keeps_xcode_selects_choice(mac):
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    mac.grant(mac.c_inner, mac.x_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    (mac.lc.state / "fda-hang").write_text("")
    fda.PROBE_TIMEOUT, saved = 0.3, fda.PROBE_TIMEOUT
    try:
        assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    finally:
        fda.PROBE_TIMEOUT = saved


def test_a_single_copy_is_never_probed(mac, monkeypatch, tmp_path):
    monkeypatch.setenv("AFTERPARTY_CLT_DIR", str(tmp_path / "none"))
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    assert not (mac.lc.state / "fda-probed").exists()


def test_hint_names_the_copy(mac, capsys):
    from cleanup.installer import main as inst
    inst._done(str(mac.c_inner), False)
    out = capsys.readouterr().out
    assert "Python.app from %s (the Command Line Tools copy, not Xcode's)" % mac.c_app in out
    assert "two Python.app entries" in out
    inst._done(str(mac.x_inner), False)
    out = capsys.readouterr().out
    assert "Python.app from %s (Xcode's copy, not the Command Line Tools one)" % mac.x_app in out
    plain = mac.tmp / "opt" / "python3"           # a controlled non-Apple interpreter, whatever runs pytest
    plain.parent.mkdir()
    plain.write_text("#!/bin/sh\necho '%s'\n" % plain)
    plain.chmod(0o755)
    assert fda.fda_hint(str(plain)) == (str(plain), "")  # not an Apple copy: no note


def test_doctor_prints_the_recorded_python_access_and_path(mac, capsys, monkeypatch, tmp_path):
    from cleanup import commands
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"version": 1, "roots": ["~/Projects"], "python": str(mac.c_inner)}))
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(cfg))
    before = sorted(os.listdir(str(cfg)))
    assert commands.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert str(mac.c_inner) in out and "launchd starts it: no" in out
    assert "%s (the Command Line Tools copy, not Xcode's)" % mac.c_app in out
    assert "Xcode: %s - Full Disk Access: no" % mac.x_app in out
    mac.grant(mac.c_inner)
    assert commands.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "launchd starts it: yes" in out and "Command Line Tools: %s - Full Disk Access: yes" % mac.c_app in out
    assert sorted(os.listdir(str(cfg))) == before and mac.leftovers() == []


# --- probe cleanup (fake launchctl switches) ----------------------------------------------------------------------

def _switch(mac, name):
    (mac.lc.state / name).write_text("")


@pytest.mark.parametrize("switch", ["fail-remove", "stuck-remove"])
def test_a_probe_that_cannot_be_removed_is_reported_and_unknown(mac, monkeypatch, capsys, switch):
    monkeypatch.setattr(fda, "GONE_TIMEOUT", 0.3)
    mac.grant(mac.c_inner)
    _switch(mac, switch)
    assert fda.launchd_has_fda(str(mac.c_inner)) is None        # never a "yes" from a job that is still loaded
    err = capsys.readouterr().err
    left = mac.leftovers()
    assert len(left) == 1 and "launchctl remove %s" % left[0] in err
    assert "remove " + left[0] in mac.lc.log.read_text()


def test_an_unverifiable_removal_is_reported(mac, capsys):
    mac.grant(mac.c_inner)
    _switch(mac, "fail-list")
    assert fda.launchd_has_fda(str(mac.c_inner)) is None
    assert "couldn't remove the Full Disk Access probe job" in capsys.readouterr().err


def test_a_removal_that_already_happened_is_fine(mac, capsys):
    mac.grant(mac.c_inner)
    assert fda.launchd_has_fda(str(mac.c_inner)) is True and capsys.readouterr().err == ""
    for lbl in mac.leftovers():                                  # none: removed and verified gone
        raise AssertionError(lbl)


@pytest.mark.parametrize("switch", ["hang-submit", "hang-remove"])
def test_every_launchctl_call_is_time_bounded(mac, monkeypatch, capsys, switch):
    import time
    # the hung call sleeps 30s; a 3s bound leaves the normal (fake) calls headroom on a loaded machine
    monkeypatch.setattr(fda, "LAUNCHCTL_TIMEOUT", 3)
    monkeypatch.setattr(fda, "GONE_TIMEOUT", 0.3)
    mac.grant(mac.c_inner)
    _switch(mac, switch)
    t = time.monotonic()
    assert fda.launchd_has_fda(str(mac.c_inner), timeout=0.3) is None
    assert time.monotonic() - t < 20
    if switch == "hang-remove":
        assert "launchctl remove" in capsys.readouterr().err


def test_prefer_granted_never_switches_on_a_leaked_probe(mac, monkeypatch, capsys):
    monkeypatch.setattr(fda, "GONE_TIMEOUT", 0.2)
    mac.grant(mac.c_inner)
    _switch(mac, "stuck-remove")
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)


def test_a_leaked_probe_of_the_original_keeps_it_even_when_the_alternate_is_granted(mac, monkeypatch):
    monkeypatch.setattr(fda, "GONE_TIMEOUT", 0.2)
    mac.grant(mac.c_inner)
    _switch(mac, "stuck-remove")                                 # the original's answer is unknown (failed cleanup)
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    assert mac.probed() == [str(mac.x_inner)]                    # no alternate is even probed


# --- mixed outcomes: uncertainty about the original always keeps the original -------------------------------------

def _outcomes(monkeypatch, table):
    calls = []

    def probe(binary, timeout=None):
        calls.append(str(binary))
        return table[str(binary)]
    monkeypatch.setattr(fda, "launchd_has_fda", probe)
    return calls


def test_unknown_original_and_granted_alternate_keeps_the_original(mac, monkeypatch):
    calls = _outcomes(monkeypatch, {str(mac.x_inner): None, str(mac.c_inner): True})
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    assert calls == [str(mac.x_inner)]


def test_denied_original_and_granted_alternate_switches(mac, monkeypatch):
    calls = _outcomes(monkeypatch, {str(mac.x_inner): False, str(mac.c_inner): True})
    assert fda.real_executable(str(mac.stub)) == str(mac.c_inner)
    assert calls == [str(mac.x_inner), str(mac.c_inner)]


def test_denied_original_and_unknown_alternate_keeps_the_original(mac, monkeypatch):
    _outcomes(monkeypatch, {str(mac.x_inner): False, str(mac.c_inner): None})
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)


@pytest.mark.parametrize("orig", [False, None, True])
def test_two_granted_alternates_keep_xcode_selects_copy(mac, monkeypatch, orig):
    third = mac.tmp / "Xcode-beta.app" / "Contents" / "Developer"
    _, t_inner, t_app = _copy(third)
    monkeypatch.setattr(fda, "apple_apps", lambda v: [("xcode", str(mac.x_app)), ("xcode", str(t_app)),
                                                      ("clt", str(mac.c_app))])
    monkeypatch.setattr(fda, "_usable_alternate", lambda a, v: True)
    _outcomes(monkeypatch, {str(mac.x_inner): orig, str(t_inner): True, str(mac.c_inner): True})
    assert fda.prefer_granted(str(mac.x_app), VER) == str(mac.x_app)


def test_default_isolation_never_discovers_the_hosts_xcode(env):
    sel = os.environ["AFTERPARTY_XCODE_SELECT"]
    assert sel != "/usr/bin/xcode-select" and os.path.isfile(sel) and env["AFTERPARTY_XCODE_SELECT"] == sel
    assert fda._xcode_developer_dir() is None and fda._xcode_developer_dirs() == []
    assert fda.apple_apps(VER) == []


# --- the copies: recorded binaries, versions, both directions, provenance ---------------------------------------

def test_a_recorded_python_app_binary_still_prefers_the_granted_copy(mac):
    mac.grant(mac.c_inner)
    assert fda.real_executable(str(mac.x_inner)) == str(mac.c_inner)
    mac.grant(mac.x_inner)
    assert fda.real_executable(str(mac.c_inner)) == str(mac.x_inner)
    assert mac.leftovers() == []


def test_update_moves_a_recorded_xcode_binary_to_the_granted_copy(mac, tmp_path, monkeypatch):
    """`afterparty update` re-installs with the recorded interpreter (here Xcode's Python.app binary)."""
    import plistlib
    import types
    from cleanup.installer import main as inst
    from tests.conftest import make_installer
    ie = make_installer(tmp_path / "inst")
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in ie.env.items():
        if k not in ("AFTERPARTY_LAUNCHCTL", "AFTERPARTY_XCODE_SELECT"):
            monkeypatch.setenv(k, v)
    assert inst.main(["install", "--yes", "--python", str(mac.x_inner)]) == 0
    cfg = ie.home / ".config/afterparty"
    assert (cfg / "python").read_text().strip() == str(mac.x_inner)
    mac.grant(mac.c_inner)                                       # the user granted the CLT copy afterwards
    orig = inst.subprocess.run

    def in_process(cmd, *a, **kw):                # the child must see this test's seams: run it here
        if "cleanup.installer" not in cmd:
            return orig(cmd, *a, **kw)
        assert cmd[0] == str(mac.x_inner)
        return types.SimpleNamespace(returncode=inst.main(cmd[cmd.index("install"):]))
    monkeypatch.setattr(inst.subprocess, "run", in_process)
    fd = os.open(str(ie.home), os.O_RDONLY)
    try:
        assert inst._reinstall(str(inst.REPO), fd) == 0
    finally:
        os.close(fd)
    assert (cfg / "python").read_text().strip() == str(mac.c_inner)
    plist = ie.home / "Library/LaunchAgents/io.github.drorlazar.afterparty.weekly.plist"
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(mac.c_inner)
    assert mac.leftovers() == []


def test_an_alternate_with_another_patch_version_is_never_chosen(mac):
    _fake(mac.c_inner, version=fda_ver_bump())
    mac.grant(mac.c_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)  # the verified original, never the stub
    assert str(mac.c_inner) not in mac.probed()                    # rejected before any launchd probe


def test_an_alternate_that_reports_another_executable_is_rejected(mac):
    _fake(mac.c_inner, reported=str(mac.x_inner))
    mac.grant(mac.c_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)


def test_an_unverified_original_keeps_the_given_interpreter(mac):
    _fake(mac.x_inner, version=fda_ver_bump())
    mac.grant(mac.c_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.stub)
    assert mac.probed() == []


def test_clt_selected_finds_an_installed_xcode_with_access(mac):
    mac.select(mac.clt)
    assert fda.real_executable(str(mac.stub)) == str(mac.c_inner)  # nothing granted: xcode-select's copy
    assert sorted(mac.probed()) == sorted([str(mac.c_inner), str(mac.x_inner)])
    mac.grant(mac.x_inner)
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    assert [k for k, _ in fda.apple_apps(VER)] == ["xcode", "clt"]
    assert fda.which_copy(str(mac.c_app)) == "the Command Line Tools copy, not Xcode's"


def test_clt_selected_without_xcode_is_never_probed(mac, monkeypatch):
    mac.select(mac.clt)
    monkeypatch.setenv("AFTERPARTY_XCODE_APPS", str(mac.tmp / "none" / "Xcode*.app"))
    assert fda.real_executable(str(mac.stub)) == str(mac.c_inner)
    assert mac.probed() == []


def test_alternates_need_provenance(mac, monkeypatch):
    mac.grant(mac.c_inner)
    versions = mac.clt / "Library" / "Frameworks" / "Python3.framework" / "Versions"
    versions.chmod(0o775)                                        # group-writable on the way
    assert fda.apple_apps(VER) == [("xcode", str(mac.x_app))]
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)
    versions.chmod(0o755)
    assert len(fda.apple_apps(VER)) == 2
    mac.c_inner.chmod(0o777)                                     # a world-writable binary
    assert len(fda.apple_apps(VER)) == 1
    mac.c_inner.chmod(0o755)
    owner = os.lstat(str(mac.c_inner)).st_uid
    if owner == 0:                                               # a root runner: hand the tree to someone else
        owner = 4242
        for d, dirs, files in os.walk(str(mac.tmp)):
            for n in [d] + [os.path.join(d, f) for f in dirs + files]:
                os.lchown(n, owner, -1)
    # trust exactly one uid that owns none of these files: the real rule (root only) with root out of the picture
    monkeypatch.setenv("AFTERPARTY_TEST_TRUSTED_UID", str(owner + 1))
    assert fda.apple_apps(VER) == []
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)


def test_symlinked_or_replaced_alternates_are_rejected(mac):
    mac.grant(mac.c_inner)
    elsewhere = _fake(mac.tmp / "elsewhere" / "Python")
    real = mac.c_inner.with_name("Python.real")
    mac.c_inner.rename(real)
    mac.c_inner.symlink_to(elsewhere)                            # the binary is a symlink
    assert fda.apple_apps(VER) == [("xcode", str(mac.x_app))]
    mac.c_inner.unlink()
    real.rename(mac.c_inner)
    moved = mac.tmp / "moved-app"
    mac.c_app.rename(moved)
    mac.c_app.symlink_to(moved)                                  # the Python.app folder is a symlink
    assert fda.apple_apps(VER) == [("xcode", str(mac.x_app))]
    assert fda.real_executable(str(mac.stub)) == str(mac.x_inner)


# --- one detection policy for the map app and the launchd probe ------------------------------------------------

def _dirs(tmp_path):
    ok = tmp_path / "ok"
    ok.mkdir()
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0)
    return str(ok), str(denied), str(tmp_path / "absent")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every folder")
def test_the_generated_payload_and_the_map_app_agree(tmp_path):
    import subprocess
    from cleanup.scanner import access
    ok, denied, absent = _dirs(tmp_path)
    try:
        for probes, want in (((absent, denied, ok), "no"), ((absent, ok, denied), "yes"), ((absent,), "none")):
            r = subprocess.run([sys.executable, "-I", "-B", "-c", access.probe_payload(probes)],
                               capture_output=True, text=True, timeout=30)
            assert r.stdout.strip() == "fda:" + want
            assert access.first_outcome(probes) == want
            assert access.has_full_disk_access(probes) is (want != "no")
    finally:
        os.chmod(denied, 0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every folder")
def test_the_probe_job_runs_the_shared_policy(mac, tmp_path, monkeypatch):
    """The fake launchctl really runs the submitted payload: an earlier denied folder decides, as in the map."""
    from cleanup.scanner import access
    ok, denied, absent = _dirs(tmp_path)
    _switch(mac, "fda-run")
    try:
        monkeypatch.setattr(access, "PROBES", (absent, denied, ok))
        assert fda.launchd_has_fda(str(mac.c_inner)) is False
        monkeypatch.setattr(access, "PROBES", (absent, ok, denied))
        assert fda.launchd_has_fda(str(mac.c_inner)) is True
        monkeypatch.setattr(access, "PROBES", (absent,))
        assert fda.launchd_has_fda(str(mac.c_inner)) is None
    finally:
        os.chmod(denied, 0o755)
    assert mac.leftovers() == []


VER = "%d.%d.%d" % sys.version_info[:3]


def fda_ver_bump():
    return "%d.%d.%d" % (sys.version_info[0], sys.version_info[1], sys.version_info[2] + 1)
