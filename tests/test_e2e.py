import glob
import os
import subprocess
import threading
from pathlib import Path

import pytest

from cleanup.scanner.scan import scan
from cleanup.scanner.store import Store
from cleanup.server import app as appmod

import json
import pwd

REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir  # HOME gets redirected in the test; browsers live in the real one
PW = sorted((p for p in glob.glob(f"{REAL_HOME}/.npm/_npx/*/node_modules/playwright/package.json")
             if "alpha" not in json.load(open(p)).get("version", "")), key=lambda p: json.load(open(p))["version"])
REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not PW, reason="playwright not installed")
def test_explorer_e2e(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home")); (tmp_path / "home").mkdir()
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))
    import time as _t
    root = tmp_path / "home" / "disk"  # under HOME so classify sees a user path, not /private "system"
    (tmp_path / "config").mkdir()
    # vault: on the keep list, so classify really marks it protected (the protected-tooltip check needs one)
    (tmp_path / "config/config.json").write_text(json.dumps({"version": 1, "roots": [str(root)],
                                                             "keep": [str(root / "vault")]}))
    big = root / "big"; big.mkdir(parents=True)
    for i in range(2500):
        (big / f"f{i:05d}x.bin").write_bytes(b"x" * (i % 7 * 1000 + 10))
    (root / "proj/node_modules").mkdir(parents=True); (root / "proj/node_modules/a.js").write_bytes(b"y" * 50_000)
    (root / "renders/v1").mkdir(parents=True)
    for i in range(1, 41):
        (root / f"renders/v1/frame_{i:04d}.png").write_bytes(b"x" * 10)
    (root / "notes").mkdir(); (root / "notes/doc.txt").write_bytes(b"n" * 4000)  # unclassified → rules won't delete
    from tests.test_preview import PNG
    (root / "notes/pic.png").write_bytes(PNG)
    (root / "notes/keep.txt").write_bytes(b"k" * 3000)
    (root / "vault").mkdir(); (root / "vault/deed.pdf").write_bytes(b"v" * 2000)
    locked = root / "locked"; locked.mkdir(); (locked / "secret.bin").write_bytes(b"z" * 100)
    store = Store(str(tmp_path / "db.sqlite")); store.save(scan(str(root), skip=()))
    locked.chmod(0)  # readable at scan time, unreadable when the UI lists it → 403
    monkeypatch.setattr(appmod, "has_full_disk_access", lambda: False)
    mole_state = tmp_path / "mole.json"
    mole_state.write_text(json.dumps({"finished": _t.time(), "errors": [], "items": [
        {"path": str(root / "proj/node_modules"), "size": 50000, "category": "Project artifacts", "source": "purge",
         "rules": {"class": "regenerable", "rule": "dependency", "reason": "deps", "action": "permanent"}, "agree": True, "stageable": True},
        {"path": str(root / "locked"), "size": 100, "category": "Apps", "source": "clean",
         "rules": {"class": "protected", "rule": "library", "reason": "account data", "action": "none"}, "agree": False, "stageable": False}]}))
    server, app = appmod.serve(port=0, store=store, roots=(str(root),), scan_skip=())
    orig = server.RequestHandlerClass.do_POST

    agent_state = {"installed": True, "path": "/x/claude", "version": "2.1.293", "signed_in": True,
                   "method": "Claude subscription", "problem": None}
    app.agent_status_fn = lambda: dict(agent_state)
    app.agent_claude_fn = lambda refresh=False: "/x/claude" if agent_state["signed_in"] else None
    app.agent_settings_file = str(tmp_path / "agent-settings.json")

    def do_POST(h):  # test-only hooks
        if h.path == "/__test_agent_signed_out":
            agent_state.update(signed_in=False, method=None, problem="Claude Code is installed but not signed in.")
            h.send_response(204); h.end_headers(); return
        if h.path == "/__test_shutdown":
            h.send_response(204); h.end_headers()

            def stop():  # close the socket too, so later requests fail like a real dead server
                server.shutdown(); server.server_close()
            threading.Thread(target=stop).start(); return
        return orig(h)
    server.RequestHandlerClass.do_POST = do_POST
    trash_bin = tmp_path / "Trash"; trash_bin.mkdir()

    def fake_trash(paths):  # never touch the real Trash from tests
        import shutil as _sh
        out = {}
        for p_ in paths:
            dst = trash_bin / os.path.basename(p_); _sh.move(p_, dst); out[p_] = str(dst)
        return out
    app.trash_fn = fake_trash
    app.mole_state = str(mole_state); app.mole_available = lambda: True
    def fake_agent(cmd, **kw):  # never call the real claude from tests
        target = kw["cwd"]
        ans = {"verdict": "safe-to-delete", "confidence": 0.9, "summary": "Build output of proj.", "evidence": ["has a.js"],
               "proposed": [{"path": os.path.join(target, "node_modules"), "action": "delete", "reason": "deps"}]}
        return type("D", (), {"stdout": json.dumps({"is_error": False, "structured_output": ans}), "returncode": 0, "stderr": ""})()
    app.agent_run_fn = fake_agent
    from cleanup.agent.cache import Cache
    app.agent_cache = Cache(str(tmp_path / "agentcache"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{app.port}/?key={app.token}"
    env = dict(os.environ, PWMOD=os.path.dirname(PW[-1]),
               PLAYWRIGHT_BROWSERS_PATH=f"{REAL_HOME}/Library/Caches/ms-playwright")
    r = subprocess.run(["node", str(REPO / "tests-e2e/explorer.cjs"), url], capture_output=True, text=True,
                       timeout=180, env=env)
    print(r.stdout)
    locked.chmod(0o700)
    assert r.returncode == 0, r.stdout + r.stderr
