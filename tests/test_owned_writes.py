"""Ruling R14: one writer for every file Afterparty owns. Outside cleanup/fsops.py nothing may replace, rename
over, copy over or truncate an existing file; owned files are written only through fsops.write_owned /
replace_owned / swap_owned (exclusive create or compare-and-swap)."""
import json
import re

import pytest

from cleanup import fsops
from tests.conftest import REPO

FORBIDDEN = [r"\bos\.replace\(", r"\bos\.rename\(", r"\bshutil\.move\(", r"\bshutil\.copy(2|file)?\(", r"\bO_TRUNC\b",
             r"\bopen\([^)]*,\s*[\"']wb?[\"']", r"\.write_(text|bytes)\("]
ALLOWED = {  # file: (substring of the line, reason)
    "cleanup/plan/bridge.py": ("shutil.move(p, landing)", "the stand-in Trash for tests (AFTERPARTY_TEST_TRASH_DIR)"),
    "cleanup/engine/core.py": ("open(os.devnull, \"w\")", "writes to /dev/null"),
}


def _sources():
    for root in (REPO / "cleanup", REPO / "scripts", REPO / "bin"):
        if root.exists():
            for p in sorted(root.rglob("*.py")):
                if p.name != "fsops.py" and "private_tests" not in p.parts:
                    yield p


def test_no_afterparty_code_overwrites_a_file_outside_fsops():
    bad = []
    for p in _sources():
        rel = str(p.relative_to(REPO))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not any(re.search(f, code) for f in FORBIDDEN):
                continue
            ok = ALLOWED.get(rel)
            if ok and ok[0] in code:
                continue
            bad.append("%s:%d: %s" % (rel, n, line.strip()))
    assert bad == []


def test_server_state_is_never_written_over_a_replacement(tmp_path):
    from cleanup import launcher
    p = tmp_path / "server.json"
    launcher._write_private(str(p), {"port": 1})
    assert fsops.receipts(str(tmp_path))["server.json"] == fsops.file_sum(str(p))
    launcher._write_private(str(p), {"port": 2})                       # ours, unchanged since: replaced
    assert json.loads(p.read_text()) == {"port": 2}


def test_a_state_file_changed_after_every_read_is_never_clobbered(tmp_path, monkeypatch):
    """replace_owned compares against what it read; bytes written after that read are never replaced unseen."""
    real, n = fsops._bytes, []
    target = tmp_path / "x.json"
    target.write_bytes(b"old")

    def racing(path):
        got = real(path)
        if path == str(target) and len(n) < 10:
            n.append(1)
            target.write_bytes(b"someone else's #%d" % len(n))   # lands just after each of our reads
        return got
    monkeypatch.setattr(fsops, "_bytes", racing)
    with pytest.raises(fsops.Conflict) as e:
        fsops.replace_owned(str(target), b"ours")
    last = b"someone else's #%d" % len(n)
    assert last in (target.read_bytes(), e.value.kept and open(e.value.kept, "rb").read())   # never lost
