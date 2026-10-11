import sys
from pathlib import Path

import pytest

from tests.conftest import manifest, seq


def test_help(run_cli):
    r = run_cli("--help")
    assert r.returncode == 0
    assert "afterparty" in r.stdout


SYSTEM_PY = "/usr/bin/python3"


@pytest.mark.parametrize("python", [sys.executable] + ([SYSTEM_PY] if Path(SYSTEM_PY).exists() else []))
def test_dry_run_on_fixture_writes_scoped_manifest(run_cli, env, tmp_path, python):
    root = tmp_path / "proj"
    seq(root / "renders/v1")
    seq(root / "renders/v2")
    r = run_cli("--root", root, "--tier", "3", python=python)
    assert r.returncode == 0, r.stderr
    assert "Dry run" in r.stdout
    rows = manifest(env)
    assert [Path(p).name for _, k, _, _, p, _ in rows if k == "frames"] == ["v1"]


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="system python missing")
def test_runs_under_system_python(run_cli):
    r = run_cli("--help", python="/usr/bin/python3")
    assert r.returncode == 0, r.stderr
