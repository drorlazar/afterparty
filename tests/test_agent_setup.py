import json
import os
import stat

import pytest

from cleanup.agent import runner as R
from cleanup.agent import setup as S


class Done:
    def __init__(self, out="", code=0, err=""):
        self.stdout, self.returncode, self.stderr = out, code, err


def exe(p):
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text("#!/bin/sh\n"); p.chmod(0o755); return str(p)


def test_find_claude_in_known_install_locations(tmp_path):
    # the official installer puts it in ~/.local/bin, which launchd's PATH doesn't include
    c = exe(tmp_path / ".local/bin/claude")
    assert S.find_claude(path="/usr/bin:/bin", home=str(tmp_path), shell_run=lambda *a, **k: Done(""), system_dirs=()) == c


def test_find_claude_asks_the_login_shell_last(tmp_path):
    c = exe(tmp_path / "weird/place/claude")
    found = S.find_claude(path="/usr/bin", home=str(tmp_path / "nohome"), shell_run=lambda *a, **k: Done(c + "\n"), system_dirs=())
    assert found == c


def test_find_claude_none_when_missing(tmp_path):
    assert S.find_claude(path="/usr/bin", home=str(tmp_path), shell_run=lambda *a, **k: Done(""), system_dirs=()) is None


def test_status_signed_in_with_subscription():
    def run(cmd, **kw):
        if cmd[1:] == ["--version"]:
            return Done("2.1.293 (Claude Code)\n")
        return Done(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    st = S.status("/x/claude", run=run)
    assert st == {"installed": True, "path": "/x/claude", "version": "2.1.293", "signed_in": True,
                  "method": "Claude subscription", "problem": None}


def test_status_signed_out_explains_next_step():
    def run(cmd, **kw):
        return Done("2.1.293 (Claude Code)") if cmd[1:] == ["--version"] else Done(json.dumps({"loggedIn": False, "authMethod": "none"}))
    st = S.status("/x/claude", run=run)
    assert st["installed"] and st["signed_in"] is False and "/login" in st["problem"]


def test_status_not_installed():
    st = S.status(None)
    assert st["installed"] is False and "Install Claude Code" in st["problem"]


def test_status_api_key_label():
    def run(cmd, **kw):
        return Done("2.1.0") if cmd[1:] == ["--version"] else Done(json.dumps({"loggedIn": True, "authMethod": "api_key"}))
    assert S.status("/x/claude", run=run)["method"] == "Anthropic API key"


def test_command_uses_resolved_binary():
    assert R.command("sonnet", claude="/opt/x/claude")[0] == "/opt/x/claude"


def test_settings_roundtrip_and_validation(tmp_path):
    f = str(tmp_path / "settings.json")
    assert S.load_settings(f)["agent_model"] == "sonnet"
    S.save_settings(f, {"agent_model": "opus"})
    assert S.load_settings(f)["agent_model"] == "opus"
    with pytest.raises(ValueError):
        S.save_settings(f, {"agent_model": "gpt-5"})


def test_default_question_setting(tmp_path):
    f = str(tmp_path / "settings.json")
    assert S.load_settings(f)["agent_default_question"] == S.DEFAULT_QUESTION
    S.save_settings(f, {"agent_model": "opus"})
    S.save_settings(f, {"agent_default_question": "  What's safe to delete in here?  "})
    d = S.load_settings(f)
    assert d["agent_default_question"] == "What's safe to delete in here?" and d["agent_model"] == "opus"
    S.save_settings(f, {"agent_default_question": ""})
    assert S.load_settings(f)["agent_default_question"] == S.DEFAULT_QUESTION
    with pytest.raises(ValueError):
        S.save_settings(f, {"agent_default_question": "x" * 501})
