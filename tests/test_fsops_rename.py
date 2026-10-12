import errno

from cleanup import fsops


def test_never_replaces_a_file_or_a_folder(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("theirs")
    assert fsops.rename_exclusive(str(a), str(b)) == "conflict" and b.read_text() == "theirs" and a.exists()
    (tmp_path / "d").mkdir()
    assert fsops.rename_exclusive(str(a), str(tmp_path / "d")) == "conflict" and (tmp_path / "d").is_dir()
    assert fsops.rename_exclusive(str(a), str(tmp_path / "c")) is None and (tmp_path / "c").read_text() == "new"
    assert fsops.rename_exclusive(str(tmp_path / "gone"), str(tmp_path / "e")).startswith("failed")


def test_without_the_native_call_it_fails_closed(tmp_path, monkeypatch):
    """Codex probe: with the native primitive unavailable, a file that arrives late must never be overwritten."""
    monkeypatch.setattr(fsops, "_renamex", None)
    src, dst = tmp_path / "new", tmp_path / "dst"
    src.write_text("ours")
    r = fsops.rename_exclusive(str(src), str(dst))
    assert r.startswith("failed") and "renamex_np" in r and src.read_text() == "ours" and not dst.exists()
    dst.write_text("the user's, arrived late")
    assert fsops.rename_exclusive(str(src), str(dst)).startswith("failed")
    assert dst.read_text() == "the user's, arrived late"


def test_a_cross_volume_move_is_refused_not_emulated(tmp_path, monkeypatch):
    def exdev(src, dst, flags):
        fsops.ctypes.set_errno(errno.EXDEV)
        return -1
    monkeypatch.setattr(fsops, "_renamex", exdev)
    src = tmp_path / "new"
    src.write_text("ours")
    r = fsops.rename_exclusive(str(src), str(tmp_path / "dst"))
    assert r.startswith("failed") and "another volume" in r and src.exists() and not (tmp_path / "dst").exists()


def test_undo_uses_the_shared_strict_rename():
    from cleanup.plan import undo
    assert undo._rename_exclusive is fsops.rename_exclusive
    with open(undo.__file__) as f:
        src = f.read()
    assert "os.rename(" not in src and "RENAME_EXCL" not in src
