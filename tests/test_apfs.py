import os
import subprocess

import pytest

from cleanup.engine import apfs
from cleanup.plan import manifest as M
from tests.test_manifest import fake_classify


def clone(src, dst):
    subprocess.run(["cp", "-c", str(src), str(dst)], check=True)


@pytest.fixture
def data(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(os.urandom(2_000_000))
    return a


def test_private_size_of_a_plain_file_is_its_size(data):
    assert apfs.private_size(str(data)) == os.lstat(data).st_blocks * 512


def test_private_of_two_clones_is_zero_each(data, tmp_path):
    clone(data, tmp_path / "b.bin")
    assert apfs.private_size(str(data)) == 0 and apfs.private_size(str(tmp_path / "b.bin")) == 0


def test_private_tree_sums_files_and_ignores_links(data, tmp_path):
    d = tmp_path / "d"; d.mkdir()
    clone(data, d / "c.bin")
    (d / "own.bin").write_bytes(os.urandom(100_000))
    os.symlink(data, d / "link")
    assert apfs.private_tree(str(d)) == os.lstat(d / "own.bin").st_blocks * 512


def test_private_size_missing_is_none(tmp_path):
    assert apfs.private_size(str(tmp_path / "nope")) is None


def test_manifest_reports_likely_freed(data, tmp_path):
    clone(data, tmp_path / "b.bin")
    m = M.build([str(tmp_path / "b.bin")], classify_fn=fake_classify)
    it = m["items"][0]
    assert it["size"] > 1_900_000 and it["private"] == 0
    assert m["totals"]["likely"][it["mode"]] == 0 and m["totals"][it["mode"]] == it["size"]


def test_compressed_file_is_not_reported_as_free(tmp_path):
    # Review I1: decmpfs-compressed files (every installed app) report private 0 — they aren't clones.
    src = tmp_path / "src.txt"; src.write_bytes(b"compress me " * 200_000)
    dst = tmp_path / "dst.txt"
    subprocess.run(["ditto", "--hfsCompression", str(src), str(dst)], check=True)
    if not os.lstat(dst).st_flags & 0x20:
        pytest.skip("volume didn't compress the file")
    assert apfs.private_size(str(dst)) == os.lstat(dst).st_blocks * 512 > 0


def test_hard_links_free_nothing_on_their_own(data, tmp_path):
    # Review minor 1 (re-graded): deleting one of two hard links frees nothing.
    os.link(data, tmp_path / "hl.bin")
    assert apfs.private_size(str(tmp_path / "hl.bin")) == 0
