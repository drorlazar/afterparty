import os
import threading
import time

from cleanup.scanner import scan as S


def blocks(p):
    return os.lstat(p).st_blocks * 512


def make_tree(root):
    (root / "a/b").mkdir(parents=True)
    (root / "a/one.bin").write_bytes(b"x" * 10_000)
    (root / "a/b/two.bin").write_bytes(b"y" * 50_000)
    (root / "c").mkdir()
    (root / "c/three.bin").write_bytes(b"z" * 5_000)
    return [root / "a/one.bin", root / "a/b/two.bin", root / "c/three.bin"]


def test_sizes_aggregate_bottom_up(tmp_path):
    files = make_tree(tmp_path)
    r = S.scan(str(tmp_path), skip=())
    total = sum(blocks(f) for f in files)
    assert r.nodes[str(tmp_path)]["size"] == total
    assert r.nodes[str(tmp_path / "a")]["size"] == blocks(files[0]) + blocks(files[1])
    assert r.nodes[str(tmp_path)]["files"] == 3
    assert r.nodes[str(tmp_path)]["dirs"] == 3
    assert r.status == "done"


def test_hard_link_counted_once(tmp_path):
    f = tmp_path / "big.bin"; f.write_bytes(b"x" * 40_000)
    os.link(f, tmp_path / "same.bin")
    r = S.scan(str(tmp_path), skip=())
    assert r.nodes[str(tmp_path)]["size"] == blocks(f)


def test_symlinks_not_followed(tmp_path):
    target = tmp_path / "real"; target.mkdir(); (target / "f.bin").write_bytes(b"x" * 40_000)
    other = tmp_path / "other"; other.mkdir()
    os.symlink(target, other / "link")
    r = S.scan(str(other), skip=())
    assert r.nodes[str(other)]["size"] == 0
    assert str(other / "link") not in r.nodes


def test_big_files_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "BIG_FILE", 30_000)
    make_tree(tmp_path)
    r = S.scan(str(tmp_path), skip=())
    assert [os.path.basename(f["path"]) for f in r.files] == ["two.bin"]


def test_skip_and_cloud_flags(tmp_path, monkeypatch):
    (tmp_path / "skipme").mkdir(); (tmp_path / "skipme/f.bin").write_bytes(b"x" * 9000)
    (tmp_path / "cloud").mkdir()
    monkeypatch.setattr(S, "CLOUD", (str(tmp_path / "cloud"),))
    r = S.scan(str(tmp_path), skip=(str(tmp_path / "skipme"), str(tmp_path / "cloud")))
    assert r.nodes[str(tmp_path / "skipme")]["flags"] & S.F_SKIPPED
    assert r.nodes[str(tmp_path / "cloud")]["flags"] & S.F_CLOUD
    assert r.nodes[str(tmp_path)]["size"] == 0


def test_unreadable_dir_flagged(tmp_path):
    locked = tmp_path / "locked"; locked.mkdir(); (locked / "f").write_bytes(b"x")
    os.chmod(locked, 0)
    try:
        r = S.scan(str(tmp_path), skip=())
        assert r.nodes[str(locked)]["flags"] & S.F_UNREADABLE
        assert r.stats["errors"] >= 1
    finally:
        os.chmod(locked, 0o700)


def test_slow_folder_times_out_and_scan_finishes(tmp_path):
    make_tree(tmp_path)
    slow = str(tmp_path / "c")
    release = threading.Event()

    def scandir(p):
        if p == slow:
            release.wait(10)  # simulates a hung cloud mount
        return os.scandir(p)

    t0 = time.time()
    r = S.scan(str(tmp_path), skip=(), scandir=scandir, timeout=0.5, workers=2)
    release.set()
    assert time.time() - t0 < 5
    assert r.nodes[slow]["flags"] & S.F_TIMEOUT
    assert r.nodes[str(tmp_path / "a/b")]["size"] > 0


def test_scan_survives_vanishing_dir(tmp_path):
    make_tree(tmp_path)
    gone = str(tmp_path / "a/b")

    def scandir(p):
        if p == gone:
            raise FileNotFoundError(p)
        return os.scandir(p)

    r = S.scan(str(tmp_path), skip=(), scandir=scandir)
    assert r.status == "done"
    assert r.nodes[gone]["flags"] & S.F_UNREADABLE


def test_progress_and_cancel(tmp_path):
    make_tree(tmp_path)
    seen = []
    r = S.scan(str(tmp_path), skip=(), progress=seen.append)
    assert r.status == "done"
    cancel = threading.Event(); cancel.set()
    r2 = S.scan(str(tmp_path), skip=(), cancel=cancel)
    assert r2.status == "cancelled"


def test_scan_never_hangs_when_every_worker_is_stuck(tmp_path, monkeypatch):
    # Review C1: more hung folders than workers + replacements must not hang the scan forever.
    monkeypatch.setattr(S, "MAX_EXTRA_WORKERS", 3)
    for i in range(30):
        (tmp_path / f"hung{i:02d}").mkdir()
    release = threading.Event()

    def scandir(p):
        if os.path.basename(p).startswith("hung"):
            release.wait(30)
        return os.scandir(p)

    t0 = time.time()
    r = S.scan(str(tmp_path), skip=(), scandir=scandir, timeout=0.2, workers=2)
    release.set()
    assert time.time() - t0 < 5
    assert r.status == "partial"
    assert all(r.nodes[str(tmp_path / f"hung{i:02d}")]["flags"] & S.F_TIMEOUT for i in range(30))


def test_cancel_stops_workers(tmp_path):
    # Review I4: after scan() returns "cancelled", workers must not keep crawling the queue.
    for i in range(300):
        (tmp_path / f"d{i:03d}").mkdir()
    visits = []
    cancel = threading.Event()

    def scandir(p):
        visits.append(p)
        if len(visits) == 3:
            cancel.set()
        time.sleep(0.005)
        return os.scandir(p)

    r = S.scan(str(tmp_path), skip=(), scandir=scandir, workers=2, cancel=cancel)
    n = len(visits)
    time.sleep(0.6)
    assert r.status == "cancelled"
    assert len(visits) - n <= 4


def test_slow_but_healthy_folder_is_kept(tmp_path):
    # Review I5: a folder slower than `timeout` that does finish must keep its data.
    make_tree(tmp_path)
    slow = str(tmp_path / "a/b")

    def scandir(p):
        if p == slow:
            time.sleep(0.6)
        return os.scandir(p)

    r = S.scan(str(tmp_path), skip=(), scandir=scandir, timeout=0.3, workers=2)
    assert r.nodes[slow]["size"] > 0
    assert not r.nodes[slow]["flags"] & S.F_TIMEOUT
