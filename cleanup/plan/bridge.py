"""Run engine items through the one deletion pipeline.

Path targets go through manifest -> executor (identity re-check, keep list, floors, rules-only permanence, Trash
with undo, journal). Delegated tool commands run only if they are on the fixed allowlist below; argv, exit code and
the end of stderr are journaled. Every deleting entry point
of the engine calls prepare() (before asking) and execute() (after); nothing else in the engine deletes."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import uuid

from cleanup import fsops
from cleanup.engine.classify import classify
from cleanup.engine.core import kept
from cleanup.plan import manifest as plan_manifest
from cleanup.plan.executor import SETTINGS_CHANGED, STOPPED, UNKNOWN_STORE, execute as plan_execute, last_gate
from cleanup.plan.trash import move_to_trash

PERMANENT, TRASH, REMOVE, DELEGATED = "permanent", "trash", "remove", "delegated"


# The only tool commands Afterparty runs (spec 4.1 "permanent, delegated"). Fixed argv, never a path from a scan.
TIER1_COMMANDS = (["env", "UV_LOCK_TIMEOUT=10", "uv", "cache", "prune"], ["pnpm", "store", "prune"],
                  ["env", "HOMEBREW_NO_AUTO_UPDATE=1", "brew", "cleanup", "-s", "--prune=all"])
TIER4_COMMANDS = (["uv", "cache", "clean", "--force"], ["npm", "cache", "clean", "--force"])
OLLAMA_MODEL = re.compile(r"^[A-Za-z0-9._:/-]+$")
NOT_ALLOWED = "not an allowed command"
# Finished work the engine never takes (spec 4.1 step 3), chosen by the ENGINE item's class first: only items the
# engine deletes for good (disposition permanent) may use the per-rule table below — rebuildable rules block only
# videos and project sources (packages ship README.md and models), `.scratch` runs block nothing (throwaway by
# convention; age, open-file and git checks still apply). Every Trash-class engine item (frames, temp, personal,
# frames-latest, anything forced) blocks the full list, whatever classify calls the folder. The user's own map picks are exempt (they never pass through here). Worktree removal is exempt only because
# `git worktree remove` refuses untracked or changed content (worktree_status sees it as dirty).
VIDEO_AND_SOURCES = (".mp4", ".mov", ".webm", ".blend", ".aep")
DELIVERABLES = VIDEO_AND_SOURCES + (".glb", ".fbx", ".md")
FINISHED_BY_RULE = {"dependency": VIDEO_AND_SOURCES, "tool-cache": VIDEO_AND_SOURCES,
                    "build-output": VIDEO_AND_SOURCES, "app-cache": VIDEO_AND_SOURCES,
                    "system-app-cache": VIDEO_AND_SOURCES, "old-version": VIDEO_AND_SOURCES,
                    "code-sign-clone": VIDEO_AND_SOURCES, "scratch": ()}  # any other rule: DELIVERABLES
DELIVERABLE_WALK_LIMIT = 200000


def allowed_command(item):
    """None if the item's command is on the allowlist for its tier, else the reason it is refused."""
    argv = getattr(item, "command", None)
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return NOT_ALLOWED
    if item.tier == 1 and argv in [list(c) for c in TIER1_COMMANDS]:
        return None
    if item.tier == 4 and argv in [list(c) for c in TIER4_COMMANDS]:
        return None
    if item.tier == 4 and len(argv) == 3 and argv[:2] == ["ollama", "rm"] and OLLAMA_MODEL.match(argv[2]) \
            and not argv[2].startswith("-"):
        return None
    return NOT_ALLOWED


def _tool(argv):
    return next(a for a in argv if a != "env" and "=" not in a)


UNRESOLVED = object()  # a store location that can't be worked out: the command is refused


def _uv_cache_dir():
    """uv's own answer (`uv cache dir`, no shell, short timeout): a path, None if uv isn't installed, UNRESOLVED if it
    is installed but can't say."""
    exe = shutil.which("uv")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "cache", "dir"], capture_output=True, text=True, timeout=10, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return UNRESOLVED
    out = (r.stdout or "").strip()
    if r.returncode != 0 or "\n" in out or not os.path.isabs(out):
        return UNRESOLVED
    return out


