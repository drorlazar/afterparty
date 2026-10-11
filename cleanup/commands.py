"""`afterparty` — the public command. Every deleting path runs core -> bridge -> executor."""
import argparse
import glob
import os
import sys

from cleanup import config


def _guard_reason():
    from cleanup.cli import live_guard
    return live_guard(["--apply"])


def _autopilot():
    try:
        return config.load()[0]["autopilot"]
    except config.ConfigError:
        return "report"  # core refuses and explains on bad settings


def _confirm(prompt):
    if not sys.stdin.isatty():
        return False
    return input(prompt + " Type 'yes' to continue: ").strip().lower() == "yes"


def _undo(run_id):
    from cleanup.plan.undo import undo
    jdir = os.path.join(config.state_dir(), "journal")
    if run_id:
        path = os.path.join(jdir, run_id + ".jsonl")
        if not run_id.replace("-", "").isalnum() or not os.path.exists(path):
            print(f"No cleanup called {run_id}.")
            return 1
    else:
        runs = sorted(glob.glob(os.path.join(jdir, "*.jsonl")), key=os.path.getmtime)
        if not runs:
            print("Nothing to undo.")
            return 1
        path = runs[-1]
    from cleanup.plan import bridge
    try:
        with bridge.lock(config.state_dir()):  # never undo while a cleanup is moving things
            out = undo(path)
    except bridge.Busy as e:
        print(str(e), file=sys.stderr)
        return 3
    if not (out["restored"] or out["conflict"] or out["refused"] or out["missing"]):
        print("Nothing to undo: that cleanup moved nothing to the Trash, or it was already undone.")
        return 1
    print(f"Restored {len(out['restored'])} item(s) from the Trash.")
    for key, why in (("conflict", "something new is already at the original place"),
                     ("missing", "no longer in the Trash"), ("refused", "the Trash copy changed")):
        for e in out[key]:
            print(f"  not restored ({why}): {e['path']}")
    return 0 if not (out["conflict"] or out["refused"]) else 1


def _autopilot_cmd(state, yes):
    try:
        cfg, _ = config.load()
    except config.ConfigError as e:
        print(f"Afterparty settings are invalid ({e}).", file=sys.stderr)
        return 2
    if state == "status":
        print("Autopilot: " + ("cleans every Sunday" if cfg["autopilot"] == "clean" else
                               "reports every Sunday (cleaning is off)") + f" [{cfg['autopilot']}]")
        return 0
    want = "clean" if state == "on" else "report"
    if want == "clean" and not (yes or _confirm(
            "Every Sunday, Afterparty will clear caches, dependency folders and expired .scratch runs on its own, "
            "and move old frame versions and stale temp files to the Trash.")):
        print("Nothing changed. Run `afterparty autopilot on --yes` to turn it on.")
        return 1
    try:
        config.update(lambda c: c.update(autopilot=want))
    except config.ConfigError as e:
        print(f"Afterparty settings are invalid ({e}).", file=sys.stderr)
        return 2
    print("Autopilot is " + ("on: it cleans every Sunday." if want == "clean" else "off: it only reports."))
    return 0


def _doctor():
    """Read-only: the recorded interpreter, whether launchd-started it has Full Disk Access, and what to add."""
    from cleanup import fda
    try:
        python, src = config.load()[0]["python"], "settings"
    except config.ConfigError as e:
        python, src = "", "settings unreadable: %s" % e
    if not python:
        python, src = sys.executable, src if src != "settings" else "none recorded; this interpreter"
    word = {True: "yes", False: "no", None: "couldn't tell"}
    print("Afterparty doctor (changes nothing)")
    print("  Python the scheduled jobs run: %s (%s)" % (python, src))
    granted = fda.launchd_has_fda(python)
    print("  Full Disk Access when launchd starts it: %s" % word[granted])
    path, which = fda.fda_hint(python)
    print("  Path to add under Full Disk Access: %s%s" % (path, " (%s)" % which if which else ""))
    got = fda._probe(python)
    apps = fda.apple_apps(got[1]) if got else []
    if len(apps) > 1:
        print("  Apple Python.app copies on this Mac (System Settings shows each as \"Python.app\"):")
        for kind, app in apps:
            ok = fda.launchd_has_fda(os.path.join(app, "Contents", "MacOS", "Python"))
            print("    %s: %s - Full Disk Access: %s" % ("Command Line Tools" if kind == "clt" else "Xcode", app,
                                                         word[ok]))
    if granted is not True:
        print("  Fix: System Settings > Privacy & Security > Full Disk Access > + > Cmd-Shift-G > paste the path "
              "above > enable it.")
    return 0 if granted is True else 1


