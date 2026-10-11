"""Final review fix M1: CDPATH never changes where the sh entry points think they live. Every test runs in a temp home with stubs."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import REPO


# ---- M1: CDPATH never changes where the sh entry points think they live
@pytest.mark.parametrize("script,args,rc,says", [
    (REPO.name + "/install.sh", ["--dry-run", "--python", sys.executable], 0, "install plan"),
    (REPO.name + "/uninstall.sh", ["--dry-run"], 1, "install record"),
    (REPO.name + "/bin/afterparty", ["autopilot", "status"], 0, "Autopilot")])
def test_sh_entry_points_ignore_cdpath(env, script, args, rc, says):
    """With CDPATH set, `cd relative/dir` prints the folder, which used to end up in the path the scripts use."""
    (Path(env["AFTERPARTY_CONFIG_DIR"]) / "python").write_text(sys.executable + "\n")
    e = dict(env, CDPATH=str(REPO.parent), AFTERPARTY_LAUNCHCTL="/usr/bin/false")
    r = subprocess.run(["/bin/sh", script, *args], cwd=REPO.parent, env=e, capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode == rc and says in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr


def test_every_sh_entry_point_unsets_cdpath():
    for f in ["install.sh", "uninstall.sh", *("bin/" + n for n in os.listdir(REPO / "bin"))]:
        assert "unset CDPATH" in (REPO / f).read_text(), f