def _env_dir(name, *sub):
    """A store location from an env var: None if unset, UNRESOLVED if not an absolute path."""
    v = os.environ.get(name)
    if not v:
        return None
    v = os.path.expanduser(v)
    return os.path.join(v, *sub) if os.path.isabs(v) else UNRESOLVED


def command_paths(item, home):
    """Every folder a delegated command may empty: the item's own path (when it is one) and every place the tool's
    store may be (its env overrides, its defaults and, for uv, uv's own answer). None if a location can't be
    resolved or the tool is unknown: the command is then refused."""
    tool = _tool(item.command)
    h = lambda *r: os.path.join(home, *r)
    if tool == "uv":
        found = [_env_dir("UV_CACHE_DIR"), _env_dir("XDG_CACHE_HOME", "uv"), h(".cache", "uv"), _uv_cache_dir()]
    elif tool == "npm":
        found = [_env_dir("npm_config_cache"), _env_dir("NPM_CONFIG_CACHE"), h(".npm")]
    elif tool == "brew":
        found = [_env_dir("HOMEBREW_CACHE"), h("Library", "Caches", "Homebrew")]
    elif tool == "pnpm":
        found = [_env_dir("PNPM_STORE_DIR"), _env_dir("XDG_DATA_HOME", "pnpm"), h("Library", "pnpm", "store"),
                 h(".local", "share", "pnpm", "store"), h(".pnpm-store")]
    elif tool == "ollama":
        found = [_env_dir("OLLAMA_MODELS"), h(".ollama", "models")]
    else:
        return None
    if any(f is UNRESOLVED for f in found):
        return None
    own = [item.path] if isinstance(item.path, str) and os.path.isabs(item.path) else []
    return own + [f for f in found if f]


