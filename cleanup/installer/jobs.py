"""launchd jobs: plist templates in launchd/templates/, rendered with absolute paths, loaded into gui/<uid>.
Every launchctl result is checked; a job counts as stopped or started only when `launchctl print` agrees."""
import os
import re
import subprocess
import time
from xml.sax.saxutils import escape

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
PREFIX = "io.github.drorlazar.afterparty"
SCHEDULED = ("weekly", "spacecheck")


def label(name):
    return PREFIX + "." + name


def template(name, repo=REPO):
    with open(os.path.join(repo, "launchd", "templates", name + ".plist"), encoding="utf-8") as f:
        return f.read()


def fill(text, values):
    """One pass over the template: a value is never searched again, so one that contains `@X@` (a folder name, say)
    is neither re-substituted nor mistaken for an unfilled placeholder."""
    left = set()

    def sub(m):
        if m.group(1) in values:
            return escape(str(values[m.group(1)]))
        left.add(m.group(0))
        return m.group(0)
    out = re.sub(r"@([A-Z]+)@", sub, text)
    if left:
        raise ValueError("plist template has unfilled values: " + ", ".join(sorted(left)))
    return out


def render(name, python, home, log_dir, repo=REPO):
    """A scheduled job's plist: absolute paths only, the interpreter from config, nothing from the user's PATH."""
    return fill(template(name, repo), {"LABEL": label(name), "PYTHON": python, "REPO": repo, "HOME": home,
                                       "LOG": os.path.join(log_dir, name + ".log")})


def launchctl():
    """AFTERPARTY_LAUNCHCTL replaces launchctl in tests, so they never touch the real launchd domain."""
    return os.environ.get("AFTERPARTY_LAUNCHCTL") or "/bin/launchctl"


def domain():
    return "gui/%d" % os.getuid()


def run(*args, timeout=30):
    cmd = [launchctl()] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


NOT_LOADED = (3, 113)  # "No such process" / "Could not find service": launchd doesn't have the job


def _why(r, fallback):
    return (r.stderr or r.stdout or fallback).strip()


def status(lbl):
    """("loaded", "") / ("absent", "") / ("error", why). A failed `launchctl print` (no permission, launchctl
    missing, a hung launchd) is an error, never "not loaded"."""
    r = run("print", domain() + "/" + lbl)
    if r.returncode == 0:
        return "loaded", ""
    if r.returncode in NOT_LOADED:
        return "absent", ""
    return "error", _why(r, "launchctl print failed (exit %d)" % r.returncode)


def is_loaded(lbl):
    state, why = status(lbl)
    if state == "error":
        raise RuntimeError("can't tell whether %s is loaded: %s" % (lbl, why))
    return state == "loaded"


def stop(lbl, wait=5.0):
    """Unload the job. (True, "not loaded"|"stopped") only when launchd says it no longer has it."""
    state, why = status(lbl)
    if state == "error":
        return False, why
    if state == "absent":
        return True, "not loaded"
    r = run("bootout", domain() + "/" + lbl)
    if r.returncode != 0 and r.returncode not in NOT_LOADED:
        return False, _why(r, "launchctl bootout failed (exit %d)" % r.returncode)
    deadline = time.time() + wait
    while True:
        state, why = status(lbl)
        if state == "error":
            return False, why
        if state == "absent":
            return True, "stopped"
        if time.time() >= deadline:
            return False, "launchctl bootout didn't stop it"
        time.sleep(0.1)


def start(lbl, plist):
    """(Re)load the job from `plist`. (True, "loaded") only when launchd reports it loaded afterwards."""
    ok, msg = stop(lbl)
    if not ok:
        return False, "the old copy couldn't be stopped (%s)" % msg
    r = run("bootstrap", domain(), plist)
    if r.returncode != 0:
        return False, _why(r, "launchctl bootstrap failed")
    state, why = status(lbl)
    if state != "loaded":
        return False, why or "launchd didn't load it"
    return True, "loaded"
