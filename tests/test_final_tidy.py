"""Final review fix I5: tidy --only/--skip ids come only from the report you saw. Every test runs in a temp home with stubs."""
import os
import sys
from pathlib import Path

import pytest

from tests.conftest import state


# ---- I5: tidy --only/--skip never maps ids through an item list the user didn't see
def _tidy_tree(home):
    proj = home / "Projects" / "p"
    (proj / ".scratch" / "run1").mkdir(parents=True)
    (proj / ".scratch" / "run1" / "x.bin").write_bytes(b"x" * 10)
    (proj / "node_modules" / "a").mkdir(parents=True)
    (proj / "package.json").write_text("{}")
    (proj / "node_modules" / "a" / "i.js").write_bytes(b"y" * 200000)
    return proj


def _ids(env):
    rows = [l.split("\t") for l in (state(env) / "last-tidy-report-manifest.tsv").read_text().splitlines()
            if not l.startswith("#")]
    return {r[4]: r[6] for r in rows if len(r) > 6 and r[6]}


def test_tidy_only_refuses_an_item_list_from_another_report(env, run_cli):
    from tests.conftest import age
    proj = _tidy_tree(Path(env["HOME"]))
    age(proj)
    assert run_cli("tidy", str(proj)).returncode == 0
    old_manifest = (state(env) / "last-tidy-report-manifest.tsv").read_bytes()
    nm = str(proj / "node_modules")
    first = _ids(env)
    (proj / ".scratch" / "run1" / "big.bin").write_bytes(b"z" * 900000)   # now .scratch outranks node_modules
    age(proj)
    assert run_cli("tidy", str(proj)).returncode == 0
    assert _ids(env)[nm] != first[nm]
    # the second report's item list couldn't be saved: the saved list is the first report's, the text the second's
    (state(env) / "last-tidy-report-manifest.tsv").write_bytes(old_manifest)
    r = run_cli("tidy", str(proj), "--apply", "--yes", "--only", first[nm])
    assert r.returncode == 2 and "run the report again" in r.stderr.lower(), r.stdout + r.stderr
    assert (proj / "node_modules").exists() and (proj / ".scratch").exists()


def test_a_report_saves_its_item_list_before_its_text_and_says_when_ids_cant_be_used(env, monkeypatch, tmp_path,
                                                                                     capsys):
    from cleanup import fsops
    from cleanup.engine import core
    monkeypatch.setattr(core, "STATE", str(tmp_path / "st"))
    item = core.Item(1, "node_modules", str(tmp_path / "x"), "rebuildable", size=5)
    order = []
    real = fsops.replace_owned

    def failing_manifest(path, data, *a, **k):
        order.append(os.path.basename(path))
        if path.endswith("-manifest.tsv"):
            raise fsops.Conflict("busy")
        return real(path, data, *a, **k)
    monkeypatch.setattr(fsops, "replace_owned", failing_manifest)
    core.report([item], [], 0, [], sys.stdout, scope="tidy x", scope_path=os.path.realpath(tmp_path))
    assert order == ["last-tidy-report-manifest.tsv"]                    # the text isn't saved without its list
    assert "--only" in capsys.readouterr().err

    def failing_text(path, data, *a, **k):
        if path.endswith(".txt"):
            raise fsops.Conflict("busy")
        return real(path, data, *a, **k)
    monkeypatch.setattr(fsops, "replace_owned", failing_text)
    core.report([item], [], 0, [], sys.stdout, scope="tidy x", scope_path=os.path.realpath(tmp_path))
    assert "--only" in capsys.readouterr().err
    with pytest.raises(core.SelectError):                               # saved list and saved text disagree
        core._select([item], str(tmp_path), "1", None, core._last_tidy_manifest())


def test_tidy_only_still_works_after_a_normal_report(env, run_cli):
    from tests.conftest import age
    proj = _tidy_tree(Path(env["HOME"]))
    age(proj)
    assert run_cli("tidy", str(proj)).returncode == 0
    r = run_cli("tidy", str(proj), "--apply", "--yes", "--only", _ids(env)[str(proj / ".scratch")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (proj / ".scratch").exists() and (proj / "node_modules").exists()
