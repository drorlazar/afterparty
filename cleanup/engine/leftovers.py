"""App update leftovers outside home: Chromium-style browsers' code-sign clones.

Chrome (and other Chromium browsers) copy their app bundle to
/private/var/folders/<xx>/<hash>/X/<bundle id>.code_sign_clone/code_sign_clone.<random>/ when they launch, so an
update on disk can't break the running copy's code signature. The copy is meant to be deleted on exit; crashes and
relaunch loops leave them behind by the hundred. They're APFS clones, so `du` wildly overstates them (each one shows
the whole app's size), but the stale ones are never reused.
"""
import os
import re
import subprocess
import time

CLONE_PATH = re.compile(r"^(?:/private)?/var/folders/[^/]+/[^/]+/X/[^/]+\.code_sign_clone/code_sign_clone\.[^/]+$")
CLONE_GROUP = re.compile(r"^(?:/private)?/var/folders/[^/]+/[^/]+/X/[^/]+\.code_sign_clone$")
FRESH_SECONDS = 15 * 60  # a clone this new may belong to a browser that is launching right now


def _seam_x():
    """DISKCLEAN_CODE_SIGN_DIR (tests): the X folder to trust, like DISKCLEAN_TEMP_DIR for …/T. Never a subprocess:
    classify runs this for thousands of map tiles."""
    x = os.environ.get("DISKCLEAN_CODE_SIGN_DIR")
    return os.path.realpath(x) if x else None


def is_code_sign_clone(path):
    if CLONE_PATH.match(path):
        return True
    x = _seam_x()
    group = os.path.dirname(path)
    return bool(x) and os.path.realpath(os.path.dirname(group)) == x and group.endswith(".code_sign_clone") \
        and os.path.basename(path).startswith("code_sign_clone.")


def is_clone_group(path):
    """The per-browser folder holding the clones (e.g. X/com.google.Chrome.code_sign_clone)."""
    if CLONE_GROUP.match(path):
        return True
    x = _seam_x()
    return bool(x) and os.path.realpath(os.path.dirname(path)) == x and path.endswith(".code_sign_clone")


def group_busy(group, opened, now=None):
    """Any clone inside is busy, or the folder holds something that isn't a clone (then it's not ours to judge)."""
    try:
        names = os.listdir(group)
    except OSError:
        return True
    for n in names:
        c = os.path.join(group, n)
        if n == ".DS_Store":
            continue
        if not n.startswith("code_sign_clone.") or os.path.islink(c) or busy(c, opened, now):
            return True
    return False


def user_x_dir():
    """The per-user X folder (sibling of $TMPDIR's T). DISKCLEAN_CODE_SIGN_DIR overrides it (tests)."""
    if "DISKCLEAN_CODE_SIGN_DIR" in os.environ:
        return os.environ["DISKCLEAN_CODE_SIGN_DIR"] or None
    try:
        t = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    x = os.path.join(os.path.dirname(t.rstrip("/")), "X") if t else ""
    return os.path.realpath(x) if os.path.isdir(x) else None


_OPEN_CACHE = [0.0, None]


