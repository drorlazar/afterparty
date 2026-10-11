import json
import os

from cleanup.plan import undo as U
from tests.test_executor import run
from tests.test_manifest import tree


def test_undo_restores_trashed(tmp_path):
    t = tree(tmp_path)
    m, j, r = run(tmp_path, [str(t / "doc.txt")])
    assert not (t / "doc.txt").exists()
    u = U.undo(str(j))
    assert [os.path.basename(x["path"]) for x in u["restored"]] == ["doc.txt"]
    assert (t / "doc.txt").read_text() == "hello"


def test_undo_conflicts_and_missing(tmp_path):
    t = tree(tmp_path)
    (t / "b.txt").write_text("b")
    m, j, r = run(tmp_path, [str(t / "doc.txt"), str(t / "b.txt")])
    (t / "doc.txt").write_text("a new file took the old name")   # conflict
    os.remove(tmp_path / ".Trash/b.txt")                           # trash emptied
    u = U.undo(str(j))
    assert [os.path.basename(x["path"]) for x in u["conflict"]] == ["doc.txt"]
    assert [os.path.basename(x["path"]) for x in u["missing"]] == ["b.txt"]
    assert (t / "doc.txt").read_text() == "a new file took the old name"
    assert any(json.loads(l).get("event") == "undo" for l in j.read_text().splitlines())


def test_undo_refuses_sources_outside_the_trash(tmp_path):
    # Codex I6: a tampered journal must not move arbitrary files.
    secret = tmp_path / "id_ed25519"; secret.write_text("key")
    j = tmp_path / "j.jsonl"
    j.write_text(json.dumps({"event": "action", "result": "trashed", "path": str(tmp_path / "stolen"),
                             "trash_path": str(secret)}) + "\n")
    u = U.undo(str(j))
    assert u["restored"] == [] and secret.exists() and not (tmp_path / "stolen").exists()
