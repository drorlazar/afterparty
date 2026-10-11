"""Execute a manifest: revalidate every target, then delete / trash / remove worktree.

Safety rules (spec §4.5 + final reviews):
- symlinks are never acted on (Finder would resolve them and trash the *target*)
- identity must be unchanged: dev/inode, plus size+mtime for files, mtime for folders, and for
  PERMANENT folders the total size (a file saved deep inside stops the delete)
- PERMANENT only if the full classify still says regenerable/permanent, and only inside allowed roots
- a user override ("forced") only ever goes to the Trash, and never past a protected verdict
- trash targets get the full preflight again immediately before each Trash call (small batches, in order; fresh
  lsof and git checks)
- the journal is fsync'd before a result is reported; Trash attempts are journaled first
- every target (Trash and permanent): refused if git tracks it or anything inside it, if it holds a git repository,
  or if any process has it (or anything inside) open — an lsof failure counts as "open"
"""
import bisect
import json
import os
import time

from cleanup.engine.classify import classify
from cleanup.engine.core import git_root, kept, kept_norm, norm_path, remove_worktree, rmtree, run as run_cmd
from cleanup.engine.leftovers import in_use, open_paths
from cleanup.plan.manifest import WHOLE_APP, _deep, _size, base_floor, override_floor, targets
from cleanup.plan.trash import BATCH, move_to_trash

SETTINGS_CHANGED = "settings changed or became invalid — nothing more was cleaned"
STOPPED = "stopped by you"
UNKNOWN_STORE = "can't tell where this tool keeps its files, so your keep list can't be checked — not run"


def last_gate(paths, keep_now, stop, halted=False):
    """THE invariant: no side effect without a last-moment gate. Every irreversible call (each trash_fn call, each
    permanent rmtree, each worktree removal, each delegated command) is preceded IMMEDIATELY by this gate; nothing but
    the call itself runs in between (no check, hook, lookup or callback).

    Strict order: (1) all filesystem work on `paths` (the exact targets, or a command's resolved store; None =
    unknown): their realpaths; (2) a fresh settings read and the keep set's realpaths; (3) with NO further I/O: the
    halt flag, Stop, and a pure in-memory keep comparison (`kept_norm`). So Stop or a settings change during any I/O
    here is still seen. The only window left is the pure in-memory code between the final Stop check and the call.
    Returns (status, blocked): status "go", "halt" (settings invalid: halt the run) or "stop"; `blocked` maps each
    path that may not be touched to its reason (with "go")."""
    targets = None if paths is None else [(p, norm_path(p)) for p in paths]  # (1) target I/O
    try:  # (2) settings I/O
        ks = [norm_path(k) for k in (keep_now() or [])]
    except Exception:
        return "halt", {}
    # (3) no I/O from here to the caller's irreversible call
    if halted:
        return "halt", {}
    if stop is not None and stop.is_set():
        return "stop", {}
    if not ks:
        return "go", {}
    if targets is None:
        return "go", {None: UNKNOWN_STORE}
    return "go", {p: "now in your keep list" for p, rp in targets if kept_norm(rp, ks)}


def _changed(t):
    """Cheap identity check (no classify): returns a reason or None."""
    p = t["path"]
    if not os.path.lexists(p):
        return "no longer exists"
    if os.path.islink(p):
        return "is a symlink — Afterparty never follows or trashes links"
    if os.path.realpath(p) != os.path.normpath(p):
        return "a folder on its path became a symlink"
    st = os.lstat(p)
    idn = t.get("identity") or {}
    if (st.st_dev, st.st_ino) != (idn.get("dev"), idn.get("ino")):
        return "replaced since review"
    if st.st_mtime_ns != idn.get("mtime"):
        return "changed since review"
    if t["kind"] == "file" and st.st_size != idn.get("size", st.st_size):
        return "changed since review"
    return None


class TrackedCheck:
    """Is a path (or anything under it) tracked by git? One `git ls-files` per repository; any git failure counts as
    tracked (refuse)."""

    def __init__(self):
        self.repos = {}

    def __call__(self, path):
        repo = git_root(path)
        if not repo:
            return False
        if repo not in self.repos:
            code, out = run_cmd(["git", "-C", repo, "ls-files", "-z"], timeout=120)
            self.repos[repo] = None if code != 0 else sorted(x for x in out.split("\0") if x)
        files = self.repos[repo]
        if files is None:
            return True
        rel = os.path.relpath(path, repo)
        if rel == ".":
            return bool(files)
        i = bisect.bisect_left(files, rel)
        if i < len(files) and files[i] == rel:
            return True
        j = bisect.bisect_left(files, rel + "/")
        return j < len(files) and files[j].startswith(rel + "/")