def main(argv, live=False):
    from cleanup.engine import core
    ap = argparse.ArgumentParser(prog="afterparty", description="Clean up after your agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="show what can be cleaned (deletes nothing)")
    r.add_argument("--notify", action="store_true")
    r.add_argument("--tier")
    r.add_argument("--days", type=int)
    c = sub.add_parser("clean", help="clean after showing the list")
    c.add_argument("--yes", action="store_true")
    c.add_argument("--tier")
    t = sub.add_parser("tidy", help="clean one finished task's folder")
    t.add_argument("folder")
    t.add_argument("--apply", action="store_true")
    t.add_argument("--yes", action="store_true")
    t.add_argument("--only")
    t.add_argument("--skip")
    u = sub.add_parser("undo", help="put the last cleanup's Trash moves back")
    u.add_argument("run_id", nargs="?")
    p = sub.add_parser("autopilot", help="turn Sunday cleaning on or off")
    p.add_argument("state", choices=["on", "off", "status"])
    p.add_argument("--yes", action="store_true")
    sub.add_parser("weekly", help="(scheduled) Sunday run")
    sub.add_parser("check-space", help="(scheduled) warn when free space is low")
    sub.add_parser("map", help="open the disk map")
    x = sub.add_parser("uninstall", help="remove what the installer added (keeps settings and history unless --purge)")
    x.add_argument("--purge", action="store_true")
    x.add_argument("--yes", action="store_true")
    x.add_argument("--dry-run", action="store_true")
    sub.add_parser("update", help="update from the official repository, then re-install")
    sub.add_parser("doctor", help="check Full Disk Access for the recorded Python (changes nothing)")
    a = ap.parse_args(argv)

    weekly_mode = _autopilot() if a.cmd == "weekly" else None  # read once: the guard and the dispatch agree
    destructive = a.cmd == "clean" or (a.cmd == "tidy" and a.apply) or weekly_mode == "clean"
    if live and destructive:
        why = _guard_reason()
        if why:
            print(why, file=sys.stderr)
            core.notify("Afterparty skipped", why)
            return 1

    if a.cmd == "report":
        args = (["--notify"] if a.notify else []) + (["--tier", a.tier] if a.tier else []) + \
               (["--days", str(a.days)] if a.days is not None else [])
        return core.main(args) or 0
    if a.cmd == "clean":
        return core.main(["--apply"] + (["--yes"] if a.yes else []) + (["--tier", a.tier] if a.tier else [])) or 0
    if a.cmd == "tidy":
        args = ["tidy", a.folder] + (["--apply"] if a.apply else []) + (["--yes"] if a.yes else []) + \
               (["--only", a.only] if a.only else []) + (["--skip", a.skip] if a.skip else [])
        return core.main(args) or 0
    if a.cmd == "undo":
        return _undo(a.run_id)
    if a.cmd == "autopilot":
        return _autopilot_cmd(a.state, a.yes)
    if a.cmd == "weekly":
        return core.main(["--auto"] if weekly_mode == "clean" else ["--notify"]) or 0
    if a.cmd == "check-space":
        try:
            gb = config.load()[0]["low_space_gb"]
        except config.ConfigError:
            gb = config.DEFAULTS["low_space_gb"]
        return core.main(["--check-space", str(gb)]) or 0
    if a.cmd == "map":
        from cleanup.launcher import ensure_server
        print(ensure_server(mode="launchd"))
        return 0
    if a.cmd == "uninstall":
        from cleanup.installer import main as installer
        return installer.uninstall(purge=a.purge, yes=a.yes, dry_run=a.dry_run)
    if a.cmd == "doctor":
        return _doctor()
    if a.cmd == "update":
        from cleanup.installer import main as installer
        return installer.update()
    return 2
