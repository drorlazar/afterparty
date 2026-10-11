from pathlib import Path

from tests.conftest import git, manifest, set_config


def _bare_remote(repo, tmp_path, env):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare), env=env)
    git(repo, "remote", "add", "origin", str(bare), env=env)
    git(repo, "push", "-q", "-u", "origin", "main", env=env)
    return bare


def wt_rows(env):
    return {Path(p).name: (mode, reason) for _, k, _, mode, p, reason in manifest(env) if k == "worktree"}


def test_worktree_verdicts(run_cli, env, git_repo, tmp_path):
    r = git_repo()
    _bare_remote(r, tmp_path, env)
    git(r, "worktree", "add", "-q", ".worktrees/merged", "-b", "merged", env=env)
    git(r, "worktree", "add", "-q", ".worktrees/locked", "-b", "locked", env=env)
    git(r, "worktree", "lock", ".worktrees/locked", env=env)
    git(r, "worktree", "add", "-q", ".worktrees/dirty", "-b", "dirty", env=env)
    (r / ".worktrees/dirty/new.txt").write_text("x")
    git(r, "worktree", "add", "-q", ".worktrees/unpushed", "-b", "unpushed", env=env)
    git(r / ".worktrees/unpushed", "commit", "-q", "--allow-empty", "-m", "wip", env=env)
    git(r, "worktree", "add", "-q", ".worktrees/deliv", "-b", "deliv", env=env)
    (r / ".git/info/exclude").write_text("notes.txt\n")
    (r / ".worktrees/deliv/notes.txt").write_text("my notes")

    run_cli("--root", r, "--tier", "2", "--days", "0")
    rows = wt_rows(env)
    assert rows["merged"][0] == "auto"
    assert rows["locked"] == ("manual", rows["locked"][1]) and "locked" in rows["locked"][1]
    assert "uncommitted" in rows["dirty"][1]
    assert "NOT pushed" in rows["unpushed"][1]
    assert "ignored files" in rows["deliv"][1] and rows["deliv"][0] == "manual"


def test_apply_removes_only_safe_worktree(run_cli, env, git_repo, tmp_path):
    r = git_repo("home/repo")  # under home: classify protects /private/var (where tmp_path lives) as system data
    _bare_remote(r, tmp_path, env)
    git(r, "worktree", "add", "-q", ".worktrees/merged", "-b", "merged", env=env)
    git(r, "worktree", "add", "-q", ".worktrees/locked", "-b", "locked", env=env)
    git(r, "worktree", "lock", ".worktrees/locked", env=env)
    set_config(env, roots=[str(r)])  # a cleaning --root must sit inside the configured project folders
    res = run_cli("--root", r, "--tier", "2", "--days", "0", "--apply", "--yes")
    assert res.returncode == 0, res.stderr
    assert not (r / ".worktrees/merged").exists()
    assert (r / ".worktrees/locked").exists()


def test_worktree_merged_only_locally_is_kept(run_cli, env, git_repo, tmp_path):
    r = git_repo()
    _bare_remote(r, tmp_path, env)
    git(r, "worktree", "add", "-q", ".worktrees/feat", "-b", "feat", env=env)
    git(r / ".worktrees/feat", "commit", "-q", "--allow-empty", "-m", "work", env=env)
    git(r, "merge", "-q", "--ff-only", "feat", env=env)  # merged into local main, never pushed
    set_config(env, roots=[str(r)])  # a cleaning --root must sit inside the configured project folders
    res = run_cli("--root", r, "--tier", "2", "--days", "0", "--apply", "--yes")
    assert res.returncode == 0, res.stderr
    assert (r / ".worktrees/feat").exists()
    assert "NOT pushed" in wt_rows(env)["feat"][1]


def test_worktree_pushed_is_removed(run_cli, env, git_repo, tmp_path):
    r = git_repo("home/repo")  # under home: classify protects /private/var (where tmp_path lives) as system data
    _bare_remote(r, tmp_path, env)
    wt = r / ".worktrees/feat"
    git(r, "worktree", "add", "-q", ".worktrees/feat", "-b", "feat", env=env)
    git(wt, "commit", "-q", "--allow-empty", "-m", "work", env=env)
    git(wt, "push", "-q", "-u", "origin", "feat", env=env)
    set_config(env, roots=[str(r)])  # a cleaning --root must sit inside the configured project folders
    res = run_cli("--root", r, "--tier", "2", "--days", "0", "--apply", "--yes")
    assert res.returncode == 0, res.stderr
    assert not wt.exists()


def test_worktree_without_remote_is_kept(run_cli, env, git_repo):
    r = git_repo()
    git(r, "worktree", "add", "-q", ".worktrees/feat", "-b", "feat", env=env)
    run_cli("--root", r, "--tier", "2", "--days", "0")
    mode, why = wt_rows(env)["feat"]
    assert mode == "manual" and "NOT pushed" in why
