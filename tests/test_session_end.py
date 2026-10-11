import json
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import REPO, state


def test_hook_returns_fast_and_writes_ledger(env, tmp_path):
    env["DISKCLEAN_LEDGER_MIN_BYTES"] = "1"
    proj = tmp_path / "proj"; proj.mkdir()
    transcript = tmp_path / "t.jsonl"; transcript.write_text("")
    time.sleep(1.1)  # renders/ must be newer than the transcript's birth time
    (proj / "renders").mkdir(); (proj / "renders/a.bin").write_bytes(b"x" * 8192)
    payload = json.dumps({"cwd": str(proj), "session_id": "s1", "transcript_path": str(transcript)})
    t0 = time.time()
    r = subprocess.run([sys.executable, "-m", "cleanup.cli", "session-end"], input=payload, text=True,
                       capture_output=True, cwd=REPO, env=env, timeout=30)
    assert r.returncode == 0
    assert time.time() - t0 < 1.5, "hook must return immediately (child is detached)"
    ledger = state(env) / "sessions.jsonl"
    for _ in range(50):
        if ledger.exists():
            break
        time.sleep(0.1)
    row = json.loads(ledger.read_text().splitlines()[-1])
    assert row["session"] == "s1" and row["dirs"][0].endswith("renders")


def test_hook_ignores_home_and_bad_json(env, tmp_path):
    for payload in ("not json", json.dumps({"cwd": env["HOME"]})):
        r = subprocess.run([sys.executable, "-m", "cleanup.cli", "session-end"], input=payload, text=True,
                           capture_output=True, cwd=REPO, env=env, timeout=30)
        assert r.returncode == 0
    assert not (state(env) / "sessions.jsonl").exists()


def test_hook_treats_symlink_to_home_as_home(env, tmp_path):
    # Scanning "home" via a symlinked path would walk the whole home folder.
    env["DISKCLEAN_LEDGER_MIN_BYTES"] = "1"
    if env["HOME"].startswith("/private/var/"):  # make HOME an unresolved path, as in real life
        env["HOME"] = env["HOME"][len("/private"):]
    link = tmp_path / "home-link"
    link.symlink_to(env["HOME"], target_is_directory=True)
    transcript = tmp_path / "t.jsonl"; transcript.write_text("")
    time.sleep(1.1)
    (Path(env["HOME"]) / "renders").mkdir(); (Path(env["HOME"]) / "renders/a.bin").write_bytes(b"x" * 8192)
    payload = json.dumps({"cwd": str(link), "session_id": "s2", "transcript_path": str(transcript)})
    subprocess.run([sys.executable, "-m", "cleanup.cli", "session-end"], input=payload, text=True,
                   capture_output=True, cwd=REPO, env=env, timeout=30)
    time.sleep(2)
    assert not (state(env) / "sessions.jsonl").exists()
