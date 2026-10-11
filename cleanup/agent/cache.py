"""Agent answers cached per (path, question), invalidated when the item's modified time changes."""
import hashlib
import json
import os

from cleanup import fsops


def fingerprint(path):
    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    return f"{st.st_mtime_ns}:{st.st_size}"


class Cache:
    def __init__(self, directory):
        self.dir = directory

    def _file(self, path, question):
        h = hashlib.sha256(f"{path}\0{question or ''}".encode()).hexdigest()[:24]
        return os.path.join(self.dir, h + ".json")

    def get(self, path, question):
        try:
            with open(self._file(path, question)) as f:
                e = json.load(f)
        except (OSError, ValueError):
            return None
        return e["result"] if e.get("path") == path and e.get("fp") == fingerprint(path) else None

    def put(self, path, question, result):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        try:
            fsops.replace_owned(self._file(path, question),
                                json.dumps({"path": path, "fp": fingerprint(path), "result": result}).encode("utf-8"))
        except fsops.Conflict:
            pass  # best-effort cache: a concurrent writer won, so this answer just isn't cached

    def forget(self, path):
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for n in names:
            fp = os.path.join(self.dir, n)
            try:
                with open(fp) as f:
                    if json.load(f).get("path") == path:
                        os.remove(fp)
            except (OSError, ValueError):
                continue
