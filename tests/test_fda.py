import json
import os
import plistlib
import sys

from cleanup.fda import fda_binary
from cleanup.installer import main as installer_main


def _framework(tmp_path):
    root = tmp_path / "Versions" / "3.9"
    (root / "bin").mkdir(parents=True)
    (root / "Resources" / "Python.app").mkdir(parents=True)
    exe = root / "bin" / "python3"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    return exe, root / "Resources" / "Python.app"


def test_framework_interpreter_resolves_to_python_app(tmp_path):
    exe, app = _framework(tmp_path)
    assert fda_binary(str(exe)) == os.path.realpath(str(app))


def test_plain_interpreter_is_itself(tmp_path):
    exe = tmp_path / "python3"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    assert fda_binary(str(exe)) == os.path.realpath(str(exe))


def test_closing_message_names_resolved_path(tmp_path, capsys):
    exe, app = _framework(tmp_path)
    installer_main._done(str(exe), False)
    out = capsys.readouterr().out
    assert os.path.realpath(str(app)) in out and "/usr/bin/python3" not in out


def test_quitting_the_app_stops_only_our_server_job(tmp_path, monkeypatch, capsys):
    import json
    from cleanup import cli, launcher
    from cleanup.installer import jobs
    from tests.conftest import fake_launchctl
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    other = "com.example.other"
    (lc.state / launcher.SERVER_LABEL).write_text("")
    (lc.state / other).write_text("")
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "launchd", "label": launcher.SERVER_LABEL, "port": 1, "token": "t"}))
    monkeypatch.setattr(launcher, "default_state", lambda: str(state))
    assert cli.main(["app", "--stop"]) == 0
    assert not jobs.is_loaded(launcher.SERVER_LABEL) and jobs.is_loaded(other)
    state.write_text(json.dumps({"mode": "launchd", "label": other, "port": 1, "token": "t"}))
    assert cli.main(["app", "--stop"]) == 0 and jobs.is_loaded(other)  # a state file never picks another job


VER = "%d.%d.%d" % sys.version_info[:3]


