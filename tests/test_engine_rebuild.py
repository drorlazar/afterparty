import os
from pathlib import Path

from tests.conftest import OLD, age, git, manifest


def test_tracked_build_kept_ignored_dist_listed(run_cli, env, git_repo):
    r = git_repo()
    (r / "package.json").write_text("{}")
    (r / "build").mkdir(); (r / "build/keep.js").write_text("src")
    (r / "dist").mkdir(); (r / "dist/out.js").write_text("x")
    (r / ".gitignore").write_text("dist/\n")
    git(r, "add", ".", env=env); git(r, "commit", "-qm", "c", env=env)
    age(r)
    run_cli("--root", r, "--tier", "1")
    found = {Path(p).name for _, k, _, _, p, _ in manifest(env)}
    assert "dist" in found and "build" not in found


def test_build_outside_git_is_skipped(run_cli, env, tmp_path):
    p = tmp_path / "ng"
    (p / "build").mkdir(parents=True); (p / "package.json").write_text("{}")
    age(p)
    run_cli("--root", p, "--tier", "1")
    assert all(Path(x).name != "build" for *_, x, _ in manifest(env))


def test_symlinked_node_modules_is_skipped(run_cli, env, tmp_path):
    precious = tmp_path / "outside/precious"; precious.mkdir(parents=True)
    (precious / "f").write_text("keep")
    p = tmp_path / "sp"; p.mkdir(); (p / "package.json").write_text("{}")
    os.symlink(precious, p / "node_modules")
    age(p)
    run_cli("--root", p, "--tier", "1")
    assert manifest(env) == []


def test_deep_fresh_edit_keeps_project_active(run_cli, env, tmp_path):
    a = tmp_path / "a"
    (a / "node_modules/x").mkdir(parents=True); (a / "src/deep/er").mkdir(parents=True)
    (a / "package.json").write_text("{}")
    age(a)
    (a / "src/deep/er/file.ts").write_text("edit")  # fresh mtime on a deep file only
    # re-age the folders themselves (NOT recursively), so only a deep walk sees the edit
    for d in (a / "src/deep/er", a / "src/deep", a / "src", a):
        os.utime(d, (OLD, OLD))
    run_cli("--root", a, "--tier", "1")
    assert all(k != "node_modules" for _, k, *_ in manifest(env))


def test_idle_project_node_modules_listed(run_cli, env, tmp_path):
    b = tmp_path / "b"
    (b / "node_modules/x").mkdir(parents=True); (b / "package.json").write_text("{}")
    age(b)
    run_cli("--root", b, "--tier", "1")
    assert [k for _, k, *_ in manifest(env)] == ["node_modules"]


def test_dependency_folder_used_by_a_configured_tool_is_kept(env, run_cli, tmp_path):
    home = Path(env["HOME"])
    proj = home / "Projects" / "mcp-server"
    venv = proj / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (proj / "server.py").write_text("x")
    age(proj)
    (home / ".claude.json").write_text('{"mcpServers": {"s": {"command": "%s/bin/python3"}}}' % venv)
    r = run_cli("--root", proj, "--tier", "1")
    assert r.returncode == 0, r.stderr
    assert ".venv" not in [row[1] for row in manifest(env)]
    (home / ".claude.json").write_text("{}")
    run_cli("--root", proj, "--tier", "1")
    assert [row[1] for row in manifest(env)] == [".venv"]
