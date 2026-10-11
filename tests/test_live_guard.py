from cleanup.cli import live_guard
from tests.conftest import git


def _engine_repo(git_repo, env):
    r = git_repo()
    (r / "cleanup").mkdir()
    (r / "cleanup/core.py").write_text("v1\n")
    git(r, "add", ".", env=env)
    git(r, "commit", "-qm", "engine", env=env)
    return r


def test_blocks_destructive_runs_when_engine_has_uncommitted_edits(git_repo, env):
    r = _engine_repo(git_repo, env)
    (r / "cleanup/core.py").write_text("half-edited\n")
    for flag in ("--auto", "--apply"):
        assert live_guard([flag], repo=r, env=env), flag


def test_allows_dry_runs_even_when_dirty(git_repo, env):
    r = _engine_repo(git_repo, env)
    (r / "cleanup/core.py").write_text("half-edited\n")
    assert live_guard(["--tier", "1"], repo=r, env=env) is None
    assert live_guard(["--check-space", "50"], repo=r, env=env) is None


def test_allows_destructive_runs_when_engine_is_committed(git_repo, env):
    r = _engine_repo(git_repo, env)
    assert live_guard(["--auto"], repo=r, env=env) is None


def test_fails_closed_when_git_status_fails(tmp_path, env):
    assert live_guard(["--apply"], repo=tmp_path, env=env)  # not a git repo
