import io
import os
import time

import pytest

from cleanup.mole import adapter as M

CLEAN = """# Mole Cleanup Preview - 2026-10-07 21:57:26
#
# How to protect files:

=== User essentials ===
/Users/u/Library/Caches/bun  # 946KB
/Users/u/Library/Caches/Google  # 94.5MB
=== Browsers ===
/Users/u/Library/Caches/Google/Chrome/Default/Cache  # 1.2GB
/Users/u/Library/Caches/Firefox  # size unknown
"""

PURGE = """→ DRY RUN MODE, No project artifacts will be removed
Purge Project Artifacts
◎ Skipped 1 project scan root because scanning did not complete:
  ~/Library/CloudStorage (status 124)
\x1b[32m✓\x1b[0m [DRY RUN] ~/Projects/app/node_modules, 917.7MB
✓ [DRY RUN] ~/Projects/tool/build, 430.2MB
✓ [DRY RUN] ~/Projects/tool/.cache/huggingface, 29KB
"""


def test_parse_size():
    assert M.parse_size("946KB") == 946 * 1024 and M.parse_size("1.5GB") == int(1.5 * 1024 ** 3)
    assert M.parse_size("size unknown") is None and M.parse_size("12B") == 12


def test_parse_clean_list_sections():
    items = M.parse_clean_list(CLEAN)
    assert [i["category"] for i in items] == ["User essentials", "User essentials", "Browsers", "Browsers"]
    assert items[0] == {"path": "/Users/u/Library/Caches/bun", "size": 946 * 1024, "category": "User essentials",
                        "source": "clean"}
    assert items[3]["size"] is None


def test_parse_purge_lines(tmp_path):
    items = M.parse_purge(M.strip_ansi(PURGE), "/Users/u")
    assert [i["path"] for i in items] == ["/Users/u/Projects/app/node_modules", "/Users/u/Projects/tool/build",
                                          "/Users/u/Projects/tool/.cache/huggingface"]
    assert items[0]["size"] == int(917.7 * 1024 ** 2) and items[0]["category"] == "Project artifacts"


def test_never_without_dry_run():
    assert M.mole_cmd("clean", "--dry-run") == ["mo", "clean", "--dry-run"]
    for bad in (("clean",), ("purge", "--yes"), ("purge",)):
        with pytest.raises(ValueError):
            M.mole_cmd(*bad)


class Hanging:
    """Fake `mo purge --dry-run`: prints its list, then waits on a prompt forever (observed real behaviour)."""
    def __init__(self, cmd, **kw):
        self.cmd = cmd; self.killed = False
        r, w = os.pipe(); os.write(w, PURGE.encode()); self.stdout = os.fdopen(r, "rb", 0); self._w = w

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True; os.close(self._w)

    def wait(self, timeout=None):
        return -9


def test_run_dry_stops_after_quiet_period_and_keeps_output():
    t = time.time()
    out = M.run_dry(["purge", "--dry-run"], popen=Hanging, quiet=1, limit=10)
    assert time.time() - t < 5 and "[DRY RUN] ~/Projects/tool/build" in out


def fake_rules(path, mtime=None, keep=(), home=None, fast=False):
    if "Keychains" in path:
        return {"class": "protected", "rule": "library", "reason": "account data", "action": "none"}
    if "node_modules" in path:
        return {"class": "regenerable", "rule": "dependency", "reason": "deps", "action": "permanent"}
    return {"class": "unknown", "rule": "unmatched", "reason": "no rule", "action": "trash"}


def test_suggestions_rechecked_by_rules_and_chrome_dropped():
    items = [{"path": "/Users/u/p/node_modules", "size": 1, "category": "Project artifacts", "source": "purge"},
             {"path": "/Users/u/Library/Keychains/x", "size": 1, "category": "Apps", "source": "clean"},
             {"path": "/Users/u/Library/Caches/Google/Chrome/Default/Cache", "size": 1, "category": "Browsers", "source": "clean"},
             {"path": "/Users/u/Library/Application Support/Google/Chrome/Profile 1", "size": 1, "category": "Browsers",
              "source": "clean"},
             {"path": "/Users/u/Library/Caches/bun", "size": 1, "category": "User essentials", "source": "clean"}]
    out = M.suggestions(items, classify_fn=fake_rules, home="/Users/u")
    assert [o["path"] for o in out] == ["/Users/u/p/node_modules", "/Users/u/Library/Keychains/x", "/Users/u/Library/Caches/bun"]
    nm, kc, bun = out
    assert nm["agree"] and nm["stageable"]
    assert not kc["agree"] and not kc["stageable"] and kc["rules"]["class"] == "protected"
    assert bun["stageable"] and bun["rules"]["class"] == "unknown"


def test_refresh_and_load_roundtrip(tmp_path):
    cl = tmp_path / "clean-list.txt"; cl.write_text(CLEAN)
    runs = []

    def run(args, **kw):
        runs.append(args)
        return PURGE if args[0] == "purge" else ""
    st = tmp_path / "mole.json"
    res = M.refresh(str(st), run=run, clean_list=str(cl), classify_fn=fake_rules, home="/Users/u")
    assert runs == [["clean", "--dry-run"], ["purge", "--dry-run"]]
    assert M.load(str(st))["items"] == res["items"] and res["finished"] > 0
    assert not any("Chrome" in i["path"] for i in res["items"])
    assert M.load(str(tmp_path / "missing.json")) == {"finished": None, "items": [], "errors": []}


class SlowStart(Hanging):
    """Like `mo purge --dry-run` on a big disk: scans silently for a while before printing anything."""
    def __init__(self, cmd, **kw):
        import threading
        self.cmd = cmd; self.killed = False
        r, w = os.pipe(); self.stdout = os.fdopen(r, "rb", 0); self._w = w
        os.write(w, "→ DRY RUN MODE, No project artifacts will be removed\nPurge Project Artifacts\n".encode())  # header first

        def later():
            time.sleep(2)
            if not self.killed:
                os.write(w, PURGE.encode())
        threading.Thread(target=later, daemon=True).start()


def test_run_dry_waits_for_first_result_before_quiet_timer():
    out = M.run_dry(["purge", "--dry-run"], popen=SlowStart, quiet=1, limit=10)
    assert "[DRY RUN] ~/Projects/tool/build" in out


def test_quiet_kill_takes_mole_children_with_it(tmp_path):
    # Review I2: `mo` is a shell script that spawns find/du; killing only `mo` orphans them.
    import subprocess
    pidfile = tmp_path / "pid"
    script = f"sleep 60 & echo $! > {pidfile}; echo '✓ [DRY RUN] ~/x/node_modules, 1MB'; wait"

    def popen(cmd, **kw):
        return subprocess.Popen(["bash", "-c", script], **kw)
    out = M.run_dry(["purge", "--dry-run"], popen=popen, quiet=1, limit=10)
    assert "[DRY RUN]" in out
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
