"""Claude Code SessionEnd hook: record large disposable folders this session created.

Writes one JSON line per session to the Afterparty state folder (sessions.jsonl);
the Afterparty report lists them under "Recent session leftovers". Detaches immediately so
session exit is never slowed down.
"""
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
NAMES = {".scratch", "renders", "render", "out", "outputs", "output", "frames"}
MAX_DEPTH = 4


def scan(cwd, since):
    found = []
    base = cwd.rstrip(os.sep).count(os.sep)
    for d, dirs, _ in os.walk(cwd):
        if d.count(os.sep) - base >= MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules", ".venv", "venv", "worktrees", ".worktrees")]
        for x in list(dirs):
            if x in NAMES or "frames" in x.lower():
                p = os.path.join(d, x)
                try:
                    if os.stat(p).st_mtime >= since:
                        found.append(p)
                except OSError:
                    pass
                dirs.remove(x)
    sizes = {}
    for p in found:
        try:
            r = subprocess.run(["du", "-sk", p], capture_output=True, text=True, timeout=300)
            sizes[p] = int(r.stdout.split()[0]) * 1024
        except (subprocess.TimeoutExpired, IndexError, ValueError, OSError):
            pass
    return sizes


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    cwd = data.get("cwd") or os.getcwd()
    if os.path.realpath(cwd) in (os.path.realpath(HOME), "/"):
        return  # too broad to scan cheaply
    if os.fork():  # parent returns at once; child does the scan
        return
    os.setsid()
    # fully detach: drop the inherited stdio pipes so Claude Code isn't kept waiting
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        since = time.time() - 12 * 3600
        tp = data.get("transcript_path")
        if tp and os.path.exists(tp):
            st = os.stat(tp)
            since = getattr(st, "st_birthtime", st.st_mtime)
        sizes = scan(cwd, since)
        total = sum(sizes.values())
        if total < int(os.environ.get("DISKCLEAN_LEDGER_MIN_BYTES", 50 * 2**20)):
            return
        from cleanup import config  # imported here so the detached child stays light
        ledger = os.path.join(config.state_dir(), "sessions.jsonl")
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        with open(ledger, "a") as f:
            f.write(json.dumps({"t": time.time(), "session": data.get("session_id"), "cwd": cwd, "bytes": total,
                                "dirs": sorted(sizes, key=lambda p: -sizes[p])[:5]}) + "\n")
    except Exception:
        pass
    finally:
        os._exit(0)

