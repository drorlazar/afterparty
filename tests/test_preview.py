import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from cleanup.scanner.scan import scan
from cleanup.scanner.store import Store
from cleanup.server import preview as P
from cleanup.server.app import serve

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def test_kind_of():
    assert P.kind_of("/a/b.PNG") == "image" and P.kind_of("/a/b.mov") == "video" and P.kind_of("/a/x.md") == "text"
    assert P.kind_of("/a/x.svg") == "thumb" and P.kind_of("/a/x.html") == "thumb" and P.kind_of("/a/x.psd") == "thumb"


@pytest.mark.parametrize("h,size,want", [
    (None, 100, None), ("bytes=0-9", 100, (0, 9)), ("bytes=90-", 100, (90, 99)), ("bytes=-10", 100, (90, 99)),
    ("bytes=95-200", 100, (95, 99)),
])
def test_parse_range_ok(h, size, want):
    assert P.parse_range(h, size) == want


@pytest.mark.parametrize("h", ["bytes=abc", "bytes=5-2", "bytes=100-", "items=0-1", "bytes=0-1,4-5"])
def test_parse_range_bad(h):
    with pytest.raises(ValueError):
        P.parse_range(h, 100)


def test_read_text_truncates_and_decodes(tmp_path):
    f = tmp_path / "big.log"; f.write_bytes(b"\xff" + b"a" * 70000)
    r = P.read_text(str(f))
    assert r["truncated"] and len(r["text"]) <= 65536 and r["text"][0] == "�"


def test_thumbnail_uses_qlmanage_and_caches(tmp_path):
    f = tmp_path / "x.psd"; f.write_bytes(b"8BPS")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd); out = cmd[cmd.index("-o") + 1]
        open(os.path.join(out, "x.psd.png"), "wb").write(PNG)
        return type("R", (), {"returncode": 0})()
    a = P.thumbnail(str(f), str(tmp_path / "cache"), run=run)
    b = P.thumbnail(str(f), str(tmp_path / "cache"), run=run)
    assert a == b and open(a, "rb").read() == PNG and len(calls) == 1 and calls[0][0] == "qlmanage"


def test_thumbnail_timeout_is_none(tmp_path):
    import subprocess
    f = tmp_path / "x.weird"; f.write_bytes(b"?")

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)
    assert P.thumbnail(str(f), str(tmp_path / "cache"), run=run) is None


@pytest.fixture
def pv(tmp_path, monkeypatch):
    home = tmp_path / "home"; d = home / "disk"; d.mkdir(parents=True); monkeypatch.setenv("HOME", str(home))
    (d / "a.png").write_bytes(PNG); (d / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    (d / "n.md").write_text("# <script>alert(1)</script>"); (d / "x.svg").write_text("<svg onload=alert(1)/>")
    (d / "sub").mkdir(); os.symlink(d / "a.png", d / "link.png"); os.mkfifo(d / "fifo")
    store = Store(str(tmp_path / "db.sqlite")); store.save(scan(str(d), skip=()))
    server, app = serve(port=0, store=store, roots=(str(d),), scan_skip=())
    app.thumb_dir = str(tmp_path / "thumbs")
    app.thumb_run = lambda cmd, **kw: None   # no thumbnails in this fixture → 415
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/preview?path={urllib.request.quote(str(path))}" +
                                     ("" if headers is None or "mode" not in headers else "&mode=" + headers.pop("mode")),
                                     headers={"X-Cleanup-Key": app.token, **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
    yield get, d
    server.shutdown()


def test_preview_serves_allowlisted_types(pv):
    get, d = pv
    s, h, b = get(d / "a.png")
    assert s == 200 and h["Content-Type"] == "image/png" and b == PNG and h["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in h["Content-Security-Policy"]
    s, h, b = get(d / "n.md")
    assert s == 200 and json.loads(b)["text"].startswith("# <script>")   # raw text in JSON; the UI escapes it
    s, h, b = get(d / "a.png", {"mode": "info"})
    assert json.loads(b)["kind"] == "image"


def test_preview_video_range(pv):
    get, d = pv
    s, h, b = get(d / "clip.mp4", {"Range": "bytes=10-19"})
    assert s == 206 and b == bytes(range(10, 20)) and h["Content-Range"] == "bytes 10-19/1024"
    assert get(d / "clip.mp4", {"Range": "bytes=5000-"})[0] == 416


def test_preview_refuses_links_dirs_fifos_and_raw_svg(pv):
    get, d = pv
    assert get(d / "link.png")[0] == 400 and get(d / "sub")[0] == 400 and get(d / "fifo")[0] == 400
    assert get(d / "nope.png")[0] == 404
    assert get(d / "x.svg")[0] == 415      # no thumbnail available in the fixture, never served raw


def test_big_image_without_range_is_served_whole(pv):
    # Review I2: <img> never sends Range — a >8 MB photo must come back complete with 200.
    get, d = pv
    big = d / "big.png"; big.write_bytes(PNG + b"\0" * (9 * 2**20))
    s, h, b = get(big)
    assert s == 200 and len(b) == big.stat().st_size and "Content-Range" not in h


def test_thumbnail_cache_is_capped(tmp_path):
    # Review I5: the thumbnail cache must not grow forever.
    cache = tmp_path / "cache"; cache.mkdir()
    for i in range(10):
        (cache / f"old{i}.png").write_bytes(b"x" * 1000)
    f = tmp_path / "x.psd"; f.write_bytes(b"8BPS")

    def run(cmd, **kw):
        open(os.path.join(cmd[cmd.index("-o") + 1], "x.psd.png"), "wb").write(PNG)
    assert P.thumbnail(str(f), str(cache), run=run, max_bytes=3000)
    assert sum(p.stat().st_size for p in cache.glob("*.png")) <= 3000
