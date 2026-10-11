import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO, age, seq, set_config, state


def ap(env, *args, stdin=subprocess.DEVNULL):
    return subprocess.run([sys.executable, "-m", "cleanup.cli", "afterparty", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180, stdin=stdin)


def _frames(env):
    base = Path(env["HOME"]) / "Projects" / "film" / "renders"
    seq(base / "shot_v1")
    seq(base / "shot_v2")
    return base / "shot_v1"


def test_report_is_dry(env):
    old = _frames(env)
    r = ap(env, "report")
    assert r.returncode == 0 and old.exists() and "Dry run" in r.stdout


def test_clean_then_undo(env):
    old = _frames(env)
    r = ap(env, "clean", "--yes", "--tier", "3")
    assert r.returncode == 0 and not old.exists() and "afterparty undo" in r.stdout
    u = ap(env, "undo")
    assert u.returncode == 0 and "Restored 1" in u.stdout and old.exists()


def test_undo_with_nothing_to_undo(env):
    r = ap(env, "undo")
    assert r.returncode == 1 and "Nothing to undo" in r.stdout


def test_autopilot_on_needs_confirmation(env):
    r = ap(env, "autopilot", "on")
    assert r.returncode == 1 and "Nothing changed" in r.stdout
    cfg = json.loads((Path(env["AFTERPARTY_CONFIG_DIR"]) / "config.json").read_text())
    assert cfg.get("autopilot", "report") == "report"
    assert ap(env, "autopilot", "on", "--yes").returncode == 0
    assert "clean" in ap(env, "autopilot", "status").stdout


def test_weekly_reports_when_autopilot_off(env):
    old = _frames(env)
    r = ap(env, "weekly")
    assert r.returncode == 0 and old.exists()


def test_weekly_cleans_when_autopilot_on(env):
    old = _frames(env)
    set_config(env, autopilot="clean")
    ap(env, "weekly")
    assert not old.exists()


def test_concurrent_run_refused(env):
    from cleanup.plan import bridge
    _frames(env)
    with bridge.lock(str(state(env))):
        r = ap(env, "clean", "--yes")
    assert r.returncode == 3 and "Another Afterparty cleanup is running" in r.stderr


def test_live_guard_blocks_destructive_only(env, monkeypatch):
    from cleanup import commands
    monkeypatch.setattr(commands, "_guard_reason", lambda: "engine has uncommitted edits")
    assert commands.main(["clean", "--yes"], live=True) == 1
    assert commands.main(["autopilot", "status"], live=True) == 0


class _TTY:
    def isatty(self):
        return True

    def readline(self):
        return "y\n"


def test_report_with_tier_4_never_cleans(env, monkeypatch, tmp_path):
    # Final review MUST-FIX 2: `report --tier 4` must never reach the interactive cleaning path (no guard there).
    from cleanup import commands
    from cleanup.engine import core
    for k in ("HOME", "AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_TEST_TRASH_DIR"):
        monkeypatch.setenv(k, env[k])
    monkeypatch.setattr(core, "STATE", str(tmp_path / "st"))
    monkeypatch.setattr(core, "LEDGER", str(tmp_path / "st" / "sessions.jsonl"))
    f = tmp_path / "old.bin"
    f.write_text("x")
    monkeypatch.setattr(core, "tier4", lambda: [core.Item(4, "personal", str(f), "old", size=1, disposition="trash")])
    monkeypatch.setattr(core, "safe_target", lambda *a: (True, ""))
    calls = []
    monkeypatch.setattr(core, "ask_tier4", lambda *a, **k: calls.append("ask"))
    monkeypatch.setattr(core, "apply", lambda *a, **k: calls.append("apply") or 0)
    monkeypatch.setattr(commands, "_guard_reason", lambda: calls.append("guard"))
    monkeypatch.setattr(sys, "stdin", _TTY())
    assert commands.main(["report", "--tier", "4"], live=True) == 0
    assert calls == [] and f.exists()


def test_weekly_reads_autopilot_once(monkeypatch):
    # Final review MUST-FIX 2: the guard decision and the dispatch use the same autopilot value.
    from cleanup import commands
    from cleanup.engine import core
    answers = iter(["report", "clean"])
    monkeypatch.setattr(commands, "_autopilot", lambda: next(answers))
    seen = []
    monkeypatch.setattr(core, "main", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(commands, "_guard_reason", lambda: "guard says no")
    assert commands.main(["weekly"], live=True) == 0
    assert seen == [["--notify"]]
