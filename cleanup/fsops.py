"""Small file-system primitives shared by the cleanup pipeline and the installer. Stdlib only.

rename_exclusive is the only way Afterparty moves something into a place that might be taken: macOS's
renamex_np(RENAME_EXCL) checks and moves in one atomic step. There is deliberately no fallback: emulating it
(check, then rename) would overwrite anything that arrives in between, so without the native call nothing moves."""
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager

RENAME_EXCL = 0x00000004  # renamex_np flag: fail if the destination exists (atomic, no race); macOS 10.12+
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
except OSError:  # no C library to load: _renamex is then None and every exclusive rename fails closed
    _libc = None
_renamex = getattr(_libc, "renamex_np", None)


def rename_exclusive(src, dst):
    """Atomic move that refuses to replace anything. None on success, "conflict" if `dst` exists, or "failed: ..."
    (including when the native call is missing or the move would cross volumes); on failure nothing moved."""
    if _renamex is None:
        return "failed: renamex_np isn't available on this system (macOS 10.12 or newer is needed), so nothing was moved"
    ctypes.set_errno(0)
    if _renamex(os.fsencode(src), os.fsencode(dst), RENAME_EXCL) == 0:
        return None
    e = ctypes.get_errno()
    if e == errno.EEXIST:
        return "conflict"
    if e == errno.EXDEV:
        return "failed: the destination is on another volume; Afterparty only moves within one volume"
    return "failed: " + os.strerror(e)


# ---- crash checkpoints (test seam)
_COUNT = [0]


def checkpoint(label):
    """Test seam. AFTERPARTY_TEST_CHECKPOINTS=<file> logs every checkpoint; AFTERPARTY_TEST_CRASH_AT=<n> ends the
    process at the n-th one with os._exit (no cleanup, like a power cut)."""
    _COUNT[0] += 1
    log = os.environ.get("AFTERPARTY_TEST_CHECKPOINTS")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write("%d %s\n" % (_COUNT[0], label))
    at = os.environ.get("AFTERPARTY_TEST_CRASH_AT", "")
    if at.isdigit() and int(at) == _COUNT[0]:
        os._exit(99)


# ---- write receipts: the only proof --purge accepts that Afterparty wrote a file
RECEIPTS = ".afterparty-receipts.json"


SELF = "__self__"  # the receipts file's checksum of its own entries (ruling R6)


def _entries(data):
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and k != SELF}


def _self_sum(entries):
    return text_sum(json.dumps(entries, sort_keys=True))


