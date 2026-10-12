import subprocess

from tests.conftest import REPO

SCRIPT = REPO / "app" / "build-app.sh"


def _run(dest, extra):
    env = {"HOME": str(dest.parent), "AFTERPARTY_APP_DEST": str(dest), "PATH": "/usr/bin:/bin",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)


def test_a_bad_build_dir_is_refused_before_any_compile(tmp_path):
    dest = tmp_path / "Applications"
    out = tmp_path / "out"
    out.mkdir()
    (out / "Afterparty.app").mkdir()
    r = _run(dest, {"AFTERPARTY_APP_BUILD_DIR": str(out)})
    assert r.returncode == 2 and "AFTERPARTY_APP_BUILD_DIR" in r.stderr
    r = _run(dest, {"AFTERPARTY_APP_BUILD_DIR": str(tmp_path / "missing")})
    assert r.returncode == 2
    assert not dest.exists()                       # nothing was staged anywhere


def test_build_script_cleans_only_its_own_stage_and_reports_codesign():
    s = SCRIPT.read_text()
    assert 'STAGE=""' in s and 'rm -rf "$WORK"; if [ -n "$STAGE" ]' in s
    assert "codesign failed" in s and "exit 70" in s
