"""What the installer owns: an ownership manifest with an intent receipt written before every change and a
completion receipt after it, checksums, the legal place and shape of every kind of item, atomic creation of the
state folder, the install lock, and checked no-clobber put/take through unique staging folders."""
import fcntl
import json
import os
import shlex
import shutil
from contextlib import contextmanager

from cleanup import config, fsops
from cleanup.fsops import file_sum, link_sum, sum_of, text_sum, tree_sum  # noqa: F401  (re-exported)
from cleanup.installer import edits, jobs, settings_hook

VERSION = 2
MANIFEST = edits.RECORD_NAME  # "install-manifest.json"
LINKS = ("afterparty", "afterparty-session-end")
SKILLS = ("tidy", "afterparty")
KINDS = {"dir": ("sum",), "link": ("sum",), "config": (), "skill": ("sum",), "hook": ("sum", "group"),
         "block": ("sum", "pad"), "job": ("sum", "label"), "app": ("sum",)}
STATES = ("pending", "done")


class ManifestError(ValueError):
    pass


class Changed(RuntimeError):
    """A target isn't what the plan (or the install record) says; nothing was replaced or removed."""


class Busy(RuntimeError):
    pass


def home():
    return os.path.expanduser("~")


def app_dest():
    return os.environ.get("AFTERPARTY_APP_DEST") or os.path.join(home(), "Applications")


def manifest_path():
    return os.path.join(config.state_dir(), MANIFEST)


def hook_command():
    return shlex.quote(os.path.join(home(), ".local", "bin", "afterparty-session-end"))


# ---- where and what each kind of item may be
def _inside(path, root):
    return path.startswith(root.rstrip(os.sep) + os.sep)


def legal(kind, path):
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        return False
    h = home()
    if kind == "dir":
        own = (config.config_dir(), config.state_dir())
        return path not in ("/", h) and (_inside(path, h) or any(path == r or _inside(r, path) for r in own))
    if kind == "link":
        return path in [os.path.join(h, ".local", "bin", n) for n in LINKS]
    if kind == "config":
        return path == config.config_path()
    if kind == "skill":
        return path in [os.path.join(h, b, "skills", n) for b in (".claude", ".codex") for n in SKILLS]
    if kind == "hook":
        return path == os.path.join(h, ".claude", "settings.json")
    if kind == "block":
        return path in (os.path.join(h, ".claude", "CLAUDE.md"), os.path.join(h, ".codex", "AGENTS.md"))
    if kind == "job":
        return path in [os.path.join(h, "Library", "LaunchAgents", jobs.label(n) + ".plist") for n in jobs.SCHEDULED]
    if kind == "app":
        return path == os.path.join(app_dest(), "Afterparty.app")
    return False


def _is_sum(v):
    return isinstance(v, str) and (v == "dir" or v.startswith(("sha256:", "tree:", "link:")))


CHECKS = {"sum": _is_sum, "prior": lambda v: v is None or _is_sum(v), "created": lambda v: isinstance(v, bool),
          "adopted": lambda v: isinstance(v, bool), "pad": lambda v: v in ("", "\n", "\n\n"),
          "label": lambda v: isinstance(v, str), "group": lambda v: isinstance(v, dict)}


def valid_item(i):
    """A receipt is acted on only if everything in it is something this installer writes, in the shape it writes it."""
    if not isinstance(i, dict) or i.get("kind") not in KINDS or not legal(i["kind"], i.get("path")):
        return False
    kind, path = i["kind"], i["path"]
    if i.get("state") not in STATES or any(k not in i for k in KINDS[kind]):
        return False
    if any(k in i and not check(i[k]) for k, check in CHECKS.items()):
        return False
    if kind == "dir" and i["sum"] != "dir":
        return False
    if kind == "link" and not (i["sum"].startswith("link:/") and i["sum"].endswith("/bin/" + os.path.basename(path))):
        return False
    if kind == "job" and i["label"] + ".plist" != os.path.basename(path):
        return False
    if kind == "hook" and i["group"] != settings_hook.entry(hook_command()):
        return False
    if kind in ("skill", "app") and not i["sum"].startswith("tree:"):
        return False
    return True


