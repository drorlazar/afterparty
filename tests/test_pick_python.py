import os
import subprocess
import sys

import pytest

from tests.conftest import REPO

PICK = str(REPO / "bin" / "afterparty-pick-python")


def _stub(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def _pick(tmp_path, candidates, clt=True, args=()):
    xs = _stub(tmp_path / "xcode-select", "exit 0" if clt else "exit 2")
    env = {"HOME": str(tmp_path), "AFTERPARTY_PYTHON_CANDIDATES": candidates, "AFTERPARTY_XCODE_SELECT": xs}
    return subprocess.run(["/bin/sh", PICK, *args], env=env, capture_output=True, text=True, timeout=60)


def test_picks_the_first_python_that_runs(tmp_path):
    too_old = _stub(tmp_path / "old-python", "exit 1")   # runs, but its version check fails
    r = _pick(tmp_path, " ".join([str(tmp_path / "missing"), too_old, sys.executable]))
    assert r.returncode == 0 and r.stdout.strip() == sys.executable, r.stderr


def test_system_python_needs_the_command_line_tools(tmp_path):
    r = _pick(tmp_path, "/usr/bin/python3 " + sys.executable, clt=False)
    assert r.returncode == 0 and r.stdout.strip() == sys.executable


def test_intel_homebrew_python_works_without_the_command_line_tools(tmp_path):
    intel = tmp_path / "usr-local" / "bin"
    intel.mkdir(parents=True)
    os.symlink(sys.executable, intel / "python3")          # stands in for /usr/local/bin/python3
    r = _pick(tmp_path, "/usr/bin/python3 %s/missing %s" % (tmp_path, intel / "python3"), clt=False)
    assert r.returncode == 0 and r.stdout.strip() == str(intel / "python3")


def test_default_candidates_cover_every_supported_install():
    text = open(PICK).read()
    line = next(l for l in text.splitlines() if l.startswith("candidates=${AFTERPARTY_PYTHON_CANDIDATES:-"))
    text = line
    order = ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
             "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"]
    assert [text.index(c) for c in order] == sorted(text.index(c) for c in order)


@pytest.mark.skipif(subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode != 0,
                    reason="this Mac has no Command Line Tools")
def test_system_python_is_preferred_when_the_tools_exist(tmp_path):
    assert _pick(tmp_path, "/usr/bin/python3 " + sys.executable).stdout.strip() == "/usr/bin/python3"


def test_no_python_explains_the_fix(tmp_path):
    r = _pick(tmp_path, str(tmp_path / "missing"), clt=False)
    assert r.returncode == 69 and "xcode-select --install" in r.stderr and r.stdout == ""


def test_an_explicit_choice_is_still_checked(tmp_path):
    bad = _stub(tmp_path / "bad", "exit 1")
    assert _pick(tmp_path, "", args=[bad]).returncode == 69
    assert _pick(tmp_path, "", args=["python3"]).returncode == 69      # must be an absolute path
    assert _pick(tmp_path, "", args=[sys.executable]).stdout.strip() == sys.executable


def test_a_candidate_path_may_contain_spaces(tmp_path):
    d = tmp_path / "with space"
    d.mkdir()
    py = d / "python3"
    py.symlink_to(sys.executable)
    r = _pick(tmp_path, str(tmp_path / "missing one") + "\n" + str(py))
    assert r.returncode == 0 and r.stdout.strip() == str(py), r.stderr
