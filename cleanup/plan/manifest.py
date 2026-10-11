"""Staged paths → exact manifest. Only the rules engine may assign PERMANENT; overrides only make things safer."""
import os
import re
import unicodedata
import time
import uuid

from cleanup.engine.apfs import SharedCounter, private_tree
from cleanup.engine.classify import SYSTEM, classify
from cleanup.engine.core import VERSION_TOKEN, family_key, kept, version_rank

SAFER = {"permanent": {"permanent", "trash", "keep"}, "remove": {"remove", "keep"}, "trash": {"trash", "keep"},
         "keep": {"keep"}, "skip": {"skip"}}
LATEST_NAMES = re.compile(r"(?i)^(master|final|latest|delivery)$")


FORCEABLE = {"trash"}  # a user's "delete anyway" only ever moves things to the Trash; only rules make things permanent
# ~/Library folders that hold irreplaceable account data (passwords, mail, messages, app sandboxes, device backups).
WHOLE_APP = re.compile(r"(?i)^/Applications/[^/]+\.app/?$")  # a whole installed app: Trash only, never permanent
LIBRARY_CORE = ("Keychains", "Mail", "Messages", "Containers", "Group Containers", "Application Support/MobileSync",
                "Accounts", "Cookies", "Preferences")


def _cf_under(path, base):
    p = unicodedata.normalize("NFC", path).casefold()
    b = unicodedata.normalize("NFC", base).casefold().rstrip("/")
    return p == b or p.startswith(b + "/")


def base_floor(path, home=None):
    """Applies to EVERY action, rule-based or not (the executor's allowed-roots check covers 'outside home')."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    if path.rstrip("/").casefold() == home.casefold():
        return "your home folder itself"
    if path.rstrip("/").casefold() == (home + "/Library").casefold():
        return "your ~/Library folder itself"
    if any(seg.casefold().endswith(".app") for seg in path.rstrip("/").split("/")[:-1]):
        return "inside an app bundle — remove the whole app instead"
    return None


def _deep(path):
    """Fingerprint of everything inside a folder: (entries, allocated bytes, newest ctime). ctime can't be set back,
    so swapping a file for a same-sized one still changes it."""
    n = size = newest = 0
    for d, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                st = os.lstat(os.path.join(d, name))
            except OSError:
                continue
            n += 1; size += st.st_blocks * 512; newest = max(newest, st.st_ctime_ns)
    return [n, size, newest]


def override_floor(path, keep=(), home=None):
    """The line a user's "Delete anyway" can never cross. Returns a reason, or None if overriding is allowed."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    if os.path.islink(path) or (os.path.lexists(path) and os.path.realpath(path) != os.path.normpath(path)):
        return "a symlink — deleting it would hit whatever it points to"
    if not _cf_under(path, home) and any(_cf_under(path, s) for s in SYSTEM):
        return "a macOS system area"
    if not _cf_under(path, home) and not WHOLE_APP.match(path):
        return "outside your home folder"
    if path.rstrip("/").casefold() == home.casefold():
        return "your home folder itself"
    lib = home + "/Library"
    if path.rstrip("/").casefold() == lib.casefold() or any(_cf_under(path, f"{lib}/{d}") for d in LIBRARY_CORE) \
            or any(_cf_under(f"{lib}/{d}", path) for d in LIBRARY_CORE):  # …or a folder that contains some of it
        return "part of your Mac's account data (~/Library) — deleting it breaks apps or loses data"
    if any(seg.casefold().endswith(".app") for seg in path.rstrip("/").split("/")[:-1]):
        return "inside an app bundle — remove the whole app instead"
    if keep and kept(path, list(keep)):
        return "in your keep list — unprotect it first"
    return None


def _size(path):
    """Allocated bytes; data shared between APFS clones inside the folder counts once (Finder counts it per copy)."""
    total, shared = 0, SharedCounter()
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512
    for d, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(d, f)
            try:
                total += shared.size(fp, os.lstat(fp))
            except OSError:
                pass
    return total


def _identity(path):
    st = os.lstat(path)
    return {"dev": st.st_dev, "ino": st.st_ino, "mtime": st.st_mtime_ns, "size": st.st_size}


def _mode_for(v, kind):
    if v["class"] == "protected":
        return "skip"
    if v.get("rule") == "worktree":  # git worktree removal can't be undone from the Trash: its own mode
        return "remove" if kind == "worktree" else "skip"
    if v.get("action") == "none":     # e.g. cloud-synced files: never acted on here
        return "skip"
    return "permanent" if v["class"] == "regenerable" and v.get("action") == "permanent" else "trash"


def _item(path, classify_fn, home, keep, with_children=True):
    if not os.path.lexists(path):
        return {"path": path, "kind": "file", "mode": "skip", "reason": "no longer exists", "rule": "", "size": 0,
                "identity": None, "children": []}
    if os.path.islink(path) or os.path.realpath(path) != os.path.normpath(path):
        return {"path": path, "kind": "link", "mode": "skip", "reason": "symlink or inside a symlinked folder — never followed",
                "rule": "symlink", "size": 0, "identity": None, "children": []}
    v = classify_fn(path, keep=keep or (), home=home)
    is_wt = os.path.isfile(os.path.join(path, ".git")) and v.get("rule") == "worktree"
    kind = "worktree" if is_wt else ("dir" if os.path.isdir(path) and not os.path.islink(path) else "file")
    mode = _mode_for(v, kind)
    reason = v.get("reason", "")
    if v.get("rule") == "frames" and mode == "trash" and _is_latest_sibling(path):
        mode, reason = "keep", "latest version of this render — kept"
    item = {"path": path, "kind": kind, "mode": mode, "reason": reason, "rule": v.get("rule", ""),
            "verdict": {"class": v["class"], "action": v.get("action"), "rule": v.get("rule")},
            "size": _size(path), "identity": _identity(path), "children": []}
    if kind == "dir" and mode == "trash" and with_children and v.get("rule") == "frames":
        item["children"] = _frame_children(path, mode)
    return item