class Manifest:
    def __init__(self, path=None):
        self.path = path or manifest_path()
        self.data = {"version": VERSION, "items": []}
        self.raw = None  # the bytes last read or written: every save is a compare-and-swap against them (R14)
        try:
            with open(self.path, "rb") as f:
                self.raw = f.read()
            raw = json.loads(self.raw.decode("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            raise ManifestError("the install record %s can't be read (%s); nothing was changed" % (self.path, e))
        if not (isinstance(raw, dict) and raw.get("version") == VERSION and isinstance(raw.get("items"), list)):
            raise ManifestError("the install record %s isn't in a format this version understands" % self.path)
        bad = [i for i in raw["items"] if not valid_item(i)]
        if bad:
            raise ManifestError("the install record %s names something Afterparty never installs (%s); "
                                "nothing was changed" % (self.path, json.dumps(bad[0])[:200]))
        self.data = raw

    def items(self):
        return list(self.data["items"])

    def get(self, kind, path):
        for item in self.data["items"]:
            if item["kind"] == kind and item["path"] == path:
                return item
        return None

    def _set(self, kind, path, state, fields):
        item = self.get(kind, path)
        if item is None:
            item = {"kind": kind, "path": path}
            self.data["items"].append(item)
        item.update(fields)
        item["state"] = state
        edits.checkpoint("before receipt %s %s" % (state, kind))
        self.save()

    def intend(self, kind, path, **fields):
        self._set(kind, path, "pending", fields)

    def done(self, kind, path, **fields):
        self._set(kind, path, "done", fields)

    def forget(self, kind, path):
        self.data["items"] = [i for i in self.data["items"] if not (i["kind"] == kind and i["path"] == path)]
        edits.checkpoint("before receipt forget %s" % kind)
        self.save()

    def text(self):
        return json.dumps(self.data, indent=1, sort_keys=True) + "\n"

    def save(self):
        text = self.text()
        edits.write_atomic(self.path, text, self.raw)  # fsops.Conflict if it isn't what we last read or wrote
        self.raw = text.encode("utf-8")


# ---- the state folder, created together with its first receipt
def missing_chain(path):
    """Folders on the way to `path` that don't exist yet (outermost first), and the nearest one that exists."""
    chain, p = [], os.path.abspath(path)
    while not os.path.lexists(p) and os.path.dirname(p) != p:
        chain.append(p)
        p = os.path.dirname(p)
    return list(reversed(chain)), p


def bootstrap(state=None):
    """Create the state folder and its missing parents with one exclusive rename, the install record already
    inside, so a crash can never leave folders we made without a record of them. Returns the folders created."""
    state = state or config.state_dir()
    if os.path.isdir(state):
        return []
    chain, base = missing_chain(state)
    if not os.path.isdir(base):
        raise OSError("%s is in the way (not a folder)" % base)
    edits.sweep(base)
    rel = os.path.relpath(state, base)
    top = rel.split(os.sep)[0]
    stage = edits.make_stage(base, "bootstrap", rel=rel, manifest=MANIFEST)
    nonce = os.path.basename(stage)[len(edits.STAGE_PREFIX):]
    for d in chain:
        os.mkdir(os.path.join(stage, os.path.relpath(d, base)), 0o700 if d == state else 0o755)
    record = {"version": VERSION, "stage": nonce,
              "items": [{"kind": "dir", "path": d, "sum": "dir", "state": "done"} for d in chain]}
    text = json.dumps(record, indent=1, sort_keys=True) + "\n"
    edits.update_mark(stage, record_sum=fsops.text_sum(text))  # before the record exists (recovery checks it)
    edits.write_atomic(os.path.join(stage, rel, MANIFEST), text, None)
    edits.checkpoint("bootstrap staged")
    if fsops.rename_exclusive(os.path.join(stage, top), os.path.join(base, top)) is not None:
        raise Changed("%s appeared while installing; run the installer again" % os.path.join(base, top))
    edits.checkpoint("bootstrap moved")
    shutil.rmtree(stage)  # our marker is all that's left in it
    return chain


@contextmanager
def lock():
    """Only one install, update or uninstall at a time, for the whole transaction. The lock is an exclusive flock on
    the home folder itself: no lock file to create, leave behind or purge. A child started by `afterparty update`
    inherits the parent's lock through AFTERPARTY_INSTALL_LOCK_FD."""
    inherited = os.environ.get("AFTERPARTY_INSTALL_LOCK_FD", "")
    if inherited.isdigit():
        try:
            a, b = os.fstat(int(inherited)), os.stat(home())
            ours = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
        except OSError:
            ours = False
        if ours:
            yield int(inherited)
            return
    fd = os.open(home(), os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy("another Afterparty install, update or uninstall is running; try again when it has finished")
        yield fd
    finally:
        os.close(fd)


# ---- checked, no-clobber replace and removal
@contextmanager
def _put_back_on_error(staged, orig, stage):
    """A check of a staged copy that raises (an unreadable file, say) counts as a failed check: the copy goes back
    exactly where it was (exclusively: never over something new), the stage is cleared, and the error is raised.
    If something new took its place meanwhile, Changed names where the copy is kept (the stage stays)."""
    try:
        yield
    except BaseException:
        if fsops.rename_exclusive(staged, orig) is not None:
            raise Changed("%s couldn't be checked and something new is in its place; the previous copy is kept at %s"
                          % (orig, staged))
        shutil.rmtree(stage, ignore_errors=True)  # our marker, and our own new copy if any
        raise


def put(dst, build, prior, before_place=None):
    """Place a fresh copy at `dst`. `build(path)` creates it inside a new staging folder. `prior` is the checksum
    `dst` had when the plan was made (None: it must not exist). The old copy is moved aside exclusively and checked
    there (put back if it changed); the new copy is placed with an exclusive rename, so anything that appears at
    `dst` meanwhile wins and Changed is raised. Nothing is ever overwritten. `before_place` is a test hook.
    Returns the checksum of the copy placed, taken from the staged copy before placement (G0 M2): a receipt is
    never a read of `dst` afterwards, which could adopt files someone adds there meanwhile."""
    stage = edits.make_stage(os.path.dirname(dst), "put", orig=dst)
    new, old = os.path.join(stage, "new"), os.path.join(stage, "old")
    try:
        build(new)
        placed = sum_of(new)  # the receipt for what we place: our staged copy, before it is placed (G0 M2)
        edits.update_mark(stage, new_sum=placed)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)  # our fresh stage: only our marker and a partial new copy
        raise
    edits.checkpoint("staged " + os.path.basename(dst))
    if prior is not None:
        if fsops.rename_exclusive(dst, old) is not None:
            shutil.rmtree(stage)  # only our new copy is in it
            raise Changed("%s disappeared or couldn't be moved after the plan" % dst)
        edits.checkpoint("moved aside " + os.path.basename(dst))
        with _put_back_on_error(old, dst, stage):
            same = sum_of(old) == prior
        if not same:
            if fsops.rename_exclusive(old, dst) is not None:
                raise Changed("%s changed after the plan and something new is in its place; the previous copy is "
                              "kept at %s" % (dst, old))
            shutil.rmtree(stage)
            raise Changed("%s changed after the plan; it was left as it is" % dst)
        edits.update_mark(stage, verified=True, old_sum=prior)
    if before_place is not None:
        before_place()
    if fsops.rename_exclusive(new, dst) is not None:
        shutil.rmtree(stage)  # our new copy, and our own verified previous copy if there was one
        raise Changed("%s appeared after the plan; it was left alone" % dst)
    edits.checkpoint("placed " + os.path.basename(dst))
    shutil.rmtree(stage)
    return placed


def take_if(path, ok, trash=None):
    """Ruling R5: the one way the installer and uninstaller delete anything. A path is never checked and then
    unlinked: `path` is first moved (exclusively) into a fresh staging folder, `ok(staged_path)` judges the staged
    copy, and only that copy is deleted, so whatever appears at `path` meanwhile is never touched. Returns "gone"
    (deleted, or already gone). When the staged copy fails the check: with `trash` (a Trash mover such as
    `bridge.default_trash()`), the staged copy itself goes to the Trash under its own name and the landing path is
    returned; otherwise, or if the Trash refuses it, it goes back exactly where it was (exclusively: never over
    something new) and "restored" is returned. Raises Changed when it can't go back because something new is there
    (the copy then stays in the staging folder, which the message names). A crash leaves a staging folder that
    `edits.sweep` puts back (unchecked copy) or clears (verified copy); one renamed for the Trash is left for a person."""
    if not os.path.lexists(path):
        return "gone"
    stage = edits.make_stage(os.path.dirname(path), "take", orig=path)
    item = os.path.join(stage, "item")
    if fsops.rename_exclusive(path, item) is not None:
        shutil.rmtree(stage)
        if os.path.lexists(path):
            raise Changed("%s couldn't be moved aside to check it; it was left alone" % path)
        return "gone"
    edits.checkpoint("moved for removal " + os.path.basename(path))
    with _put_back_on_error(item, path, stage):
        passed = ok(item)
        if passed:
            edits.update_mark(stage, verified=True, item_sum=sum_of(item))
    if passed:
        shutil.rmtree(stage)  # the checked copy and our marker
        return "gone"
    if trash is not None:
        named = os.path.join(stage, os.path.basename(path))
        if fsops.rename_exclusive(item, named) is None:
            landing = trash([named]).get(named, "ERR:no answer")
            if not landing.startswith("ERR:"):
                shutil.rmtree(stage)  # only our marker is left
                return landing
            item = named
    if fsops.rename_exclusive(item, path) is not None:
        raise Changed("%s was recreated meanwhile; the previous copy is kept at %s" % (path, item))
    shutil.rmtree(stage)
    return "restored"


def take(path, expected):
    """Remove `path` only if its checksum is still `expected` (checked on the staged copy, take_if). True when
    removed (or already gone); False when it changed (then it is put back exactly where it was)."""
    return take_if(path, lambda staged: sum_of(staged) == expected) == "gone"
