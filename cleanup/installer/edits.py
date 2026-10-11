"""Low-level file work for the installer: crash checkpoints (test seam), uniquely created staging folders whose
markers record what they may contain, and edits of a user's file that re-read it right before the rename."""
import json
import os
import shutil
import sys
import time
import uuid

from cleanup import fsops

STAGE_PREFIX = ".afterparty-stage-"
STAGE_MARK = ".afterparty-stage.json"
ALLOWED = {"put": ("new", "old"), "take": ("item",), "edit": ("new",)}
checkpoint = fsops.checkpoint  # crash checkpoints (test seam), shared with fsops' exchange window


class EditConflict(RuntimeError):
    pass


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def read_text(path):
    """The file's text, or None if it doesn't exist; ValueError if it isn't UTF-8 text."""
    data = read_bytes(path)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("%s isn't UTF-8 text" % path)


def write_atomic(path, text, expected, mode=None):
    """An Afterparty-owned text file, through the one owned-file writer (ruling R14): exclusive create when
    `expected` is None, else a compare-and-swap against `expected` (the bytes last read or written)."""
    fsops.write_owned(path, text.encode("utf-8"), expected, mode)


# ---- staging folders: a folder we just created, a marker naming its nonce, and a record of what is inside
def make_stage(parent, purpose, orig=None, **fields):
    nonce = uuid.uuid4().hex
    stage = os.path.join(parent, STAGE_PREFIX + nonce)
    os.mkdir(stage, 0o700)
    mark = dict(fields, nonce=nonce, purpose=purpose, orig=orig, verified=False)
    write_atomic(os.path.join(stage, STAGE_MARK), json.dumps(mark), None)
    return stage


def read_mark(stage):
    try:
        d = json.loads(read_text(os.path.join(stage, STAGE_MARK)) or "")
    except ValueError:
        return None
    if not isinstance(d, dict) or STAGE_PREFIX + str(d.get("nonce")) != os.path.basename(stage):
        return None
    return d


def update_mark(stage, **fields):
    raw = read_bytes(os.path.join(stage, STAGE_MARK))
    d = read_mark(stage)
    d.update(fields)
    write_atomic(os.path.join(stage, STAGE_MARK), json.dumps(d), raw)


def _own_marks(stage, d):
    """Copies of this stage's own marker that a crash inside its update left behind (fsops.write_owned's temporary
    file, G0 S4): regular files named .afterparty-*.tmp holding a marker with this stage's nonce and purpose."""
    out = set()
    for n in os.listdir(stage):
        p = os.path.join(stage, n)
        if not (n.startswith(".afterparty-") and n.endswith(".tmp")) or os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            m = json.loads(read_text(p) or "")
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("nonce") == d.get("nonce") and m.get("purpose") == d.get("purpose"):
            out.add(n)
    return out


def complete_walk(top):
    """os.walk(top) as a list, or None when any folder in it couldn't be listed: a walk that missed something never
    vouches for what it didn't see (G0 round 2, NEW2)."""
    failed = []
    out = list(os.walk(top, onerror=failed.append))
    return None if failed else out


RECORD_NAME = "install-manifest.json"  # owned.MANIFEST: the only file a bootstrap/teardown stage may hold


def _layout(stage, d):
    """(rel, manifest) from a bootstrap/teardown marker, only while both stay inside the stage (G0 round 3, P1-1):
    `rel` relative, normalized, with no empty, "." or ".." part, resolving inside the stage; `manifest` exactly the
    install record's name. Else None: the stage is preserved and nothing in or outside it is deleted."""
    rel, manifest = d.get("rel"), d.get("manifest")
    if not isinstance(rel, str) or manifest != RECORD_NAME or not rel or os.path.isabs(rel) or "\0" in rel:
        return None
    if os.path.normpath(rel) != rel or any(part in ("", ".", "..") for part in rel.split(os.sep)):
        return None
    real_stage = os.path.realpath(stage)
    if not os.path.realpath(os.path.join(stage, rel, manifest)).startswith(real_stage + os.sep):
        return None
    return rel, manifest


def _chain_of(rel):
    chain, p = set(), rel
    while p:
        chain.add(p)
        p = os.path.dirname(p)
    return chain


def _bootstrap_ok(stage, d):
    """A bootstrap/teardown stage is ours only while a complete walk finds exactly the folders on the way to the
    record, our marker (and its own leftover copies), and at most the record itself, byte for byte the record whose
    checksum the marker holds (record_sum, written before the record was; G0 round 2, NEW1)."""
    layout = _layout(stage, d)
    if layout is None:
        return False
    rel, manifest = layout
    chain = _chain_of(rel)
    marks = _own_marks(stage, d)
    walk = complete_walk(stage)
    if walk is None:
        return False
    for folder, dirs, files in walk:
        r = os.path.relpath(folder, stage)
        for x in dirs:
            if os.path.normpath(os.path.join(r, x)) not in chain or os.path.islink(os.path.join(folder, x)):
                return False
        for f in files:
            fr = os.path.normpath(os.path.join(r, f))
            if fr == STAGE_MARK or fr in marks:
                continue
            p = os.path.join(stage, fr)
            if fr != os.path.join(rel, manifest) or os.path.islink(p) or not d.get("record_sum"):
                return False
            try:
                if fsops.file_sum(p) != d["record_sum"]:
                    return False
            except OSError:
                return False
    return True