def _latest_per_family(names):
    """{family → name of its highest version} for version-marked names (shotA_v2 and shotB_v7 are separate families)."""
    tops = {}
    for n in names:
        if not VERSION_TOKEN.search(n):
            continue
        fam = family_key(n)
        if fam not in tops or version_rank(n) > version_rank(tops[fam]):
            tops[fam] = n
    return set(tops.values())


def _is_latest_sibling(path):
    name = os.path.basename(path)
    if LATEST_NAMES.match(name):
        return True
    if not VERSION_TOKEN.search(name):
        return False
    try:
        siblings = os.listdir(os.path.dirname(path))
    except OSError:
        return True  # can't tell → keep (safe direction)
    return name in _latest_per_family(siblings)


def _frame_children(path, mode):
    """Render folders: keep master/final/latest and the highest version of each family; older versions follow the folder."""
    kids = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return kids
    tops = _latest_per_family(names)
    for n in names:
        p = os.path.join(path, n)
        if os.path.islink(p):
            continue  # links are never acted on
        keepit = bool(LATEST_NAMES.match(n)) or n in tops
        kids.append({"path": p, "kind": "dir" if os.path.isdir(p) else "file", "mode": "keep" if keepit else mode,
                     "reason": "latest version — kept" if keepit else "older version", "rule": "frames",
                     "size": _size(p), "identity": _identity(p)})
    return kids


def _totals(items):
    t = {"permanent": 0, "remove": 0, "trash": 0, "keep": 0, "skip": 0}
    likely = dict(t)
    for i in items:
        for x in (i["children"] if i["children"] and i["mode"] != "skip" else [i]):
            t[x["mode"]] += x["size"]
            p = x.get("private")
            likely[x["mode"]] += x["size"] if p is None else p
    t["likely"] = likely
    return t


def build(paths, home=None, keep=None, classify_fn=classify):
    norm = sorted({os.path.normpath(p) for p in paths}, key=len)
    chosen = []
    for p in norm:  # drop anything inside an already-chosen folder
        if not any(p == c or p.startswith(c.rstrip("/") + "/") for c in chosen):
            chosen.append(p)
    items = [_item(p, classify_fn, home, keep) for p in chosen]
    for i in items:  # what deleting would actually free on APFS (clones share blocks) — display only, never a safety check
        for x in [i] + i["children"]:
            x["private"] = private_tree(x["path"]) if x.get("kind") in ("dir", "file", "worktree") and x["size"] else 0
            if x.get("kind") == "dir":
                x["deep"] = _deep(x["path"])
    for i in items:
        floor = override_floor(i["path"], keep or (), home) if os.path.lexists(i["path"]) else "no longer exists"
        if floor is None and _protected(i):
            floor = "protected by the rules — " + (i.get("reason") or "never deleted here")
        i["overridable"] = floor is None and i["kind"] in ("dir", "file")
        i["floor_reason"] = floor
    return {"id": uuid.uuid4().hex[:12], "created": time.time(), "items": items, "totals": _totals(items)}


def _protected(item):
    return (item.get("verdict") or {}).get("class") == "protected"


def apply_overrides(manifest, overrides, forced=None, keep=None, home=None):
    """overrides: safer-only changes. forced: the user's "Delete anyway" — Trash only, refused at the floor and for
    anything the rules protect."""
    m = {**manifest, "items": [dict(i, children=[dict(c) for c in i["children"]]) for i in manifest["items"]]}
    for i in m["items"]:
        want = (forced or {}).get(i["path"])
        if i.get("forced"):  # already your call — re-applying must not nest the reason; "keep" still backs out
            if overrides.get(i["path"]) == "keep":
                i["mode"], i["forced"], i["reason"] = "keep", False, i.get("rule_reason", i["reason"])
            continue
        if want == "permanent":
            want = "trash"  # overrides never make anything permanent: "delete anyway" moves it to the Trash
        if want in FORCEABLE and _protected(i):
            if "can't override" not in i["reason"]:
                i["reason"] = "can't override: protected by the rules — " + i["reason"]
            continue
        if isinstance(want, str) and want in FORCEABLE and i["kind"] in ("dir", "file") \
                and os.path.lexists(i["path"]) and override_floor(i["path"], keep or (), home) is None:
            i["rule_reason"] = i["reason"]
            i["mode"], i["forced"] = want, True
            i["reason"] = f"your call (rules said: {i['rule_reason']})"
            i["children"] = []  # the whole item, exactly as you chose
            continue
        for x in [i] + i["children"]:
            want = overrides.get(x["path"])
            if isinstance(want, str) and want in SAFER.get(x["mode"], set()):
                x["mode"] = want
                if x is i and want == "keep":
                    for c in i["children"]:
                        c["mode"] = "keep"
    m["totals"] = _totals(m["items"])
    return m


def targets(manifest):
    out = []
    for i in manifest["items"]:
        if i["mode"] in ("keep", "skip"):
            continue
        if i["children"]:
            out.extend(c for c in i["children"] if c["mode"] in ("permanent", "remove", "trash"))
        else:
            out.append(i)
    return out
