"""Run headless Claude Code in a read-only sandbox and return a validated, sanitized answer (or an error card)."""
import json
import os
import signal
import subprocess

from cleanup.agent.context import prompt
from cleanup.agent.schema import SCHEMA, sanitize, validate

MODEL = os.environ.get("CLEANUP_AGENT_MODEL", "sonnet")
TIMEOUT = 180
# Only commands with no flag that writes. (`git log --output=F` and `file -C` write files — deliberately absent.)
# No git: `git status` runs repo-configured programs (core.fsmonitor) — the agent reads .git with Read instead.
NAMES_ONLY_TOOLS = ["Glob", "Bash(ls:*)", "Bash(du:*)", "Bash(stat:*)"]
READ_TOOLS = ["Read", "Grep"]
ALLOWED_TOOLS = READ_TOOLS + NAMES_ONLY_TOOLS  # everything the agent can ever be allowed (with read_files on)
# Never let a planted instruction read credentials and send them to the model.
SECRETS = ["~/.ssh/**", "~/.aws/**", "~/.gnupg/**", "~/.kube/**", "~/.docker/**", "~/.netrc", "**/.netrc", "~/.npmrc",
           "~/.pypirc", "~/Library/Keychains/**", "~/Library/Cookies/**", "~/.config/gh/**", "~/.config/**/auth*",
           "~/.config/**/*credentials*", "~/.claude/**", "~/.claude.json", "~/.codex/**", "**/.env*", "**/.envrc",
           "**/secrets.env", "**/*credentials*", "**/*secret*", "**/*token*", "**/*.pem", "**/*.key", "**/id_rsa*",
           "**/id_ed25519*"]
DENIED = [f"{tool}({pat})" for pat in SECRETS for tool in ("Read", "Grep", "Glob")]


def command(model=MODEL, claude="claude", read_files=False):
    """argv for `claude -p` (prompt appended last). dontAsk = anything not allow-listed is denied, never prompted.
    Default is names and sizes only; reading file contents needs the user's separate opt-in."""
    allowed = NAMES_ONLY_TOOLS + (READ_TOOLS if read_files else [])
    tools = "Glob,Bash" + (",Read,Grep" if read_files else "")
    return [claude, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--model", model,
            "--tools", tools, "--allowedTools", " ".join(allowed),
            "--disallowedTools", " ".join(DENIED),
            "--permission-mode", "dontAsk", "--no-session-persistence", "--strict-mcp-config",
            "--setting-sources", "", "--max-budget-usd", "1"]


def run_group(cmd, cwd=None, timeout=TIMEOUT, **_):
    """subprocess.run, but a timeout kills the whole process group (claude and anything it started), not just claude."""
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def ask(ctx, question=None, history=(), run_fn=run_group, timeout=TIMEOUT, model=MODEL, claude="claude", read_files=False):
    cwd = ctx["path"] if ctx["kind"] == "dir" else os.path.dirname(ctx["path"])
    if not os.path.isdir(cwd) or not os.access(cwd, os.R_OK | os.X_OK):
        return {"ok": False, "error": "Afterparty can't open that folder (it may have moved, or macOS blocks access)."}
    try:
        r = run_fn(command(model, claude, read_files) + [prompt(ctx, question, history)], cwd=cwd, capture_output=True, text=True,
                   timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        if not os.path.isdir(cwd):
            return {"ok": False, "error": "Afterparty can't open that folder (it may have moved, or macOS blocks access)."}
        return {"ok": False, "error": "Claude Code isn't installed (no `claude` on PATH)."}
    except PermissionError:
        return {"ok": False, "error": "Afterparty can't open that folder (it may have moved, or macOS blocks access)."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"The agent took too long (over {timeout}s) and was stopped."}
    try:
        d = json.loads(r.stdout)
    except (ValueError, TypeError):
        return {"ok": False, "error": "The agent's reply was unreadable. " + (r.stderr or "")[-300:]}
    if d.get("is_error") or r.returncode != 0:
        msg = str(d.get("result") or r.stderr or "")
        if any(w in msg.lower() for w in ("authenticate", "oauth", "not logged in", "/login", "invalid api key")):
            return {"ok": False, "setup": True, "error": "Claude Code isn't signed in. Open Terminal, run `claude`, type /login "
                                                         "and sign in — then ask again."}
        return {"ok": False, "error": "The agent failed: " + str(d.get("result") or r.stderr or "unknown error")[:400]}
    ans = d.get("structured_output")
    err = validate(ans)
    if err:
        return {"ok": False, "error": f"The agent's answer didn't match the expected shape ({err}) and was discarded."}
    return {"ok": True, "answer": sanitize(ans, ctx["path"]), "cost": d.get("total_cost_usd")}
