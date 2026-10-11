import os
import plistlib
import subprocess

import pytest

from cleanup.installer import jobs
from tests.conftest import REPO, fake_launchctl


@pytest.mark.parametrize("name", jobs.SCHEDULED)
def test_scheduled_plists_use_absolute_paths_and_the_configured_python(name, tmp_path):
    text = jobs.render(name, python="/opt/py/bin/python3", home="/Users/u",
                       log_dir="/Users/u/.local/state/afterparty/logs")
    p = tmp_path / "job.plist"
    p.write_text(text)
    assert subprocess.run(["/usr/bin/plutil", "-lint", str(p)], capture_output=True).returncode == 0
    d = plistlib.loads(text.encode())
    assert d["Label"] == "io.github.drorlazar.afterparty." + name
    assert d["ProgramArguments"] == ["/opt/py/bin/python3", "-B", "-m", "cleanup.cli", "afterparty",
                                     {"weekly": "weekly", "spacecheck": "check-space"}[name]]
    env = d["EnvironmentVariables"]
    assert d["WorkingDirectory"] == jobs.REPO and env["PYTHONPATH"] == jobs.REPO
    assert env["AFTERPARTY_LIVE"] == "1" and env["HOME"] == "/Users/u"
    assert env["PATH"] == "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert d["StandardOutPath"] == d["StandardErrorPath"] == "/Users/u/.local/state/afterparty/logs/%s.log" % name


def test_schedules():
    w = plistlib.loads(jobs.render("weekly", "/p", "/h", "/l").encode())["StartCalendarInterval"]
    s = plistlib.loads(jobs.render("spacecheck", "/p", "/h", "/l").encode())["StartCalendarInterval"]
    assert w == {"Weekday": 0, "Hour": 10, "Minute": 0} and s == {"Hour": 12, "Minute": 0}


def test_values_are_xml_escaped():
    d = plistlib.loads(jobs.render("weekly", "/p & q/<py>", "/h", "/l").encode())
    assert d["ProgramArguments"][0] == "/p & q/<py>"


def test_unfilled_template_value_is_an_error():
    with pytest.raises(ValueError, match="@PORT@"):
        jobs.fill(jobs.template("server"), {"LABEL": "x"})


def test_start_and_stop_are_checked(tmp_path, monkeypatch):
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    lbl = "io.github.drorlazar.afterparty.weekly"
    assert jobs.stop(lbl) == (True, "not loaded")
    assert jobs.start(lbl, "/x/%s.plist" % lbl) == (True, "loaded") and jobs.is_loaded(lbl)
    assert jobs.stop(lbl) == (True, "stopped") and not jobs.is_loaded(lbl)
    uid = os.getuid()
    assert "bootstrap gui/%d /x/%s.plist" % (uid, lbl) in lc.log.read_text()


def test_a_failed_bootstrap_is_reported(tmp_path, monkeypatch):
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    (lc.state / "fail-bootstrap").write_text("")
    ok, msg = jobs.start("io.github.drorlazar.afterparty.weekly", "/x/j.plist")
    assert ok is False and "Input/output error" in msg


def test_a_job_that_wont_stop_is_reported(tmp_path, monkeypatch):
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    lbl = "com.example.legacy"
    (lc.state / lbl).write_text("")
    (lc.state / ("stuck-" + lbl)).write_text("")
    ok, _ = jobs.stop(lbl, wait=0.3)
    assert ok is False and jobs.is_loaded(lbl)


def test_a_failing_print_is_never_taken_for_not_loaded(tmp_path, monkeypatch):
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    lbl = "io.github.drorlazar.afterparty.weekly"
    (lc.state / lbl).write_text("")
    (lc.state / "fail-print").write_text("")
    assert jobs.status(lbl)[0] == "error"
    ok, msg = jobs.stop(lbl)
    assert ok is False and "Operation not permitted" in msg and (lc.state / lbl).exists()
    with pytest.raises(RuntimeError):
        jobs.is_loaded(lbl)


def test_launchctl_missing_never_raises(monkeypatch):
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", "/nonexistent/launchctl")
    ok, msg = jobs.start("x", "/x.plist")
    assert ok is False and msg


def test_env_fixture_never_reaches_real_launchd(env):
    assert env["AFTERPARTY_LAUNCHCTL"] == "/usr/bin/false"


def test_launcher_uses_the_public_server_label_and_the_template():
    from cleanup import launcher
    assert launcher.SERVER_LABEL == "io.github.drorlazar.afterparty.server"
    src = (REPO / "cleanup" / "launcher.py").read_text()
    assert "<plist" not in src and 'label="' not in src and '"-B", "-m", "cleanup.cli", "serve"' in src
    assert "<string>-B</string>" in jobs.template("server")


def test_no_legacy_plists_in_repo():
    assert sorted(p.name for p in (REPO / "launchd").iterdir()) == ["templates"]
    assert sorted(p.name for p in (REPO / "launchd" / "templates").iterdir()) == \
        ["server.plist", "spacecheck.plist", "weekly.plist"]