def open_paths(max_age=5):
    """Every path any process has open, or None if lsof failed (callers must then treat everything as in use).
    Reused for a few seconds so checking hundreds of clones in a row runs lsof once."""
    if _OPEN_CACHE[1] is not None and time.monotonic() - _OPEN_CACHE[0] < max_age:
        return _OPEN_CACHE[1]
    try:
        r = subprocess.run(["lsof", "-n", "-w", "-Fn"], capture_output=True, text=True, timeout=60, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:  # a failed lsof is never authoritative, even with partial output
        return None
    _OPEN_CACHE[:] = [time.monotonic(), [ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n/")]]
    return _OPEN_CACHE[1]


def in_use(clone, opened):
    if opened is None:
        return True
    real = os.path.realpath(clone)
    return any(p == clone or p.startswith(clone + "/") or p == real or p.startswith(real + "/") for p in opened)


START_MARGIN = 60  # a browser makes its launch copy right after it starts


def busy(clone, opened, now=None, starts=None):
    """In use by a process, brand new, unreadable — or made since its browser's current launch. A running browser opens
    its own copy only now and then, so "nothing has it open" alone doesn't prove a copy is stale."""
    try:
        st = os.lstat(clone)
    except OSError:
        return True
    born = getattr(st, "st_birthtime", st.st_mtime)
    if (time.time() if now is None else now) - max(st.st_mtime, born) < FRESH_SECONDS:
        return True
    starts = process_starts() if starts is None else starts
    if starts is None:
        return True
    group = os.path.basename(os.path.dirname(clone))
    started = starts.get(group[:-len(".code_sign_clone")].casefold()) if group.endswith(".code_sign_clone") else None
    if started is not None and born >= started - START_MARGIN:
        return True
    return in_use(clone, opened)


def stale_clones(x_dir, opened=None, now=None, starts=None):
    """Code-sign clones under x_dir that no process uses and that aren't brand new. Returns {clone folder: [clones]}."""
    if not x_dir or not os.path.isdir(x_dir):
        return {}
    opened = open_paths() if opened is None else opened
    now = time.time() if now is None else now
    out = {}
    for group in sorted(os.listdir(x_dir)):
        gp = os.path.join(x_dir, group)
        if not group.endswith(".code_sign_clone") or os.path.islink(gp) or not os.path.isdir(gp):
            continue
        for name in sorted(os.listdir(gp)):
            c = os.path.join(gp, name)
            if not name.startswith("code_sign_clone.") or os.path.islink(c) or not os.path.isdir(c):
                continue
            if not busy(c, opened, now, starts):
                out.setdefault(gp, []).append(c)
    return out


# ------------------------------------------------------------ old versions next to a newer one
VERSION = re.compile(r"^\d+(?:\.\d+)+$")
TEMP_DAYS = 3
_DARWIN_DIRS = {}


def _vkey(name):
    return tuple(int(x) for x in name.split("."))


def old_versions(parent, keep=()):
    """Version-named children of `parent` older than the newest one; anything in `keep` (real paths) stays."""
    try:
        names = [n for n in os.listdir(parent) if VERSION.match(n)]
    except OSError:
        return []
    if len(names) < 2:
        return []
    newest = max(names, key=_vkey)
    keep = {os.path.realpath(k) for k in keep}
    return [os.path.join(parent, n) for n in sorted(names, key=_vkey)
            if n != newest and os.path.realpath(os.path.join(parent, n)) not in keep
            and not os.path.islink(os.path.join(parent, n))]


def claude_version_dirs(home):
    lib = home + "/Library/Application Support/Claude"
    return [home + "/.local/share/claude/versions", lib + "/claude-code", lib + "/claude-code-vm"]


def claude_keep(home):
    """The Claude Code binary the `claude` command currently runs stays, whatever its version."""
    out = []
    for link in (home + "/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.path.lexists(link):
            out.append(os.path.realpath(link))
    return out


def is_old_claude_version(path, home):
    parent = os.path.dirname(path)
    return parent in claude_version_dirs(home) and path in old_versions(parent, claude_keep(home))


# ------------------------------------------------------------ stale per-user temp files
def _darwin_dir(var, env):
    """A per-user Darwin folder (getconf), real path. `env` overrides it (tests); empty = none."""
    if env in os.environ:
        return os.environ[env] or None
    if var not in _DARWIN_DIRS:
        try:
            t = subprocess.run(["getconf", var], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            t = ""
        _DARWIN_DIRS[var] = os.path.realpath(t) if t and os.path.isdir(t) else ""
    return _DARWIN_DIRS[var] or None


def user_temp_dir():
    """$TMPDIR's real folder (…/T)."""
    return _darwin_dir("DARWIN_USER_TEMP_DIR", "DISKCLEAN_TEMP_DIR")


def user_cache_dir():
    """The per-user system cache folder (…/C): apps' GPU shader and compiler caches, one folder per app id."""
    return _darwin_dir("DARWIN_USER_CACHE_DIR", "DISKCLEAN_CACHE_DIR")


def is_temp_entry(path):
    t = user_temp_dir()
    if not t or os.path.realpath(os.path.dirname(path)) != t:
        return False
    return not os.path.basename(path).lstrip(".").startswith("com.apple")


def newest_mtime(path, limit=20000):
    """Newest mtime in a tree (bounded); None if unreadable or too big to judge."""
    st = os.lstat(path)
    best, seen = st.st_mtime, 0
    if not os.path.isdir(path) or os.path.islink(path):
        return best
    for d, dirs, files in os.walk(path, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
        for n in dirs + files:
            seen += 1
            if seen > limit:
                return None
            best = max(best, os.lstat(os.path.join(d, n)).st_mtime)
    return best


def temp_stale(path, opened, now=None):
    """Yours, untouched for TEMP_DAYS and open in no process."""
    try:
        if os.lstat(path).st_uid != os.getuid():  # system services keep their own items here
            return False
        newest = newest_mtime(path)
    except OSError:
        return False
    if newest is None or (time.time() if now is None else now) - newest < TEMP_DAYS * 86400:
        return False
    return not in_use(path, opened)


def stale_temp(t_dir, opened=None, now=None):
    if not t_dir or not os.path.isdir(t_dir):
        return []
    opened = open_paths() if opened is None else opened
    out = []
    for n in sorted(os.listdir(t_dir)):
        p = os.path.join(t_dir, n)
        if not n.lstrip(".").startswith("com.apple") and not os.path.islink(p) and temp_stale(p, opened, now):
            out.append(p)
    return out


# ------------------------------------------------------------ apps' caches in the per-user system cache folder (…/C)
CACHE_IDLE_DAYS, BIG_CACHE, BIG_CACHE_IDLE_DAYS = 7, 2**30, 1
_CACHE_SUFFIX = re.compile(r"(?i)(\+.*| helper.*|\.helper)$")


def is_cache_entry(path):
    c = user_cache_dir()
    if not c or os.path.realpath(os.path.dirname(path)) != c:
        return False
    return not os.path.basename(path).casefold().startswith("com.apple")


def cache_owner(name):
    """App id a cache folder belongs to: 'com.google.Chrome.helper' → 'com.google.chrome'."""
    return _CACHE_SUFFIX.sub("", name).casefold()


def running_ids():
    """Lowercased bundle ids of every running process's enclosing apps (helpers included) plus executable names.
    App extensions (.appex: Quick Look thumbnailers, Finder sync) don't count — macOS keeps them alive while the app
    itself is closed. None if ps failed (callers must then treat every app as running)."""
    try:
        r = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return ids_of(r.stdout.splitlines())


_START_CACHE = [0.0, None]


def process_starts(max_age=5):
    """{lowercased bundle id: earliest start (epoch)} over running processes; None if ps failed."""
    if _START_CACHE[1] is not None and time.monotonic() - _START_CACHE[0] < max_age:
        return _START_CACHE[1]
    try:
        r = subprocess.run(["ps", "-axo", "lstart=,comm="], capture_output=True, text=True, timeout=30, errors="replace",
                           env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    out, plists = {}, {}
    for line in r.stdout.splitlines():
        try:  # lstart is fixed width: "Fri Oct  9 12:37:17 2026"
            t = time.mktime(time.strptime(line[:24].strip(), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        for bid in ids_of([line[24:]], plists):
            if "." in bid:  # bundle ids only, not bare executable names
                out[bid] = min(out.get(bid, t), t)
    _START_CACHE[:] = [time.monotonic(), out]
    return out


def ids_of(executables, plists=None):
    """Lowercased executable names and enclosing bundle ids for a list of executable paths (see running_ids).
    `plists`: optional {app path: bundle id} cache shared across calls."""
    import plistlib
    plists = {} if plists is None else plists
    ids, apps = set(), set()
    for exe in executables:
        exe = exe.strip()
        if ".appex/" in exe:
            continue
        ids.add(os.path.basename(exe).casefold())
        parts = exe.split("/")
        for i, part in enumerate(parts):
            if part.endswith((".app", ".framework", ".xpc")):
                apps.add("/".join(parts[:i + 1]))
    for app in apps:
        if app not in plists:
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                    bid = plistlib.load(f).get("CFBundleIdentifier")
            except Exception:  # unreadable or odd plist: the executable name above still counts
                bid = None
            plists[app] = bid.casefold() if isinstance(bid, str) else None
        if plists[app]:
            ids.add(plists[app])
    return ids


def owner_running(name, running):
    if running is None:
        return True
    owner = cache_owner(name)
    return owner in running or any(r.startswith(owner + ".") for r in running)


def cache_stale(path, opened, running, now=None):
    """Owning app not running, nothing open inside, and unused for CACHE_IDLE_DAYS (BIG_CACHE_IDLE_DAYS if over
    BIG_CACHE — shader caches grow without bound)."""
    if os.path.islink(path) or owner_running(os.path.basename(path), running):
        return False
    try:
        newest = newest_mtime(path, limit=200000)
    except OSError:
        return False
    if newest is None:
        return False
    idle = ((time.time() if now is None else now) - newest) / 86400
    if idle < BIG_CACHE_IDLE_DAYS:
        return False
    if idle < CACHE_IDLE_DAYS:
        from cleanup.engine.apfs import SharedCounter
        shared, size = SharedCounter(), 0
        for d, _, files in os.walk(path):
            for f in files:
                try:
                    size += shared.size(os.path.join(d, f), os.lstat(os.path.join(d, f)))
                except OSError:
                    pass
        if size < BIG_CACHE:
            return False
    return not in_use(path, opened)


def stale_caches(c_dir, opened=None, running=None, now=None):
    if not c_dir or not os.path.isdir(c_dir):
        return []
    opened = open_paths() if opened is None else opened
    running = running_ids() if running is None else running
    return [os.path.join(c_dir, n) for n in sorted(os.listdir(c_dir))
            if not n.casefold().startswith("com.apple") and os.path.isdir(os.path.join(c_dir, n))
            and cache_stale(os.path.join(c_dir, n), opened, running, now)]
