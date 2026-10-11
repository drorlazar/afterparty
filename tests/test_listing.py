import os

import pytest

from cleanup.scanner import access
from cleanup.scanner.listing import list_dir


def test_list_sorts_by_size_and_never_follows_symlinks(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 50_000)
    (tmp_path / "small.bin").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    os.symlink(tmp_path / "sub", tmp_path / "link")
    r = list_dir(str(tmp_path))
    names = [e["name"] for e in r["entries"]]
    assert names.index("big.bin") < names.index("small.bin")
    link = next(e for e in r["entries"] if e["name"] == "link")
    assert link["kind"] == "link" and link["size"] == 0
    with pytest.raises(ValueError):
        list_dir(str(tmp_path / "link"))


def test_list_sort_by_name_ascending(tmp_path):
    for n in ("b", "a", "C"):
        (tmp_path / n).write_text(n)
    r = list_dir(str(tmp_path), sort="name", order="asc")
    assert [e["name"] for e in r["entries"]] == ["a", "b", "C"]


def test_list_paginates_large_folder(tmp_path):
    for i in range(2500):
        (tmp_path / f"f{i:05d}").write_bytes(b"")
    r = list_dir(str(tmp_path), sort="name", order="asc", offset=1000, limit=5000)
    assert r["total"] == 2500 and r["limit"] == 1000
    assert len(r["entries"]) == 1000 and r["entries"][0]["name"] == "f01000"


def test_list_rejects_bad_paths(tmp_path):
    for bad in ("relative/path", str(tmp_path) + "/../x", "/tmp/\x00x"):
        with pytest.raises(ValueError):
            list_dir(bad)
    with pytest.raises(FileNotFoundError):
        list_dir(str(tmp_path / "missing"))


def test_full_disk_access_probe(tmp_path):
    ok = tmp_path / "ok"; ok.mkdir()
    assert access.has_full_disk_access(probes=(str(ok),)) is True
    denied = tmp_path / "denied"; denied.mkdir(); os.chmod(denied, 0)
    try:
        assert access.has_full_disk_access(probes=(str(denied),)) is False
    finally:
        os.chmod(denied, 0o700)
    assert access.has_full_disk_access(probes=(str(tmp_path / "absent"),)) is True
    assert os.path.isabs(access.interpreter())
from cleanup.scanner.listing import list_dir, seq_key


def test_seq_key():
    assert seq_key("frame_0001.png") == seq_key("frame_2400.png") != seq_key("frame_001.png")
    assert seq_key("shot_A_0001.exr") != seq_key("frame_0001.png") and seq_key("notes.txt") is None


def test_numbered_files_group_into_one_row(tmp_path):
    for i in range(1, 2501):
        (tmp_path / f"frame_{i:04d}.png").write_bytes(b"x" * 10)
    for i in range(3):
        (tmp_path / f"take{i}.mov").write_bytes(b"y")       # fewer than 10 → not grouped
    (tmp_path / "notes.txt").write_text("n")
    r = list_dir(str(tmp_path), group=True)
    seqs = [e for e in r["entries"] if e["kind"] == "seq"]
    assert len(seqs) == 1 and seqs[0]["count"] == 2500 and seqs[0]["name"] == "frame_0001…2500.png ×2500"
    assert seqs[0]["path"] == str(tmp_path / "frame_0001.png") and r["total"] == 5
    m = list_dir(str(tmp_path), group=True, seq=seqs[0]["seq"], sort="name", order="asc", limit=200)
    assert m["total"] == 2500 and len(m["entries"]) == 200 and m["entries"][0]["name"] == "frame_0001.png"


def test_without_group_nothing_changes(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i:03d}.png").write_bytes(b"x")
    assert list_dir(str(tmp_path))["total"] == 20
