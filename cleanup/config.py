"""Afterparty settings — the only reader and writer of ~/.config/afterparty/config.json.

Malformed settings raise ConfigError; callers must refuse to delete anything when that happens."""
import json
import os
import uuid
from contextlib import contextmanager

from cleanup import fsops

VERSION = 1
AUTOPILOT = ("report", "clean")
DEFAULTS = {
    "version": VERSION,
    "python": "",
    "roots": [],
    "keep": [],
    "autopilot": "report",
    "low_space_gb": 50,
    "scratch_days": 3,
    "idle_days": 14,
    "agent": {"enabled": False, "read_files": False},
}


class ConfigError(ValueError):
    pass


def config_dir():
    return os.environ.get("AFTERPARTY_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".config", "afterparty")


def config_path():
    return os.path.join(config_dir(), "config.json")


def state_dir():
    return os.environ.get("AFTERPARTY_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".local", "state",
                                                                    "afterparty")


def _real(p):
    return os.path.realpath(os.path.expanduser(p))


def validate(raw):
    if not isinstance(raw, dict):
        raise ConfigError("settings must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError("unknown setting(s): " + ", ".join(unknown))
    cfg = json.loads(json.dumps(DEFAULTS))
    for key, value in raw.items():
        if key == "agent":
            ok = isinstance(value, dict) and not set(value) - set(DEFAULTS["agent"]) \
                and all(isinstance(v, bool) for v in value.values())
            if not ok:
                raise ConfigError("agent must look like {\"enabled\": false, \"read_files\": false}")
            cfg["agent"].update(value)
        elif key in ("roots", "keep"):
            if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
                raise ConfigError(key + " must be a list of folder paths")
            cfg[key] = value
        elif key in ("low_space_gb", "scratch_days", "idle_days", "version"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(key + " must be a whole number, 0 or more")
            cfg[key] = value
        elif not isinstance(value, str):
            raise ConfigError(key + " must be text")
        else:
            if key == "python" and any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ConfigError("python must not contain control characters")
            cfg[key] = value
    if cfg["version"] != VERSION:
        raise ConfigError("unsupported settings version %r" % cfg["version"])
    if cfg["autopilot"] not in AUTOPILOT:
        raise ConfigError("autopilot must be \"report\" or \"clean\"")
    if cfg["scratch_days"] < 1:
        raise ConfigError("scratch_days must be at least 1")
    return cfg


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError("can't read %s: %s" % (path, e.strerror or e))


def _parse(data, path):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigError("%s can't be read as text (it isn't UTF-8: %s)" % (path, e))
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ConfigError("%s is not valid JSON (%s)" % (path, e))
    return validate(raw)


def pending_dir():
    return os.path.join(state_dir(), "keep-pending")


class _Vanished(Exception):
    pass


def _pending_names():
    d = pending_dir()
    try:
        # dot-files are never pending protections: .tmp-* is one being written (unpublished), others (.DS_Store) aren't ours
        return sorted(n for n in os.listdir(d) if not n.startswith("."))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise ConfigError("can't read %s: %s" % (d, e.strerror or e))


def _pending_entries(names):
    """{name: protected path} for the listed pending files. Raises _Vanished if one disappeared since the listing
    (merged and withdrawn: the caller reads again); anything that isn't a complete pending entry is ConfigError."""
    out = {}
    for n in names:
        p = os.path.join(pending_dir(), n)
        try:
            with open(p, encoding="utf-8") as f:
                entry = json.load(f)
        except FileNotFoundError:
            raise _Vanished(p)
        except (OSError, ValueError) as e:
            raise ConfigError("the pending protection %s can't be read (%s)" % (p, e))
        if not (n.endswith(".json") and isinstance(entry, dict) and set(entry) == {"keep"}
                and isinstance(entry["keep"], str)):
            raise ConfigError("%s isn't a pending protection Afterparty wrote" % p)
        out[n] = entry["keep"]
    return out


def load(path=None, attempts=5):
    """The settings, validated, with every pending protection added to `keep` (rulings R8, R10). This is the only
    settings authority, so `current_keep` and therefore `executor.last_gate`'s fresh read see a Protect press the
    moment it is published, before it gets the settings lock. Stable read (R10): list the pending files and read
    them, read the settings, list again; if the list changed or a listed file vanished, start again (at most
    `attempts` times), else the settings count as malformed (ConfigError: deleting refuses). A pending file is
    withdrawn only after the merged settings are durably written, so a stable read sees each protection in one
    place or the other."""
    path = path or config_path()
    for _ in range(attempts):
        names = _pending_names()
        try:
            pending = _pending_entries(names)
        except _Vanished:
            continue
        data = _read(path)
        if _pending_names() != names:
            continue  # published or withdrawn during the read: read everything again
        cfg = validate({}) if data is None else _parse(data, path)
        for k in pending.values():
            if k not in cfg["keep"]:
                cfg["keep"].append(k)
        return cfg, data is not None
    raise ConfigError("protections kept changing while the settings were read (%d tries), so they count as "
                      "unreadable for now" % attempts)


@contextmanager
def writers_lock(path=None, create=True, blocking=True):
    """The one lock every Afterparty settings writer takes (installer, migration, map app, CLI, save, update), for
    one short read-merge-write only. A long transaction that writes settings takes the cleanup run lock first
    (installer/main.py settings_transaction), so a Protect press never waits behind it while a cleanup runs.
    create=False / blocking=False (uninstall --purge): don't recreate a missing folder; raise fsops.Busy if held."""
    path = path or config_path()
    folder = os.path.dirname(path)
    if not create and not os.path.isdir(folder):
        yield
        return
    with fsops.held(os.path.join(folder, ".lock"), blocking=blocking, create_dirs=create,
                    busy="Afterparty's settings are being changed right now"):
        yield


def _sidecar(path):
    return os.path.join(os.path.dirname(path), "python")


def _write_sidecar(path, python):
    data = (python + "\n").encode("utf-8")
    try:  # the sidecar mirrors the settings on disk (written by us): their python is a sidecar we wrote too
        on_disk = _read(path)
        known = None if on_disk is None else (_parse(on_disk, path)["python"] + "\n").encode("utf-8")
    except (ConfigError, OSError):
        known = None
    try:
        written = fsops.write_receipted(_sidecar(path), data, known=known)  # G0 M4: never over a file we don't own
    except fsops.Conflict as e:
        raise ConfigError(str(e))
    if written:
        fsops.note_written_or_warn(_sidecar(path), data)  # R17: the bytes we wrote; never fails a done write


def save(cfg, path=None):
    """Replace the settings with `cfg` (validated), under the writers' lock. Prefer update(), which merges into
    what is on disk now instead of replacing it. Both files go through the one owned-file writer (R14)."""
    path = path or config_path()
    cfg = validate(cfg)
    with writers_lock(path):
        # the sidecar the shims read goes first: a crash in between never leaves settings naming a python the shims lack
        _write_sidecar(path, cfg["python"])
        data = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
        try:
            fsops.write_owned(path, data, _read(path))
        except fsops.Conflict as e:
            raise ConfigError(str(e))
        fsops.note_written_or_warn(path, data)
    return cfg


def _reconcile_sidecar(path):
    """After an abandoned write, make the python sidecar agree with the settings that are on disk."""
    try:
        data = _read(path)
        if data is not None:
            _write_sidecar(path, _parse(data, path)["python"])
    except (ConfigError, OSError, fsops.Conflict):
        pass


def update(mutate, path=None, attempts=3, before_replace=None):
    """Change the settings by applying `mutate(cfg)` to a fresh, validated copy of what is on disk right now,
    holding the writers' lock, so no Afterparty writer can interleave. The settings file is replaced by a
    compare-and-swap against exactly the bytes we read (fsops.write_owned, R14): if a program that doesn't take our
    lock changed it since, nothing is replaced and we start again from its content (at most `attempts` times, then
    ConfigError); on any failure the sidecar is reconciled with the file on disk. Malformed settings raise
    ConfigError and are never overwritten. `before_replace` is a test hook.

    Honest limit: a program that writes config.json WITHOUT Afterparty's lock can still lose a change it makes in
    the instant between our last comparison and the swap completing. Every Afterparty writer takes the lock, so
    Afterparty's own writers never interleave."""
    path = path or config_path()
    with writers_lock(path):
        try:
            for _ in range(attempts):
                before = _read(path)
                cfg = validate({}) if before is None else _parse(before, path)
                work = json.loads(json.dumps(cfg))
                mutate(work)
                new = validate(work)
                if before is not None and new == cfg and _read(_sidecar(path)) == (new["python"] + "\n").encode("utf-8"):
                    return new
                _write_sidecar(path, new["python"])
                if before_replace is not None:
                    before_replace()
                data = (json.dumps(new, indent=2) + "\n").encode("utf-8")
                try:
                    fsops.write_owned(path, data, before)
                except fsops.Conflict as e:
                    if e.kept:
                        raise ConfigError(str(e))
                    continue  # someone else saved meanwhile: theirs stays, we merge into it on the next pass
                fsops.note_written_or_warn(path, data)
                return new
        except BaseException:
            _reconcile_sidecar(path)
            raise
        _reconcile_sidecar(path)
    raise ConfigError("%s kept changing while Afterparty was saving it, so nothing was changed" % path)


def roots(cfg):
    found, missing, seen = [], [], set()
    for p in cfg["roots"]:
        rp = _real(p)
        if not os.path.isdir(rp):
            missing.append(p)
        elif rp not in seen:
            seen.add(rp)
            found.append(rp)
    return found, missing


def keep_paths(cfg):
    out = []
    for p in cfg["keep"]:
        rp = _real(p)
        if rp not in out:
            out.append(rp)
    return out


def current_keep(path=None):
    """The keep list as the settings say right now, for deleting paths: raises ConfigError if the settings are
    malformed or missing (a missing file must never turn into "no protections")."""
    cfg, present = load(path)
    if not present:
        raise ConfigError("the settings file %s is missing" % (path or config_path()))
    return keep_paths(cfg)


class KeepPending(ConfigError):
    """add_keep published its pending protection (which keeps protecting) but couldn't merge it into the
    settings."""


def add_keep(path, cfg_path=None):
    """The map's Protect button (ruling R8). The intent is published before anything waits: an exclusive pending file
    that every load() sees at once (no lock needed), then the entry is merged into the settings under the settings
    lock, then the pending file is withdrawn. Ruling R13: it is withdrawn only after the merged settings and the
    folder fsync both succeeded; on any failure (or if the process dies) it stays and keeps protecting: a stale
    pending protection only ever protects more, never less. The error is raised (the button reports it)."""
    d = pending_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    name = uuid.uuid4().hex
    tmp, mine = os.path.join(d, ".tmp-" + name), os.path.join(d, name + ".json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"keep": path}, f)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, mine)  # exclusive and atomic: a reader sees the whole entry or nothing
    finally:
        os.unlink(tmp)

    def mutate(cfg):
        if _real(path) not in keep_paths(cfg):
            cfg["keep"].append(path)
    try:
        cfg = update(mutate, cfg_path)  # any failure raises with the pending file still in place (R13)
        _fsync_dir(os.path.dirname(cfg_path or config_path()))  # R10: merged settings durable before withdrawal
    except (ConfigError, OSError, fsops.Conflict, fsops.Busy) as e:
        raise KeepPending("protected (pending), settings not updated: %s" % e) from e
    try:
        os.unlink(mine)  # only now: the merge and the folder fsync both succeeded
    except FileNotFoundError:
        pass  # --purge moved it to the Trash meanwhile (it runs only when no cleanup can)
    return cfg


def _fsync_dir(folder):
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