def run_command(argv, timeout=900):
    """(exit code, stderr). A command that can't start or times out counts as failed."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.returncode, r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    except OSError as e:
        return -1, str(e)


def _tool_running(tool):
    from cleanup.engine.core import running
    return running(tool)  # True if running or unknown


def _finished_exts(t, item):
    if item is None or item.disposition != PERMANENT or item.force_trash:
        return DELIVERABLES
    rule = t.get("rule") or ""
    if rule == "dependency" and os.path.basename(os.path.normpath(t["path"])).casefold() == ".scratch":
        rule = "scratch"  # tidy's whole .scratch folder is matched by the dependency rule
    return FINISHED_BY_RULE.get(rule, DELIVERABLES)


def _deliverable_why(t, item=None):
    """`item`: the engine item that owns the target (None counts as Trash-class: the full list)."""
    if t["mode"] == REMOVE:
        return None
    exts = _finished_exts(t, item)
    if not exts:
        return None
    p = t["path"]
    if os.path.splitext(p)[1].casefold() in exts:
        return "a finished file (video, 3D, project file or notes) — never cleaned automatically"
    if os.path.isdir(p) and not os.path.islink(p):
        seen = 0
        for d, dirs, files in os.walk(p):
            dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
            seen += len(dirs) + len(files)
            if seen > DELIVERABLE_WALK_LIMIT:
                return "too big to check for finished files — not cleaned"
            if any(os.path.splitext(f)[1].casefold() in exts for f in files):
                return "holds finished files (video, 3D, project files or notes) — never cleaned automatically"
    return None


def _owner(owner, path):
    p = os.path.normpath(path)
    while p and p != "/":
        if p in owner:
            return owner[p]
        p = os.path.dirname(p)
    return None


class Busy(RuntimeError):
    pass


class _Lock:
    def __init__(self, state):
        self.path = os.path.join(state, "run.lock")
        self._held = None

    def __enter__(self):
        held = fsops.held(self.path, blocking=False,
                          busy="Another Afterparty cleanup is running. Try again when it finishes.")
        try:
            held.__enter__()
        except fsops.Busy as e:
            raise Busy(str(e))
        self._held = held
        return self

    def __exit__(self, *exc):
        self._held.__exit__(None, None, None)


def lock(state):
    return _Lock(state)


def test_trash(dest):
    """Stand-in Trash for tests (AFTERPARTY_TEST_TRASH_DIR): moves into <dest>/.Trash, answers like the real one."""
    root = os.path.join(dest, ".Trash")

    def move(paths):
        os.makedirs(root, exist_ok=True)
        out = {}
        for p in paths:
            landing = os.path.join(root, uuid.uuid4().hex[:8] + "-" + os.path.basename(p))
            try:
                shutil.move(p, landing)
                out[p] = landing
            except OSError as e:
                out[p] = "ERR:" + str(e)
        return out

    return move


test_trash.__test__ = False  # not a pytest test, despite the name


def default_trash():
    """Real Trash mover. AFTERPARTY_TEST_TRASH_DIR is a test-only seam."""
    seam = os.environ.get("AFTERPARTY_TEST_TRASH_DIR")
    return test_trash(seam) if seam else move_to_trash


def new_run_id():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _journal(journal_path, **event):
    event["ts"] = time.time()
    fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _merge(res, out):
    for key in ("deleted", "trashed", "skipped", "failed"):
        res[key] += out.get(key, [])
    res["freed"] += out.get("freed", 0)
    res["trash_bytes"] += out.get("trash_bytes", 0)
    if out.get("halted"):
        res["halted"] = out["halted"]


class Prepared:
    """What the user approves: the exact identity-bearing manifests, delegated commands and totals. Built before
    asking; execute() acts on exactly this (anything changed since is skipped as "changed since review")."""

    def __init__(self, run_id, state, home, allowed_roots, classify_fn):
        self.run_id, self.state, self.home = run_id, state, home
        self.allowed_roots, self.classify_fn = list(allowed_roots), classify_fn
        self.owner = {}       # path -> Item
        self.commands = []    # delegated items
        self.failed = []      # [(event dict, result dict)] found while preparing; journaled at execute
        self.skipped = []     # same shape: targets dropped while preparing (e.g. an ancestor conflict)
        self.prechecked = []  # items whose precheck runs again right before acting
        self.pre_skips = []   # [(item, reason)] refused by the caller's own filter before prepare (core.safe_target)
        self.manifests = []   # permanent pass first, then the Trash pass
        self.totals = {"permanent": 0, "remove": 0, "trash": 0, "targets": 0, "commands": 0}

    def has_work(self):
        return bool(self.commands) or self.totals["targets"] > 0


def _inside(path, parent):
    return path.startswith(parent.rstrip("/") + "/")


def _resolve_conflicts(prep, permanent, trash, forced, removes):
    """The permanent pass runs first, so it must never take a Trash-bound item with it:
    - a permanent target holding a Trash or worktree target becomes a Trash target itself (a worktree holding a
      Trash target is not removed at all: the Trash pass never runs `git worktree remove`);
    - a permanent target inside (or equal to) a Trash target is dropped: the Trash move covers it."""
    soft = [os.path.normpath(p) for p in trash + forced]
    perm = [os.path.normpath(p) for p in permanent]
    keep_perm, moved = [], []
    for p in perm:
        holds = [c for c in soft + [r for r in removes if r != p] if _inside(c, p)]
        if not holds:
            keep_perm.append(p)
        elif p in removes:
            why = "holds items going to the Trash — worktree not removed"
            prep.skipped.append((dict(event="action", path=p, mode="remove", result="skipped", reason=why),
                                 {"path": p, "size": 0, "reason": why}))
        else:
            moved.append(p)  # downgraded: the Trash move takes its contents along, undoably
    soft += moved
    keep_perm = [p for p in keep_perm if not any(p == s_ or _inside(p, s_) for s_ in soft)]
    return keep_perm, [os.path.normpath(p) for p in trash] + moved


def prepare(items, keep, allowed_roots, state, home=None, classify_fn=classify, run_id=None):
    """Prechecks, identity-bearing manifests, delegated commands and totals. No side effects."""
    home = home or os.path.expanduser("~")
    keep_now = list(keep() if callable(keep) else (keep or []))
    prep = Prepared(run_id or new_run_id(), state, home, allowed_roots, classify_fn)
    permanent, trash, forced, removes = [], [], [], set()
    for it in items:
        if not it.disposition:
            continue
        prep.owner[it.path] = it
        if it.disposition == DELEGATED:
            why = allowed_command(it)
            if why:
                prep.failed.append((dict(event="delegated", path=it.path, kind=it.kind, argv=it.command,
                                         result="failed", reason=why), {"path": it.path, "size": 0, "reason": why}))
            else:
                prep.commands.append(it)
            continue
        try:
            paths = list(it.precheck()) if it.precheck else list(it.targets)
        except Exception as e:
            prep.failed.append((dict(event="precheck", path=it.path, kind=it.kind, result="failed", reason=str(e)),
                                {"path": it.path, "size": 0, "reason": str(e)}))
            continue
        prep.prechecked.append(it) if it.precheck else None
        for p in paths:
            prep.owner[os.path.normpath(p)] = it
            if it.disposition in (PERMANENT, REMOVE):
                permanent.append(p)
                if it.disposition == REMOVE:
                    removes.add(os.path.normpath(p))
            elif it.force_trash and classify_fn(p, keep=keep_now, home=home)["class"] != "protected":
                forced.append(p)
            else:
                trash.append(p)
    permanent, trash = _resolve_conflicts(prep, permanent, trash, forced, removes)
    if permanent:
        prep.manifests.append(plan_manifest.build(permanent, home=home, keep=keep_now, classify_fn=classify_fn))
    if trash or forced:
        m = plan_manifest.build(trash + forced, home=home, keep=keep_now, classify_fn=classify_fn)
        safer = {}
        for t in plan_manifest.targets(m):
            if t["mode"] == PERMANENT:
                safer[t["path"]] = TRASH
            elif t["mode"] == REMOVE:  # trash-class items never run `git worktree remove`
                safer[t["path"]] = "keep"
        prep.manifests.append(plan_manifest.apply_overrides(m, safer, forced={p: TRASH for p in forced},
                                                            keep=keep_now, home=home))
    for m in prep.manifests:
        for t in plan_manifest.targets(m):
            prep.totals[t["mode"]] += t.get("size", 0)
            prep.totals["targets"] += 1
    prep.totals["commands"] = len(prep.commands)
    return prep


def _preflight(prepared):
    """The check each target gets right before it is acted on (queueing, the Trash flush, the last look before a
    permanent step): its item's precheck is called again every time — never a result from earlier — then the
    finished-file rule."""
    prechecked = {id(it) for it in prepared.prechecked}

    def check(t):
        it = _owner(prepared.owner, t["path"])
        if it is not None and id(it) in prechecked:
            try:
                ok = [os.path.normpath(p) for p in it.precheck()]
            except Exception as e:
                return f"no longer safe to clean: {e}"
            p = os.path.normpath(t["path"])
            if not any(p == q or _inside(p, q) for q in ok):
                return "no longer passes its check (changed since review, or in use)"
        return _deliverable_why(t, it)
    return check


def execute(prepared, keep, trash_fn=None, progress=None, command_fn=None, running_fn=None, open_fn=None, stop=None):
    """Act on exactly what prepare() built. `keep`: a list or a zero-arg callable re-read before every action; if it
    raises (settings became invalid or missing), nothing more is cleaned: a halt in any pass (commands, permanent,
    Trash) skips everything left in the whole run."""
    trash_fn = trash_fn or default_trash()
    command_fn = command_fn or run_command
    running_fn = running_fn or _tool_running
    keep_now = keep if callable(keep) else (lambda: keep)
    jdir = os.path.join(prepared.state, "journal")
    os.makedirs(jdir, exist_ok=True)
    journal = os.path.join(jdir, prepared.run_id + ".jsonl")
    res = {"run_id": prepared.run_id, "journal": journal, "deleted": [], "trashed": [], "skipped": [], "failed": [],
           "delegated": [], "freed": 0, "trash_bytes": 0, "owner": dict(prepared.owner)}
    for event, row in prepared.failed:
        _journal(journal, **event)
        res["failed"].append(row)
    for event, row in prepared.skipped:
        _journal(journal, **event)
        res["skipped"].append(row)
    for it in prepared.commands:
        if stop is not None and stop.is_set() and not res.get("halted"):  # Stop wins over every later command
            res["skipped"].append({"path": it.path, "size": 0, "reason": "stopped by you"})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, result="skipped", reason="stopped by you")
            continue
        k = []
        if not res.get("halted"):
            try:
                k = list(keep_now() or [])  # current protections, re-read right before each command
            except Exception:
                res["halted"] = SETTINGS_CHANGED
                _journal(journal, event="halted", reason=SETTINGS_CHANGED)
        if res.get("halted"):
            res["skipped"].append({"path": it.path, "size": 0, "reason": SETTINGS_CHANGED})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, result="skipped", reason=SETTINGS_CHANGED)
            continue
        argv = list(it.command or [])
        why = allowed_command(it)
        if not why and it.tier == 1 and running_fn(_tool(argv)):
            why = f"{_tool(argv)} is running (or Afterparty can't tell) — not pruned"
        if why:
            res["failed"].append({"path": it.path, "size": 0, "reason": why})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, argv=argv, result="failed", reason=why)
            continue
        stores = command_paths(it, prepared.home)  # resolved every time: the last gate needs it even if k is empty
        if k and stores is None:
            why = UNKNOWN_STORE
        elif k and any(kept(p, k) for p in stores):
            why = "in your keep list (or holds something in it) — not run"
        if why:
            res["skipped"].append({"path": it.path, "size": 0, "reason": why})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, argv=argv, result="skipped", reason=why)
            continue
        _journal(journal, event="attempt", mode="delegated", path=it.path, argv=argv)
        status, blocked = last_gate(stores, keep_now, stop, res.get("halted"))
        if status != "go" or blocked:
            if status == "halt" and not res.get("halted"):
                res["halted"] = SETTINGS_CHANGED
                _journal(journal, event="halted", reason=SETTINGS_CHANGED)
            why = SETTINGS_CHANGED if status == "halt" else STOPPED if status == "stop" else \
                "in your keep list (or holds something in it) — not run" if None not in blocked else UNKNOWN_STORE
            res["skipped"].append({"path": it.path, "size": 0, "reason": why})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, argv=argv, result="skipped", reason=why)
            continue
        code, err = command_fn(argv)  # nothing between the gate and this call
        tail = (err or "").strip()[-500:]
        if code == 0:
            res["delegated"].append({"path": it.path, "kind": it.kind})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, argv=argv, exit=code, stderr=tail,
                     result="ok")
        else:  # report and keep going
            reason = f"{' '.join(argv)} exited {code}" + (f": {tail.splitlines()[-1]}" if tail else "")
            res["failed"].append({"path": it.path, "size": 0, "reason": reason})
            _journal(journal, event="delegated", path=it.path, kind=it.kind, argv=argv, exit=code, stderr=tail,
                     result="failed", reason=reason)
    check = _preflight(prepared) if prepared.manifests and not res.get("halted") else None
    for m in prepared.manifests:
        # a halt is terminal for the whole run (no later pass starts, even if settings recover); so is Stop
        why = SETTINGS_CHANGED if res.get("halted") else (
            "stopped by you" if stop is not None and stop.is_set() else None)
        if why:
            for t in plan_manifest.targets(m):
                res["skipped"].append({"path": t["path"], "size": t.get("size", 0), "reason": why})
                _journal(journal, event="action", path=t["path"], mode=t["mode"], result="skipped", reason=why)
            continue
        _merge(res, plan_execute(m, keep=keep_now, home=prepared.home, classify_fn=prepared.classify_fn,
                                 trash_fn=trash_fn, journal=journal, allowed_roots=prepared.allowed_roots,
                                 progress=progress, open_fn=open_fn, preflight=check, stop=stop))
    return res


def run(items, keep, allowed_roots, state, home=None, trash_fn=None, classify_fn=classify, run_id=None,
        command_fn=None, running_fn=None, open_fn=None, stop=None):
    """prepare + execute in one go (no review step in between)."""
    return execute(prepare(items, keep, allowed_roots, state, home=home, classify_fn=classify_fn, run_id=run_id),
                   keep, trash_fn=trash_fn, command_fn=command_fn, running_fn=running_fn, open_fn=open_fn, stop=stop)
