"""Which file macOS wants in Full Disk Access for a given Python interpreter (shared by the map app and the installer)."""
import glob
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


def _framework_app(exe):
    """The framework's Python.app for <version dir>/bin/python3, or the enclosing Python.app when `exe` already is
    its Contents/MacOS/ binary (a recorded interpreter); else None."""
    real = os.path.realpath(exe)
    marker = os.sep + os.path.join("Contents", "MacOS") + os.sep
    if marker in real:
        app = real[:real.rindex(marker)]
        if os.path.basename(app) == "Python.app" and os.path.isdir(app):
            return app
    app = os.path.realpath(os.path.join(os.path.dirname(real), "..", "Resources", "Python.app"))
    return app if os.path.isdir(app) else None


def _inner(app):
    return os.path.join(app, "Contents", "MacOS", "Python")


_PROBE = "import sys;print(sys.executable);print('%d.%d.%d' % sys.version_info[:3]);print(int(sys.prefix != sys.base_prefix))"


def _probe(python):
    """(executable, version, in_venv) as `python` reports itself, or None."""
    try:
        r = subprocess.run([python, "-I", "-B", "-c", _PROBE], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.split("\n") if r.returncode == 0 else []
    return (lines[0].strip(), lines[1].strip(), lines[2].strip() == "1") if len(lines) >= 3 else None


def _stable(path):
    """Homebrew's versioned <prefix>/Cellar/<formula>/<version>/... -> <prefix>/opt/<formula>/..., which survives a
    `brew upgrade` + cleanup. Any other path is returned unchanged."""
    if "/Cellar/" not in path:
        return path
    prefix, rest = path.split("/Cellar/", 1)
    parts = rest.split("/", 2)
    if len(parts) < 3:
        return path
    return "%s/opt/%s/%s" % (prefix, parts[0], parts[2])


def _is_clt(real):
    return (real.startswith("/Library/Developer/CommandLineTools/") or "/Contents/Developer/" in real
            or real.startswith(os.path.realpath(_clt_dir()) + os.sep))


def _same_version(exe, version):
    """`exe` runs, reports exactly `version` and is outside a venv."""
    got = _probe(exe) if os.access(exe, os.X_OK) else None
    return bool(got) and got[1] == version and not got[2]


def real_executable(python):
    """The executable launchd jobs must start (macOS attributes Full Disk Access to the program launchd starts).
    - Command Line Tools / Xcode: /usr/bin/python3 is a stub; the answer is the binary inside the framework's
      Python.app (<version dir>/Resources/Python.app/Contents/MacOS/Python), the one fda_binary names.
    - Homebrew: the version-free <prefix>/opt/<formula>/... path, so a brew upgrade doesn't break jobs.
    - Anything else (python.org, venvs, other installs) is returned unchanged.
    A candidate counts only if it is executable and a probe reports the same version, outside a venv."""
    first = _probe(python)
    if not first:
        return python
    reported, version, venv = first
    exe = reported if reported and os.path.isabs(reported) and os.access(reported, os.X_OK) else python
    if venv:
        return python
    real = os.path.realpath(exe)
    cand = None
    if _is_clt(real):
        app = _framework_app(exe)
        if not app or not _same_version(_inner(app), version):
            return python  # no verified inner binary: keep what we were given
        # the verified original is the fallback; prefer_granted only ever returns it or a verified alternate
        return _inner(prefer_granted(app, version))
    if "/Cellar/" in real:
        cand = _stable(real)
        if cand == real:
            cand = None
    if cand and os.access(cand, os.X_OK):
        got = _probe(cand)
        if got and got[1] == version and not got[2]:
            return cand
    return python


def fda_binary(python):
    """The path to add under Full Disk Access: the framework's Python.app when there is one (the Command Line Tools'
    /usr/bin/python3 is only a stub for it), else the interpreter itself. Homebrew paths are made version-free."""
    inside = os.path.realpath(python)
    marker = ".app/Contents/MacOS/"
    if marker in inside and os.path.basename(inside[:inside.index(marker) + 4]) == "Python.app":
        return _stable(inside[:inside.index(marker) + 4])  # already the binary inside the Python.app
    app = _framework_app(python)
    if app:
        return _stable(app)
    try:  # a stub reports the real binary it runs
        r = subprocess.run([python, "-I", "-B", "-c", "import os,sys;print(os.path.realpath(sys.executable))"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        real = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        real = ""
    if real and os.path.isabs(real):
        return _stable(_framework_app(real) or real)
    return _stable(os.path.realpath(python))


# --- several Apple Python.app copies -------------------------------------------------------------------------------
# With both Xcode and the Command Line Tools installed there are two framework Python.app copies, both
# com.apple.python3 and both listed as "Python.app" in System Settings, each with its own Full Disk Access entry.
# /usr/bin/python3 runs whichever `xcode-select -p` names; the user may have granted the other one.

FRAMEWORK = os.path.join("Library", "Frameworks", "Python3.framework", "Versions")
PROBE_TIMEOUT = 15.0      # waiting for the probe's answer
LAUNCHCTL_TIMEOUT = 10.0  # each launchctl call the probe makes (submit, remove, list)
GONE_TIMEOUT = 3.0        # waiting for launchd to drop the removed probe job
PROBE_LABEL = "io.github.drorlazar.afterparty.fdaprobe"


def _clt_dir():
    """AFTERPARTY_CLT_DIR replaces the Command Line Tools folder in tests."""
    return os.environ.get("AFTERPARTY_CLT_DIR") or "/Library/Developer/CommandLineTools"


def _xcode_developer_dir():
    """Xcode's Developer folder when xcode-select names one (AFTERPARTY_XCODE_SELECT replaces it in tests), else None."""
    tool = os.environ.get("AFTERPARTY_XCODE_SELECT") or "/usr/bin/xcode-select"
    try:
        r = subprocess.run([tool, "-p"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    d = r.stdout.strip() if r.returncode == 0 else ""
    if not d or not os.path.isabs(d) or os.path.realpath(d) == os.path.realpath(_clt_dir()):
        return None
    return d


def _xcode_developer_dirs():
    """xcode-select's Xcode (if it names one) and every installed /Applications/Xcode*.app, whichever copy
    xcode-select names (AFTERPARTY_XCODE_APPS, a glob, replaces /Applications/Xcode*.app in tests)."""
    out = []
    sel = _xcode_developer_dir()
    if sel:
        out.append(sel)
    for app in sorted(glob.glob(os.environ.get("AFTERPARTY_XCODE_APPS") or "/Applications/Xcode*.app")):
        out.append(os.path.join(app, "Contents", "Developer"))
    return out


def _trusted_uids():
    """root, plus (tests only) AFTERPARTY_TEST_TRUSTED_UID: the one check a tmp tree can't pass otherwise."""
    uids = {0}
    try:
        uids.add(int(os.environ["AFTERPARTY_TEST_TRUSTED_UID"]))
    except (KeyError, ValueError):
        pass
    return uids


def _sound(path, uids, want):
    """`path` itself (never a symlink) is a `want` ("dir"/"file") owned by a trusted uid, writable by nobody else."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    kind = stat.S_ISDIR(st.st_mode) if want == "dir" else stat.S_ISREG(st.st_mode)
    return kind and st.st_uid in uids and not st.st_mode & 0o022


def _trusted_copy(base, mm):
    """The Python.app under `base` (a Developer or Command Line Tools folder) for major.minor `mm`, only when its
    provenance holds: every folder from `base` (and the enclosing .app bundle) down to Contents/MacOS/Python is a
    real folder, not a symlink, owned by root and not group/world-writable, and the binary is too. Else None."""
    uids = _trusted_uids()
    rbase = os.path.realpath(base)
    up = rbase
    while ".app" + os.sep in up + os.sep and up != os.path.dirname(up):  # Xcode.app/Contents/Developer: the bundle
        if not _sound(up, uids, "dir"):
            return None
        if up.endswith(".app"):
            break
        up = os.path.dirname(up)
    if not _sound(rbase, uids, "dir"):
        return None
    path = rbase
    parts = FRAMEWORK.split(os.sep) + [mm, "Resources", "Python.app", "Contents", "MacOS"]
    for part in parts:
        path = os.path.join(path, part)
        if not _sound(path, uids, "dir"):
            return None
    exe = os.path.join(path, "Python")
    if not _sound(exe, uids, "file") or not os.access(exe, os.X_OK):
        return None
    app = os.path.dirname(os.path.dirname(path))
    return app if os.path.realpath(exe) == exe else None


def apple_apps(version):
    """[(kind, Python.app)] for every Apple framework copy of `version`'s major.minor whose provenance holds
    (`_trusted_copy`): kind is "xcode" or "clt"."""
    mm = ".".join(str(version).split(".")[:2])
    out = []
    bases = [("xcode", d) for d in _xcode_developer_dirs()] + [("clt", _clt_dir())]
    for kind, base in bases:
        app = _trusted_copy(base, mm)
        if app and app not in [a for _, a in out]:
            out.append((kind, app))
    return out


def kind_of(app):
    """"clt", "xcode" or None for a Python.app path."""
    real = os.path.realpath(app)
    if real.startswith(os.path.realpath(_clt_dir()) + os.sep):
        return "clt"
    if "/Contents/Developer/" in real and "/Python3.framework/" in real:
        return "xcode"
    return None


def _probe_code():
    """The probe's program: cleanup.scanner.access's own detection policy (the map app's), run in the probed
    interpreter on this user's folders."""
    from cleanup.scanner import access
    return access.probe_payload([os.path.expanduser(p) for p in access.PROBES])


def _remove_probe(jobs, lbl):
    """Remove the probe job and verify launchd no longer has it: "" when gone, else why not."""
    r = jobs.run("remove", lbl, timeout=LAUNCHCTL_TIMEOUT)
    why = "" if r.returncode == 0 or r.returncode in jobs.NOT_LOADED else jobs._why(
        r, "launchctl remove failed (exit %d)" % r.returncode)
    end = time.monotonic() + GONE_TIMEOUT
    while True:
        l = jobs.run("list", lbl, timeout=LAUNCHCTL_TIMEOUT)
        if l.returncode in jobs.NOT_LOADED:
            return ""
        if l.returncode != 0:
            return why or jobs._why(l, "launchctl list failed (exit %d)" % l.returncode)
        if time.monotonic() >= end:
            return why or "launchd still lists it after removal"
        time.sleep(0.1)


def launchd_has_fda(binary, timeout=None):
    """True / False / None (couldn't tell): whether `binary`, started by launchd the way our jobs are, can read
    folders only Full Disk Access opens. Runs a one-off `launchctl submit` job, always removes it and verifies it
    is gone; every launchctl call is time-bounded. A probe job that can't be removed is reported on stderr (with
    the exact command to remove it) and makes the answer None."""
    from cleanup.installer import jobs  # the AFTERPARTY_LAUNCHCTL seam
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    tmp = tempfile.mkdtemp(prefix="afterparty-fdaprobe-")
    out = os.path.join(tmp, "out.txt")
    lbl = "%s.%d.%s" % (PROBE_LABEL, os.getpid(), uuid.uuid4().hex[:12])
    result = None
    try:
        r = jobs.run("submit", "-l", lbl, "-o", out, "-e", out, "--", binary, "-I", "-B", "-c", _probe_code(),
                     timeout=LAUNCHCTL_TIMEOUT)
        end = time.monotonic() + timeout
        answered = r.returncode != 0
        while not answered and time.monotonic() < end:
            try:
                with open(out, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                text = ""
            for line in text.splitlines():
                if line.startswith("fda:"):
                    result, answered = {"yes": True, "no": False}.get(line[4:].strip()), True
                    break
            else:
                time.sleep(0.1)
    finally:
        why = _remove_probe(jobs, lbl)
        shutil.rmtree(tmp, ignore_errors=True)
    if why:
        print("afterparty: couldn't remove the Full Disk Access probe job %s (%s). Remove it with: "
              "launchctl remove %s" % (lbl, why, lbl), file=sys.stderr)
        return None
    return result


def _usable_alternate(app, version):
    """An alternate copy counts only if its own binary runs, reports exactly `version` outside a venv, and
    reports itself as the executable (not some other interpreter it hands off to)."""
    exe = _inner(app)
    got = _probe(exe)
    return bool(got) and got[1] == version and not got[2] and os.path.realpath(got[0]) == os.path.realpath(exe)


def prefer_granted(app, version):
    """`app` (the verified copy we were given), or another Apple copy of the same major.minor that already has
    Full Disk Access by a launchd probe. Alternates are validated first (provenance in apple_apps, full version
    and identity here). The original is probed first and kept unless its probe says a definite no: granted, or
    unknown (timeout, failed submit, failed probe cleanup) keeps `app` without probing any alternate. After a
    definite no, we switch only when exactly one alternate is granted and no alternate is unknown; none granted,
    several granted, or any uncertainty keeps `app`."""
    real = os.path.realpath(app)
    apps = [a for _, a in apple_apps(version)]
    if real not in apps:
        return app
    alts = [a for a in apps if a != real and _usable_alternate(a, version)]
    if not alts:
        return app
    if launchd_has_fda(_inner(real)) is not False:
        return app
    results = [(a, launchd_has_fda(_inner(a))) for a in alts]
    if any(r is None for _, r in results):
        return app
    granted = [a for a, r in results if r is True]
    return granted[0] if len(granted) == 1 else app


def which_copy(path):
    """How to tell an Apple Python.app apart in System Settings, or "" for any other interpreter."""
    kind = kind_of(path)
    if kind is None:
        return ""
    ver = os.path.basename(os.path.dirname(os.path.dirname(os.path.realpath(path))))
    other = [k for k, _ in apple_apps(ver) if k != kind]
    if kind == "clt":
        return "the Command Line Tools copy" + (", not Xcode's" if other else "")
    return "Xcode's copy" + (", not the Command Line Tools one" if other else "")


def fda_hint(python):
    """(path to add, how to tell it apart or "")."""
    path = fda_binary(python)
    return path, which_copy(path)
