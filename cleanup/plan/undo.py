"""Put trashed items back where they were. Never overwrites; only moves things that live in a Trash folder."""
import json
import os
import time

from cleanup.fsops import rename_exclusive as _rename_exclusive  # one shared no-clobber move, no fallback


def _in_trash(path):
    rp = os.path.realpath(path)
    home_trash = os.path.realpath(os.path.expanduser("~/.Trash"))
    return rp.startswith(home_trash + "/") or "/.Trashes/" in rp or "/.Trash/" in rp


def undo(journal):
    res = {"restored": [], "conflict": [], "missing": [], "refused": []}
    entries = []
    with open(journal) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "action" and e.get("result") == "trashed":
                entries.append(e)
            if e.get("event") == "undo" and e.get("result") == "restored":
                entries = [x for x in entries if x["path"] != e["path"]]
    fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as jf:
        for e in entries:
            src, dst = e.get("trash_path"), e["path"]
            if not src or not os.path.lexists(src):
                kind = "missing"
            elif not _in_trash(src) or not os.path.isabs(dst):
                kind = "refused"  # a journal must never be able to move files from outside the Trash
            elif e.get("ino") is None or (os.lstat(src).st_ino, os.lstat(src).st_dev) != (e["ino"], e.get("dev")):
                kind = "refused"  # not provably the file Afterparty put there (or recorded before identity tracking)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                err = _rename_exclusive(src, dst)
                kind = "restored" if err is None else ("conflict" if err == "conflict" else "missing")
            res[kind].append({"path": dst, "trash_path": src})
            jf.write(json.dumps({"ts": time.time(), "event": "undo", "path": dst, "result": kind}) + "\n")
    return res
