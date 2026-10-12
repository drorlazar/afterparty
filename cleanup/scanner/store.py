"""SQLite snapshot of a scan: directories + big files, queried by path."""
import os
import sqlite3
import threading
import time

from cleanup import fsops
from cleanup.scanner.scan import F_CLOUD, F_TIMEOUT, F_UNREADABLE

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan(
  id INTEGER PRIMARY KEY, root TEXT, started REAL, finished REAL, files INTEGER, dirs INTEGER,
  bytes INTEGER, errors INTEGER, status TEXT, took REAL);
CREATE TABLE IF NOT EXISTS node(
  scan_id INTEGER, path TEXT, parent TEXT, name TEXT, kind TEXT, size INTEGER, files INTEGER,
  dirs INTEGER, mtime REAL, flags INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS node_path ON node(scan_id, path);
CREATE INDEX IF NOT EXISTS node_parent ON node(scan_id, parent);
"""
COLS = ("path", "parent", "name", "kind", "size", "files", "dirs", "mtime", "flags")
KEEP_SCANS = 2


def default_db():
    from cleanup import config
    return os.path.join(config.state_dir(), "scan.db")


def _name(path):
    return os.path.basename(path.rstrip("/")) or path


class Store:
    def __init__(self, path=None):
        self.path = path or default_db()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        try:
            self._open()
        except sqlite3.DatabaseError:
            if fsops.rename_exclusive(self.path, f"{self.path}.corrupt-{time.time_ns()}") is not None:
                raise  # couldn't move the unreadable database aside without replacing anything: report it
            self._open()

    def _open(self):
        db = sqlite3.connect(self.path, check_same_thread=False)
        try:
            db.executescript(SCHEMA)
            db.execute("SELECT count(*) FROM scan").fetchone()
        except BaseException:
            db.close()  # never left open on the file we are about to move aside
            raise
        self.db = db

    # ---------------------------------------------------------------- writes
    def _rows(self, scan_id, result, root_parent=None):
        for n in result.nodes.values():
            parent = n["parent"] if n["path"] != result.root else root_parent
            yield (scan_id, n["path"], parent, _name(n["path"]), "dir", n["size"], n["files"],
                   n["dirs"], n["mtime"], n["flags"])
        for f in result.files:
            yield (scan_id, f["path"], f["parent"], _name(f["path"]), "file", f["size"], 1, 0, f["mtime"], 0)

    def save(self, result):
        with self.lock, self.db:
            r = result.nodes[result.root]
            cur = self.db.execute(
                "INSERT INTO scan(root, started, finished, files, dirs, bytes, errors, status, took)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (result.root, time.time() - result.took, time.time(), r["files"], r["dirs"], r["size"],
                 result.stats["errors"], result.status, result.took))
            sid = cur.lastrowid
            self.db.executemany(f"INSERT INTO node(scan_id,{','.join(COLS)}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                self._rows(sid, result))
            old = [x[0] for x in self.db.execute("SELECT id FROM scan ORDER BY id DESC").fetchall()[KEEP_SCANS:]]
            for o in old:
                self.db.execute("DELETE FROM node WHERE scan_id=?", (o,))
                self.db.execute("DELETE FROM scan WHERE id=?", (o,))
        return sid

    def replace_subtree(self, result):
        meta = self.latest()
        if not meta:
            self.save(result)
            return
        sid, root = meta["id"], result.root
        mroot = meta["root"].rstrip("/") or "/"
        inside = root == mroot or mroot == "/" or root.startswith(mroot + "/")
        if not inside or root == mroot:
            self.save(result)  # at/above the snapshot root: this IS a full snapshot
            return
        old = self.node(root)
        prefix = root.rstrip("/") + "/"
        with self.lock, self.db:
            self.db.execute("DELETE FROM node WHERE scan_id=? AND (path=? OR substr(path,1,?)=?)",
                            (sid, root, len(prefix), prefix))
            parent = old["parent"] if old else os.path.dirname(root)
            if parent == root:  # "/" has no parent
                parent = None
            self.db.executemany(f"INSERT INTO node(scan_id,{','.join(COLS)}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                self._rows(sid, result, root_parent=parent))
            new = result.nodes[root]
            ds = new["size"] - (old["size"] if old else 0)
            df = new["files"] - (old["files"] if old else 0)
            dd = new["dirs"] - (old["dirs"] if old else 0)
            p = parent
            while p:
                self.db.execute("UPDATE node SET size=size+?, files=files+?, dirs=dirs+? WHERE scan_id=? AND path=?",
                                (ds, df, dd, sid, p))
                if p == meta["root"]:
                    break
                p = os.path.dirname(p) if p != "/" else None
            self.db.execute("UPDATE scan SET bytes=bytes+?, files=files+? WHERE id=?", (ds, df, sid))

    # ---------------------------------------------------------------- reads
    def latest(self):
        with self.lock:
            row = self.db.execute("SELECT id, root, started, finished, files, dirs, bytes, errors, status, took"
                                  " FROM scan ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        keys = ("id", "root", "started", "finished", "files", "dirs", "bytes", "errors", "status", "took")
        return dict(zip(keys, row))

    def _sid(self):
        m = self.latest()
        return m["id"] if m else None

    def node(self, path):
        sid = self._sid()
        if sid is None:
            return None
        with self.lock:
            row = self.db.execute(f"SELECT {','.join(COLS)} FROM node WHERE scan_id=? AND path=?",
                                  (sid, path)).fetchone()
        return dict(zip(COLS, row)) if row else None

    def children(self, path):
        sid = self._sid()
        me = self.node(path)
        if sid is None or me is None:
            return []
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(COLS)} FROM node WHERE scan_id=? AND parent=?"
                                   " ORDER BY size DESC", (sid, path)).fetchall()
        kids = [dict(zip(COLS, r)) for r in rows]
        rest = me["size"] - sum(k["size"] for k in kids)
        if rest > 0:
            kids.append({"path": None, "parent": path, "name": "(smaller files)", "kind": "rest", "size": rest,
                         "files": 0, "dirs": 0, "mtime": 0, "flags": 0})
        return kids

    def tree(self, path, depth=1):
        me = self.node(path)
        if me is None:
            return None

        def expand(n, d):
            if d <= 0 or n["kind"] != "dir":
                return n
            n = dict(n)
            n["children"] = [expand(k, d - 1) if k["kind"] == "dir" and d > 1 else k for k in self.children(n["path"])]
            return n

        return expand(me, max(1, min(int(depth), 4)))

    def counts(self):
        sid = self._sid()
        if sid is None:
            return {"unreadable": 0, "cloud": 0, "timeout": 0}
        with self.lock:
            def c(flag):
                return self.db.execute("SELECT count(*) FROM node WHERE scan_id=? AND (flags & ?)!=0",
                                       (sid, flag)).fetchone()[0]
            return {"unreadable": c(F_UNREADABLE), "cloud": c(F_CLOUD), "timeout": c(F_TIMEOUT)}
