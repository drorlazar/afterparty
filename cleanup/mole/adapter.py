"""Mole (tw93/mole) as a *suggestion source only*: dry-run output → suggestions re-checked by our rules."""
import json
import os
import re
import select
import signal
import subprocess
import time

from cleanup import fsops
from cleanup.engine.classify import classify

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
CHROME = re.compile(r"(?i)/Google/Chrome(/|$)")
CLEAN_LIST = os.path.expanduser("~/.config/mole/clean-list.txt")


def strip_ansi(s):
    return ANSI.sub("", s)


def parse_size(s):
    m = re.match(r"^\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*$", s or "", re.I)
    return int(float(m.group(1)) * UNITS[m.group(2).upper()]) if m else None


def parse_clean_list(text):
    out, cat = [], "Other"
    for line in text.splitlines():
        line = line.rstrip()
        m = re.match(r"^===\s*(.+?)\s*===$", line)
        if m:
            cat = m.group(1); continue
        if not line.startswith("/"):
            continue
        path, _, size = line.partition("  # ")
        out.append({"path": path.rstrip(), "size": parse_size(size), "category": cat, "source": "clean"})
    return out


def parse_purge(text, home):
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*✓\s*\[DRY RUN\]\s+(.+),\s*([\d.]+\s*[KMGT]?B)\s*$", line)
        if m:
            p = m.group(1)
            p = home.rstrip("/") + p[1:] if p.startswith("~/") else p
            out.append({"path": p, "size": parse_size(m.group(2)), "category": "Project artifacts", "source": "purge"})
    return out


def mole_cmd(*args):
    if "--dry-run" not in args or "--yes" in args:
        raise ValueError("Afterparty only ever runs Mole with --dry-run")
    return ["mo", *args]


def run_dry(args, popen=subprocess.Popen, quiet=30, limit=600):
    """Run a Mole dry-run, return its text. Mole may scan silently for minutes, then wait on a prompt after listing —
    so stop `quiet` seconds after output goes silent, or after `limit` seconds in total."""
    p = popen(mole_cmd(*args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              env=dict(os.environ, NO_COLOR="1", TERM="dumb"), start_new_session=True)
    chunks, start, last, started = [], time.time(), time.time(), False
    fd = p.stdout.fileno()
    try:
        while p.poll() is None:
            now = time.time()
            if (started and now - last > quiet) or now - start > limit:  # quiet timer starts at the first result
                break
            ready, _, _ = select.select([fd], [], [], 0.25)
            if ready:
                b = os.read(fd, 65536)
                if not b:
                    break
                chunks.append(b); last = time.time()
                started = started or b"[DRY RUN]" in b or b"===" in b
        while True:  # drain what's buffered
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
    finally:
        try:  # take Mole's find/du/subshells with it, not just the `mo` script
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
    return strip_ansi(b"".join(chunks).decode("utf-8", "replace"))


def suggestions(items, classify_fn=classify, keep=(), home=None):
    out, seen = [], set()
    for i in items:
        p = os.path.normpath(i["path"])
        if CHROME.search(p) or p in seen:
            continue
        seen.add(p)
        v = classify_fn(p, keep=keep, home=home, fast=True)
        rules = {k: v.get(k) for k in ("class", "rule", "reason", "action")}
        protected = v.get("class") == "protected" or v.get("action") == "none"
        out.append(dict(i, path=p, rules=rules, agree=not protected, stageable=not protected))
    return out


def refresh(state_file, run=run_dry, clean_list=CLEAN_LIST, classify_fn=classify, keep=(), home=None):
    home = home or os.path.expanduser("~")
    items, errors = [], []
    before = os.path.getmtime(clean_list) if os.path.exists(clean_list) else 0
    try:
        run(["clean", "--dry-run"])
        if os.path.exists(clean_list) and (os.path.getmtime(clean_list) >= before):
            with open(clean_list, encoding="utf-8", errors="replace") as f:
                items += parse_clean_list(f.read())
    except Exception as e:  # Mole missing or broken: the lens shows the error, everything else keeps working
        errors.append(f"mo clean --dry-run: {e}")
    try:
        items += parse_purge(run(["purge", "--dry-run"]), home)
    except Exception as e:
        errors.append(f"mo purge --dry-run: {e}")
    res = {"finished": time.time(), "items": suggestions(items, classify_fn, keep, home), "errors": errors}
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    try:
        fsops.replace_owned(state_file, json.dumps(res).encode("utf-8"))  # R14
    except fsops.Conflict:
        res["errors"].append("the saved Mole results changed while they were written, so they were left as they are")
    return res


def load(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"finished": None, "items": [], "errors": []}
