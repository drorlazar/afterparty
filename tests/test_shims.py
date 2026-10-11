import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import REPO


def _bare_env(env, with_python=True):
    cfg = Path(env["AFTERPARTY_CONFIG_DIR"])
    if with_python:
        (cfg / "python").write_text(sys.executable + "\n")
    keep = ("HOME", "AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_TEST_TRASH_DIR")
    return {k: env[k] for k in keep if k in env}  # no PATH at all


def test_shim_runs_with_no_path(env, tmp_path):
    link = tmp_path / "bin" / "afterparty"
    link.parent.mkdir()
    os.symlink(REPO / "bin" / "afterparty", link)
    r = subprocess.run(["/bin/sh", str(link), "autopilot", "status"], env=_bare_env(env), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and "Autopilot" in r.stdout, (r.stdout, r.stderr)


def test_shim_explains_missing_interpreter(env):
    r = subprocess.run(["/bin/sh", str(REPO / "bin" / "afterparty"), "report"], env=_bare_env(env, False),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 69 and "Re-run the installer" in r.stderr


def test_no_hardcoded_system_python():
    for f in ("bin/afterparty", "bin/cleanup", "bin/afterparty-session-end", "app/Afterparty.swift"):
        assert "/usr/bin/python3" not in (REPO / f).read_text(), f


def test_swift_has_no_private_fallback():
    src = (REPO / "app" / "Afterparty.swift").read_text()
    assert "/Users/" not in src and '"-m", "cleanup.cli", "app"' in src


def test_server_python_prefers_sidecar(env, monkeypatch, tmp_path):
    from cleanup import launcher
    fake = tmp_path / "py"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    (Path(env["AFTERPARTY_CONFIG_DIR"]) / "python").write_text(str(fake) + "\n")
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", env["AFTERPARTY_CONFIG_DIR"])
    assert launcher.server_python() == str(fake)


def test_shim_ignores_a_useless_path(env, tmp_path):
    # Final review SHOULD-FIX: dirname/readlink/head resolve even when the caller's PATH finds nothing.
    for shim, args in (("afterparty", ["autopilot", "status"]),):
        link = tmp_path / "bin" / shim
        link.parent.mkdir(exist_ok=True)
        os.symlink(REPO / "bin" / shim, link)
        e = dict(_bare_env(env), PATH=str(tmp_path / "nothing-here"))
        r = subprocess.run(["/bin/sh", str(link), *args], env=e, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "Autopilot" in r.stdout, (r.stdout, r.stderr)
    for shim in ("afterparty", "cleanup"):
        head = (REPO / "bin" / shim).read_text().splitlines()[:6]
        assert any(l.startswith("PATH=/usr/bin:/bin") for l in head), shim


def test_shim_ignores_a_cleanup_folder_in_the_working_directory(env, tmp_path):
    shadow = tmp_path / "work" / "cleanup"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text('raise SystemExit("imported the wrong cleanup package")\n')
    r = subprocess.run(["/bin/sh", str(REPO / "bin" / "afterparty"), "autopilot", "status"], env=_bare_env(env),
                       cwd=str(shadow.parent), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Autopilot" in r.stdout, r.stderr


def test_shims_write_no_bytecode_into_the_checkout(env, tmp_path):
    import shutil
    copy = tmp_path / "checkout"
    shutil.copytree(REPO / "cleanup", copy / "cleanup", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "bin", copy / "bin")
    r = subprocess.run(["/bin/sh", str(copy / "bin" / "afterparty"), "autopilot", "status"], env=_bare_env(env),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not list(copy.rglob("__pycache__")) and not list(copy.rglob("*.pyc"))
    for f in ("bin/afterparty", "bin/cleanup", "bin/afterparty-session-end"):
        assert ' -I -B -c "$entry"' in (REPO / f).read_text(), f


def test_session_end_shim_is_silent_without_python(env):
    r = subprocess.run(["/bin/sh", str(REPO / "bin" / "afterparty-session-end")], env=_bare_env(env, False),
                       input='{"cwd": "/"}', capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_session_end_shim_records_the_session_in_the_ledger(env, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".scratch" / "run1").mkdir(parents=True)
    (proj / ".scratch" / "run1" / "f.bin").write_bytes(b"x" * 4096)
    e = dict(_bare_env(env), DISKCLEAN_LEDGER_MIN_BYTES="0")
    r = subprocess.run(["/bin/sh", str(REPO / "bin" / "afterparty-session-end")], env=e,
                       input=json.dumps({"cwd": str(proj), "session_id": "s-1"}), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    ledger = Path(env["AFTERPARTY_STATE_DIR"]) / "sessions.jsonl"
    deadline = time.time() + 15            # the hook detaches; the scan finishes in the background
    while not ledger.exists() and time.time() < deadline:
        time.sleep(0.1)
    (entry,) = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert entry["session"] == "s-1" and entry["cwd"] == str(proj) and entry["dirs"] == [str(proj / ".scratch")]


def test_every_bin_file_is_a_sh_shim_and_the_legacy_wrappers_are_gone():
    assert not (REPO / "bin" / "diskclean").exists() and not (REPO / "bin" / "diskclean-session-end").exists()
    for f in (REPO / "bin").iterdir():
        assert f.read_text().startswith("#!/bin/sh\n"), f.name
        assert f.stat().st_mode & 0o111, f.name
