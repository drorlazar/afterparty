from pathlib import Path

from tests.conftest import seq, state


def _ids(env, name="last-tidy-report"):
    rows = [l.split("\t") for l in (state(env) / f"{name}-manifest.tsv").read_text().splitlines()
            if not l.startswith("#")]
    return {r[4]: r[6] for r in rows}


def _tidy_tree(home, folder="My Café Project"):
    proj = home / "Projects" / folder
    (proj / ".scratch" / "run1").mkdir(parents=True)
    (proj / ".scratch" / "run1" / "x.png").write_text("x")
    seq(proj / "renders" / "shot_v1")
    return proj


def test_report_numbers_items(env, run_cli):
    proj = _tidy_tree(Path(env["HOME"]))
    r = run_cli("tidy", str(proj))
    assert "#1 " in r.stdout and "Afterparty report" in r.stdout
    assert sorted(_ids(env).values()) == ["1", "2"]


def test_tidy_only_with_spaces_and_accents(env, run_cli):
    proj = _tidy_tree(Path(env["HOME"]))
    run_cli("tidy", str(proj))
    scratch_id = _ids(env)[str(proj / ".scratch")]
    run_cli("tidy", str(proj), "--apply", "--yes", "--only", scratch_id)
    assert not (proj / ".scratch").exists()
    assert (proj / "renders" / "shot_v1").exists()


def test_tidy_skip(env, run_cli):
    proj = _tidy_tree(Path(env["HOME"]))
    run_cli("tidy", str(proj))
    scratch_id = _ids(env)[str(proj / ".scratch")]
    run_cli("tidy", str(proj), "--apply", "--yes", "--skip", scratch_id)
    assert (proj / ".scratch").exists() and not (proj / "renders" / "shot_v1").exists()


def test_unknown_id_refuses(env, run_cli):
    proj = _tidy_tree(Path(env["HOME"]))
    run_cli("tidy", str(proj))
    r = run_cli("tidy", str(proj), "--apply", "--yes", "--only", "99")
    assert r.returncode == 2 and "Unknown item #99" in r.stderr
    assert (proj / ".scratch").exists()


def test_only_needs_a_report_for_this_folder(env, run_cli):
    proj = _tidy_tree(Path(env["HOME"]))
    other = _tidy_tree(Path(env["HOME"]), "Other")
    run_cli("tidy", str(other))
    r = run_cli("tidy", str(proj), "--apply", "--yes", "--only", "1")
    assert r.returncode == 2 and "Run the report for this folder first" in r.stderr
