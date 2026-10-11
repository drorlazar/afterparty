"""Entry point: python -m cleanup.cli <command> [args]."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESTRUCTIVE = ("--apply", "--auto")


def live_guard(args, repo=REPO, env=None):
    """Live wrappers run straight from the repo checkout. Refuse deleting runs while the
    engine has uncommitted edits (or git can't tell), so half-edited code never deletes.
    Returns a reason string to refuse, or None to allow."""
    if not any(a in DESTRUCTIVE for a in args):
        return None
    try:
        r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "cleanup"],
                           capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot check engine state ({e})"
    if r.returncode != 0:
        return "cannot check engine state (git status failed)"
    if r.stdout.strip():
        return "engine has uncommitted edits in " + str(repo) + " — commit or stash them first"
    return None


def main(argv=None, live=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: cleanup.cli {afterparty,diskclean,session-end,serve,app} [args]")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "diskclean":
        from cleanup.engine import core
        why = live_guard(rest) if live else None
        if why:
            print(f"diskclean: refusing to delete — {why}", file=sys.stderr)
            core.notify("diskclean skipped", why)
            return 1
        return core.main(rest) or 0
    if cmd == "session-end":
        from cleanup.hooks import session_end
        return session_end.main() or 0
    if cmd == "serve":
        import argparse
        from cleanup.server.app import serve
        ap = argparse.ArgumentParser(prog="cleanup serve")
        ap.add_argument("--port", type=int, default=0)
        ap.add_argument("--token")
        ap.add_argument("--roots", nargs="+", default=["/"])
        ap.add_argument("--live", action="store_true")
        a = ap.parse_args(rest)
        server, _ = serve(port=a.port, token=a.token or os.environ.pop("CLEANUP_TOKEN", None), roots=a.roots, live=a.live)
        server.serve_forever()
        return 0
    if cmd == "app":
        from cleanup.launcher import ensure_server, stop_server
        if "--stop" in rest:  # the app is quitting: stop its own server so the next launch starts a fresh one
            ok, why = stop_server()
            if not ok:
                print("couldn't stop the Afterparty server: %s" % why, file=sys.stderr)
            return 0 if ok else 1
        return print(ensure_server(open_browser="--no-open" not in rest,
                                   mode="launchd" if "--launchd" in rest else "spawn")) or 0
    if cmd == "afterparty":
        from cleanup import commands
        return commands.main(rest, live=live)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


def run():
    """Entry for the bin/ shims, which start Python isolated (-I -B) so a `cleanup` folder in the caller's
    working directory can never be imported instead of ours: python -I -B -c ENTRY REPO <command> [args]."""
    return main(sys.argv[1:], live=os.environ.get("AFTERPARTY_LIVE") == "1")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], live=os.environ.get("AFTERPARTY_LIVE") == "1"))