def _git_why(path, tracked_fn):
    if tracked_fn(path):
        return "tracked by git — not deleted"
    if os.path.isdir(path) and not os.path.islink(path):
        for d, dirs, files in os.walk(path):
            if any(n.casefold() == ".git" for n in dirs + files):
                return "contains a git repository — not deleted"
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
    return None


def _inside_check(path, keep):
    """Walk a folder about to be deleted for good: refuse if it holds a git repo or anything in the keep list."""
    for d, dirs, files in os.walk(path):
        for n in dirs + files:
            full = os.path.join(d, n)
            if n.casefold() == ".git":
                return "contains a git repository — not deleted"
            if keep and kept(full, keep):
                return "contains something in your keep list — not deleted"
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
    return None


def _why_not(t, keep, home, classify_fn, allowed_roots, checks=None):
    why = _changed(t)
    if why:
        return why
    p = t["path"]
    forced = bool(t.get("forced"))
    if keep and kept(p, list(keep)):
        return "now in your keep list"
    floor = base_floor(p, home)
    if floor:
        return f"never deleted: {floor}"
    if forced:
        if t["mode"] != "trash":
            return "can't override: your choices only move things to the Trash"
        floor = override_floor(p, keep or (), home)
        if floor:
            return f"can't override: {floor}"
    if t["mode"] == "permanent" and t["kind"] == "dir":
        if _size(p) != t["size"] or ("deep" in t and _deep(p) != t["deep"]):
            return "changed since review (contents changed inside)"
        why = _inside_check(p, list(keep or []))
        if why:
            return why
    if True:  # every action stays inside the allowed roots (default: your home folder)
        rp = os.path.realpath(p)
        app_to_trash = t["mode"] == "trash" and WHOLE_APP.match(rp) and (not forced or override_floor(p, keep or (), home) is None)
        if not app_to_trash and not any(rp == r or rp.startswith(r.rstrip("/") + "/") for r in allowed_roots):
            return "outside the folders Afterparty may delete from"
    if checks:
        if t["mode"] != "remove":  # `git worktree remove` does its own clean-and-pushed check
            why = _git_why(p, checks["tracked"])
            if why:
                return why
        opened = checks["open"]()
        if opened is None:
            return "can't tell which files are open right now — not deleted"
        if in_use(p, opened):
            return "open in an app right now — not deleted"
        if checks.get("preflight"):
            why = checks["preflight"](t)
            if why:
                return why
    v = classify_fn(p, keep=keep or (), home=home)
    if v["class"] == "protected":
        return "can't override: protected by the rules" if forced else "verdict changed: now protected"
    if forced:
        return None  # the user's Trash decision replaces the rule-based checks below (identity, keep, floor still applied)
    rec = t.get("verdict")
    if rec and (v["class"], v.get("action")) != (rec.get("class"), rec.get("action")):
        return "verdict changed since review"
    if t["mode"] == "permanent" and not (v["class"] == "regenerable" and v.get("action") == "permanent"):
        return "verdict changed: no longer regenerable"
    return None