def _fake(path, reported=None, version=VER, venv=0, probe_ok=True):
    """An interpreter script: answers fda's probe with (reported, version, venv), runs the real Python otherwise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    answer = ('printf "%%s\\n%s\\n%d\\n" "%s"' % (version, venv, reported or path)) if probe_ok else "exit 3"
    path.write_text('#!/bin/sh\ncase "$*" in *sys.executable*) %s;; *) exec %s "$@";; esac\n' % (answer, sys.executable))
    path.chmod(0o755)
    return path


def _clt(tmp_path, monkeypatch, **inner_kw):
    from cleanup import fda
    monkeypatch.setattr(fda, "_is_clt", lambda real: True)
    root = tmp_path / "fw" / "Versions" / "3.9"
    real = _fake(root / "bin" / "python3")
    inner = root / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    if inner_kw is not None:
        _fake(inner, **inner_kw)
    return real, inner, root / "Resources" / "Python.app"


def test_clt_stub_resolves_to_the_inner_binary(tmp_path, monkeypatch):
    from cleanup.fda import fda_binary, real_executable
    stub = _fake(tmp_path / "stub", reported=str(tmp_path / "fw" / "Versions" / "3.9" / "bin" / "python3"))
    real, inner, app = _clt(tmp_path, monkeypatch)
    assert real_executable(str(stub)) == str(inner)
    assert fda_binary(str(inner)) == fda_binary(str(real)) == os.path.realpath(str(app))


def test_fallbacks_return_the_interpreter(tmp_path, monkeypatch):
    from cleanup.fda import real_executable
    real, inner, _ = _clt(tmp_path, monkeypatch)
    assert real_executable(str(real)) == str(inner)
    inner.chmod(0o644)                                                   # not executable
    assert real_executable(str(real)) == str(real)
    inner.unlink()                                                       # missing
    assert real_executable(str(real)) == str(real)
    _fake(inner, version="2.7.1")                                        # a different version
    assert real_executable(str(real)) == str(real)
    _fake(inner)
    assert real_executable(str(real)) == str(inner)
    _fake(inner, venv=1)                                                 # the inner binary reports a venv
    assert real_executable(str(real)) == str(real)


def test_a_venv_and_a_failed_probe_keep_the_interpreter(tmp_path, monkeypatch):
    from cleanup.fda import real_executable
    _clt(tmp_path, monkeypatch)
    venv = _fake(tmp_path / "venv" / "bin" / "python3", venv=1)
    assert real_executable(str(venv)) == str(venv)
    bad = _fake(tmp_path / "bad" / "python3", probe_ok=False)
    assert real_executable(str(bad)) == str(bad)
    assert real_executable(str(tmp_path / "missing")) == str(tmp_path / "missing")


def test_python_org_and_plain_interpreters_are_unchanged(tmp_path):
    from cleanup.fda import real_executable
    other = _fake(tmp_path / "Library" / "Frameworks" / "Python.framework" / "Versions" / "3.9" / "bin" / "python3")
    assert real_executable(str(other)) == str(other)


def test_homebrew_uses_the_version_free_opt_path(tmp_path):
    from cleanup.fda import fda_binary, real_executable
    cellar = tmp_path / "brew" / "Cellar" / "python39" / "3.9.1"
    real = _fake(cellar / "bin" / "python3")
    (tmp_path / "brew" / "opt").mkdir()
    (tmp_path / "brew" / "opt" / "python39").symlink_to(cellar)
    (tmp_path / "brew" / "bin").mkdir()
    (tmp_path / "brew" / "bin" / "python3").symlink_to(real)
    opt = os.path.realpath(str(tmp_path / "brew")) + "/opt/python39/bin/python3"
    got = real_executable(str(tmp_path / "brew" / "bin" / "python3"))
    assert got == opt and "/Cellar/" not in got
    assert "/Cellar/" not in fda_binary(got) and "/Cellar/" not in fda_binary(str(tmp_path / "brew" / "bin" / "python3"))
    (tmp_path / "brew" / "opt" / "python39").unlink()                  # no opt link: keep what it was
    assert real_executable(str(tmp_path / "brew" / "bin" / "python3")) == str(tmp_path / "brew" / "bin" / "python3")


def _stub_install(tmp_path, monkeypatch):
    from tests.conftest import make_installer
    inst_env = make_installer(tmp_path)
    real, inner, _ = _clt(tmp_path, monkeypatch)
    stub = _fake(tmp_path / "usr-bin-python3", reported=str(real))
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in inst_env.env.items():
        monkeypatch.setenv(k, v)
    return inst_env, stub, inner


def test_a_stub_interpreter_is_recorded_as_the_inner_binary(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    inst_env, stub, inner = _stub_install(tmp_path, monkeypatch)
    assert inst.main(["install", "--yes", "--python", str(stub)]) == 0
    assert (inst_env.home / ".config/afterparty/python").read_text().strip() == str(inner)
    plist = inst_env.home / "Library/LaunchAgents/io.github.drorlazar.afterparty.weekly.plist"
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(inner)


def test_update_migrates_a_stub_sidecar(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    inst_env, stub, inner = _stub_install(tmp_path, monkeypatch)
    assert inst.main(["install", "--yes", "--python", sys.executable]) == 0
    cfg = inst_env.home / ".config/afterparty"
    c = json.loads((cfg / "config.json").read_text())
    c["python"] = str(stub)
    (cfg / "config.json").write_text(json.dumps(c))
    (cfg / "python").write_text(str(stub) + "\n")
    plist = inst_env.home / "Library/LaunchAgents/io.github.drorlazar.afterparty.weekly.plist"
    from tests.conftest import recorded_python
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == recorded_python()  # under this test's CLT patch
    import types
    ran = []

    orig = inst.subprocess.run

    def in_process(cmd, *a, **kw):                # the child can't see this test's CLT patch, so run it here
        if "cleanup.installer" not in cmd:
            return orig(cmd, *a, **kw)
        ran.append(cmd)
        return types.SimpleNamespace(returncode=inst.main(cmd[cmd.index("install"):]))
    monkeypatch.setattr(inst.subprocess, "run", in_process)
    fd = os.open(str(inst_env.home), os.O_RDONLY)
    try:
        assert inst._reinstall(str(inst.REPO), fd) == 0
        assert ran[0][0] == str(stub) and "--reuse-answers" in ran[0]
    finally:
        os.close(fd)
    assert (cfg / "python").read_text().strip() == str(inner)
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(inner)


def test_reuse_answers_still_honours_explicit_app_and_hygiene(tmp_path, monkeypatch):
    from cleanup.installer import main as inst
    inst_env, stub, _ = _stub_install(tmp_path, monkeypatch)
    assert inst.main(["install", "--yes", "--python", sys.executable]) == 0
    seen = {}
    real_plan = inst.plan_install

    def spy(man, python, roots, keep, agent, hygiene, app, adopt):
        seen.update(hygiene=hygiene, app=app)
        return real_plan(man, python, roots, keep, agent, hygiene, app, adopt)
    monkeypatch.setattr(inst, "plan_install", spy)
    inst.main(["install", "--dry-run", "--python", sys.executable, "--reuse-answers", "--app", "yes",
               "--hygiene-rule", "yes"])
    assert seen == {"hygiene": True, "app": True}


def test_quitting_the_app_stops_only_our_server_job(tmp_path, monkeypatch, capsys):
    from cleanup import cli, launcher
    from cleanup.installer import jobs
    from tests.conftest import fake_launchctl
    lc = fake_launchctl(tmp_path)
    monkeypatch.setenv("AFTERPARTY_LAUNCHCTL", str(lc.path))
    other = "com.example.other"
    (lc.state / launcher.SERVER_LABEL).write_text("")
    (lc.state / other).write_text("")
    state = tmp_path / "server.json"
    state.write_text(json.dumps({"mode": "launchd", "label": launcher.SERVER_LABEL, "port": 1, "token": "t"}))
    monkeypatch.setattr(launcher, "default_state", lambda: str(state))
    assert cli.main(["app", "--stop"]) == 0
    assert not jobs.is_loaded(launcher.SERVER_LABEL) and jobs.is_loaded(other)
    state.write_text(json.dumps({"mode": "launchd", "label": other, "port": 1, "token": "t"}))
    assert cli.main(["app", "--stop"]) == 0 and jobs.is_loaded(other)  # a state file never picks another job
