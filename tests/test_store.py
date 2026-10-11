import os

from cleanup.scanner import scan as S
from cleanup.scanner.store import Store


def tree_fixture(root):
    (root / "a/b").mkdir(parents=True)
    (root / "a/b/big.bin").write_bytes(b"x" * 64_000)
    (root / "a/small.txt").write_bytes(b"y" * 100)
    (root / "c").mkdir()
    return root


def test_save_and_latest_and_children(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "BIG_FILE", 32_000)
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite"))
    sid = st.save(S.scan(str(root), skip=()))
    meta = st.latest()
    assert meta["id"] == sid and meta["root"] == str(root) and meta["status"] == "done"
    kids = st.children(str(root / "a"))
    names = [k["name"] for k in kids]
    assert names[0] == "b"
    assert "(smaller files)" in names                      # small.txt folded
    b = st.children(str(root / "a/b"))
    assert b[0]["kind"] == "file" and b[0]["name"] == "big.bin"


def test_tree_depth(tmp_path):
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite")); st.save(S.scan(str(root), skip=()))
    t = st.tree(str(root), depth=1)
    assert t["path"] == str(root)
    assert all("children" not in c for c in t["children"])
    t2 = st.tree(str(root), depth=2)
    a = next(c for c in t2["children"] if c["name"] == "a")
    assert any(c["name"] == "b" for c in a["children"])
    assert st.tree(str(root / "nope"), depth=1) is None


def test_replace_subtree_updates_ancestors(tmp_path):
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite")); st.save(S.scan(str(root), skip=()))
    before_root = st.node(str(root))["size"]
    before_b = st.node(str(root / "a/b"))["size"]
    (root / "a/b/big.bin").unlink()
    st.replace_subtree(S.scan(str(root / "a/b"), skip=()))
    assert st.node(str(root / "a/b"))["size"] == 0
    assert st.node(str(root))["size"] == before_root - before_b
    assert st.node(str(root / "a/b"))["parent"] == str(root / "a")


def test_keeps_only_two_scans(tmp_path):
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite"))
    ids = [st.save(S.scan(str(root), skip=())) for _ in range(3)]
    n = st.db.execute("select count(distinct scan_id) from node").fetchone()[0]
    assert n == 2 and st.latest()["id"] == ids[-1]


def test_counts_flags(tmp_path):
    root = tree_fixture(tmp_path / "r")
    locked = root / "c"; os.chmod(locked, 0)
    try:
        st = Store(str(tmp_path / "db.sqlite")); st.save(S.scan(str(root), skip=()))
        assert st.counts()["unreadable"] == 1
    finally:
        os.chmod(locked, 0o700)


def test_store_recovers_from_corrupt_db(tmp_path):
    db = tmp_path / "db.sqlite"; db.write_bytes(b"this is not sqlite" * 100)
    st = Store(str(db))
    assert st.latest() is None
    assert any(p.name.startswith("db.sqlite.corrupt-") for p in tmp_path.iterdir())


def test_subtree_scan_above_snapshot_root_becomes_full_snapshot(tmp_path):
    # Review C2: rescanning a path at/above the snapshot root must not corrupt totals.
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite"))
    st.save(S.scan(str(root / "a"), skip=()))
    full = S.scan(str(root), skip=())
    st.replace_subtree(full)
    meta = st.latest()
    assert meta["root"] == str(root)
    assert meta["bytes"] == full.nodes[str(root)]["size"]
    assert st.node(str(root))["size"] == full.nodes[str(root)]["size"]


def test_root_is_never_its_own_parent(tmp_path):
    root = tree_fixture(tmp_path / "r")
    st = Store(str(tmp_path / "db.sqlite"))
    st.save(S.scan(str(root / "a"), skip=()))
    st.replace_subtree(S.scan(str(root / "a"), skip=()))     # rescan the root itself
    assert st.node(str(root / "a"))["parent"] != str(root / "a")
    assert all(k["path"] != str(root / "a") for k in st.children(str(root / "a")))
