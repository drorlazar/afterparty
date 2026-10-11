import json
import os
import stat
import subprocess
import sys
import uuid

import pytest

from cleanup import launcher


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("AFTERPARTY_REAL_LAUNCHD_TEST") != "1",
                    reason="loads a real job into your launchd domain; opt in with AFTERPARTY_REAL_LAUNCHD_TEST=1")
def test_launchd_mode_starts_private_job_and_stops(tmp_path, env, monkeypatch):
    monkeypatch.setenv("HOME", env["HOME"])
    label = f"io.github.drorlazar.afterparty.test-{uuid.uuid4().hex[:8]}"
    state = tmp_path / "server.json"
    try:
        url = launcher.ensure_server(open_browser=False, state_path=str(state), mode="launchd", label=label,
                                     python=sys.executable)
        s = json.loads(state.read_text())
        plist = tmp_path / f"{label}.plist"
        assert s["mode"] == "launchd" and s["label"] == label and str(s["port"]) in url
        assert stat.S_IMODE(os.stat(plist).st_mode) == 0o600
        assert s["token"] not in subprocess.run(["ps", "-o", "command=", "-p", str(s["pid"])],
                                                capture_output=True, text=True).stdout
        assert launcher.ensure_server(open_browser=False, state_path=str(state), mode="launchd", label=label,
                                      python=sys.executable) == url  # reused
    finally:
        launcher.stop_server(state_path=str(state), label=label)
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    assert label not in out
