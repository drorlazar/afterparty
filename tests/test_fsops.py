import json
import os
import threading
import time

import pytest

from cleanup import fsops


def test_a_lock_file_deleted_while_held_is_never_shared(tmp_path):
    p = str(tmp_path / "x.lock")
    seen = {}

    def waiter():
        with fsops.held(p) as fd:
            seen["same"] = os.fstat(fd).st_ino == os.stat(p).st_ino

    with fsops.held(p):
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.3)
        os.unlink(p)                   # e.g. --purge removing it while holding it
    t.join(5)
    assert seen["same"] is True        # the waiter locked the new file, not the deleted one


def test_a_lock_file_it_creates_is_receipted_and_one_it_finds_is_not(tmp_path):
    mine, theirs = tmp_path / "mine.lock", tmp_path / "theirs.lock"
    theirs.write_text("")
    with fsops.held(str(mine)):
        pass
    with fsops.held(str(theirs)):
        pass
    rec = fsops.receipts(str(tmp_path))
    assert rec == {"mine.lock": fsops.file_sum(str(mine))}


def test_receipts_record_the_checksum_at_write_time(tmp_path):
    f = tmp_path / "data.json"
    f.write_text("1")
    fsops.note_written(str(f), f.read_bytes())
    assert fsops.receipts(str(tmp_path)) == {"data.json": fsops.file_sum(str(f))}
    f.write_text("2")                                   # changed later by someone else: the receipt no longer matches
    assert fsops.receipts(str(tmp_path))["data.json"] != fsops.file_sum(str(f))
    assert fsops.receipts(str(tmp_path / "nowhere")) == {}


def test_a_replacement_between_the_write_and_the_receipt_is_never_certified(tmp_path):
    """Ruling R17: the receipt is the checksum of the bytes Afterparty wrote, from memory. A replacement landing
    after the write and before the receipt never matches it, so --purge's deletion test fails for it."""
    f = tmp_path / "server.json"
    ours = b'{"port": 1}'
    fsops.write_owned(str(f), ours, None)
    f.write_bytes(b"the user's replacement")                       # lands between the write and the receipt
    fsops.note_written(str(f), ours)
    want = fsops.receipts(str(tmp_path))["server.json"]
    assert want == fsops.text_sum(ours.decode()) and want != fsops.file_sum(str(f))


def test_a_receipts_file_certifies_its_own_content(tmp_path):
    """Ruling R6: a receipts file is ours to delete only while it is exactly what Afterparty wrote."""
    f = tmp_path / "data.json"
    f.write_text("1")
    fsops.note_written(str(f), f.read_bytes())
    (tmp_path / "made").mkdir()
    fsops.note_made(str(tmp_path / "made"))
    rf = tmp_path / fsops.RECEIPTS
    assert fsops.receipts(str(tmp_path)) == {"data.json": fsops.file_sum(str(f)), "made/": "dir"}
    assert fsops.receipts_file_ok(str(rf))
    data = json.loads(rf.read_text())
    data["notes.txt"] = "sha256:" + "0" * 64                     # a hand edit
    rf.write_text(json.dumps(data))
    assert not fsops.receipts_file_ok(str(rf))
    rf.write_text("{}")
    assert not fsops.receipts_file_ok(str(rf))                    # no self checksum: not certified either


@pytest.mark.parametrize("content", [b"the user's own notes\n", b"\xff\xfe not text", b'{"data.json": "x"}'])
def test_a_receipt_write_never_overwrites_content_afterparty_doesnt_own(tmp_path, content):
    """Codex probe (R11): whatever sits at the receipts path that Afterparty can't vouch for stays byte for byte;
    the write stops with a clear message instead of treating it as empty."""
    f = tmp_path / "data.json"
    f.write_text("1")
    rf = tmp_path / fsops.RECEIPTS
    rf.write_bytes(content)
    with pytest.raises(fsops.ReceiptError, match="didn't replace it"):
        fsops.note_written(str(f), f.read_bytes())
    assert rf.read_bytes() == content and sorted(p.name for p in tmp_path.iterdir()) == [fsops.RECEIPTS, "data.json"]