def _is_mark_of(d):
    def ok(p):
        try:
            m = json.loads(read_text(p) or "")
        except (ValueError, OSError):
            return False
        return isinstance(m, dict) and m.get("nonce") == d.get("nonce") and m.get("purpose") == d.get("purpose")
    return ok


def _remove_checked(stage, path, ok):
    """Delete the file `path` only as the verified object (G0 round 3, P1-2): it is first moved exclusively into a
    fresh private quarantine folder, re-checked there (`ok(quarantined_copy)`), and only that copy is deleted. A
    copy that no longer passes goes back exclusively (or stays in the quarantine, which is kept) and False is
    returned. Anything that lands at `path` meanwhile is never touched."""
    q = os.path.join(stage, ".afterparty-quarantine-" + uuid.uuid4().hex)
    os.mkdir(q, 0o700)
    held = os.path.join(q, os.path.basename(path))
    if fsops.rename_exclusive(path, held) is not None:
        os.rmdir(q)
        return False
    if not os.path.islink(held) and os.path.isfile(held) and ok(held):
        os.unlink(held)
        os.rmdir(q)
        return True
    if fsops.rename_exclusive(held, path) is None:
        os.rmdir(q)
    return False


def remove_record_stage(stage, d):
    """Remove a verified bootstrap/teardown stage exactly: the record and our marker copies, each re-checked as the
    object it is (quarantine, then checksum or nonce: _remove_checked); then each chain folder (rmdir, deepest first:
    it refuses anything that arrived meanwhile); then the marker (re-checked the same way) and the stage. Never a
    recursive delete, never a path outside the stage (_layout). False when something stopped it (whatever is left
    stays) or the marker's layout isn't valid."""
    layout = _layout(stage, d)
    if layout is None:
        return False
    rel, manifest = layout
    want = d.get("record_sum")
    try:
        rec = os.path.join(stage, rel, manifest)
        if os.path.lexists(rec) and not _remove_checked(stage, rec, lambda p: bool(want) and fsops.file_sum(p) == want):
            return False
        for n in _own_marks(stage, d):
            if not _remove_checked(stage, os.path.join(stage, n), _is_mark_of(d)):
                return False
        for c in sorted(_chain_of(rel), key=len, reverse=True):
            if os.path.lexists(os.path.join(stage, c)):
                os.rmdir(os.path.join(stage, c))
        if not _remove_checked(stage, os.path.join(stage, STAGE_MARK), _is_mark_of(d)):
            return False
        os.rmdir(stage)
    except OSError:
        return False
    return True


def _contents_ok(stage, d):
    """Only what this stage's marker says it holds may be in it, and our own copies must match their recorded sums."""
    if d.get("purpose") in ("bootstrap", "teardown"):  # teardown: the purge's last step, bootstrap in reverse
        return _bootstrap_ok(stage, d)
    names = set(os.listdir(stage)) - {STAGE_MARK} - _own_marks(stage, d)
    if d.get("purpose") not in ALLOWED or not names <= set(ALLOWED[d["purpose"]]):
        return False
    # "new" is our new copy, or (a crash inside swap_owned's exchange window, G0 S4) the displaced copy, which is
    # removable only while it is exactly the bytes the edit read (old_sum): anything else stays for a person
    ok_new = {d.get("new_sum")} | ({d.get("old_sum")} if d.get("purpose") == "edit" and d.get("old_sum") else set())
    if "new" in names and fsops.sum_of(os.path.join(stage, "new")) not in ok_new:
        return False
    for x in ("old", "item"):
        if x in names and d.get("verified") and fsops.sum_of(os.path.join(stage, x)) != d.get(x + "_sum"):
            return False
    return True


def sweep(parent):
    """Finish what a crashed run of ours left in `parent`. A stage is touched only if it carries our marker and its
    contents are exactly what the marker records: an item we had moved aside but not yet verified goes back where it
    was (exclusively: never over something new); our own copies and verified leftovers are removed. Anything else,
    whatever its name, is left for a person to look at. Returns the purposes of the stages it cleared."""
    cleared = []
    try:
        names = os.listdir(parent)
    except OSError:
        return cleared
    for n in names:
        p = os.path.join(parent, n)
        if not n.startswith(STAGE_PREFIX) or os.path.islink(p) or not os.path.isdir(p):
            continue
        try:
            d = read_mark(p)
            if d is None or not _contents_ok(p, d):
                continue
        except OSError:
            continue  # couldn't look at it completely: left as it is
        if d.get("purpose") in ("bootstrap", "teardown"):
            if remove_record_stage(p, d):
                cleared.append(d.get("purpose"))
            continue
        held = [x for x in ("old", "item") if os.path.lexists(os.path.join(p, x))]
        if held and not d.get("verified"):
            orig = d.get("orig")
            if not orig or len(held) != 1 or fsops.rename_exclusive(os.path.join(p, held[0]), orig) is not None:
                continue
        shutil.rmtree(p)
        cleared.append(d.get("purpose"))
    return cleared