def receipts(folder):
    """{name: checksum when Afterparty last wrote it} for one folder ({} when there is none or it's unreadable). A
    sub-folder Afterparty made is listed as "<name>/": "dir". The file's own checksum (SELF) is not an entry."""
    try:
        with open(os.path.join(folder, RECEIPTS), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return _entries(data) if isinstance(data, dict) else {}


def _certified_file(path):
    """The entries of the receipts file at `path` if it certifies itself, else None: one read, and the entries come
    from exactly the bytes that were certified (G0 M3)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return _certified(raw)


def receipts_file_ok(path):
    """Ruling R6: a receipts file gets no special treatment. It certifies itself: true only while it holds exactly
    what Afterparty last wrote (its SELF checksum matches its entries and nothing else is in it). An edited one is
    not Afterparty's to delete."""
    return _certified_file(path) is not None


def certified_receipts(folder):
    """Ruling P4: the folder's receipts, only while its receipts file certifies itself ({} otherwise). Certifying
    and parsing use the same single read, so a replacement landing between two reads can never vouch for anything."""
    return _certified_file(os.path.join(folder, RECEIPTS)) or {}


def note_written(path, data):
    """Record that Afterparty wrote `data` (the bytes it actually wrote, from memory) to `path`. Ruling R17: the
    checksum is never taken from a later read of the path, so a replacement that lands between the write and this
    receipt is never certified as Afterparty's (its checksum won't match, so --purge won't delete it). Callers hold
    the lock that serializes the writers of that folder; if two writers ever raced anyway, a receipt could be lost,
    which only means --purge keeps that file (the safe direction)."""
    _note(os.path.dirname(path), os.path.basename(path), _sha(data))


def note_made(folder):
    """Record that Afterparty made the (new, empty) folder `folder`; --purge may then remove it once it is empty."""
    folder = folder.rstrip(os.sep)
    _note(os.path.dirname(folder), os.path.basename(folder) + "/", "dir")


# ---- the one writer for files Afterparty owns (ruling R14)
RENAME_SWAP = 0x00000002  # renamex_np flag: exchange the two names atomically


class Conflict(RuntimeError):
    """An Afterparty-owned file isn't what Afterparty last read or wrote; nothing of it was replaced. `kept` names
    where a displaced copy was kept, if the rare double race below happened."""

    def __init__(self, msg, kept=None):
        RuntimeError.__init__(self, msg)
        self.kept = kept


class ReceiptError(Conflict):
    """A receipts file is in the way that Afterparty can't vouch for (edited, unreadable or someone else's)."""


def _swap(a, b):
    if _renamex is None:
        return "renamex_np isn't available on this system (macOS 10.12 or newer is needed)"
    ctypes.set_errno(0)
    if _renamex(os.fsencode(a), os.fsencode(b), RENAME_SWAP) == 0:
        return None
    return os.strerror(ctypes.get_errno())


def _bytes(path):
    """The bytes of a regular file at `path`, None if nothing is there; a link or folder is never "the same"."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        return b"\0not a regular file"
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _preserve(p, path):
    """Ruling R16: unexpected bytes are never deleted. Move them exclusively to a fresh name next to `path` and
    return that name (or `p` itself if even that move fails)."""
    for _ in range(5):
        aside = "%s.afterparty-kept-%d" % (path, time.time_ns())
        if rename_exclusive(p, aside) is None:
            return aside
    return p


def swap_owned(path, new_file, expected):
    """Put `new_file` (complete and fsynced, in the same folder) at `path`, only if `path` holds exactly what the
    caller last read or wrote. `expected` None: nothing may be there (exclusive create: a hard link, then the
    temporary name goes). `expected` bytes: the two names are exchanged atomically (renamex_np RENAME_SWAP), so
    `path` is never missing; the displaced copy is then compared byte for byte with `expected`: equal, it is ours
    and is deleted; different, the names are exchanged back and Conflict is raised. Foreign bytes are never deleted:
    the rollback compares with our own new bytes held in memory (R16), and any copy that isn't exactly ours is
    moved aside exclusively and kept (Conflict.kept names it). Honest
    limit: in that conflict case a reader can see our new content at `path` for an instant before the exchange
    back. On success, and on any Conflict without `kept`, `new_file` no longer exists."""
    if os.path.islink(path):
        os.unlink(new_file)
        raise Conflict("%s is a symbolic link; Afterparty doesn't write through links it didn't make" % path)
    if expected is None:
        try:
            os.link(new_file, path)
        except FileExistsError:
            os.unlink(new_file)
            raise Conflict("%s already exists and isn't the file Afterparty expected; nothing was replaced" % path)
        os.unlink(new_file)
        return
    with open(new_file, "rb") as f:
        ours = f.read()  # our own new bytes, in memory before anything moves (R16)
    err = _swap(new_file, path)
    if err is not None:
        os.unlink(new_file)
        raise Conflict("%s couldn't be replaced (%s); nothing was changed" % (path, err))
    checkpoint("exchanged " + os.path.basename(path))  # the exchange/verification window (G0 S4)
    # From here on `new_file` holds the displaced copy, which may be someone else's: it is deleted only after a
    # successful read proved it exactly ours; any failure to read it preserves it (G0 M1), never deletes it.
    try:
        displaced = _bytes(new_file)
    except OSError as e:
        kept = _preserve(new_file, path)
        raise Conflict("%s was replaced, but the copy it replaced couldn't be read back (%s); nothing was deleted, "
                       "and that copy is kept at %s" % (path, e.strerror or e, kept), kept=kept)
    if displaced == expected:
        os.unlink(new_file)  # what we last read or wrote, now under our own temporary name
        return
    # Ruling R16: roll back by comparing with OUR OWN new bytes (held in memory since before the exchange), never
    # with a fresh read of the live path; whatever isn't exactly ours is preserved, never deleted.
    try:
        back = _swap(new_file, path) is None and _bytes(new_file) == ours
    except OSError:
        back = False
    if back:
        os.unlink(new_file)  # exactly our own new content, back under its temporary name
        raise Conflict("%s changed since Afterparty read it, so it was left as it is" % path)
    kept = _preserve(new_file, path)
    raise Conflict("%s changed while Afterparty replaced it; nothing was deleted, and the copy that didn't match "
                   "is kept at %s" % (path, kept), kept=kept)


def write_owned(path, data, expected, mode=None):
    """Write `data` (bytes) to the Afterparty-owned file `path` through swap_owned: exclusive create when
    `expected` is None, else a compare-and-swap against `expected` (what the caller last read or wrote)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".afterparty-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600 if mode is None else mode)
    except BaseException:
        os.unlink(tmp)
        raise
    swap_owned(path, tmp, expected)


def replace_owned(path, data, mode=None, attempts=3):
    """For Afterparty's own state files with no record of their last content (reports, caches, server state):
    compare-and-swap against what is there right now, so a concurrent change is never overwritten."""
    for _ in range(attempts):
        try:
            write_owned(path, data, _bytes(path), mode)
            return
        except Conflict as e:
            if e.kept:
                raise
    raise Conflict("%s kept changing while Afterparty wrote it; nothing was replaced" % path)


def write_receipted(path, data, mode=None, known=None):
    """For an Afterparty file whose last content only Afterparty's own records prove (G0 M4): nothing there, an
    exclusive create; exactly `data` already there, nothing to do; content whose checksum matches this folder's
    self-certifying receipt for it, or that is exactly `known` (bytes another Afterparty record says it wrote there,
    for when that receipt couldn't be written), a compare-and-swap against those bytes. Anything else is not
    Afterparty's: Conflict, and nothing is replaced. True when written (the caller then receipts `data`)."""
    old = _bytes(path)
    if old == data:
        return False
    if old is not None and old != known and \
            certified_receipts(os.path.dirname(path)).get(os.path.basename(path)) != _sha(old):
        raise Conflict("%s exists and no receipt shows Afterparty wrote it, so it wasn't replaced; move it "
                       "elsewhere and try again" % path)
    write_owned(path, data, old, mode)
    return True


def _certified(raw):
    """The entries of receipts-file bytes that still certify themselves (SELF matches), else None."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) - set(_entries(data)) != {SELF}:
        return None
    return _entries(data) if data[SELF] == _self_sum(_entries(data)) else None


def _note(folder, name, value):
    """Ruling R11: a receipt write never overwrites content Afterparty doesn't own. Through write_owned: a new
    receipts file is an exclusive create; an existing one must certify itself (exactly what Afterparty last wrote)
    and is replaced only by a compare-and-swap against the bytes read. An unreadable, edited or foreign file raises
    ReceiptError and nothing is replaced (it is never treated as empty)."""
    rf = os.path.join(folder, RECEIPTS)
    for _ in range(5):
        try:
            old = _bytes(rf)
        except OSError as e:
            raise ReceiptError("%s can't be read (%s); nothing was changed" % (rf, e.strerror or e))
        data = {} if old is None else _certified(old)
        if data is None:
            raise ReceiptError("%s isn't a receipts file as Afterparty wrote it (edited, unreadable or someone "
                               "else's), so Afterparty didn't replace it; move it elsewhere and try again" % rf)
        data[name] = value
        out = dict(data)
        out[SELF] = _self_sum(data)
        try:
            write_owned(rf, json.dumps(out, indent=1, sort_keys=True).encode("utf-8"), old)
            return
        except Conflict as e:
            if e.kept:
                raise ReceiptError(str(e), kept=e.kept)
    raise ReceiptError("%s kept changing while Afterparty updated it; nothing was replaced" % rf)


# ---- locks
class Busy(RuntimeError):
    pass


_unreceipted = set()  # folders where a receipt couldn't be written (reported once each)


def note_written_or_warn(path, data):
    """note_written for a write that has already happened (rulings P9 and its follow-up): a missing receipt must
    never turn a successful lock or settings write into a failure. If the receipt can't be written (a receipts
    file Afterparty can't vouch for, an unwritable folder), go on without it: that file then only gets the Trash
    treatment at --purge (the safe direction). Reported once per folder on stderr."""
    try:
        note_written(path, data)
    except (ReceiptError, OSError) as e:
        folder = os.path.dirname(path)
        if folder not in _unreceipted:
            _unreceipted.add(folder)
            print("Afterparty: no write receipt for %s (%s); --purge will move it to the Trash instead of deleting "
                  "it" % (path, e), file=sys.stderr)


def note_made_or_warn(folder):
    """note_made for a folder that has already been made (the same rule as note_written_or_warn): without the
    receipt, that folder only gets the Trash treatment at --purge (the safe direction). Reported once per parent."""
    try:
        note_made(folder)
    except (ReceiptError, OSError) as e:
        parent = os.path.dirname(folder.rstrip(os.sep))
        if parent not in _unreceipted:
            _unreceipted.add(parent)
            print("Afterparty: no receipt for the folder %s (%s); --purge will move it to the Trash instead of removing it" % (folder, e),
                  file=sys.stderr)


@contextmanager
def held(path, blocking=True, busy="busy", create_dirs=True):
    """Exclusive lock on the file at `path` (created if missing; its folder too, unless create_dirs is False). A lock
    file this call creates is receipted. After locking, the file must still be the one at `path`, else we let go and
    lock the one that is there now. Lock files are never deleted in normal operation (Task 10's --purge removes them
    only as its last step, after proving nobody holds them)."""
    while True:
        if create_dirs:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                fd = os.open(path, os.O_WRONLY)
            except FileNotFoundError:
                continue  # removed between the two opens: start again
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise Busy(busy)
            raise
        try:
            a, b = os.fstat(fd), os.stat(path)
            same = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
        except FileNotFoundError:
            same = False
        if same:
            break
        os.close(fd)
    try:
        if created:
            note_written_or_warn(path, b"")  # we created it empty (O_EXCL) a moment ago
        yield fd
    finally:
        os.close(fd)


# ---- checksums
def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def data_sum(data):
    return _sha(data)


def text_sum(text):
    return _sha(text.encode("utf-8"))


def file_sum(path):
    with open(path, "rb") as f:
        return _sha(f.read())


def link_sum(target):
    return "link:" + target


def tree_sum(root):
    """Checksum of a folder: every name, kind, execute bit, link target and file content inside it."""
    entries = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in dirs + files:
            p = os.path.join(d, name)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                entries.append([rel, "link", os.readlink(p)])
            elif os.path.isdir(p):
                entries.append([rel, "dir", ""])
            else:
                entries.append([rel, "file" + ("+x" if os.stat(p).st_mode & 0o111 else ""), file_sum(p)])
    return "tree:" + hashlib.sha256(json.dumps(sorted(entries)).encode("utf-8")).hexdigest()


def sum_of(path):
    if os.path.islink(path):
        return link_sum(os.readlink(path))
    if os.path.isdir(path):
        return tree_sum(path)
    if os.path.isfile(path):
        return file_sum(path)
    return None
