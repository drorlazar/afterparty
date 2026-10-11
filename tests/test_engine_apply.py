import os
import shutil
import stat
from pathlib import Path

import pytest

from tests.conftest import age, git, manifest, seq, set_config, state


def test_tier_without_apply_never_deletes(run_cli, env, tmp_path):
    b = tmp_path / "b"; (b / "node_modules/x").mkdir(parents=True); (b / "package.json").write_text("{}")
    age(b)
    r = run_cli("--root", b, "--tier", "1,4", "--yes")
    assert r.returncode == 0, r.stderr
    assert (b / "node_modules").exists()


def test_apply_deletes_old_versions_keeps_latest(run_cli, env, tmp_path):
    p = Path(env["HOME"]) / "p"  # under home: classify protects /private/var (where tmp_path lives) as system data
    seq(p / "renders/v1"); seq(p / "renders/v2")
    set_config(env, roots=[str(p)])
    run_cli("--root", p, "--tier", "3", "--apply", "--yes")
    assert not any((p / "renders/v1").glob("*.png"))
    assert len(list((p / "renders/v2").glob("*.png"))) == 40


def test_keep_txt_protects_tidy_latest(run_cli, env, tmp_path):
    p = tmp_path / "p"
    seq(p / "renders/shot_01"); seq(p / "renders/shot_02")
    set_config(env, keep=[str(p / "renders/shot_01")], roots=[str(p)])
    r = run_cli("tidy", p)
    assert "shot_02" in r.stdout and "shot_01" not in r.stdout


def test_scoped_run_does_not_touch_global_report(run_cli, env, tmp_path):
    st = state(env); st.mkdir(parents=True)
    (st / "last-report.txt").write_text("GLOBAL MARKER")
    b = tmp_path / "b"; b.mkdir()
    run_cli("--root", b, "--tier", "1")
    run_cli("--root", b, "--tier", "1,3")
    assert (st / "last-report.txt").read_text() == "GLOBAL MARKER"


def test_paths_with_spaces_and_unicode(run_cli, env, tmp_path):
    p = Path(env["HOME"]) / "klondike - Oranges ✦ שלום"  # under home: tmp_path itself counts as system data
    seq(p / "renders/v1"); seq(p / "renders/v2")
    set_config(env, roots=[str(p)])
    run_cli("--root", p, "--tier", "3", "--apply", "--yes")
    assert not any((p / "renders/v1").glob("*.png"))
    assert any((p / "renders/v2").glob("*.png"))


def test_vanished_target_is_logged_not_fatal(run_cli, env, tmp_path):
    # Delete the candidate after the scan but before deletion by racing through a
    # sitecustomize hook that removes the folder after it was reviewed, right before it is acted on.
    b = Path(env["HOME"]) / "b"; (b / "node_modules/x").mkdir(parents=True); (b / "package.json").write_text("{}")
    age(b)
    hook = tmp_path / "hook"; hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import shutil, cleanup.engine.core as c\n"
        "_orig = c.run_prepared\n"
        "def run_prepared(prep, *a, **k):\n"
        f"    shutil.rmtree({str(b / 'node_modules')!r}, ignore_errors=True)\n"
        "    return _orig(prep, *a, **k)\n"
        "c.run_prepared = run_prepared\n")
    env["PYTHONPATH"] = f"{hook}{os.pathsep}{env['PYTHONPATH']}"
    set_config(env, roots=[str(b)])
    r = run_cli("--root", b, "--tier", "1", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert (b / "package.json").exists()
    assert "failed" not in r.stdout  # nothing left to act on is a skip, not a failure
    log = next(state(env).glob("log-*.tsv")).read_text()
    assert any(l.split("\t")[1] == "SKIP" and l.split("\t")[4].endswith("node_modules")
               and "no longer exists" in l for l in log.splitlines())
    # Final review SHOULD-FIX: SKIP/FAIL rows say what the item was and which tier offered it
    assert any(l.split("\t")[1] == "SKIP" and l.split("\t")[2] == "node_modules/tier1" for l in log.splitlines())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_folder_reported(run_cli, env, tmp_path):
    p = tmp_path / "p"; locked = p / "secret"; locked.mkdir(parents=True)
    (locked / "data.bin").write_bytes(b"x")
    seq(p / "renders/v1"); seq(p / "renders/v2")
    os.chmod(locked, 0)
    try:
        r = run_cli("--root", p, "--tier", "3")
        assert r.returncode == 0, r.stderr
        assert "Could not read" in r.stdout
        assert any(k == "frames" for _, k, *_ in manifest(env)), "scan must still report candidates outside the unreadable folder"
    finally:
        os.chmod(locked, stat.S_IRWXU)