def _free():
    st = os.statvfs("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    return st.f_bavail * st.f_frsize


def _trash_dir_path(r):
    """Only trust the Trash call's answer if it's an absolute path inside a Trash folder."""
    if not r or not os.path.isabs(r):
        return None
    rp = os.path.realpath(r)
    trash = os.path.realpath(os.path.expanduser("~/.Trash"))
    if rp.startswith(trash + "/") or "/.Trashes/" in rp or "/.Trash/" in rp:
        return r
    return None


def execute(manifest, keep=None, home=None, classify_fn=classify, trash_fn=move_to_trash, progress=None, stop=None,
            journal=None, allowed_roots=None, open_fn=None, tracked_fn=None, preflight=None):
    """`keep` may be a list or a zero-arg callable (re-read before every action; if it raises, nothing more is done).
    open_fn: paths open in any process, or None if unknown (default: lsof). preflight: optional extra check,
    callable(target) -> reason or None, run right before each action."""
    keep_now = keep if callable(keep) else (lambda: keep)
    checks = {"open": open_fn or open_paths, "tracked": tracked_fn or TrackedCheck(), "preflight": preflight}
    allowed_roots = [os.path.realpath(r) for r in (allowed_roots or [os.path.expanduser("~")])]
    res = {"deleted": [], "trashed": [], "skipped": [], "failed": [], "freed": 0, "trash_bytes": 0}
    jf = None
    if journal:
        fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        jf = os.fdopen(fd, "a", buffering=1)

    def log(**e):
        if jf:
            jf.write(json.dumps({"ts": time.time(), **e}) + "\n")
            jf.flush()
            os.fsync(jf.fileno())

    def ov(t):
        return {"override": True, "rule_reason": t.get("rule_reason", "")} if t.get("forced") else {}

    def skip(t, why):
        log(event="action", path=t["path"], mode=t["mode"], result="skipped", reason=why, **ov(t))
        res["skipped"].append({"path": t["path"], "size": t["size"], "reason": why})

    def halt(rest):
        """Settings became invalid or missing: the whole run stops here, everything left is skipped."""
        if not res.get("halted"):
            res["halted"] = SETTINGS_CHANGED
            log(event="halted", reason=SETTINGS_CHANGED, remaining=len(rest))
        for x in rest:
            skip(x, SETTINGS_CHANGED)

    def stopped(rest):
        """Stop pressed (or the run halted): skip everything left. True if the caller must stop now."""
        if res.get("halted"):
            halt(rest)
            return True
        if stop is not None and stop.is_set():
            log(event="stopped", remaining=len(rest))
            for x in rest:
                skip(x, "stopped by you")
            return True
        return False

    def flush(batch):
        """Move a batch to the Trash. False if the run halted (the batch is skipped; the caller stops too)."""
        ready = []
        if not batch:
            return True
        try:
            k = keep_now()
        except Exception:  # settings became invalid or missing: nothing more moves, in this batch or after it
            halt(batch)
            return False
        # The full preflight again, right before the move: identity, keep list, floors, roots, git-tracked, a fresh
        # open-file check (one lsof per batch, never a cached one; a failure means "in use"), the caller's preflight
        # and the rules. Whatever fails is skipped; the rest moves.
        opened = (open_fn or (lambda: open_paths(max_age=0)))()
        now = {"open": lambda: opened, "tracked": tracked_fn or TrackedCheck(), "preflight": preflight}
        for t in batch:
            if stop is not None and stop.is_set():
                skip(t, "stopped by you"); continue
            why = _why_not(t, k, home, classify_fn, allowed_roots, now)
            skip(t, why) if why else ready.append(t)
        if not ready:
            return True
        log(event="attempt", mode="trash", paths=[t["path"] for t in ready],
            overrides=[t["path"] for t in ready if t.get("forced")])
        status, blocked = last_gate([t["path"] for t in ready], keep_now, stop, res.get("halted"))
        if status == "halt":
            halt(ready)
            return False
        if status == "stop":
            for t in ready:
                skip(t, STOPPED)
            return True
        go = [t for t in ready if t["path"] not in blocked]
        try:
            out = trash_fn([t["path"] for t in go]) if go else {}  # nothing between the gate and this call
        except Exception as e:  # the Trash call died mid-batch: verify each path below
            out = {t["path"]: "ERR:" + str(e) for t in go}
        for t in ready:
            if t["path"] in blocked:
                skip(t, blocked[t["path"]]); continue
            r = out.get(t["path"], "ERR:no answer")
            if not r.startswith("ERR:") and not os.path.lexists(t["path"]):
                tp = _trash_dir_path(r)
                log(event="action", path=t["path"], mode="trash", result="trashed", size=t["size"], trash_path=tp, **ov(t),
                    ino=(t.get("identity") or {}).get("ino"), dev=(t.get("identity") or {}).get("dev"),
                    **({} if tp else {"reason": "moved to the Trash (location not reported — undo by hand)"}))
                res["trashed"].append({"path": t["path"], "size": t["size"], "trash_path": tp})
                res["trash_bytes"] += t["size"]
            elif not os.path.lexists(t["path"]):  # moved, but we weren't told where
                log(event="action", path=t["path"], mode="trash", result="trashed", size=t["size"], trash_path=None,
                    reason="moved to the Trash (location not reported — undo by hand)", **ov(t))
                res["trashed"].append({"path": t["path"], "size": t["size"], "trash_path": None})
                res["trash_bytes"] += t["size"]
            else:
                log(event="action", path=t["path"], mode="trash", result="failed", reason=r[4:], **ov(t))
                res["failed"].append({"path": t["path"], "size": t["size"], "reason": r[4:]})
        return True

    todo = targets(manifest)
    before = _free()
    log(event="start", plan=manifest["id"], targets=len(todo))
    for t in todo:
        if t.get("forced"):
            log(event="override", path=t["path"], mode=t["mode"], rule_reason=t.get("rule_reason", ""))
    for i in manifest["items"]:
        if i["mode"] == "skip":
            res["skipped"].append({"path": i["path"], "size": i["size"], "reason": i["reason"]})
    batch = []
    for n, t in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            log(event="stopped", remaining=len(todo) - n + 1 + len(batch))
            for x in batch + todo[n - 1:]:  # nothing else moves after Stop, not even queued Trash items
                skip(x, "stopped by you")
            batch = []
            break
        try:
            k = keep_now()
        except Exception:  # settings became invalid or missing: stop, skip everything left
            halt(batch + todo[n - 1:])
            batch = []
            break
        why = _why_not(t, k, home, classify_fn, allowed_roots, checks)
        if why:
            skip(t, why)
        elif t["mode"] == "trash":
            batch.append(t)
            if len(batch) >= BATCH:
                ok = flush(batch); batch = []
                if not ok:  # a halt is terminal: nothing after this batch runs
                    halt(todo[n:])
                    break
                if stopped(todo[n:]):  # Stop pressed during the flush
                    break
        else:
            ok = flush(batch); batch = []  # keep actions in manifest order
            if not ok:
                halt(todo[n - 1:])
                break
            if stopped(todo[n - 1:]):  # Stop pressed during (or right after) the flush
                break
            try:  # the settings once more, right before the irreversible step
                k = keep_now()
            except Exception:
                halt(todo[n - 1:])
                break
            last = _changed(t) or ("now in your keep list" if k and kept(t["path"], list(k)) else None) or (
                # one more look right before the irreversible step (incl. deep contents and a fresh precheck)
                "changed since review (contents changed inside)"
                if t["mode"] == "permanent" and t["kind"] == "dir" and "deep" in t and _deep(t["path"]) != t["deep"] else None) \
                or (preflight(t) if preflight else None)
            if last:
                skip(t, last)
                if progress:
                    progress({"done": n, "total": len(todo), "path": t["path"]})
                continue
            if stopped(todo[n - 1:]):  # the very last look: Stop or a halt always wins over an irreversible step
                break
            log(event="attempt", mode=t["mode"], path=t["path"], **ov(t))  # on disk before the irreversible step
            status, blocked = last_gate([t["path"]], keep_now, stop, res.get("halted"))
            if status != "go" or blocked:
                if status == "halt":
                    halt(todo[n - 1:])
                    break
                if status == "stop":
                    stopped(todo[n - 1:])
                    break
                skip(t, blocked[t["path"]])
                if progress:
                    progress({"done": n, "total": len(todo), "path": t["path"]})
                continue
            try:  # nothing between the gate and the irreversible call
                if t["mode"] == "remove":
                    remove_worktree(t["path"])
                else:
                    rmtree(t["path"])  # engine's remove: unlinks links/files, rmtree never follows symlinks
                log(event="action", path=t["path"], mode=t["mode"], result="deleted", size=t["size"], **ov(t))
                res["deleted"].append({"path": t["path"], "size": t["size"]})
            except Exception as e:
                log(event="action", path=t["path"], mode=t["mode"], result="failed", reason=str(e), **ov(t))
                res["failed"].append({"path": t["path"], "size": t["size"], "reason": str(e)})
        if progress:
            progress({"done": n, "total": len(todo), "path": t["path"]})
    if batch:
        flush(batch)
    res["freed"] = max(0, _free() - before)
    log(event="end", freed=res["freed"], trash_bytes=res["trash_bytes"],
        counts={k: len(v) for k, v in res.items() if isinstance(v, list)})
    if jf:
        jf.close()
    return res
