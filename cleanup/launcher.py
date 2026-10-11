"""Start (or reuse) the Afterparty server and open it in the browser."""
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from cleanup import config, fsops
from cleanup.installer import jobs

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_LABEL = jobs.label("server")


def server_python(repo=REPO):
    """The installer's interpreter first, then the dev venv, then whatever runs us."""
    try:
        with open(os.path.join(config.config_dir(), "python")) as f:
            py = f.readline().strip()
        if py and os.access(py, os.X_OK):
            return py
    except OSError:
        pass
    venv = os.path.join(repo, ".venv", "bin", "python")
    return venv if os.access(venv, os.X_OK) else sys.executable


def default_state():
    return os.path.join(config.state_dir(), "server.json")


def _healthy(s):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{s['port']}/api/status",
                                     headers={"X-Cleanup-Key": s["token"]})
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _port_free(port):
    with socket.socket() as so:
        try:
            so.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def _stop_stale(s):
    """Terminate a recorded server that no longer answers with its token (only if it IS our server)."""
    pid = s.get("pid") if s else None
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return
    if "cleanup.cli" in cmd and "serve" in cmd:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _write_private(path, obj):
    data = json.dumps(obj).encode("utf-8")
    fsops.replace_owned(path, data, mode=0o600)  # R14: CAS, never over a change
    fsops.note_written_or_warn(path, data)  # R17; --purge deletes it only through this receipt


def _launchd_pid(label):
    for line in jobs.run("list").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] == label and parts[0].isdigit():
            return int(parts[0])
    return None


def _bootout(label):
    """(ok, why): ok only when launchd reports the job gone."""
    return jobs.stop(label)


def _start_launchd(python, port, token, state_dir, label):
    """Run the server as a launchd job: macOS then checks Full Disk Access against Python.app itself,
    not the terminal that launched us. The token lives only in this 0600 plist and the job's env."""
    plist = os.path.join(state_dir, f"{label}.plist")
    body = jobs.fill(jobs.template("server"), {"LABEL": label, "PYTHON": python, "PORT": port, "TOKEN": token,
                                                "REPO": REPO, "HOME": os.path.expanduser("~"),
                                                "LOG": os.path.join(state_dir, "server.log")})
    fsops.replace_owned(plist, body.encode("utf-8"), mode=0o600)  # R14
    fsops.note_written_or_warn(plist, body.encode("utf-8"))  # R17
    ok, why = _bootout(label)  # waits until launchd reports the old job gone
    if not ok:
        raise RuntimeError(f"couldn't stop the previous Afterparty server job: {why}")
    domain = jobs.domain()
    err = ""
    for _ in range(3):
        r = jobs.run("bootstrap", domain, plist)
        if r.returncode == 0:
            return
        err = (r.stderr or r.stdout or "").strip()
        time.sleep(0.5)
    raise RuntimeError(f"couldn't start the Afterparty server with launchd: {err}")


LOG_TAIL = 4000  # bytes of server.log quoted in a start failure


def _start_failure(proc, dead, wait, log_path, log_start):
    """Why the server didn't come up, with what it wrote to server.log during this start (the token never goes
    there: it is passed in the environment and the server doesn't print it)."""
    if proc is not None and proc.poll() is not None:
        why = "the server process exited with status %s" % proc.returncode
    elif dead:
        why = "the launchd job isn't running"
    else:
        why = "the server didn't answer within %gs" % wait
    try:
        with open(log_path, "rb") as f:
            f.seek(log_start)
            out = f.read()
    except OSError:
        out = b""
    tail = out[-LOG_TAIL:].decode("utf-8", "replace").strip()
    return "Afterparty server failed to start (%s) — see %s%s" % (
        why, log_path, (":\n" + tail) if tail else " (it wrote nothing)")


def stop_server(state_path=None, label=SERVER_LABEL):
    """(ok, why): ok when no Afterparty server job of ours is left loaded. The state file only says how the server
    was started; it never chooses what is stopped: only our own `label` is booted out, and only a pid that `ps`
    shows is our server is signalled (a stale or edited state file can't make us stop someone else's job)."""
    state_path = state_path or default_state()
    try:
        with open(state_path) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return True, ""
    if not isinstance(s, dict):
        return True, ""
    if s.get("mode") == "launchd":
        if s.get("label") == label:
            return _bootout(label)
        return True, ""  # names another job (or none): not ours to stop
    _stop_stale(s)  # only a real pid (> 1) that is our server: never pid 0/-1 (a whole process group) or a stranger
    return True, ""


def ensure_server(open_browser=True, python=None, state_path=None, wait=15.0, mode="spawn",
                  label=SERVER_LABEL):
    python = python or server_python()
    state_path = state_path or default_state()
    state_dir = os.path.dirname(state_path)
    os.makedirs(state_dir, exist_ok=True)
    s = None
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                s = json.load(f)
        except ValueError:
            s = None
        if not isinstance(s, dict):
            s = None
    if not (s and _healthy(s)):
        # as in stop_server: the state file never chooses what is stopped; only our own label is booted out
        if s and s.get("mode") == "launchd" and s.get("label") == label:
            ok, why = _bootout(label)
            if not ok:
                raise RuntimeError(f"couldn't stop the previous Afterparty server job: {why}")
        _stop_stale(s)
        port = s["port"] if s and isinstance(s.get("port"), int) and s["port"] > 1024 and _port_free(s["port"]) \
            else _free_port()
        token = secrets.token_hex(16)
        proc = None
        log_path = os.path.join(state_dir, "server.log")
        try:
            log_start = os.path.getsize(log_path)  # report only what this start wrote (the log is appended to)
        except OSError:
            log_start = 0
        if mode == "launchd":
            _start_launchd(python, port, token, state_dir, label)
        else:
            log = open(log_path, "ab")
            env = dict(os.environ, PYTHONPATH=REPO, CLEANUP_TOKEN=token)  # never in argv: `ps` is world-readable
            proc = subprocess.Popen([python, "-B", "-m", "cleanup.cli", "serve", "--live", "--port", str(port)],
                                    cwd=REPO, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        s = {"pid": proc.pid if proc else None, "port": port, "token": token, "mode": mode}
        started, deadline = time.time(), time.time() + wait
        while not _healthy(s):
            dead = (proc is not None and proc.poll() is not None) or \
                (mode == "launchd" and time.time() - started > 3 and _launchd_pid(label) is None)
            if time.time() > deadline or dead:
                raise RuntimeError(_start_failure(proc, dead, wait, log_path, log_start))
            time.sleep(0.1)
        if mode == "launchd":
            s["pid"], s["label"] = _launchd_pid(label), label
        _write_private(state_path, s)
    url = f"http://127.0.0.1:{s['port']}/?key={s['token']}"
    if open_browser:
        subprocess.run(["open", url])
    return url