def sweep_all(parents):
    return [x for p in parents for x in sweep(p)]


# ---- editing a user's file
def _clear_stage(stage, ours):
    """Remove an edit stage only while it holds nothing but our marker and, at most, our own unused new copy (byte
    for byte `ours`). Anything else (a displaced copy, an unknown name, an unreadable entry) keeps the whole stage
    (G0 M1: a stage that may hold displaced or unknown bytes is never deleted). True when removed."""
    try:
        names = set(os.listdir(stage)) - {STAGE_MARK}
        if names - {"new"}:
            return False
        p = os.path.join(stage, "new")
        if "new" in names and (os.path.islink(p) or not os.path.isfile(p) or read_bytes(p) != ours):
            return False
        mark = os.path.join(stage, STAGE_MARK)
        if os.path.lexists(mark) and (os.path.islink(mark) or not os.path.isfile(mark)):
            return False
    except OSError:
        return False
    shutil.rmtree(stage, ignore_errors=True)
    return True


def _backup(path, data, backup_dir):
    if not os.path.isdir(backup_dir):
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        fsops.note_made(backup_dir)  # so --purge may remove the folder once it is empty
    name = "%s.%d-%s.bak" % (os.path.basename(path), time.time_ns(), uuid.uuid4().hex[:8])
    fd = os.open(os.path.join(backup_dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    # R17: the bytes written, from memory; lets --purge remove it. Task 7 ruling: the backup is already written,
    # so a receipt that can't be written only warns (that backup then gets the Trash treatment at --purge).
    fsops.note_written_or_warn(os.path.join(backup_dir, name), data)


def edit(path, transform, backup_dir=None, attempts=3, before_check=None):
    """Rewrite `path` with `transform(old_text_or_None)` (returns the new text, or None for "nothing to do").
    A symlink is followed, and re-resolved on every attempt, so a retargeted link is honoured; the link itself
    stays. All preparatory work (the new file, the backup copy) happens first; then the link target and the file's
    bytes are checked again immediately before the rename. If either changed, our result is thrown away and we
    start over from a fresh read, at most `attempts` times, then EditConflict with the file as the other writer
    left it. Honest limit: a write landing in the instant between that last check and the rename can't be detected
    by any rename-based method; Afterparty's own writers never race each other (they hold the install lock).
    Backups are for manual recovery only; nothing restores them automatically. Returns True when written.
    Rev 4.4 (R14): the final step is fsops.swap_owned (exchange, byte compare with what we read, exchange back on
    a difference), so a write landing in that instant is detected too and never lost; the honest limit above now
    only means a reader may see our version for an instant before it is exchanged back."""
    for _ in range(attempts):
        real = os.path.realpath(path)
        old = read_bytes(real)
        try:
            text = None if old is None else old.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("%s isn't UTF-8 text" % path)
        new = transform(text)
        if new is None:
            return False
        stage = make_stage(os.path.dirname(real), "edit")
        keep_stage = done = False
        try:
            tmp = os.path.join(stage, "new")
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            if old is not None:
                os.chmod(tmp, os.stat(real).st_mode & 0o7777)
            update_mark(stage, new_sum=fsops.file_sum(tmp), old_sum=None if old is None else fsops.data_sum(old))
            if old is not None and backup_dir:
                _backup(real, old, backup_dir)
            if before_check is not None:
                before_check()
            if os.path.realpath(path) != real:
                continue  # retargeted meanwhile: start over from a fresh read
            try:
                fsops.swap_owned(real, tmp, old)  # compare-and-swap against exactly what we read (R14)
            except fsops.Conflict as e:
                if e.kept:
                    keep_stage = True  # a foreign copy was kept (e.kept), maybe in our stage
                    raise EditConflict("%s Nothing was lost; move that copy back if you need it." % e)
                continue  # changed meanwhile: theirs stays, start over from a fresh read
            done = True
            checkpoint("edited " + os.path.basename(real))
            return True
        finally:
            if not keep_stage and not _clear_stage(stage, new.encode("utf-8")):
                why = sys.exc_info()[1]
                raise EditConflict("%s %s%s. Afterparty's staging folder %s holds something it can't vouch for, so "
                                   "nothing in it was deleted; look inside before removing it."
                                   % (path, "was edited" if done else "wasn't edited",
                                      " (%s)" % why if why else "", stage))
    raise EditConflict("%s kept changing while Afterparty was editing it, so it was left as it is. "
                       "Close whatever is editing it and run the installer again." % path)
