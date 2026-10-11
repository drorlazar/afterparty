"""Afterparty cleaner — find and remove disk junk left by Claude/Codex sessions.

Dry run by default. Settings live in the Afterparty config file; reports and logs go to config.state_dir().

Tiers
  1  rebuildable: node_modules, venvs, git-ignored build output, tool caches, expired .scratch
  2  git worktrees: clean, unlocked, idle, every commit on a remote branch
  3  frame/image sequences: older versions of a render (highest version kept)
  4  ask-only: big personal items (local models, old Codex logs, full package caches, Downloads)

Usage (through the public `afterparty` command, cleanup/commands.py)
  afterparty report               report only (full manifest in the Afterparty state folder)
  afterparty clean                apply tiers 1-3 after one confirmation
  afterparty clean --tier 4       interactive, asks per item
  afterparty weekly               scheduled: report and notify, or with autopilot on apply tiers 1+3
  afterparty tidy PATH            one finished task's folder (no idle threshold)
  afterparty check-space          notify when free space is below low_space_gb
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import unicodedata
import re
import shutil
import subprocess
import sys
import time

from cleanup import config, fsops

HOME = os.path.expanduser("~")
STATE = config.state_dir()
LEDGER = f"{STATE}/sessions.jsonl"

IMG_EXT = {".png", ".jpg", ".jpeg", ".exr", ".webp", ".tif", ".tiff", ".tga", ".bmp"}
SEQ_MIN_FILES = int(os.environ.get("DISKCLEAN_SEQ_MIN_FILES", 30))
SEQ_MIN_BYTES = int(os.environ.get("DISKCLEAN_SEQ_MIN_BYTES", 50 * 2**20))
SEQ_PROTECTED_PARTS = {"assets", "public", "src", "static", "sprites", "textures", "Assets"}

# Always-disposable dependency/cache dirs (still skipped if tracked by git)
REBUILDABLE = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
               ".turbo", ".parcel-cache", ".vite", ".venv", "venv"}
# Build output: only deleted inside a git repo where git itself marks the folder as ignored
BUILD_OUTPUT = {".next": "package.json", "dist": "package.json", "build": "package.json", "target": "Cargo.toml"}
WORKTREE_CONTAINERS = {"worktrees", ".worktrees"}
NO_DESCEND = {".git", ".scratch"} | REBUILDABLE | set(BUILD_OUTPUT) | WORKTREE_CONTAINERS
# Ignored files that may be lost with a worktree without asking
DISPOSABLE_IGNORED = REBUILDABLE | set(BUILD_OUTPUT) | {
    ".DS_Store", ".scratch", "coverage", ".cache", ".vercel", "test-results", "test-results.json",
    "playwright-report", ".superpowers", ".claude", "settings.local.json"}

# Only explicit version markers define a "version family"; shot_01/shot_02 stay distinct
VERSION_TOKEN = re.compile(
    r"(?i)(?<![a-z])(v|ver|version|round|iter|take|try|attempt|rev|r|test|draft|final-v|delivery-v)[-_]?(\d+)(?![a-z])")
DATE_TOKEN = re.compile(r"(\d{8}T\d{6}Z|\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------- helpers
def gb(n):
    return f"{max(n, 0) / 2**30:6.1f} GB"


def short(p):
    return p.replace(HOME, "~", 1)


def run(cmd, cwd=None, timeout=60, env=None):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return -1, ""


def du(path):
    code, out = run(["du", "-sk", path], timeout=600)
    try:
        return int(out.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def newest_mtime(path, depth=2):
    """Cheap activity proxy: newest mtime of path and entries down to `depth` levels.
    Unreadable paths count as active (now), never as ancient."""
    try:
        best = os.lstat(path).st_mtime
    except OSError:
        return time.time()
    if depth <= 0 or not os.path.isdir(path) or os.path.islink(path):
        return best
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        best = max(best, newest_mtime(e.path, depth - 1))
                    else:
                        best = max(best, e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    return time.time()
    except OSError:
        return time.time()
    return best


def idle_days(path, depth=2):
    return (time.time() - newest_mtime(path, depth)) / 86400


def project_idle_days(path, limit=20000):
    """Newest mtime across a project's own files (skipping dependency/build dirs), plus .git/index.
    Bounded walk; hitting the limit or any read error counts as active."""
    best, seen = 0, 0
    for d, dirs, files in os.walk(path, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs[:] = [x for x in dirs if x not in NO_DESCEND and not os.path.islink(os.path.join(d, x))]
        for n in files + dirs:
            seen += 1
            if seen > limit:
                return 0
            try:
                best = max(best, os.lstat(os.path.join(d, n)).st_mtime)
            except OSError:
                return 0
    for marker in (".git/index", ".git/HEAD"):
        try:
            best = max(best, os.lstat(os.path.join(path, marker)).st_mtime)
        except OSError:
            pass
    return (time.time() - best) / 86400


_IDLE_CACHE = {}


def safe_project_idle(path):
    if path in _IDLE_CACHE:
        return _IDLE_CACHE[path]
    try:
        _IDLE_CACHE[path] = project_idle_days(path)
    except OSError:
        _IDLE_CACHE[path] = 0
    return _IDLE_CACHE[path]


def load_keep(cfg):
    return config.keep_paths(cfg)


def norm_path(x):
    """The form keep-list comparisons use (does I/O: resolves symlinks). APFS is case-insensitive by default:
    case-folded so "Scratch" can't dodge a kept "scratch"; Unicode-normalised: APFS treats "café" (NFC) and
    "cafe\u0301" (NFD) as the same name."""
    return unicodedata.normalize("NFC", os.path.realpath(x)).casefold()


def kept_norm(rp, ks):
    """Pure, no I/O: is the normalised path `rp` kept by (or holding) any normalised keep entry in `ks`?"""
    return any(rp == k or rp.startswith(k + os.sep) or k.startswith(rp + os.sep) for k in ks)


def kept(path, keep):
    return kept_norm(norm_path(path), [norm_path(k) for k in keep])


def running(proc_name):
    """True if running OR unknown (pgrep failed) — only 'definitely absent' is False."""
    code, _ = run(["pgrep", "-xq", proc_name])
    return code != 1


def notify(title, msg):
    if os.environ.get("AFTERPARTY_NO_NOTIFY") == "1":
        return
    msg = msg.replace('"', "'")
    run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'])


def free_bytes():
    st = os.statvfs(HOME)
    return st.f_bavail * st.f_frsize


class Item:
    """disposition: "permanent" (rules must agree), "trash", "remove" (git worktree), "delegated" (a fixed tool command
    in `command`, an argv list the bridge runs only if it is on its allowlist), or None (report only). targets: the
    exact paths acted on. precheck: zero-arg callable that returns the targets still safe (or raises RuntimeError to
    skip the item); it runs when the cleanup is prepared and again right before acting."""

    def __init__(self, tier, kind, path, reason, size=0, command=None, files=None, children=None,
                 disposition=None, targets=None, precheck=None, force_trash=False):
        self.tier, self.kind, self.path, self.reason = tier, kind, path, reason
        self.size, self.command, self.files = size, (list(command) if command else None), files
        self.children = children  # tier 4: list of paths approved one by one
        self.disposition, self.precheck, self.force_trash = disposition, precheck, force_trash
        self.targets = list(targets) if targets is not None else [path]


# ---------------------------------------------------------------- scanning
def git_root(path):
    d = path
    while d and d != "/":
        if os.path.exists(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return None


def tracked(path):
    """True if git tracks anything under path. Git errors count as tracked (refuse to delete)."""
    repo = git_root(path)
    if not repo:
        return False
    code, out = run(["git", "-C", repo, "ls-files", "--", os.path.relpath(path, repo)], timeout=60)
    return code != 0 or bool(out)


def git_ignored(path):
    repo = git_root(os.path.dirname(path))
    if not repo:
        return False
    code, _ = run(["git", "-C", repo, "check-ignore", "-q", os.path.relpath(path, repo)])
    return code == 0


def image_seq(entries):
    imgs = [e for e in entries if os.path.splitext(e.name)[1].lower() in IMG_EXT and e.is_file(follow_symlinks=False)]
    if len(imgs) < SEQ_MIN_FILES:
        return None
    numbered = sum(1 for e in imgs if re.search(r"\d+$", os.path.splitext(e.name)[0]))
    if numbered < 0.8 * len(imgs):
        return None
    files, size, newest = [], 0, 0
    for e in imgs:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            return None  # can't judge it — leave it alone
        size += st.st_size
        newest = max(newest, st.st_mtime)
        files.append(e.path)
    return (files, size, newest) if size >= SEQ_MIN_BYTES else None


def walk(roots):
    """Single pruned walk. Returns rebuildable dirs, worktree containers, image seqs, scratch dirs,
    and roots/dirs that could not be read (e.g. macOS privacy blocks under launchd)."""
    rebuild, containers, seqs, scratch, blocked = [], [], [], [], []
    stack = [(r, 0) for r in roots if os.path.isdir(r)]
    while stack:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            blocked.append(d)
            continue
        seq = image_seq(entries)
        if seq:
            seqs.append((d, *seq))
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            n = e.name
            if n == ".scratch":
                scratch.append(e.path)
            elif n in WORKTREE_CONTAINERS:
                containers.append(e.path)
            elif n in REBUILDABLE or n in BUILD_OUTPUT:
                rebuild.append(e.path)
            if n not in NO_DESCEND:
                stack.append((e.path, depth + 1))
    return rebuild, containers, seqs, scratch, blocked


def rmtree(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)  # raises on failure; the executor logs it


TOOL_CONFIGS = [f"{HOME}/.claude.json", f"{HOME}/.claude/settings.json", f"{HOME}/.codex/config.toml",
                f"{HOME}/.cursor/mcp.json", f"{HOME}/.gemini/settings.json",
                f"{HOME}/Library/Application Support/Claude/claude_desktop_config.json"]


def tool_referenced():
    """Text of AI-tool configs (MCP servers etc.): a dependency folder a configured command runs from isn't idle,
    however old its project looks."""
    out = []
    for f in TOOL_CONFIGS:
        try:
            with open(f, errors="replace") as fh:
                out.append(fh.read())
        except OSError:
            pass
    return "\n".join(out)


def in_service(p, configs, opened):
    """Used by a configured tool, or open in a running process (unknown open files count as in use)."""
    from cleanup.engine.leftovers import in_use
    return (p + "/") in configs or p + '"' in configs or in_use(p, opened)


def _still(paths, ok):
    """Paths that still exist and pass ok(path, opened) with a fresh lsof snapshot (lsof failure -> all in use)."""
    from cleanup.engine.leftovers import open_paths
    opened = open_paths(max_age=0)
    return [p for p in paths if os.path.lexists(p) and ok(p, opened)]


def _frame_targets(d, files):
    """Trash the whole folder only when it holds nothing but these frames; otherwise only the frames."""
    names = {os.path.basename(f) for f in files}
    try:
        others = set(os.listdir(d)) - names - {".DS_Store"}
    except OSError:
        return list(files)
    return [d] if not others else list(files)


def _frames_unchanged(d, files, days):
    for f in files:
        try:
            if (time.time() - os.lstat(f).st_mtime) / 86400 < days:
                raise RuntimeError("frames modified since scan")
        except FileNotFoundError:
            pass
    alive = [f for f in files if os.path.lexists(f)]
    return [p for p in _frame_targets(d, alive) if os.path.lexists(p)]


def _scratch_still_expired(sub, scratch_days):
    """Precheck for one .scratch run: still a real folder, and untouched for scratch_days (fresh walk, no cache)."""
    if not os.path.isdir(sub) or os.path.islink(sub):
        return []
    try:
        idle = project_idle_days(sub)
    except OSError:
        idle = 0
    if idle < scratch_days:
        raise RuntimeError(f"used in the last {scratch_days} days — kept")
    return [sub]


def tier1(rebuild, scratch, days, scratch_days=config.DEFAULTS["scratch_days"]):
    from cleanup.engine.leftovers import open_paths
    items, configs, opened = [], None, None
    for p in rebuild:
        name, parent = os.path.basename(p), os.path.dirname(p)
        if name in BUILD_OUTPUT:
            if not os.path.exists(os.path.join(parent, BUILD_OUTPUT[name])) or not git_ignored(p):
                continue
        if name in ("venv", ".venv") and not os.path.exists(os.path.join(p, "pyvenv.cfg")):
            continue
        if idle_days(p, 1) < days:
            continue
        proj_idle = safe_project_idle(parent)  # the project must be idle too, not just the dependency folder
        if proj_idle < days or tracked(p):
            continue
        if configs is None:
            configs, opened = tool_referenced(), open_paths()
        if in_service(p, configs, opened):
            continue
        items.append(Item(1, name, p, f"project idle {proj_idle:.0f}d", disposition="permanent"))
    for p in scratch:
        # tidy (days=0) clears the whole .scratch; otherwise expire each run folder separately
        if days == 0:  # tidy: you asked for this folder's scratch to go; open files are still checked
            items.append(Item(1, ".scratch", p, "scratch (tidy)", disposition="permanent"))
            continue
        try:  # each run folder (a real folder, not a file or a link) expires on its own
            subs = [os.path.join(p, n) for n in sorted(os.listdir(p))]
        except OSError:
            continue
        for sub in subs:
            if os.path.isdir(sub) and not os.path.islink(sub) and safe_project_idle(sub) >= scratch_days:
                items.append(Item(1, ".scratch", sub, f"scratch run idle {scratch_days}+ days", disposition="permanent",
                                  precheck=lambda sub=sub: _scratch_still_expired(sub, scratch_days)))
    return items


def _clone_aware_size(paths):
    """Allocated bytes with APFS clone data counted once (an upper bound when it's shared with an installed app)."""
    from cleanup.engine.apfs import SharedCounter
    shared, size = SharedCounter(), 0
    for p in paths:
        walk_ = os.walk(p) if os.path.isdir(p) and not os.path.islink(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]
        for d, _, files in walk_:
            for f in files:
                try:
                    size += shared.size(os.path.join(d, f), os.lstat(os.path.join(d, f)))
                except OSError:
                    pass
    return size


def tier1_leftovers(x_dir, t_dir, c_dir=None):
    """App leftovers: stale browser code-sign clones, stale $TMPDIR items, old Claude Code versions, updater and
    extension download caches. Returns (items, extra allowed roots). Each item's precheck re-checks, right before
    acting, that no process has the path open."""
    from cleanup.engine.leftovers import (busy, cache_stale, claude_keep, claude_version_dirs, in_use, old_versions,
                                          running_ids, stale_caches, stale_clones, stale_temp, temp_stale)
    items, allowed = [], [d for d in (x_dir, t_dir, c_dir) if d]
    for group, clones in stale_clones(x_dir).items():
        app = os.path.basename(group)[:-len(".code_sign_clone")]
        items.append(Item(1, "app-leftover", group, f"{len(clones)} stale {app} launch copies (APFS clones; frees up to this)",
                          size=_clone_aware_size(clones), disposition="permanent", targets=clones,
                          precheck=lambda clones=clones: _still(clones, lambda c, o: not busy(c, o))))

    temp = stale_temp(t_dir)
    if temp:
        items.append(Item(1, "temp", t_dir, f"{len(temp)} temp items untouched 3+ days, open in no app",
                          size=_clone_aware_size(temp), disposition="trash", targets=temp,
                          precheck=lambda temp=temp: _still(temp, lambda p, o: temp_stale(p, o))))

    app_caches = stale_caches(c_dir)
    if app_caches:
        names = ", ".join(os.path.basename(p) for p in app_caches[:4]) + ("…" if len(app_caches) > 4 else "")
        items.append(Item(1, "app-cache", c_dir, f"{len(app_caches)} app GPU/compiler caches, apps not running: {names}",
                          size=_clone_aware_size(app_caches), disposition="permanent", targets=app_caches,
                          precheck=lambda c=app_caches: (lambda run_ids: _still(c, lambda p, o: cache_stale(p, o, run_ids)))(running_ids())))

    keep = claude_keep(HOME)
    for d in claude_version_dirs(HOME):
        for p in old_versions(d, keep):
            items.append(Item(1, "old-version", p, "older Claude Code version; a newer one is installed",
                              size=_clone_aware_size([p]), disposition="permanent",
                              precheck=lambda p=p: _still([p], lambda q, o: not in_use(q, o))))

    support = f"{HOME}/Library/Application Support"
    caches = [(f"{support}/Google/GoogleUpdater/crx_cache", 1, "Google updater downloads (already installed)")]
    try:
        caches += [(f"{support}/{a}/CachedExtensionVSIXs", 0, f"{a} extension downloads") for a in sorted(os.listdir(support))]
    except OSError:
        pass
    for d, days, why in caches:
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        old = [os.path.join(d, n) for n in sorted(os.listdir(d)) if idle_days(os.path.join(d, n)) >= days]
        if old:
            items.append(Item(1, "cache", d, why, size=_clone_aware_size(old), disposition="permanent", targets=old,
                              precheck=lambda old=old: [p for p in old if os.path.lexists(p)]))
    return items, allowed


def tier1_caches():
    items = []
    if shutil.which("uv"):
        items.append(Item(1, "cache", f"{HOME}/.cache/uv", "uv cache prune (unused entries; size unknown until run)",
                          size=-1, disposition="delegated", command=["env", "UV_LOCK_TIMEOUT=10", "uv", "cache", "prune"]))
    if shutil.which("pnpm"):
        items.append(Item(1, "cache", "pnpm store", "pnpm store prune (unreferenced packages)",
                          size=-1, disposition="delegated", command=["pnpm", "store", "prune"]))
    if shutil.which("brew"):
        items.append(Item(1, "cache", f"{HOME}/Library/Caches/Homebrew", "brew cleanup (old versions)", disposition="delegated",
                          command=["env", "HOMEBREW_NO_AUTO_UPDATE=1", "brew", "cleanup", "-s", "--prune=all"]))
    for path, proc in [(f"{HOME}/Library/Caches/com.spotify.client", "Spotify"),
                       (f"{HOME}/Library/Caches/com.openai.codex", "Codex"),
                       (f"{HOME}/Library/Caches/Codex", "Codex")]:
        if os.path.isdir(path) and not os.path.islink(path) and not running(proc):
            def children(p=path, proc=proc):
                if running(proc):  # re-check right before acting
                    raise RuntimeError(f"{proc} started")
                return [os.path.join(p, c) for c in os.listdir(p)]
            items.append(Item(1, "cache", path, f"app cache ({proc} not running)", disposition="permanent",
                              targets=[], precheck=children))
    return items


def worktree_status(wt):
    """Return (ok_to_remove, reason). Every git failure means 'not ok'."""
    code, out = run(["git", "-C", wt, "status", "--porcelain", "--ignored"], timeout=120)
    if code != 0:
        return False, "git status failed"
    dirty = [l for l in out.splitlines() if not l.startswith("!! ")]
    if dirty:
        return False, f"{len(dirty)} uncommitted changes"
    ignored = [l[3:].rstrip("/") for l in out.splitlines() if l.startswith("!! ")]
    keepers = [p for p in ignored if os.path.basename(p) not in DISPOSABLE_IGNORED and not p.endswith(".log")]
    if keepers:
        return False, f"has ignored files git would delete ({', '.join(keepers[:3])})"
    code, wl = run(["git", "-C", wt, "worktree", "list", "--porcelain"])
    if code != 0:
        return False, "git worktree list failed"
    real = os.path.realpath(wt)
    blocks = wl.split("\n\n")
    mine = [b for b in blocks if b.startswith("worktree ") and os.path.realpath(b.splitlines()[0][9:]) == real]
    if not mine:
        return False, "not a registered worktree"
    if any(l.startswith("locked") for l in mine[0].splitlines()):
        return False, "locked"
    quiet = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    code, _ = run(["git", "-C", wt, "fetch", "--quiet", "--no-tags", "--prune"], timeout=60, env=quiet)
    if code != 0:
        return False, "couldn't reach the remote to confirm every commit is pushed"
    code, remotes = run(["git", "-C", wt, "branch", "-r", "--contains", "HEAD"], timeout=60)
    if code == 0 and remotes.strip():
        return True, "clean, every commit on the remote"
    code, br = run(["git", "-C", wt, "branch", "--contains", "HEAD"], timeout=60)
    return False, "clean but NOT pushed" + (" (local branch keeps commits)" if br else " (detached!)")


def remove_worktree(wt):
    ok, why = worktree_status(wt)  # re-check right before removal
    if not ok:
        raise RuntimeError(f"no longer safe: {why}")
    code, common = run(["git", "-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if code != 0:
        raise RuntimeError("rev-parse failed")
    code, _ = run(["git", "-C", wt, "worktree", "remove", wt], timeout=600)  # never --force
    if code != 0:
        raise RuntimeError("git refused to remove worktree")
    run(["git", "--git-dir", common, "worktree", "prune"])


def find_worktrees(containers, include_codex=True):
    wts = []
    for c in containers + ([f"{HOME}/.codex/worktrees"] if include_codex else []):
        if not os.path.isdir(c):
            continue
        try:
            subs = [os.path.join(c, x) for x in os.listdir(c)]
        except OSError:
            continue
        for d in subs:
            if os.path.isfile(os.path.join(d, ".git")):
                wts.append(d)
            elif os.path.isdir(d) and not os.path.islink(d):  # codex layout: worktrees/<id>/<repo>
                try:
                    wts += [os.path.join(d, x) for x in os.listdir(d) if os.path.isfile(os.path.join(d, x, ".git"))]
                except OSError:
                    pass
    return sorted(set(wts))


def tier2(containers, days, include_codex=True):
    items, needs = [], []

    def check(wt):
        commit_age = 1e9
        code, ct = run(["git", "-C", wt, "log", "-1", "--format=%ct"])
        if code == 0 and ct.isdigit():
            commit_age = (time.time() - int(ct)) / 86400
        age = min(commit_age, idle_days(wt, 2))
        ok, why = worktree_status(wt)
        return wt, age, ok, why

    with cf.ThreadPoolExecutor(8) as ex:
        for wt, age, ok, why in ex.map(check, find_worktrees(containers, include_codex)):
            if age < days:
                continue
            it = Item(2, "worktree", wt, f"{why}, idle {age:.0f}d", disposition="remove" if ok else None)
            (items if ok else needs).append(it)
    return items, needs


def family_key(path):
    """Path with explicit version markers (v3, round8, test2, dates) blanked out."""
    return DATE_TOKEN.sub("#", VERSION_TOKEN.sub(lambda m: m.group(1).lower() + "#", path))


def version_rank(path):
    """Numeric version tuple used to pick the latest member of a family."""
    nums = [int(m.group(2)) for m in VERSION_TOKEN.finditer(path)]
    dates = [m.group(1) for m in DATE_TOKEN.finditer(path)]
    return (dates, nums)


def tier3(seqs, days):
    families = {}
    for d, files, size, newest in seqs:
        if SEQ_PROTECTED_PARTS & set(d.split(os.sep)):
            continue
        families.setdefault(family_key(d), []).append((version_rank(d), newest, d, files, size))
    items, kept_latest = [], 0
    for fam in families.values():
        fam.sort(key=lambda m: (m[0], m[1]), reverse=True)  # highest version, then newest frames
        kept_latest += fam[0][4]
        for _, newest, d, files, size in fam[1:]:
            if (time.time() - newest) / 86400 < days or tracked(d):
                continue
            items.append(Item(3, "frames", d, f"{len(files)} frames, older of {len(fam)} versions (kept {short(fam[0][2])})",
                              size=size, files=files, disposition="trash", force_trash=True,
                              targets=_frame_targets(d, files),
                              precheck=lambda d=d, f=files: _frames_unchanged(d, f, days)))
    return items, kept_latest


def tier4():
    items = []
    cutoff = time.time() - 60 * 86400

    def add(path, reason, command=None):
        if os.path.exists(path):
            if command is None:
                items.append(Item(4, "personal", path, reason, disposition="trash"))
            else:
                items.append(Item(4, "personal", path, reason, command=command, disposition="delegated"))

    def info(path, reason, size=0):
        if os.path.exists(path):
            items.append(Item(4, "info", path, reason, size=size))

    for sess in (f"{HOME}/.codex/sessions", f"{HOME}/.codex/archived_sessions"):
        if os.path.isdir(sess):
            old = [os.path.join(r, f) for r, _, fs in os.walk(sess) for f in fs
                   if os.path.getmtime(os.path.join(r, f)) < cutoff]
            if old:
                items.append(Item(4, "personal", sess, f"{len(old)} Codex session logs > 60d",
                                  size=sum(os.path.getsize(f) for f in old), disposition="trash", targets=old))
    if shutil.which("uv"):
        add(f"{HOME}/.cache/uv", "full uv cache, removed for good (it re-downloads)",
            command=["uv", "cache", "clean", "--force"])
    if shutil.which("npm"):
        add(f"{HOME}/.npm/_cacache", "full npm cache, removed for good (it re-downloads)",
            command=["npm", "cache", "clean", "--force"])
    add(f"{HOME}/.cache/huggingface", "HuggingFace models (re-downloadable)")
    add(f"{HOME}/.cache/mlx", "MLX models (re-downloadable)")
    pw = f"{HOME}/Library/Caches/ms-playwright"
    if os.path.isdir(pw):
        revs = {}
        for n in os.listdir(pw):
            m = re.match(r"(.+)-(\d+)$", n)
            if m:
                revs.setdefault(m.group(1), []).append((int(m.group(2)), n))
        for k, vs in revs.items():
            for v, n in sorted(vs)[:-1]:
                add(os.path.join(pw, n), f"old Playwright {k} (newest kept; projects pinned to it re-download)")
    if shutil.which("ollama"):
        code, out = run(["ollama", "list"])
        for line in out.splitlines()[1:]:
            name = line.split()[0] if line.split() else ""
            if name:
                items.append(Item(4, "model", f"ollama:{name}", "local Ollama model, removed for good (it re-downloads)",
                                  command=["ollama", "rm", name], disposition="delegated"))
    for folder, label in ((f"{HOME}/Downloads", "Downloads"),):
        try:
            old = [os.path.join(folder, n) for n in os.listdir(folder)
                   if newest_mtime(os.path.join(folder, n), 2) < cutoff]
        except OSError:
            old = []
        if old:
            items.append(Item(4, "personal", folder, f"{len(old)} {label} items > 60d — asked one by one",
                              size=sum(du(p) for p in old), children=old))
    info(f"{HOME}/Library/Application Support/Google/DriveFS", "Google Drive cache — managed by Drive")
    info(f"{HOME}/Library/Application Support/Google/Chrome", "Chrome profile — never touched; clear cache inside Chrome")
    info(f"{HOME}/Library/Containers/com.docker.docker", "Docker data — use `docker system df` / `docker system prune`")
    info(f"{HOME}/.claude/projects", "Claude transcripts — retention via cleanupPeriodDays in settings")
    return items


# ---------------------------------------------------------------- reporting / applying
def size_items(items):
    todo = [i for i in items if i.size == 0 and os.path.exists(i.path)]
    with cf.ThreadPoolExecutor(8) as ex:
        for it, s in zip(todo, ex.map(lambda i: du(i.path), todo)):
            it.size = s
    models = [i for i in items if i.kind == "model"]
    if models:
        total = du(f"{HOME}/.ollama/models")
        for i in models:
            i.size = total // len(models)


def report(items, needs, kept_latest, blocked, out, scope="global", scope_path=""):
    names = {1: "Rebuildable & caches", 2: "Worktrees", 3: "Old frame versions", 4: "Ask-only (personal)"}
    ordered = sorted(items + needs, key=lambda i: (i.tier, -i.size, i.path))
    ids = {id(i): n for n, i in enumerate(ordered, 1)}
    lines = [f"Afterparty report ({scope}) — {dt.datetime.now():%Y-%m-%d %H:%M} — free: {gb(free_bytes()).strip()}", ""]
    if blocked:
        lines.append(f"!! Could not read {len(blocked)} folders (not scanned): "
                     + ", ".join(short(b) for b in blocked[:5]))
        lines.append("")
    for t in (1, 2, 3, 4):
        ti = sorted([i for i in items if i.tier == t], key=lambda i: -i.size)
        if not ti:
            continue
        lines.append(f"== Tier {t}: {names[t]} — {gb(sum(i.size for i in ti)).strip()} in {len(ti)} items")
        groups = {}
        for i in ti:
            groups.setdefault(i.kind, []).append(i)
        for k, g in sorted(groups.items(), key=lambda kv: -sum(i.size for i in kv[1])):
            lines.append(f"   {k:<14} {gb(sum(i.size for i in g))}  ({len(g)})")
        cap = 40 if t == 4 else 15
        for i in ti[:cap]:
            tag = "" if i.disposition else "  [manual]"
            lines.append(f"  #{ids[id(i)]:<3} {gb(i.size)}  {short(i.path)}  — {i.reason}{tag}")
        if len(ti) > cap:
            lines.append(f"     … {len(ti) - cap} more (see manifest)")
        lines.append("")
    if kept_latest:
        lines.append(f"(kept latest frame versions: {gb(kept_latest).strip()})")
    if needs:
        lines.append(f"== Needs you: {len(needs)} worktrees not removable automatically")
        for i in sorted(needs, key=lambda i: -i.size)[:25]:
            lines.append(f"  #{ids[id(i)]:<3} {gb(i.size)}  {short(i.path)}  — {i.reason}")
        lines.append("")
    leftovers = session_leftovers()
    if leftovers and scope == "global":
        lines.append("== Recent session leftovers (from Claude SessionEnd ledger)")
        lines += [f"     {l}" for l in leftovers]
    text = "\n".join(lines)
    out.write(text + "\n")
    os.makedirs(STATE, exist_ok=True)
    name = ("last-report" if scope == "global" else
            "last-tidy-report" if scope.startswith("tidy") else "last-scoped-report")
    saved = (text + "\n").encode("utf-8")
    # The item list first, naming the exact text it belongs to; the text only after it. `tidy --only/--skip` maps
    # ids through this list only while the saved text is the one it names (_select), so a list that couldn't be
    # saved (a concurrent report) never lends its ids to the report you just saw.
    rows = [f"# {' '.join(sys.argv)}  {dt.datetime.now().isoformat(timespec='seconds')}\n", f"# scope\t{scope_path}\n",
            f"# report\t{hashlib.sha256(saved).hexdigest()}\n"]
    for i in ordered:  # every candidate, untruncated
        rows.append(f"{i.tier}\t{i.kind}\t{i.size}\t{'auto' if i.disposition else 'manual'}\t{i.path}\t{i.reason}\t{ids[id(i)]}\n")
        rows += [f"{i.tier}\t{i.kind}-item\t\task\t{c}\tchild of {short(i.path)}\t\n" for c in i.children or []]
    unsaved = "Couldn't save this report (another report was being written at the same time), so its #ids can't be " \
              "used with --only/--skip. Run the report again before picking items."
    try:
        fsops.replace_owned(f"{STATE}/{name}-manifest.tsv", "".join(rows).encode("utf-8"))
        fsops.replace_owned(f"{STATE}/{name}.txt", saved)  # R14
    except fsops.Conflict:
        print(unsaved, file=sys.stderr)  # the report was already printed; the saved pair stays unusable or old


class SelectError(Exception):
    pass


def _last_tidy_manifest():
    """(item list lines, checksum of the saved report text or None) of the last tidy report."""
    try:
        with open(f"{STATE}/last-tidy-report-manifest.tsv") as f:
            lines = f.read().splitlines()
    except OSError:
        return [], None
    try:
        with open(f"{STATE}/last-tidy-report.txt", "rb") as f:
            text_sum = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        text_sum = None
    return lines, text_sum


def _select(items, folder, only, skip, saved):
    """Pick items by the ids of the last tidy report for this same folder (read before this run rewrites it). The
    ids count only while the saved item list names the saved report text: else they may be another report's."""
    lines, text_sum = saved
    scope = next((l.split("\t", 1)[1] for l in lines if l.startswith("# scope\t")), None)
    if scope != os.path.realpath(folder):
        raise SelectError('Run the report for this folder first: afterparty tidy "%s"' % folder)
    names = next((l.split("\t", 1)[1] for l in lines if l.startswith("# report\t")), None)
    if names is None or names != text_sum:
        raise SelectError('The saved item list doesn\'t belong to the last report you saw (two reports ran at '
                          'once). Run the report again: afterparty tidy "%s"' % folder)
    by_id = {r[6]: r[4] for r in (l.split("\t") for l in lines if not l.startswith("#")) if len(r) > 6 and r[6]}
    wanted = [x.strip().lstrip("#") for x in (only or skip).split(",") if x.strip()]
    for w in wanted:
        if w not in by_id:
            raise SelectError(f"Unknown item #{w} — run the report again to see current ids")
    chosen = {by_id[w] for w in wanted}
    return [i for i in items if (i.path in chosen) == bool(only)]


def session_leftovers(days=14):
    if not os.path.exists(LEDGER):
        return []
    cutoff, rows = time.time() - days * 86400, []
    for line in open(LEDGER):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t", 0) >= cutoff and r.get("bytes", 0) > 200 * 2**20:
            rows.append(r)
    rows.sort(key=lambda r: -r["bytes"])
    return [f"{gb(r['bytes'])}  {short(r['cwd'])}  ({', '.join(short(p) for p in r.get('dirs', [])[:3])})"
            for r in rows[:10]]


def off_root_allowed(home):
    """The only places outside your project folders the engine may act (spec 4.2). Tier-4 locations are asked
    one by one; leftovers folders are added when tier 1 runs."""
    from cleanup.engine.leftovers import claude_version_dirs
    lib = home + "/Library"
    paths = [home + "/.cache/uv", home + "/.npm/_cacache", lib + "/Caches/com.spotify.client", lib + "/Caches/com.openai.codex",
             lib + "/Caches/Codex", lib + "/Caches/Homebrew", home + "/.codex/worktrees",
             lib + "/Application Support/Google/GoogleUpdater/crx_cache",
             home + "/.codex/sessions", home + "/.codex/archived_sessions", home + "/.cache/huggingface",
             home + "/.cache/mlx", lib + "/Caches/ms-playwright",
             home + "/Downloads"]
    paths += claude_version_dirs(home)
    try:
        support = lib + "/Application Support"
        paths += [f"{support}/{a}/CachedExtensionVSIXs" for a in os.listdir(support)]
    except OSError:
        pass
    return sorted({os.path.realpath(p) for p in paths})


def _inside_roots(path, roots):
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    return any(rp == r or rp.startswith(r.rstrip("/") + "/") for r in roots)


def safe_target(item, keep, allowed_roots):
    """Central guard for every deletion: keep.txt, no symlinked target, must resolve inside allowed roots."""
    p = item.path
    if not os.path.isabs(p):  # pseudo-paths like "pnpm store", "ollama:x"
        return True, ""
    if kept(p, keep):
        return False, "protected by keep.txt"
    if os.path.islink(p):
        return False, "is a symlink"
    rp = os.path.realpath(p)
    if not any(rp == r or rp.startswith(r + os.sep) for r in allowed_roots):
        return False, "resolves outside allowed roots"
    return True, ""


FAILED = []
LAST = {}


def _owner_tier(res, path):
    p = path
    while p and p != "/":
        if p in res["owner"]:
            return res["owner"][p]
        p = os.path.dirname(p)
    return None


def prepare(items, keep, allowed_roots):
    """Everything the user is about to approve: safe-target filter, prechecks and the identity-bearing manifests
    (bridge.prepare). Nothing is deleted or written here."""
    from cleanup.plan import bridge
    todo, pre = [], []
    for i in items:
        if not i.disposition:
            continue
        ok, why = safe_target(i, keep, allowed_roots)
        if ok:
            todo.append(i)
        else:
            pre.append((i, why))
    prep = bridge.prepare(todo, keep, allowed_roots, STATE)
    prep.pre_skips = pre
    return prep


def run_prepared(prep, log_reason, keep_fn=None):
    """Execute exactly what was prepared (and approved). The keep list is re-read from the settings before every
    action; if the settings become invalid or go missing, nothing more is cleaned."""
    from cleanup.plan import bridge
    os.makedirs(STATE, exist_ok=True)
    before = free_bytes()
    stamp = lambda: f"{dt.datetime.now():%H:%M:%S}"
    with open(f"{STATE}/log-{dt.date.today()}.tsv", "a") as log:
        for i, why in prep.pre_skips:
            log.write(f"{stamp()}\tSKIP\t{i.kind}\t0\t{i.path}\t{why}\t{log_reason}\n")
        with bridge.lock(STATE):
            res = bridge.execute(prep, keep_fn or config.current_keep)
        for key, tag in (("deleted", "tier"), ("trashed", "trash")):
            for e in res[key]:
                it = _owner_tier(res, e["path"])
                n = it.tier if it else 0
                log.write(f"{stamp()}\t{tag}{n}\t{it.kind if it else '?'}\t{e.get('size', 0)}\t{e['path']}\t"
                          f"{it.reason if it else ''}\t{log_reason}\n")
        for e in res["delegated"]:
            it = res["owner"].get(e["path"])
            log.write(f"{stamp()}\tcmd{it.tier if it else 0}\t{e['kind']}\t0\t{e['path']}\tfixed tool command\t{log_reason}\n")
        for key, tag in (("skipped", "SKIP"), ("failed", "FAIL")):
            for e in res[key]:
                it = _owner_tier(res, e["path"])
                if key == "failed":
                    FAILED.append((e["path"], e.get("reason", "")))
                kind = f"{it.kind}/tier{it.tier}" if it else "-"
                log.write(f"{stamp()}\t{tag}\t{kind}\t0\t{e['path']}\t{e.get('reason', '')}\t{log_reason}\n")
    LAST.clear()
    LAST.update(res)
    return max(0, free_bytes() - before)


def apply(items, log_reason, keep, allowed_roots):
    """Unattended: prepare and execute at once (autopilot). Interactive callers prepare, ask, then run_prepared."""
    return run_prepared(prepare(items, keep, allowed_roots), log_reason)


def _log_only(prep, log_reason):
    """Nothing to act on, but record why (failed prechecks, refused targets) in the log and journal."""
    if prep.failed or prep.skipped or prep.manifests or prep.pre_skips:
        run_prepared(prep, log_reason)


def plan_text(prep):
    """What a prepared cleanup will do, in plain words."""
    t, parts = prep.totals, []
    if t["permanent"]:
        parts.append(f"{gb(t['permanent']).strip()} of caches, dependency folders and scratch cleared for good")
    if t["remove"]:
        parts.append(f"{gb(t['remove']).strip()} of idle worktrees removed (only when every commit is on the remote)")
    if t["trash"]:
        parts.append(f"{gb(t['trash']).strip()} moved to the Trash")
    if t["commands"]:
        parts.append(f"{t['commands']} tool cleanup command(s) run")
    return "; ".join(parts) or "nothing"


def confirm(prompt):
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} Type 'yes' to continue: ").strip().lower() == "yes"


def _why_nothing(prep):
    reasons = [why for _, why in prep.pre_skips] + [row.get("reason", "") for _, row in prep.failed + prep.skipped] \
        + [i["reason"] for m in prep.manifests for i in m["items"] if i["mode"] in ("skip", "keep")]
    return reasons[0] if reasons else "nothing left to clean"


def ask_tier4(items, keep, allowed_roots):
    """Each item is prepared first, then shown and asked; a yes executes exactly that prepared item."""
    for i in [x for x in items if x.tier == 4 and (x.disposition or x.children)]:
        if i.children:
            print(f"\n{short(i.path)}: {i.reason}")
            for c in sorted(i.children, key=lambda p: -du(p)):
                prep = prepare([Item(4, "personal", c, "approved", disposition="trash")], keep, allowed_roots)
                if not prep.has_work():
                    print(f"  {short(c)}: left alone ({_why_nothing(prep)})")
                    continue
                ans = input(f"  move {short(c)} ({gb(prep.totals['trash']).strip()}) to the Trash? [y/N/q] ").strip().lower()
                if ans == "q":
                    break
                if ans == "y":
                    print(_summary(run_prepared(prep, "manual-tier4")))
            continue
        prep = prepare([i], keep, allowed_roots)
        if not prep.has_work():
            print(f"{short(i.path)}: left alone ({_why_nothing(prep)})")
            continue
        if i.disposition == "delegated":
            q = f"Remove {short(i.path)} for good ({gb(max(i.size, 0)).strip()}, {i.reason}; it re-downloads)? [y/N] "
        else:
            q = f"Move {short(i.path)} ({gb(prep.totals['trash']).strip()}, {i.reason}) to the Trash? [y/N] "
        if input(q).strip().lower() == "y":
            print(_summary(run_prepared(prep, "manual-tier4")))


def _summary(freed):
    msg = f"freed {gb(freed).strip()}"
    if LAST.get("trash_bytes"):
        msg += f"; {gb(LAST['trash_bytes']).strip()} moved to the Trash (undo: afterparty undo {LAST['run_id']})"
    if FAILED:
        msg += f"; {len(FAILED)} failed (see log)"
    return msg


def collect(roots, tiers, days, include_codex=True, scratch_days=config.DEFAULTS["scratch_days"]):
    rebuild, containers, seqs, scratch, blocked = walk(roots)
    items, needs, kept_latest = [], [], 0
    if 1 in tiers:
        items += tier1(rebuild, scratch, days, scratch_days)
    if 2 in tiers:
        a, needs = tier2(containers, days, include_codex)
        items += a
    if 3 in tiers:
        a, kept_latest = tier3(seqs, days)
        items += a
    return items, needs, kept_latest, seqs, blocked


def main(argv=None):
    from cleanup.plan import bridge
    try:
        return _main(argv)
    except bridge.Busy as e:
        print(str(e), file=sys.stderr)
        return 3


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="afterparty", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["tidy"], help="tidy: one finished task's folder")
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--apply", action="store_true", help="clean after confirmation")
    ap.add_argument("--yes", action="store_true", help="skip confirmation (with --apply)")
    ap.add_argument("--auto", action="store_true", help="unattended: apply tiers 1+3, report the rest, notify")
    ap.add_argument("--tier", default=None, help="comma list, e.g. 1,3 or 4")
    ap.add_argument("--days", type=int, default=None, help="idle threshold (default: idle_days in the settings)")
    ap.add_argument("--root", action="append", help="limit scan to these roots")
    ap.add_argument("--check-space", type=float, metavar="GB", help="notify if free space below GB")
    ap.add_argument("--only", help="tidy: comma list of report ids to clean")
    ap.add_argument("--skip", help="tidy: comma list of report ids to keep")
    ap.add_argument("--notify", action="store_true", help="dry run: post a notification with what was found")
    a = ap.parse_args(argv)

    if a.check_space is not None:
        free = free_bytes() / 2**30
        if free < a.check_space:
            notify("Disk space low", f"Only {free:.0f} GB free — run /afterparty")
        print(f"free: {free:.1f} GB")
        return

    try:
        cfg, present = config.load()
    except config.ConfigError as e:
        print(f"Afterparty settings are invalid ({e}). Nothing was deleted. Fix {config.config_path()} and try again.",
              file=sys.stderr)
        return 2
    if (a.apply or a.auto) and not present:
        print("Afterparty isn't set up yet, so nothing was deleted. Install it (see INSTALL.md) or create "
              f"{config.config_path()}.", file=sys.stderr)
        return 2
    keep = load_keep(cfg)
    days = cfg["idle_days"] if a.days is None else a.days
    cfg_roots = config.roots(cfg)[0]
    outside = ("That folder isn't inside your Afterparty project folders. Add it to roots in "
               f"{config.config_path()} first.")
    if a.root and (a.apply or a.auto) and not all(_inside_roots(r, cfg_roots) for r in a.root):
        print(outside, file=sys.stderr)  # --root narrows a cleaning run; it never widens the boundary
        return 2
    if a.root:
        roots = [os.path.abspath(os.path.expanduser(r)) for r in a.root]
    else:
        roots, missing = config.roots(cfg)
        for m in missing:
            print(f"note: project folder {m} does not exist — skipped", file=sys.stderr)
        if not present:
            print("note: Afterparty isn't set up yet — only app caches and leftovers were checked. "
                  "Install it (see INSTALL.md) to scan your project folders.", file=sys.stderr)
    allowed = sorted({os.path.realpath(r) for r in roots} | set(off_root_allowed(HOME)))

    if a.cmd == "tidy":
        path = os.path.abspath(a.path or os.getcwd())
        if os.path.realpath(path) in (os.path.realpath(HOME), "/"):
            sys.exit("tidy needs a project folder, not your home folder")
        if not _inside_roots(path, cfg_roots):
            print(outside, file=sys.stderr)
            return 2
        allowed = [os.path.realpath(path)]
        items, _, _, seqs, blocked = collect([path], {1, 3}, 0, include_codex=False)
        listed = {i.path for i in items}
        for d, f, s, _ in seqs:  # in tidy, the latest version's frames are offered too
            if d not in listed and not tracked(d) and not SEQ_PROTECTED_PARTS & set(d.split(os.sep)):
                items.append(Item(3, "frames-latest", d, f"{len(f)} frames (latest version)", size=s, files=f,
                                  disposition="trash", force_trash=True, targets=_frame_targets(d, f),
                                  precheck=lambda d=d, f=f: _frames_unchanged(d, f, 0)))
        items = [i for i in items if not i.disposition or safe_target(i, keep, allowed)[0]]
        size_items(items)
        prev = _last_tidy_manifest() if a.apply and (a.only or a.skip) else ([], None)
        report(items, [], 0, blocked, sys.stdout, scope=f"tidy {short(path)}", scope_path=os.path.realpath(path))
        if a.apply and not present:
            print("Afterparty isn't set up yet, so nothing was deleted. Install it (see INSTALL.md).", file=sys.stderr)
            return 2
        if a.apply and (a.only or a.skip):
            try:
                items = _select(items, path, a.only, a.skip, prev)
            except SelectError as e:
                print(e, file=sys.stderr)
                return 2
        if a.apply and items:
            prep = prepare(items, keep, allowed)  # what you approve is exactly what runs
            if prep.has_work() and (a.yes or confirm(f"Clean this folder? {plan_text(prep)}.")):
                print(_summary(run_prepared(prep, "tidy")))
            elif not prep.has_work():
                _log_only(prep, "tidy")
                print(f"Nothing to clean here: {_why_nothing(prep)}.")
        return

    if a.auto:
        tiers = {1, 2, 3, 4}
    elif a.tier:
        tiers = {int(t) for t in a.tier.split(",")}
    else:
        tiers = {1, 2, 3} if a.apply else {1, 2, 3, 4}
    items, needs, kept_latest, _, blocked = collect(roots, tiers, days, include_codex=not a.root,
                                                    scratch_days=cfg["scratch_days"])
    if 1 in tiers and not a.root:
        items += tier1_caches()
        from cleanup.engine.leftovers import user_cache_dir, user_temp_dir, user_x_dir
        found, extra = tier1_leftovers(user_x_dir(), user_temp_dir(), user_cache_dir())
        allowed = sorted(set(allowed) | set(extra))
        items += found
    if 4 in tiers and not a.root:
        items += tier4()
    items = [i for i in items if not i.disposition or safe_target(i, keep, allowed)[0]]
    size_items(items + needs)
    if a.root:
        scope = "roots " + ", ".join(short(r) for r in roots)
    elif a.tier and not a.auto:
        scope = f"tiers {a.tier}"
    else:
        scope = "global"

    if a.auto:
        freed = apply([i for i in items if i.tier in (1, 3)], "auto", keep, allowed)
        rest = [i for i in items if i.tier in (2, 4)]
        report(rest, needs, kept_latest, blocked, open(os.devnull, "w"), scope)
        waiting = sum(i.size for i in rest if i.disposition)
        warn = (f" · {len(blocked)} folders unreadable" if blocked else "") + (f" · {len(FAILED)} FAILED (see log)" if FAILED else "")
        notify("Afterparty", f"Freed {gb(freed).strip()} · {gb(waiting).strip()} more waiting for your OK{warn} — run /afterparty")
        print("auto: " + _summary(freed) + f"; waiting {gb(waiting).strip()}; unreadable {len(blocked)}")
        return

    report(items, needs, kept_latest, blocked, sys.stdout, scope)
    if not a.apply:  # a report never deletes anything, whatever the tiers (tier 4 asks only under --apply)
        found = sum(i.size for i in items if i.disposition)
        if a.notify and found:
            notify("Afterparty", f"Found {gb(found).strip()} to clean. Run /afterparty when you're ready.")
        print("Dry run — nothing deleted. Run `afterparty clean` to clean, or ask Claude: /afterparty.")
        return
    if 4 in tiers:
        if not present:
            print("Afterparty isn't set up yet, so nothing was deleted. Install it (see INSTALL.md) or create "
                  f"{config.config_path()}.", file=sys.stderr)
            return 2
        if sys.stdin.isatty():
            ask_tier4(items, keep, allowed)
        items = [x for x in items if x.tier != 4]
    todo = [i for i in items if i.disposition]
    if not todo:
        return
    prep = prepare(todo, keep, allowed)  # built before asking: what you approve is exactly what runs
    if not prep.has_work():
        _log_only(prep, "apply")
        print(f"Nothing to clean: {_why_nothing(prep)}.")
        return
    if a.yes or confirm(f"Clean {prep.totals['targets'] + prep.totals['commands']} items? {plan_text(prep)}. "
                        "Idle worktrees are removed only when every commit is on the remote."):
        print(_summary(run_prepared(prep, "apply")))