def test_a_receipt_update_swaps_only_what_afterparty_wrote(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("1")
    b.write_text("2")
    fsops.note_written(str(a), a.read_bytes())
    fsops.note_written(str(b), b.read_bytes())
    assert fsops.receipts(str(tmp_path)) == {"a": fsops.file_sum(str(a)), "b": fsops.file_sum(str(b))}
    assert fsops.receipts_file_ok(str(tmp_path / fsops.RECEIPTS))
    assert sorted(p.name for p in tmp_path.iterdir()) == [fsops.RECEIPTS, "a", "b"]   # no swap folder left


def test_a_receipts_file_edited_mid_update_is_kept(tmp_path, monkeypatch):
    f = tmp_path / "data.json"
    f.write_text("1")
    fsops.note_written(str(f), f.read_bytes())
    rf = tmp_path / fsops.RECEIPTS
    real = fsops._swap

    def racing(a, b):
        if b == str(rf) and not getattr(racing, "done", False):
            racing.done = True
            rf.write_text("edited by hand meanwhile")   # changes after our read, before the swap
        return real(a, b)
    monkeypatch.setattr(fsops, "_swap", racing)
    with pytest.raises(fsops.ReceiptError):
        fsops.note_written(str(f), f.read_bytes())
    assert rf.read_text() == "edited by hand meanwhile"


def test_an_owned_file_replaced_since_it_was_read_is_never_overwritten(tmp_path):
    """Codex probe class (R14): content that isn't what Afterparty last read or wrote survives byte for byte."""
    p = tmp_path / "record.json"
    fsops.write_owned(str(p), b"ours v1", None)                      # exclusive create
    with pytest.raises(fsops.Conflict):
        fsops.write_owned(str(p), b"again", None)                    # exists: never overwritten
    p.write_bytes(b"the user's replacement")
    with pytest.raises(fsops.Conflict, match="changed since"):
        fsops.write_owned(str(p), b"ours v2", b"ours v1")
    assert p.read_bytes() == b"the user's replacement"
    fsops.write_owned(str(p), b"ours v2", b"the user's replacement")   # CAS against what is there: allowed
    assert p.read_bytes() == b"ours v2" and sorted(x.name for x in tmp_path.iterdir()) == ["record.json"]


def test_owned_writes_refuse_links(tmp_path):
    target, link = tmp_path / "elsewhere", tmp_path / "state.json"
    target.write_bytes(b"x")
    os.symlink(str(target), str(link))
    with pytest.raises(fsops.Conflict, match="symbolic link"):
        fsops.write_owned(str(link), b"ours", b"x")
    assert target.read_bytes() == b"x" and os.path.islink(str(link))
    assert sorted(x.name for x in tmp_path.iterdir()) == ["elsewhere", "state.json"]   # no temporary file left


def _write_bytes(p, data):
    p.write_bytes(data)


def _open_and_write(p, data):
    with open(str(p), "wb") as f:
        f.write(data)


@pytest.mark.parametrize("edit", [_write_bytes, _open_and_write])
def test_a_third_party_edit_during_the_rollback_is_never_deleted(tmp_path, monkeypatch, edit):
    """Codex probe (R16): a third party edits the path after the exchange, just before the exchange back. The
    rollback compares with Afterparty's own new bytes from memory, so that edit is kept, never deleted."""
    p = tmp_path / "r.json"
    p.write_bytes(b"theirs")
    real, calls = fsops._swap, []

    def racing(a, b):
        calls.append(1)
        if len(calls) == 2:                                            # right before the exchange back
            edit(p, b"a third party's edit")
        return real(a, b)
    monkeypatch.setattr(fsops, "_swap", racing)
    with pytest.raises(fsops.Conflict) as e:
        fsops.write_owned(str(p), b"ours", b"stale")
    assert p.read_bytes() == b"theirs"                                 # back in place
    assert ".afterparty-kept-" in e.value.kept
    with open(e.value.kept, "rb") as f:
        assert f.read() == b"a third party's edit"                    # nobody's bytes were deleted


def test_a_lock_goes_ahead_without_a_receipt_when_the_receipts_file_isnt_ours(tmp_path, capsys):
    """Ruling P9: a cleanup or settings write never fails because a lock file's receipt can't be written."""
    rf = tmp_path / fsops.RECEIPTS
    rf.write_bytes(b"the user's own notes\n")
    for name in ("a.lock", "b.lock"):
        with fsops.held(str(tmp_path / name)) as fd:
            assert fd >= 0
    assert rf.read_bytes() == b"the user's own notes\n"
    assert capsys.readouterr().err.count("no write receipt") == 1      # reported once for the folder


def test_busy_when_not_blocking(tmp_path):
    p = str(tmp_path / "x.lock")
    with fsops.held(p):
        with pytest.raises(fsops.Busy, match="taken"):
            with fsops.held(p, blocking=False, busy="taken"):
                pass


def test_the_cleanup_run_lock_still_reports_busy(tmp_path):
    from cleanup.plan import bridge
    with bridge.lock(str(tmp_path)):
        with pytest.raises(bridge.Busy, match="Another Afterparty cleanup is running"):
            with bridge.lock(str(tmp_path)):
                pass
