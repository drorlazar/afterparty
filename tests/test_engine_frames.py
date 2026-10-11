import time
from pathlib import Path

from tests.conftest import manifest, seq

NOW = time.time()


def frames(env):
    return sorted(Path(p).relative_to(Path(p).parents[2]).as_posix()
                  for _, k, _, _, p, _ in manifest(env) if k == "frames")


def test_shots_are_not_versions(run_cli, env, tmp_path):
    seq(tmp_path / "p/renders/shot_01")
    seq(tmp_path / "p/renders/shot_02")
    run_cli("--root", tmp_path / "p", "--tier", "3")
    assert frames(env) == []


def test_highest_version_kept_even_if_older(run_cli, env, tmp_path):
    seq(tmp_path / "p/renders/v9/frames", ts=NOW - 20 * 86400)   # newest mtime
    seq(tmp_path / "p/renders/v10/frames", ts=NOW - 40 * 86400)
    seq(tmp_path / "p/renders/v11/frames", ts=NOW - 60 * 86400)  # oldest mtime, highest version
    run_cli("--root", tmp_path / "p", "--tier", "3")
    assert frames(env) == ["renders/v10/frames", "renders/v9/frames"]


def test_recent_old_version_is_kept(run_cli, env, tmp_path):
    seq(tmp_path / "p/renders/v1", ts=NOW - 2 * 86400)  # touched 2 days ago
    seq(tmp_path / "p/renders/v2")
    run_cli("--root", tmp_path / "p", "--tier", "3", "--days", "14")
    assert frames(env) == []


def test_protected_folders_are_skipped(run_cli, env, tmp_path):
    seq(tmp_path / "p/assets/sprites/v1")
    seq(tmp_path / "p/assets/sprites/v2")
    run_cli("--root", tmp_path / "p", "--tier", "3")
    assert frames(env) == []
