"""Deferred review minors in fsops, owned, edits and settings_hook (plan-b rulings, Tasks 6A and 8)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cleanup import fsops
from cleanup.installer import edits, owned, settings_hook
from tests.conftest import REPO

GROUP = settings_hook.entry("/Users/u/.local/bin/afterparty-session-end")


def test_no_loadable_c_library_fails_closed_instead_of_at_import(tmp_path):
    code = ("import ctypes\n"
            "def no(*a, **k):\n    raise OSError('no libc')\n"
            "ctypes.CDLL = no\n"
            "from cleanup import fsops\n"
            "r = fsops.rename_exclusive(%r, %r)\n"
            "assert fsops._renamex is None and r.startswith('failed'), r\n" % (str(tmp_path / "a"), str(tmp_path / "b")))
    (tmp_path / "a").write_text("x")
    subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True)
    assert (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_note_made_or_warn_never_raises(tmp_path, capsys):
    (tmp_path / fsops.RECEIPTS).write_text("someone else's")
    (tmp_path / "made").mkdir()
    fsops.note_made_or_warn(str(tmp_path / "made"))
    assert "no receipt for the folder" in capsys.readouterr().err
    assert (tmp_path / fsops.RECEIPTS).read_text() == "someone else's"


def test_backup_receipts_every_folder_it_made_and_only_those(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{"a": 1}')
    settings_hook.add(str(s), GROUP, backup_dir=str(tmp_path / "x" / "b"))
    assert fsops.receipts(str(tmp_path)) == {"x/": "dir"} and fsops.receipts(str(tmp_path / "x")) == {"b/": "dir"}
    (tmp_path / "pre").mkdir()
    settings_hook.remove(str(s), GROUP, backup_dir=str(tmp_path / "pre"))
    assert "pre/" not in fsops.receipts(str(tmp_path))  # it was already there: not ours


def test_backup_never_receipts_a_folder_that_appeared_meanwhile(tmp_path, monkeypatch):
    s, b = tmp_path / "settings.json", tmp_path / "b"
    s.write_text('{"a": 1}')
    real = os.mkdir

    def theirs_first(p, *a):
        if str(p) == str(b):
            real(p)  # someone else makes it in the instant before ours
            raise FileExistsError(p)
        return real(p, *a)
    monkeypatch.setattr(edits.os, "mkdir", theirs_first)
    assert settings_hook.add(str(s), GROUP, backup_dir=str(b)) is True
    assert "b/" not in fsops.receipts(str(tmp_path)) and len(list(b.iterdir())) == 2  # the backup + its receipt


def test_backup_folder_receipt_that_cant_be_written_only_warns(tmp_path, capsys):
    s = tmp_path / "settings.json"
    s.write_text('{"a": 1}')
    (tmp_path / fsops.RECEIPTS).write_text("edited by hand")
    assert settings_hook.add(str(s), GROUP, backup_dir=str(tmp_path / "b")) is True
    assert settings_hook.status(s.read_text(), GROUP) == "same" and "no receipt for the folder" in capsys.readouterr().err


def test_a_file_vanishing_after_the_read_is_retried(tmp_path):
    s = tmp_path / "f.txt"
    s.write_text("old")
    seen = []

    def transform(text):
        seen.append(text)
        if text is not None:
            s.unlink()  # gone between our read and the chmod
        return "new"
    assert edits.edit(str(s), transform) is True
    assert seen == ["old", None] and s.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_an_odd_look_alike_stage_never_stops_the_sweep(tmp_path, monkeypatch, capsys):
    odd = edits.make_stage(str(tmp_path), "take", orig=123)  # a marker in our shape with a nonsense orig
    (tmp_path / os.path.basename(odd) / "item").write_text("whose?")
    stuck = edits.make_stage(str(tmp_path), "put", orig=str(tmp_path / "x"))
    fine = edits.make_stage(str(tmp_path), "put", orig=str(tmp_path / "y"))
    real = edits.shutil.rmtree

    def refuse(p, *a, **k):
        if p == stuck:
            raise PermissionError(13, "denied", p)
        return real(p, *a, **k)
    monkeypatch.setattr(edits.shutil, "rmtree", refuse)
    assert edits.sweep(str(tmp_path)) == ["put"]
    assert os.path.isdir(odd) and os.path.isfile(os.path.join(odd, "item")) and os.path.isdir(stuck)
    assert not os.path.exists(fine) and "couldn't clear the staging folder" in capsys.readouterr().err


def test_bootstrap_says_what_failed_when_the_move_fails(tmp_path, monkeypatch):
    state = tmp_path / "s" / "afterparty"
    monkeypatch.setattr(fsops, "rename_exclusive", lambda a, b: "failed: no space left")
    with pytest.raises(OSError, match="couldn't be created .*no space left") as e:
        owned.bootstrap(str(state))
    assert not isinstance(e.value, owned.Changed) and not state.exists()
    monkeypatch.undo()
    assert edits.sweep(str(tmp_path)) == ["bootstrap"] and os.listdir(str(tmp_path)) == []


def test_put_puts_the_old_copy_back_if_its_verified_receipt_fails(tmp_path, monkeypatch):
    dst = tmp_path / "f"
    dst.write_text("old")
    real = edits.update_mark

    def fail_verified(stage, **fields):
        if fields.get("verified"):
            raise OSError("disk full")
        return real(stage, **fields)
    monkeypatch.setattr(edits, "update_mark", fail_verified)
    with pytest.raises(OSError, match="disk full"):
        owned.put(str(dst), lambda p: Path(p).write_text("new"), fsops.sum_of(str(dst)))
    assert dst.read_text() == "old" and os.listdir(str(tmp_path)) == ["f"]


def test_take_if_records_the_sum_from_before_the_check(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_text("as found")
    before = fsops.sum_of(str(p))
    marks = []
    real = edits.update_mark
    monkeypatch.setattr(edits, "update_mark", lambda stage, **f: (marks.append(f), real(stage, **f)))

    def ok(staged):
        with open(staged, "w") as f:
            f.write("changed while it was judged")
        return True
    assert owned.take_if(str(p), ok) == "gone"
    assert marks == [{"verified": True, "item_sum": before}]


def test_take_if_with_an_unreadable_item_still_lets_ok_decide(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_text("x")

    def unreadable(path):
        raise PermissionError(13, "denied", path)
    monkeypatch.setattr(owned, "sum_of", unreadable)
    assert owned.take_if(str(p), lambda staged: False) == "restored" and p.read_text() == "x"


def test_tab_indentation_is_kept(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text('{\n\t"model": "a"\n}\n')
    settings_hook.add(str(s), GROUP)
    assert '\n\t"model": "a"' in s.read_text() and '\n\t\t"SessionEnd"' in s.read_text()
    assert json.loads(s.read_text())["model"] == "a"
