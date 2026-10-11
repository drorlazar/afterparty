"""Live listing of one folder (every file, incl. small ones). Never follows symlinks."""
import os
import re
import stat

SORTS = {"size", "name", "mtime"}
SEQ_RE = re.compile(r"^(.*?)(\d+)(\.[A-Za-z0-9]{1,8})$")
SEQ_DISPLAY_MIN = 10


def seq_key(name):
    m = SEQ_RE.match(name)
    return f"{m.group(1)}\0{len(m.group(2))}\0{m.group(3).lower()}" if m else None


def _group(entries):
    by = {}
    for e in entries:
        if e["kind"] == "file":
            k = seq_key(e["name"])
            if k:
                by.setdefault(k, []).append(e)
    out, done = [], set()
    for e in entries:
        k = seq_key(e["name"]) if e["kind"] == "file" else None
        members = by.get(k) if k else None
        if not members or len(members) < SEQ_DISPLAY_MIN:
            out.append(e); continue
        if k in done:
            continue
        done.add(k)
        members.sort(key=lambda x: x["name"])
        first, last = members[0]["name"], members[-1]["name"]
        lm = SEQ_RE.match(last)
        out.append({"name": f"{first[:-len(SEQ_RE.match(first).group(3))]}…{lm.group(2)}{lm.group(3)} ×{len(members)}",
                    "path": members[0]["path"], "kind": "seq", "seq": k, "count": len(members),
                    "size": sum(x["size"] or 0 for x in members), "mtime": max(x["mtime"] for x in members)})
    return out


def check_path(path):
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        raise ValueError("path must be absolute")
    if ".." in path.split("/"):
        raise ValueError("path must not contain '..'")
    return os.path.normpath(path)


def list_dir(path, sort="size", order="desc", offset=0, limit=200, store=None, group=False, seq=None):
    path = check_path(path)
    if os.path.islink(path):
        raise ValueError("refusing to list through a symlink")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    sort = sort if sort in SORTS else "size"
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                kind, size = "link", 0
            elif stat.S_ISDIR(st.st_mode):
                kind = "dir"
                n = store.node(e.path) if store is not None else None
                size = n["size"] if n else None
            else:
                kind, size = "file", st.st_blocks * 512
            entries.append({"name": e.name, "path": e.path, "kind": kind, "size": size, "mtime": st.st_mtime})
    if seq is not None:
        entries = [e for e in entries if e["kind"] == "file" and seq_key(e["name"]) == seq]
    elif group:
        entries = _group(entries)
    if sort == "name":
        entries.sort(key=lambda x: x["name"].casefold())
    elif sort == "mtime":
        entries.sort(key=lambda x: x["mtime"])
    else:
        entries.sort(key=lambda x: -1 if x["size"] is None else x["size"])
    if order == "desc":
        entries.reverse()
    return {"path": path, "total": len(entries), "offset": offset, "limit": limit,
            "entries": entries[offset:offset + limit]}
