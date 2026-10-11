"""Is Claude Code available to Afterparty? Find the `claude` binary (launchd's PATH is minimal), check its sign-in,
and keep the user's agent settings."""
import json
import os
import shutil
import subprocess

from cleanup import config, fsops

MODELS = ("sonnet", "opus", "haiku")
DEFAULT_QUESTION = "What is this, what created it, and is it safe to remove? Point out anything worth keeping."
DEFAULTS = {"agent_model": os.environ.get("CLEANUP_AGENT_MODEL", "sonnet"), "agent_default_question": DEFAULT_QUESTION}
INSTALL_HELP = "Install Claude Code (see the steps below), sign in once, then press Check again."
LOGIN_HELP = "Claude Code is installed but not signed in. Open Terminal, run claude, type /login and sign in."


SYSTEM_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _candidates(home, system_dirs):
    return [f"{home}/.local/bin/claude", f"{home}/.claude/local/claude", *[f"{d}/claude" for d in system_dirs],
            f"{home}/.npm-global/bin/claude", f"{home}/.bun/bin/claude", f"{home}/.volta/bin/claude",
            f"{home}/.nvm/current/bin/claude"]


def _runnable(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def find_claude(path=None, home=None, shell_run=subprocess.run, system_dirs=SYSTEM_DIRS):
    """Absolute path of `claude`, or None. PATH first, then the usual install locations, then the login shell."""
    home = home or os.path.expanduser("~")
    hit = shutil.which("claude", path=path if path is not None else os.environ.get("PATH", ""))
    if _runnable(hit):
        return hit
    for c in _candidates(home, system_dirs):
        if _runnable(c):
            return c
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        r = shell_run([shell, "-l", "-c", "command -v claude"], capture_output=True, text=True, timeout=8,
                      stdin=subprocess.DEVNULL)
        line = (r.stdout or "").strip().splitlines()
        if line and os.path.isabs(line[-1]) and _runnable(line[-1]):
            return line[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def status(claude, run=subprocess.run):
    """What the Agent tab shows before anyone asks a question."""
    if not claude:
        return {"installed": False, "path": None, "version": None, "signed_in": False, "method": None,
                "problem": INSTALL_HELP}
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    try:
        v = run([claude, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, env=env)
        version = (v.stdout or "").split()[0] if (v.stdout or "").strip() else None
        a = run([claude, "auth", "status"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, env=env)
        auth = json.loads(a.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"installed": True, "path": claude, "version": None, "signed_in": None, "method": None,
                "problem": "Couldn't check Claude Code's sign-in. Try running claude in Terminal."}
    signed = bool(auth.get("loggedIn"))
    m = str(auth.get("authMethod") or "")
    method = None if not signed else ("Anthropic API key" if "api" in m.lower() or "key" in m.lower() else "Claude subscription")
    return {"installed": True, "path": claude, "version": version, "signed_in": signed, "method": method,
            "problem": None if signed else LOGIN_HELP}


def settings_file():
    """The agent settings file, resolved at call time (never frozen at import, so tests and a changed
    AFTERPARTY_CONFIG_DIR are always honoured)."""
    return os.path.join(config.config_dir(), "agent.json")


def load_settings(path=None):
    path = path or settings_file()
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    out = dict(DEFAULTS)
    if d.get("agent_model") in MODELS:
        out["agent_model"] = d["agent_model"]
    q = d.get("agent_default_question")
    if isinstance(q, str) and q.strip():
        out["agent_default_question"] = q.strip()[:500]
    return out


def save_settings(path, new):
    """Partial update: only the keys given are changed (each validated). `path` None: settings_file()."""
    path = path or settings_file()
    cur = load_settings(path)
    if "agent_model" in new:
        if new["agent_model"] not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        cur["agent_model"] = new["agent_model"]
    if "agent_default_question" in new:
        q = new["agent_default_question"]
        if not isinstance(q, str) or len(q.strip()) > 500:
            raise ValueError("the default question must be text of at most 500 characters")
        cur["agent_default_question"] = q.strip() or DEFAULT_QUESTION
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fsops.replace_owned(path, json.dumps(cur).encode("utf-8"))  # R14
    return cur
