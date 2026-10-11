"""Quick Look-style previews: a short allowlist served as-is, everything else as a macOS-generated PNG thumbnail."""
import hashlib
import os
import re
import subprocess
import tempfile

from cleanup import fsops

IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "heif", "tif", "tiff", "avif"}
VIDEO = {"mp4", "m4v", "mov", "webm"}
AUDIO = {"mp3", "m4a", "wav", "aac", "flac", "ogg"}
TEXT = {"txt", "md", "json", "log", "csv", "tsv", "py", "js", "mjs", "ts", "tsx", "jsx", "css", "yml", "yaml", "toml",
        "ini", "cfg", "conf", "sh", "zsh", "bash", "swift", "c", "h", "cpp", "hpp", "m", "go", "rs", "java", "kt", "rb",
        "php", "sql", "xml", "plist", "gitignore"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
        "bmp": "image/bmp", "heic": "image/heic", "heif": "image/heif", "tif": "image/tiff", "tiff": "image/tiff",
        "avif": "image/avif", "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "aac": "audio/aac", "flac": "audio/flac",
        "ogg": "audio/ogg"}
CSP = "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"
TEXT_LIMIT = 65536


def _ext(path):
    name = os.path.basename(path).lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def kind_of(path):
    e = _ext(path)
    return "image" if e in IMAGE else "video" if e in VIDEO else "audio" if e in AUDIO else "text" if e in TEXT else "thumb"


def read_text(path, limit=TEXT_LIMIT):
    with open(path, "rb") as f:
        raw = f.read(limit + 1)
    return {"text": raw[:limit].decode("utf-8", "replace"), "truncated": len(raw) > limit}


def parse_range(header, size):
    if not header:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        raise ValueError("bad range")
    a, b = m.group(1), m.group(2)
    if a == "":
        n = int(b)
        if n == 0:
            raise ValueError("bad range")
        start, end = max(0, size - n), size - 1
    else:
        start = int(a)
        end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        raise ValueError("unsatisfiable")
    return start, end


THUMB_CACHE_MAX = 200 * 2**20


def _prune(cache_dir, max_bytes):
    """Keep the thumbnail cache under `max_bytes`, dropping the least recently written first."""
    files = []
    for n in os.listdir(cache_dir):
        fp = os.path.join(cache_dir, n)
        if n.endswith(".png") and os.path.isfile(fp):
            st = os.stat(fp); files.append((st.st_mtime, st.st_size, fp))
    total = sum(f[1] for f in files)
    for _, sz, fp in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.remove(fp); total -= sz
        except OSError:
            pass


def thumbnail(path, cache_dir, run=subprocess.run, timeout=15, size=1024, max_bytes=THUMB_CACHE_MAX):
    st = os.stat(path)
    key = hashlib.sha256(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\0{size}".encode()).hexdigest()[:24]
    os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    out = os.path.join(cache_dir, key + ".png")
    if os.path.exists(out):
        return out
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        try:
            run(["qlmanage", "-t", "-s", str(size), "-o", tmp, path], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None
        made = os.path.join(tmp, os.path.basename(path) + ".png")
        if not os.path.exists(made):
            return None
        try:
            fsops.swap_owned(out, made, None)  # R14: exclusive create; another thumbnailer may have won the race
        except fsops.Conflict:
            pass
    os.utime(out)  # newest in the cache — pruning removes older thumbnails first
    _prune(cache_dir, max_bytes)
    return out if os.path.exists(out) else None
