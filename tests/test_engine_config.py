from pathlib import Path

from tests.conftest import age, set_config, state


def _project_with_deps(env):
    home = Path(env["HOME"])
    nm = home / "Projects" / "app" / "node_modules"
    nm.mkdir(parents=True)
    (nm / "x.js").write_text("x")
    age(nm.parent)
    return nm


def test_no_config_refuses_destructive(env, run_cli):
    nm = _project_with_deps(env)
    (Path(env["AFTERPARTY_CONFIG_DIR"]) / "config.json").unlink()
    r = run_cli("--apply", "--yes")
    assert r.returncode == 2
    assert "isn't set up yet" in r.stderr
    assert nm.exists()
    report = run_cli()
    assert report.returncode == 0
    assert "isn't set up yet" in report.stderr


def test_malformed_config_refuses_everything(env, run_cli):
    nm = _project_with_deps(env)
    (Path(env["AFTERPARTY_CONFIG_DIR"]) / "config.json").write_text("{broken")
    r = run_cli("--apply", "--yes")
    assert r.returncode == 2 and "settings are invalid" in r.stderr
    assert nm.exists()


def test_roots_come_from_config(env, run_cli):
    home = Path(env["HOME"])
    other = home / "Code" / "app" / "node_modules"
    other.mkdir(parents=True)
    age(other.parent)
    _project_with_deps(env)
    set_config(env, roots=["~/Code"])
    run_cli()
    rows = (state(env) / "last-report-manifest.tsv").read_text()
    assert str(other) in rows
    assert "/Projects/app/node_modules" not in rows


def test_missing_root_is_noted_not_fatal(env, run_cli):
    set_config(env, roots=["~/Projects", "~/Gone"])
    r = run_cli()
    assert r.returncode == 0
    assert "~/Gone does not exist" in r.stderr


def test_keep_from_config_protects(env, run_cli):
    nm = _project_with_deps(env)
    set_config(env, keep=[str(nm.parent)])
    run_cli("--apply", "--yes")
    assert nm.exists()


def test_no_config_refuses_tier4(env, run_cli):
    home = Path(env["HOME"])
    (home / "Downloads").mkdir()
    (home / "Downloads" / "big.bin").write_bytes(b"x" * 100)
    (Path(env["AFTERPARTY_CONFIG_DIR"]) / "config.json").unlink()
    r = run_cli("--apply", "--tier", "4")
    assert r.returncode == 2
    assert "isn't set up yet" in r.stderr
    assert (home / "Downloads" / "big.bin").exists()
