"""Afterparty's installer (spec 5.3). It plans every change, prints the plan, stops on any conflict before
touching anything, then applies the steps under the install lock: an intent receipt before each change, a
completion receipt after it, and a check that the target is still what the plan saw immediately before anything
is replaced. It never deletes user data. Output is plain ASCII (it may run with no locale at all)."""
import argparse
import contextlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from cleanup import config, fsops
from cleanup.fda import fda_hint, real_executable
from cleanup.installer import blocks, edits, jobs, owned, settings_hook
from cleanup.installer.owned import LINKS, SKILLS, app_dest, home, hook_command  # noqa: F401

REPO = jobs.REPO
BUNDLE_ID = "io.github.drorlazar.afterparty"
DETECT = ("Projects", "Developer", "Code", "code", "src", "dev", "GitHub", "git", "repos", "work", "Sites")
OFFICIAL = ("https://github.com/drorlazar/afterparty", "https://github.com/drorlazar/afterparty.git",
            "git@github.com:drorlazar/afterparty.git", "ssh://git@github.com/drorlazar/afterparty.git")
OK, DECLINED, BAD, CONFLICT, FAILED, BUSY = 0, 1, 2, 3, 4, 5
MARK = {"new": "+", "update": "~", "same": "=", "adopt": "?", "conflict": "!", "skip": "-"}
JOB_NOTES = {"weekly": "Sundays 10:00: report and notify (cleans only after `afterparty autopilot on`)",
             "spacecheck": "daily 12:00: warns when free space is low"}
STOPPED = (owned.Changed, owned.ManifestError, OSError, RuntimeError, ValueError, subprocess.SubprocessError)


def tilde(path):
    h = home()
    if path == h or path.startswith(h + os.sep):
        return "~" + path[len(h):]
    return path


def _key(path):
    return os.path.realpath(os.path.expanduser(path))


def backup_dir():
    return os.path.join(config.state_dir(), "backups")


def settings_transaction(mutate):
    """A settings write that is part of a longer transaction (installer, update, migration). Fixed lock order: the
    cleanup run lock first (non-blocking: refused while a cleanup or undo runs), then the settings lock for one
    read-merge-write. So whenever a Protect press could be waiting for the settings lock behind us, no cleanup can be
    running; and once we're done, Protect lands and the next cleanup's last_gate sees it on its fresh read."""
    from cleanup.plan import bridge
    with bridge.lock(config.state_dir()):
        return config.update(mutate)


CLEANUP_RUNNING = ("an Afterparty cleanup is running (or an undo), so no Afterparty job was stopped or restarted. "
                   "Run it again when it has finished.")


def cleanup_lock():
    """The cleanup run lock, taken without waiting: bridge.Busy while a cleanup or an undo runs. Held around every
    launchd job stop or restart (install, update, uninstall), so none of them ever stops a job in the middle of a
    cleanup. No state folder yet means no cleanup can be running, and none is created for the check."""
    from cleanup.plan import bridge
    if not os.path.isdir(config.state_dir()):
        return contextlib.nullcontext()
    return bridge.lock(config.state_dir())


def official_origins():
    extra = os.environ.get("AFTERPARTY_TEST_OFFICIAL_ORIGIN")  # test-only: a local stand-in for the public repo
    return OFFICIAL + ((extra,) if extra else ())


def confirm(question):
    if not sys.stdin.isatty():
        print("%s Re-run with --yes to confirm." % question)
        return False
    return input(question + " Type 'yes' to continue: ").strip().lower() == "yes"


def python_works(py):
    if not os.path.isabs(py):
        return False
    try:
        r = subprocess.run([py, "-I", "-B", "-c", "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)"],
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def detect_roots():
    found, seen = [], set()
    for name in DETECT:
        p = os.path.join(home(), name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if os.path.isdir(p) and not os.path.islink(p) and (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            found.append(tilde(p))
    return found


def native_app_data():
    """Data macOS and WebKit keep for Afterparty.app itself (window frame, the map's own preferences in the web view's
    storage: theme, text size, pane widths), as exact paths. The installer never deletes these: it didn't write them
    and a name with our bundle id proves nothing. Uninstall lists the ones present so the user can remove them."""
    lib = os.path.join(home(), "Library")
    return [os.path.join(lib, "WebKit", BUNDLE_ID), os.path.join(lib, "Caches", BUNDLE_ID),
            os.path.join(lib, "HTTPStorages", BUNDLE_ID), os.path.join(lib, "HTTPStorages", BUNDLE_ID + ".binarycookies"),
            os.path.join(lib, "Preferences", BUNDLE_ID + ".plist"),
            os.path.join(lib, "Saved Application State", BUNDLE_ID + ".savedState")]


def _native_data_note():
    present = [p for p in native_app_data() if os.path.lexists(p)]
    if not present:
        return
    print("macOS kept Afterparty.app's own data (window size, the map's theme, text size and layout). Afterparty "
          "didn't write it, so it leaves it; to remove it yourself:")
    for p in present:
        print("  rm -rf %s" % shlex.quote(p))


def bundle_id(app):
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def swift_ready():
    if os.environ.get("AFTERPARTY_TEST_APP_BUILDER"):
        return True
    xcode_select = os.environ.get("AFTERPARTY_XCODE_SELECT") or "/usr/bin/xcode-select"
    try:
        if subprocess.run([xcode_select, "-p"], capture_output=True, timeout=60).returncode != 0:
            return False  # without the Command Line Tools, xcrun would open an install dialog: never call it
        for args in (["--find", "swiftc"], ["--show-sdk-path"]):
            if subprocess.run(["/usr/bin/xcrun"] + args, capture_output=True, timeout=60).returncode != 0:
                return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def sweep_parents():
    h = home()
    cands = [h, os.path.join(h, ".local"), os.path.join(h, ".local", "state"), os.path.join(h, ".local", "bin"),
             os.path.join(h, ".claude"), os.path.join(h, ".claude", "skills"), os.path.join(h, ".codex"),
             os.path.join(h, ".codex", "skills"), os.path.join(h, "Library", "LaunchAgents"), app_dest(),
             os.path.dirname(config.state_dir()), os.path.dirname(config.config_dir())]
    out = []
    for c in cands:
        if c not in out and os.path.isdir(c):
            out.append(c)
    return out


class Step:
    """One planned change. `do(intend)` makes it and returns extra receipt fields; `fields` go into the intent
    receipt written before it. A step that only knows its checksum mid-way (the app: after building) sets
    late_intent and writes its intent itself, before it changes anything, by calling `intend(**fields)`."""

    def __init__(self, kind, path, state, label, do=None, fields=None, rerun=False, note="", late_intent=False):
        self.kind, self.path, self.state, self.label = kind, path, state, label
        self.do, self.fields, self.rerun, self.note = do, dict(fields or {}), rerun, note
        self.late_intent = late_intent

    def line(self):
        return "  %s %-6s %s%s" % (MARK[self.state], self.kind, self.label, ": " + self.note if self.note else "")


def _state(man, kind, path, now, want, adopt):
    item = man.get(kind, path)
    if now is None:
        return "new"
    if now == want:
        if item is not None and item.get("sum") == want:
            return "same"
        return "adopt" if adopt else "conflict"
    if item is not None and (now == item.get("sum") or (item.get("state") == "pending" and now == item.get("prior"))):
        return "update"
    return "conflict"


def _note(state, path, identical):
    if state == "conflict" and identical:
        return ("%s is identical to Afterparty's copy but isn't recorded as ours; re-run with --adopt to let "
                "Afterparty own it (uninstall would then remove it)" % tilde(path))
    if state == "conflict":
        return "%s already exists and isn't Afterparty's (or changed since it was installed)" % tilde(path)
    if state == "adopt":
        return "identical to Afterparty's copy; recorded as ours (--adopt)"
    return ""


def _put_do(path, build, prior):
    def do(intend):
        return {"sum": owned.put(path, build, prior)}  # what we placed, never a later read of `path` (G0 M2)
    return do


# ---------------------------------------------------------------- planning
def _mkdir(path, mode):
    def do(intend):
        os.mkdir(path, mode)
        return {}
    return do


def _real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def plan_dirs(man, want_app):
    wanted = [os.path.join(config.state_dir(), "logs"), config.config_dir(), os.path.join(home(), ".local", "bin"),
              os.path.join(home(), "Library", "LaunchAgents"), os.path.join(home(), ".claude", "skills"),
              config.pending_dir()]  # R8's pending protections: created here so uninstall removes it by its receipt
    if os.path.isdir(os.path.join(home(), ".codex")):
        wanted.append(os.path.join(home(), ".codex", "skills"))
    if want_app:
        wanted.append(app_dest())
    with_record, _ = owned.missing_chain(config.state_dir())
    steps, planned = [], set()
    for item in man.items():
        # a run that stopped between making a folder and its done receipt: finish the receipt (the folder is a real
        # folder now); one stopped before the folder was made is planned again as a normal create
        if item["kind"] != "dir" or item["state"] != "pending":
            continue
        if _real_dir(item["path"]):
            planned.add(item["path"])
            steps.append(Step("dir", item["path"], "same", tilde(item["path"]),
                              note="finishing an interrupted install"))
        elif not os.path.lexists(item["path"]) and item["path"] not in wanted:
            wanted.append(item["path"])
    for w in wanted:
        chain, base = owned.missing_chain(w)
        if not os.path.isdir(base):
            steps.append(Step("dir", w, "conflict", tilde(w), note="%s is in the way (not a folder)" % tilde(base)))
            continue
        for d in chain:
            if d in planned:
                continue
            planned.add(d)
            first = d in with_record
            steps.append(Step("dir", d, "new", tilde(d), fields={"sum": "dir"},
                              do=None if first else _mkdir(d, 0o700 if d == config.config_dir() else 0o755),
                              note="made together with the install record" if first else ""))
    return steps


def plan_links(man, adopt):
    steps = []
    for name in LINKS:
        path = os.path.join(home(), ".local", "bin", name)
        target = os.path.join(REPO, "bin", name)
        want = owned.link_sum(target)
        if os.path.lexists(path) and not os.path.islink(path):
            state, now = "conflict", None
        else:
            now = owned.sum_of(path)
            state = _state(man, "link", path, now, want, adopt)
        steps.append(Step("link", path, state, "%s -> %s" % (tilde(path), tilde(target)),
                          do=_put_do(path, lambda p, t=target: os.symlink(t, p), now),
                          fields={"sum": want, "prior": now}, note=_note(state, path, now == want)))
    return steps


def _answers(present, python, roots, keep, agent):
    if not present and not roots:
        roots = detect_roots()

    def mutate(cfg):
        cfg["python"] = python
        for key, extra in (("roots", roots), ("keep", keep)):
            have = {_key(p) for p in cfg[key]}
            for p in extra:
                if _key(p) not in have:
                    have.add(_key(p))
                    cfg[key].append(tilde(os.path.abspath(os.path.expanduser(p))))
        if agent is not None:
            cfg["agent"]["enabled"] = agent
    return mutate


def plan_config(python, roots, keep, agent):
    path = config.config_path()
    try:
        cfg, present = config.load()
    except config.ConfigError as e:
        return Step("config", path, "conflict", tilde(path),
                    note="%s; fix it or move it away, then run the installer again" % e)
    mutate = _answers(present, python, roots, keep, agent)
    preview = json.loads(json.dumps(cfg))
    mutate(preview)
    preview = config.validate(preview)
    side = edits.read_bytes(os.path.join(config.config_dir(), "python"))
    unchanged = present and preview == cfg and side == (python + "\n").encode("utf-8")
    state = "new" if not present else ("same" if unchanged else "update")
    label = "%s (project folders: %s; protected: %s; autopilot: %s)" % (
        tilde(path), ", ".join(preview["roots"]) or "none", ", ".join(preview["keep"]) or "none", preview["autopilot"])

    def do(intend):
        settings_transaction(mutate)  # run lock, then a fresh merge into the settings as they are on disk now
        return {}
    return Step("config", path, state, label, do=do, fields={"created": not present})


def skill_bases():
    bases = [os.path.join(home(), ".claude", "skills")]
    if os.path.isdir(os.path.join(home(), ".codex")):
        bases.append(os.path.join(home(), ".codex", "skills"))
    return bases


def plan_skills(man, adopt):
    steps = []
    for base in skill_bases():
        for name in SKILLS:
            src, dst = os.path.join(REPO, "skills", name), os.path.join(base, name)
            want = owned.tree_sum(src)
            if os.path.lexists(dst) and (os.path.islink(dst) or not os.path.isdir(dst)):
                state, now = "conflict", None
            else:
                now = owned.sum_of(dst)
                state = _state(man, "skill", dst, now, want, adopt)
            steps.append(Step("skill", dst, state, tilde(dst),
                              do=_put_do(dst, lambda p, s=src: shutil.copytree(s, p, symlinks=True), now),
                              fields={"sum": want, "prior": now}, note=_note(state, dst, now == want)))
    return steps


def plan_hook(man, adopt):
    path = os.path.join(home(), ".claude", "settings.json")
    group = settings_hook.entry(hook_command())
    label = "SessionEnd hook in %s (%s)" % (tilde(path), tilde(group["hooks"][0]["command"]))
    try:
        text = edits.read_text(path)
        st = settings_hook.status(text, group)
    except ValueError as e:
        return Step("hook", path, "conflict", label, note="%s; fix it, then run the installer again" % e)
    item = man.get("hook", path)
    fields = {"sum": owned.text_sum(json.dumps(group, sort_keys=True)), "group": group, "created": text is None}
    if st == "same":
        state = "same" if item is not None and item.get("group") == group else ("adopt" if adopt else "conflict")
        fields["created"] = False
    elif st == "edited":
        state = "conflict"
    else:
        state = "new"
    note = {"conflict": "an Afterparty entry is there but was edited by hand, or isn't recorded as ours"
            + ("" if st == "edited" else "; re-run with --adopt to let Afterparty own it")}.get(state, "")

    def do(intend):
        if os.path.lexists(path) == fields["created"]:
            raise owned.Changed("%s appeared or disappeared after the plan" % tilde(path))
        settings_hook.add(path, group, backup_dir=backup_dir())
        return {}
    return Step("hook", path, state, label, do=do, fields=fields, note=note)


def block_files():
    files = [os.path.join(home(), ".claude", "CLAUDE.md")]
    if os.path.isdir(os.path.join(home(), ".codex")):
        files.append(os.path.join(home(), ".codex", "AGENTS.md"))
    return files


def plan_blocks(man, adopt, scratch_days):
    block = blocks.render(scratch_days)
    want = owned.text_sum(block)
    steps = []
    for path in block_files():
        label = "%s (the .scratch rule, between afterparty markers)" % tilde(path)
        try:
            text = edits.read_text(path)
            cur = blocks.current(text)
        except ValueError as e:
            steps.append(Step("block", path, "conflict", label, note=str(e)))
            continue
        item = man.get("block", path)
        if cur is None:
            pad = blocks.pad_for(text)
            created = text is None

            def do(intend, path=path, pad=pad, created=created):
                if os.path.lexists(path) == created:
                    raise owned.Changed("%s appeared or disappeared after the plan" % tilde(path))
                blocks.put(path, block, None, pad, backup_dir=backup_dir())
                return {}
            steps.append(Step("block", path, "new", label, do=do, fields={"sum": want, "pad": pad, "created": created}))
        elif cur == block:
            state = "same" if item is not None and item.get("sum") == want else ("adopt" if adopt else "conflict")
            steps.append(Step("block", path, state, label, fields={"sum": want, "pad": "", "created": False},
                              note=_note(state, path, True)))
        elif item is not None and owned.text_sum(cur) == item.get("sum"):
            steps.append(Step("block", path, "update", label, fields={"sum": want},
                              do=lambda intend, path=path, cur=cur: blocks.put(path, block, cur, None, backup_dir=backup_dir()) or {}))
        else:
            steps.append(Step("block", path, "conflict", label, note="has an Afterparty block that was edited by hand"))
    return steps


def _job_do(lbl, path, text, prior, state, pending=False):
    """Writes (new/update) and (re)starts one launchd job, both under the cleanup run lock, so bridge.Busy leaves the
    old plist in place. A job whose record was still pending before this run (an earlier run stopped after writing
    its plist but before restarting it) is restarted even when it now reads "same": launchd may still run the old
    definition. Only a done, unchanged and loaded job is left alone."""
    def build(p):
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o644)
        lint = subprocess.run(["/usr/bin/plutil", "-lint", p], capture_output=True, text=True)
        if lint.returncode != 0:
            raise RuntimeError("the rendered job %s isn't a valid plist: %s" % (lbl, lint.stdout.strip()))

    def do(intend):
        with cleanup_lock():  # bridge.Busy: never write or restart a job while a cleanup runs
            if state in ("new", "update"):
                owned.put(path, build, prior)
            elif state == "same" and not pending and jobs.is_loaded(lbl):
                return {}  # unchanged and running: left alone (a restart would stop a cleanup it is running)
            ok, msg = jobs.start(lbl, path)
        if not ok:
            raise RuntimeError("couldn't start %s (%s)" % (lbl, msg))
        return {}
    return do


def plan_jobs(man, python, adopt):
    steps = []
    for name in jobs.SCHEDULED:
        lbl = jobs.label(name)
        path = os.path.join(home(), "Library", "LaunchAgents", lbl + ".plist")
        text = jobs.render(name, python=python, home=home(), log_dir=os.path.join(config.state_dir(), "logs"))
        want = owned.text_sum(text)
        if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
            state, now = "conflict", None
        else:
            now = owned.sum_of(path)
            state = _state(man, "job", path, now, want, adopt)
        item = man.get("job", path)
        pending = item is not None and item.get("state") == "pending"
        steps.append(Step("job", path, state, lbl, do=_job_do(lbl, path, text, now, state, pending), rerun=True,
                          fields={"sum": want, "label": lbl, "prior": now},
                          note=_note(state, path, now == want) or JOB_NOTES[name]))
    return steps


def plan_app(man, adopt):
    dst = os.path.join(app_dest(), "Afterparty.app")
    if os.path.islink(dst) or (os.path.lexists(dst) and bundle_id(dst) != BUNDLE_ID):
        return Step("app", dst, "conflict", tilde(dst), note="%s exists and isn't Afterparty's app" % tilde(dst))
    if not swift_ready():
        return Step("app", dst, "skip", tilde(dst), note="needs Swift and the macOS SDK (xcode-select --install); skipped")
    now = owned.sum_of(dst)
    item = man.get("app", dst)
    if now is None:
        state = "new"
    elif item is not None and (now == item.get("sum")
                               or (item.get("state") == "pending" and now == item.get("prior"))):
        state = "update"  # ours, or an upgrade interrupted before the new bundle was placed
    else:
        state = "adopt" if adopt else "conflict"
    if state in ("adopt", "conflict"):
        note = ("an Afterparty.app that this installer didn't put there (or that changed since)"
                + ("; re-run with --adopt to let Afterparty own it" if state == "conflict" else ""))
        return Step("app", dst, state, tilde(dst), fields={"sum": now}, note=note)

    def do(intend):
        work = tempfile.mkdtemp(prefix="afterparty-app-")
        try:
            builder = os.environ.get("AFTERPARTY_TEST_APP_BUILDER")
            cmd = [builder] if builder else ["/bin/bash", os.path.join(REPO, "app", "build-app.sh")]
            r = subprocess.run(cmd, env=dict(os.environ, AFTERPARTY_APP_BUILD_DIR=work), capture_output=True,
                               text=True, timeout=1800)
            built = os.path.join(work, "Afterparty.app")
            if r.returncode != 0 or bundle_id(built) != BUNDLE_ID:
                raise RuntimeError("the app build failed: " + (r.stderr or r.stdout).strip()[-400:])
            intend(sum=owned.tree_sum(built), prior=now)  # the receipt names the exact bundle before it is placed
            # G0 M2: the completion receipt is the sum of the copy we staged and placed, never the live bundle
            return {"sum": owned.put(dst, lambda p: shutil.copytree(built, p, symlinks=True), now)}
        finally:
            shutil.rmtree(work, ignore_errors=True)  # our own mkdtemp folder
    return Step("app", dst, state, tilde(dst) + " (built from source)", do=do, late_intent=True)


def _scratch_days():
    try:
        return config.load()[0]["scratch_days"]
    except config.ConfigError:
        return config.DEFAULTS["scratch_days"]


def plan_install(man, python, roots, keep, agent, hygiene, app, adopt):
    steps = plan_dirs(man, app) + plan_links(man, adopt) + [plan_config(python, roots, keep, agent)]
    steps += plan_skills(man, adopt) + [plan_hook(man, adopt)]
    if hygiene:
        steps += plan_blocks(man, adopt, _scratch_days())
    steps += plan_jobs(man, python, adopt)
    if app:
        steps.append(plan_app(man, adopt))
    return steps


# ---------------------------------------------------------------- apply
def apply(steps, man):
    from cleanup.plan import bridge
    for s in steps:
        if s.state == "skip":
            print("  skipped %s: %s" % (s.label, s.note))
            continue
        if s.kind == "dir" and s.state == "new" and (s.do is None or os.path.isdir(s.path)):
            continue  # made with the install record, or someone else made it meanwhile (then it isn't ours)
        item = man.get(s.kind, s.path)
        try:
            if s.state == "adopt":
                man.done(s.kind, s.path, adopted=True, **s.fields)
                continue
            if s.state == "same":
                if s.rerun:  # first: a pending job stays pending until its restart has happened (bridge.Busy)
                    s.do(lambda **fields: None)
                    edits.checkpoint("rechecked " + s.kind)
                if item is None:
                    man.done(s.kind, s.path, **s.fields)
                elif item.get("state") == "pending":
                    man.done(s.kind, s.path)  # finish our interrupted change; its intent receipt keeps the provenance
                continue
            if not s.late_intent:
                man.intend(s.kind, s.path, **s.fields)
            extra = s.do(lambda **fields: man.intend(s.kind, s.path, **fields)) or {}
            edits.checkpoint("changed " + s.kind)
            man.done(s.kind, s.path, **extra)
        except bridge.Busy:
            print("", file=sys.stderr)
            print("Stopped at %s: %s" % (s.label, CLEANUP_RUNNING), file=sys.stderr)
            return BUSY
        except STOPPED as e:
            print("", file=sys.stderr)
            print("Stopped at %s: %s" % (s.label, e), file=sys.stderr)
            print("Nothing of yours was changed. Run the installer again to finish, or ./uninstall.sh to remove what "
                  "was added.", file=sys.stderr)
            return FAILED
    return OK


def _check_answers(a):
    for p in a.root + a.keep:
        if not os.path.isabs(os.path.expanduser(p)):
            return "%s must be a full path (or start with ~)" % p
    for p in a.root:
        if not os.path.isdir(os.path.expanduser(p)):
            return "%s isn't a folder" % p
    return None


def _done(python, app):
    print("")
    print("Afterparty is installed. Nothing was deleted.")
    print("  First look (deletes nothing): ~/.local/bin/afterparty report")
    print("  To type just `afterparty`, add ~/.local/bin to your PATH.")
    print("  Full Disk Access lets reports see every folder: System Settings > Privacy & Security >")
    path, which = fda_hint(python)
    print("  Full Disk Access > add %s%s%s." % (
        ("Python.app from " if path.endswith("/Python.app") else "") + path, " (%s)" % which if which else "",
        " and %s" % tilde(os.path.join(app_dest(), "Afterparty.app")) if app else ""))
    if which:
        print("  System Settings may list two Python.app entries; add (or enable) the one at exactly that path.")
    print("  To check later: ~/.local/bin/afterparty doctor")


def install(a):
    python = a.python
    if not python_works(python):
        print("install: %s isn't a working Python 3.9 or newer." % python, file=sys.stderr)
        return BAD
    python = real_executable(python)  # never the Command Line Tools stub: launchd jobs must start the real binary
    why = _check_answers(a)
    if why:
        print("install: " + why, file=sys.stderr)
        return BAD
    try:
        with owned.lock():  # held from the first read of the install record to the last receipt
            return _install_locked(a, python)
    except owned.Busy as e:
        print("install: %s" % e, file=sys.stderr)
        return BUSY


def _install_locked(a, python):
    if not a.dry_run:
        # finish what a crashed run of ours left (an item moved aside goes back) before anything is planned, so the
        # plan sees the real state; a dry run changes nothing, so it only plans
        edits.sweep_all(sweep_parents())
    try:
        man = owned.Manifest()
    except owned.ManifestError as e:
        print("install: %s" % e, file=sys.stderr)
        return CONFLICT
    if a.reuse_answers:
        kinds = {i["kind"] for i in man.items()}
        hygiene = "block" in kinds or a.hygiene_rule == "yes"
        app, agent = "app" in kinds or a.app == "yes", None
    else:
        hygiene, app = a.hygiene_rule == "yes", a.app == "yes"
        agent = None if a.agent is None else a.agent == "yes"
    steps = plan_install(man, python, a.root, a.keep, agent, hygiene, app, a.adopt)
    print("Afterparty install plan%s - installing never deletes your files"
          % (" (dry run: nothing changes)" if a.dry_run else ""))
    print("  Python: %s" % python)
    print("  Code:   %s" % tilde(REPO))
    for s in steps:
        print(s.line())
    conflicts = [s for s in steps if s.state == "conflict"]
    if conflicts:
        print("")
        print("Stopped before changing anything: %d item(s) marked ! are in the way." % len(conflicts))
        print("Move them aside (Afterparty never moves or deletes them), then run the installer again.")
        return CONFLICT
    if a.dry_run:
        print("")
        print("Run again with --yes to install.")
        return OK
    if not (a.yes or confirm("Install Afterparty?")):
        print("Nothing changed.")
        return DECLINED
    from cleanup.plan import bridge
    try:
        with cleanup_lock():
            pass  # refuse up front, before anything changes; each job restart takes the lock again (apply)
    except bridge.Busy:
        print("install: nothing changed: " + CLEANUP_RUNNING, file=sys.stderr)
        return BUSY
    try:
        owned.bootstrap()
        man = owned.Manifest()
        edits.sweep_all(sweep_parents())
        rc = apply(steps, man)
    except STOPPED as e:
        print("install: %s. Run the installer again." % e, file=sys.stderr)
        return FAILED
    if rc == OK:
        _done(python, app)
    return rc


# ---------------------------------------------------------------- uninstall
_purge_hook = None  # test seam: called with "locked" (every lock proven free and held) and "unlinked"


def _purge_refusal():
    h = os.path.realpath(home())
    for d in (config.config_dir(), config.state_dir()):
        real = os.path.realpath(d)
        if not (real.startswith(h + os.sep) and os.path.basename(real) == "afterparty") or os.path.islink(d):
            return "%s isn't an Afterparty folder inside your home folder, so --purge won't touch it" % d
    return None


def trusted_receipts(folder):
    """Ruling P4: a folder's receipts count only while its receipts file still certifies itself; entries from an
    edited or foreign one vouch for nothing (those files then get the Trash treatment, never a permanent delete)."""
    return fsops.certified_receipts(folder)  # G0 M3: certified and parsed from one read


def _lock_files():
    return [os.path.join(config.config_dir(), ".lock"), os.path.join(config.state_dir(), "run.lock")]


class Purge:
    """--purge's data steps (coordinator rulings R1, R5, R6). Nothing is deleted because of its name, and no path is
    ever checked and then unlinked: every deletion goes through owned.take_if, which first moves the candidate
    exclusively into a fresh staging folder, checks the staged copy and deletes only that copy; a copy that fails its
    check goes to the Trash (listed) when it lies in a folder the installer created, else it is put back and left.
    Files there that no receipt vouches for go to the Trash; files elsewhere are never touched. An empty folder is
    removed only if Afterparty made it (a `dir` receipt); an empty folder it didn't make goes to the Trash if it is
    inside one Afterparty created, else it stays."""

    def __init__(self, created):
        from cleanup.plan import bridge
        self.created, self.trash = set(created), bridge.default_trash()
        self.kept, self.trashed = [], []
        self.trash_failed = False  # G0 S3: a Trash move failed: the install record stays so a rerun can finish
        self.removal_failed = False  # G0 round 3 (S-1): the record's own last step failed part-way
        self.unreadable = set()  # folders that couldn't be listed: left as they are, and the run exits non-zero

    def listing(self, folder):
        """The folder's entries, or None (listed once as left) when it can't be read."""
        try:
            return sorted(os.listdir(folder))
        except OSError as e:
            if folder not in self.unreadable:
                self.unreadable.add(folder)
                self.keep(folder, "couldn't read it: %s; left as it is" % (e.strerror or e))
            return None

    def ours(self, path):
        return any(path == c or path.startswith(c + os.sep) for c in self.created)

    def keep(self, path, why):
        self.kept.append("%s (%s)" % (tilde(path), why))

    def to_trash(self, path):
        landing = self.trash([path]).get(path, "ERR:no answer")
        if landing.startswith("ERR:"):
            self.trash_failed = True
            self.keep(path, "couldn't be moved to the Trash: " + landing[4:])  # never turns into a deletion
        else:
            self.trashed.append((path, landing))

    def remove(self, path, ok):
        ours = self.ours(path)
        try:
            r = owned.take_if(path, ok, trash=self.trash if ours else None)
        except owned.Changed as e:
            self.trash_failed = True  # G0 round 2: a blocked restore leaves the run unfinished: the record stays
            self.kept.append(str(e))
            return
        if r == "restored":
            self.trash_failed = self.trash_failed or ours
            self.keep(path, "couldn't be moved to the Trash" if ours else
                      "changed since Afterparty wrote it, in a folder Afterparty didn't create")
        elif r != "gone":
            self.trashed.append((path, r))

    def plan(self, folder, skip, out):
        """Phase 1, preparation only (ruling R15: no destructive action here): list what to do with every file
        except receipts files and `skip`, as (action, path, receipt checksum or reason) in `out`. Recovery of our own
        crashed steps (edits.sweep) has already run for every folder."""
        entries = self.listing(folder)
        if entries is None:
            return
        rec = trusted_receipts(folder)
        for name in entries:
            p = os.path.join(folder, name)
            if p in skip or name == fsops.RECEIPTS:
                continue
            if name.startswith(edits.STAGE_PREFIX):
                out.append(("keep", p, "an unfinished Afterparty step it couldn't verify; look inside before deleting it"))
            elif os.path.isdir(p) and not os.path.islink(p):
                self.plan(p, skip, out)
            elif name in rec:
                out.append(("remove", p, rec[name]))
            elif self.ours(p):
                out.append(("trash", p, None))
            else:
                out.append(("keep", p, "not in a folder Afterparty created"))

    def run(self, actions):
        """Phase 1, the actions planned above (each deletion staged and re-checked by take_if)."""
        for action, p, extra in actions:
            if action == "remove":
                self.remove(p, lambda s, want=extra: os.path.isfile(s) and not os.path.islink(s)
                            and fsops.file_sum(s) == want)
            elif action == "trash":
                self.to_trash(p)
            else:
                self.keep(p, extra)

    def receipts_and_folders(self, folder):
        """Phase 2 (after the lock files): receipts files, then empty folders, deepest first (ruling R6: a receipts
        file is deleted only inside a folder the installer created and only if it still certifies its own content;
        an edited one goes to the Trash; one still vouching for a file that is here stays with that file)."""
        entries = self.listing(folder)
        if entries is None:
            return
        for name in entries:
            p = os.path.join(folder, name)
            if os.path.isdir(p) and not os.path.islink(p) and not name.startswith(edits.STAGE_PREFIX):
                self.receipts_and_folders(p)
        rf = os.path.join(folder, fsops.RECEIPTS)
        if os.path.lexists(rf):
            if any(os.path.lexists(os.path.join(folder, n.rstrip("/"))) for n in fsops.receipts(folder)):
                pass  # it still vouches for something here (written after the files step): it stays with it
            elif self.ours(folder):
                self.remove(rf, fsops.receipts_file_ok)
            else:
                self.keep(rf, "Afterparty's receipts, in a folder Afterparty didn't create")
        if folder in self.created or self.listing(folder) != []:
            return  # folders with an install receipt go with it (_remove_dirs); others only when empty
        parent, name = os.path.split(folder)
        if fsops.certified_receipts(parent).get(name + "/") == "dir":  # only a self-certifying receipt (M3)
            try:
                os.rmdir(folder)  # Afterparty made it and it is empty (rmdir never removes anything else)
            except OSError:
                pass
        elif self.ours(folder):
            self.to_trash(folder)


def _empty(empty):
    def ok(staged):
        if not os.path.isfile(staged) or os.path.islink(staged):
            return False
        try:
            text = edits.read_text(staged)
        except ValueError:
            return False
        return text == empty
    return ok


def _drop_if_empty(path, empty):
    """A file the installer created (its receipt says `created`) and that holds again exactly what removing our part
    leaves (`empty`: "" for a block file, "{}\n" for the settings file) goes away; checked on the staged copy (R5).
    Anything else in it, even whitespace, means someone uses the file: it stays. Returns a line for the "left in
    place" list, or None."""
    try:
        if owned.take_if(path, _empty(empty)) == "restored":
            return None  # someone put content in it: it stays, nothing to report
    except owned.Changed as e:
        return str(e)
    return None


def _stop_server():
    from cleanup import launcher
    ok, msg = launcher.stop_server()
    if ok:
        ok, msg = jobs.stop(jobs.label("server"))
    return ok, msg  # its state files are receipted by the launcher; --purge removes them only if unchanged


def git_bin():
    for g in ("/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if os.access(g, os.X_OK):
            return g
    return "/usr/bin/git"


def _git(repo, *args):
    cmd = [git_bin(), "-C", repo] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _our_clean_clone(repo):
    """The code folder may go to the Trash with --purge only if it is ~/.afterparty, cloned from the official
    repository, with nothing changed, untracked or ignored in it (spec 5.4, rev 5)."""
    if os.path.realpath(repo) != os.path.realpath(os.path.join(home(), ".afterparty")):
        return False
    url = _git(repo, "remote", "get-url", "origin")
    status = _git(repo, "status", "--porcelain", "--ignored")
    return url.returncode == 0 and url.stdout.strip() in official_origins() and status.returncode == 0 \
        and not status.stdout.strip()


def uninstall(purge=False, yes=False, dry_run=False):
    why = _purge_refusal() if purge else None
    if why:
        print("uninstall: " + why, file=sys.stderr)
        return BAD
    try:
        with owned.lock():  # held from the first read of the install record to its deletion
            return _uninstall_locked(purge, yes, dry_run)
    except owned.Busy as e:
        print("uninstall: %s Nothing was removed." % e, file=sys.stderr)
        return BUSY


ENTRY_KINDS = ("link", "app", "job")  # paths that can start Afterparty: shims, the app, launchd plists


def _entry_paths():
    """Every path where an Afterparty entry point lives, whatever the install record says: the commands, the app,
    and the launchd plists of the scheduled jobs and the server."""
    agents = os.path.join(home(), "Library", "LaunchAgents")
    return ([os.path.join(home(), ".local", "bin", n) for n in LINKS] + [os.path.join(app_dest(), "Afterparty.app")]
            + [os.path.join(agents, jobs.label(n) + ".plist") for n in jobs.SCHEDULED + ("server",)])


def _entry_points_left(items, man, left):
    """Rulings R9/R12: an Afterparty entry point (command, app, job plist) whose path is occupied again counts as
    remaining, whatever put it there; its record is re-kept for the re-run and a line added to `left`. The fixed
    entry paths count too, recorded or not (after a plain uninstall the record no longer names the ones it removed).
    Returns True if any is found."""
    found = False
    recorded = {it["path"] for it in items if it["kind"] in ENTRY_KINDS}
    for path in _entry_paths():
        if path not in recorded and os.path.lexists(path):
            found = True
            if not any(x.startswith(tilde(path) + " (") for x in left):
                left.append(tilde(path) + " (an Afterparty command, app or job is here, whoever put it there)")
    for it in items:
        if it["kind"] in ENTRY_KINDS and os.path.lexists(it["path"]):
            found = True
            if man.get(it["kind"], it["path"]) is None:
                man.done(it["kind"], it["path"], **{k: v for k, v in it.items() if k not in ("kind", "path", "state")})
            if not any(x.startswith(tilde(it["path"]) + " (") for x in left):
                left.append(tilde(it["path"]) + " (something new was put here while Afterparty removed its copy)")
    return found


def _stop_for_entry_points(left):
    # R7: something that could still start or point at Afterparty is in place: never touch data or locks then
    print("", file=sys.stderr)
    print("--purge stopped before touching Afterparty's settings, history or locks, because these are still in "
          "place (you changed them, so Afterparty leaves them to you). Remove them yourself, then run "
          "./uninstall.sh --purge again:", file=sys.stderr)
    for line in left:
        print("  " + line, file=sys.stderr)
    return FAILED


def _remove_entry_points(items, man):
    """Step 1 of uninstall (ruling R7): the server, then every job, shim, app, hook, skill and block, in reverse
    install order. Returns (left in place, failures)."""
    left, failed = [], []
    ok, msg = _stop_server()
    if not ok:
        failed.append("the Afterparty server job is still loaded or can't be checked (%s)" % msg)
    for it in reversed(items):
        kind, path = it["kind"], it["path"]
        if kind in ("dir", "config") or failed:
            continue
        before = len(left)
        try:
            edits.checkpoint("uninstall " + kind)
            if kind == "job":
                ok, msg = jobs.stop(it["label"])
                if not ok:
                    failed.append("%s is still loaded or can't be checked (%s)" % (it["label"], msg))
                    continue
            if kind == "hook":
                if settings_hook.remove(path, it["group"], backup_dir=backup_dir()) == "edited":
                    left.append(tilde(path) + " (its Afterparty hook entry was edited by hand)")
                elif it.get("created"):
                    note = _drop_if_empty(path, "{}\n")
                    if note:
                        left.append(note)
            elif kind == "block":
                if not blocks.take(path, it["sum"], it.get("pad") or "", backup_dir=backup_dir()):
                    left.append(tilde(path) + " (its Afterparty block was edited; remove it by hand)")
                elif it.get("created"):
                    note = _drop_if_empty(path, "")
                    if note:
                        left.append(note)
            elif not owned.take(path, it["sum"]):
                left.append(tilde(path) + " (changed since install)")
            elif os.path.lexists(path):
                # R9: our copy is gone, but something new was put there meanwhile (take_if left it alone)
                left.append(tilde(path) + " (something new was put here while Afterparty removed its copy)")
            if len(left) > before:
                continue  # R7: stays in the record, so any later run (even --purge after a plain uninstall) checks
                #           it again before any data is touched
            man.forget(kind, path)
        except STOPPED as e:
            failed.append("%s (%s)" % (tilde(path), e))
    return left, failed


def _uninstall_locked(purge, yes, dry_run):
    finished = []
    if not dry_run:
        edits.sweep(os.path.dirname(owned.manifest_path()))  # a record a crashed run had moved aside goes back first
        finished = [x for x in edits.sweep_all(sweep_parents()) if x == "teardown"]  # a crashed purge's last step
    try:
        man = owned.Manifest()
    except owned.ManifestError as e:
        print("uninstall: %s." % e, file=sys.stderr)
        return CONFLICT
    items = man.items()
    if not items and finished:
        print("An earlier `--purge` stopped during its very last step; that step is finished now. Afterparty is "
              "uninstalled.")
        return OK
    if not items:
        print("Afterparty's install record (%s) is missing, so nothing was removed." % tilde(man.path))
        print("See README.md > Uninstall for what the installer adds.")
        return DECLINED
    print("Afterparty uninstall%s - only what the installer added and nobody changed since:"
          % (" (dry run: nothing changes)" if dry_run else ""))
    for it in items:
        if it["kind"] not in ("dir", "config"):
            print("  - %-6s %s" % (it["kind"], tilde(it["path"])))
    folders = "%s and %s" % (tilde(config.config_dir()), tilde(config.state_dir()))
    if purge:
        print("  - data   in %s: files Afterparty's receipts vouch for are deleted; anything else in folders it "
              "created goes to the Trash (--purge)" % folders)
    else:
        print("  = data   %s stay: settings, cleanup history and logs (add --purge to remove them)" % folders)
    if dry_run:
        return OK
    if not (yes or confirm("Uninstall Afterparty?")):
        print("Nothing changed.")
        return DECLINED
    # 1. Entry points first (ruling R7): the server, then every job, shim, app, hook, skill and block, in reverse
    #    install order. Afterwards nothing can start Afterparty any more.
    edits.sweep_all(sweep_parents())
    from cleanup.plan import bridge
    try:
        with cleanup_lock():  # no job is stopped while a cleanup runs (it would be killed part-way)
            left, failed = _remove_entry_points(items, man)
    except bridge.Busy:
        print("uninstall: nothing was removed: " + CLEANUP_RUNNING, file=sys.stderr)
        return BUSY
    if failed:
        print("", file=sys.stderr)
        print("Couldn't remove everything; the install record is kept so you can run this again:", file=sys.stderr)
        for line in failed:
            print("  " + line, file=sys.stderr)
        return FAILED
    if purge:
        _entry_points_left(items, man, left)  # R9: early, for a clear message before taking any lock
    if purge and left:
        return _stop_for_entry_points(left)
    state_chain = [it["path"] for it in reversed(items) if it["kind"] == "dir"
                   and (it["path"] == config.state_dir() or config.state_dir().startswith(it["path"] + os.sep))]
    kept, trashed, unreadable, unfinished, removal_failed = [], [], False, False, False
    if purge:
        # 2. Prove nobody holds or waits for a lock: take each without waiting, in the fixed order (run lock, then
        #    settings lock). If any is held, stop: no data has been touched.
        from cleanup.plan import bridge
        created = {it["path"] for it in items if it["kind"] == "dir"}
        for folder in (config.config_dir(), config.state_dir()):
            for d, dirs, _ in os.walk(folder):
                # finish our own crashed steps first: a lock file a crashed purge had moved aside goes back before
                # taking the lock could create a new one in its place
                dirs[:] = [x for x in dirs if not x.startswith(edits.STAGE_PREFIX)]
                edits.sweep(d)
        try:
            with bridge.lock(config.state_dir()), config.writers_lock(create=False, blocking=False):
                if _purge_hook:
                    _purge_hook("locked")
                # Rulings R12/R15: first all recovery and preparatory I/O, then the decisive entry-point check, then
                # the first destructive action, with only in-memory work between the check and p.run below.
                folders = [f for f in (config.config_dir(), config.state_dir())
                           if os.path.isdir(f) and not os.path.islink(f)]
                for folder in folders:
                    for d, dirs, _ in os.walk(folder):              # recovery (again, now under the locks)
                        dirs[:] = [x for x in dirs if not x.startswith(edits.STAGE_PREFIX)]
                        edits.sweep(d)
                p = Purge(created)
                skip = set(_lock_files()) | {man.path}
                actions = []
                for folder in folders:
                    p.plan(folder, skip, actions)                   # preparation: reads only
                lock_wants = [(f, trusted_receipts(os.path.dirname(f)).get(os.path.basename(f))) for f in _lock_files()]
                if _entry_points_left(items, man, left):            # the decisive check: last I/O before deleting
                    return _stop_for_entry_points(left)
                p.run(actions)                                      # 3. data
                for lock_file, want in lock_wants:                  # 4. the lock files, as the final data step
                    p.remove(lock_file, lambda s, want=want: want is not None and os.path.isfile(s)
                             and not os.path.islink(s) and fsops.file_sum(s) == want)
                if _purge_hook:
                    _purge_hook("unlinked")
                for folder in (config.config_dir(), config.state_dir()):
                    if os.path.isdir(folder) and not os.path.islink(folder):
                        p.receipts_and_folders(folder)       # 5. receipts files and empty folders
                unreadable, unfinished = bool(p.unreadable), p.trash_failed
                if not (unreadable or unfinished):
                    _remove_dirs(items, state_chain, man)
                    edits.checkpoint("before removing the install record")  # the final window (G0 S4)
                    try:
                        torn = _teardown(man, state_chain)
                    except owned.Changed as e:
                        torn = True  # the record is in the named staging folder, kept for a person
                        p.kept.append(str(e))
                        p.removal_failed = True  # G0 round 3 (S-1): unfinished, a non-zero exit
                    except OSError as e:
                        torn = True
                        p.kept.append("the install record's last step failed (%s); what is left stays" % e)
                        p.removal_failed = True
                    if not torn:
                        p.remove(man.path, _as_written(man))  # kept otherwise, so `--purge` can run again
                kept, trashed = p.kept, p.trashed
                removal_failed = p.removal_failed
        except (bridge.Busy, fsops.Busy) as e:
            why = ("an Afterparty cleanup is running (or an undo)" if isinstance(e, bridge.Busy)
                   else "Afterparty's settings are being changed right now")
            print("", file=sys.stderr)
            print("Everything the installer added is removed, but %s, so Afterparty's settings and history were "
                  "left alone. Run `./uninstall.sh --purge` again when it has finished." % why, file=sys.stderr)
            return FAILED
        for d in state_chain:
            try:
                os.rmdir(d)
            except OSError:
                pass
    else:
        kept += _drop_server_state()  # ruling P3: the plist holds the server's key; only data stays
        # Ruling P2: the settings, history and logs stay, and so does a reduced install record (their folders, the
        # settings item and anything left in place above), so a later `--purge` can still remove them, and still
        # stops first while something left in place could start Afterparty (R7).
        _remove_dirs(items, [it["path"] for it in items if it["kind"] == "dir" and _data_dir(it["path"])], man)
    print("")
    print("Afterparty is uninstalled." + (" Left in place:" if left or kept else ""))
    for line in left + kept:
        print("  " + line)
    _native_data_note()
    if not purge:
        print("Your settings, cleanup history and logs stay in %s, with a short install record so that "
              "`./uninstall.sh --purge` can remove them later." % folders)
    if trashed:
        print("Moved to the Trash (no receipt shows Afterparty wrote them as they are now; restore them from there):")
        for path, landing in trashed:
            print("  %s -> %s" % (tilde(path), landing))
    if unreadable:
        print("Some of Afterparty's folders couldn't be read, so they and the install record were left. Give "
              "yourself access to them (or remove them by hand), then run `./uninstall.sh --purge` again.",
              file=sys.stderr)
        return FAILED
    if removal_failed:
        print("The last step (removing Afterparty's install record and its folders) couldn't finish; what is left is "
              "listed above. Look at it, then run `./uninstall.sh --purge` again.", file=sys.stderr)
        return FAILED
    if unfinished:
        print("Some files couldn't be moved to the Trash (listed above), so the install record was kept. Run "
              "`./uninstall.sh --purge` again to finish.", file=sys.stderr)
        return FAILED
    if purge and _our_clean_clone(REPO):
        from cleanup.plan import bridge
        landing = bridge.default_trash()([REPO]).get(REPO, "ERR:no answer")
        if landing.startswith("ERR:"):
            print("The code folder %s couldn't go to the Trash (%s); delete it yourself." % (tilde(REPO), landing[4:]))
        else:
            print("The code folder %s was moved to the Trash." % tilde(REPO))
    else:
        print("The code folder %s stays; delete it yourself if you want: rm -rf %s" % (REPO, shlex.quote(REPO)))
    return OK


def _only_chain(top, chain, record):
    """True when a complete walk finds that `top` holds nothing but folders of `chain` and the install record (no
    links, nothing else). A folder that can't be listed makes it False (G0 round 2, NEW2)."""
    walk = edits.complete_walk(top)
    if walk is None or os.path.islink(top):
        return False
    for d, dirs, files in walk:
        if d not in chain or any(os.path.join(d, f) != record for f in files):
            return False
        if any(os.path.islink(os.path.join(d, x)) or os.path.join(d, x) not in chain for x in dirs):
            return False
    return True


def _teardown(man, state_chain):
    """G0 S4, the purge's last step: bootstrap in reverse. The install record and the folders on the way to it that
    the installer made go together, in one exclusive rename into a staging folder next to the outermost of them, so
    a crash never leaves folders Afterparty made without a record of them: the next run's sweep finishes it (the
    stage is ours only while it holds exactly those folders and a record naming its nonce). The staged record must
    still be exactly what we last wrote (R5), else everything goes back. False (nothing moved) when those folders
    hold anything else; the caller then removes the record alone."""
    chain = set(state_chain)
    top = next((d for d in sorted(chain, key=len) if os.path.isdir(d) and not os.path.islink(d)
                and _only_chain(d, chain, man.path)), None)
    if top is None:
        return False
    base = os.path.dirname(top)
    stage = edits.make_stage(base, "teardown", rel=os.path.relpath(config.state_dir(), base), manifest=owned.MANIFEST)
    man.data["stage"] = os.path.basename(stage)[len(edits.STAGE_PREFIX):]
    edits.update_mark(stage, record_sum=fsops.text_sum(man.text()))  # NEW1: recovery deletes only exactly this record
    man.save()
    edits.checkpoint("install record marked for removal")
    moved = os.path.join(stage, os.path.basename(top))
    if fsops.rename_exclusive(top, moved) is not None:
        shutil.rmtree(stage)  # only our marker is in it
        return False
    edits.checkpoint("install record moved out")
    mark = edits.read_mark(stage)
    if not (mark and edits._contents_ok(stage, mark) and _as_written(man)(os.path.join(stage, os.path.relpath(
            man.path, base)))):
        if fsops.rename_exclusive(moved, top) is not None:
            raise owned.Changed("%s changed during the purge and something new is in its place; Afterparty's copy is "
                                "kept at %s" % (top, moved))
        shutil.rmtree(stage)  # only our marker is left
        return False
    if not edits.remove_record_stage(stage, mark):  # exact removal, never recursive (NEW2)
        raise owned.Changed("%s couldn't be removed completely (something in it changed, or the file system refused); "
                            "what is left there stays for you to look at" % stage)
    edits.checkpoint("install record removed")
    return True


def _as_written(man):
    """R5 for our own record too: the staged copy is deleted only if it is exactly what we last wrote."""
    want = json.dumps(man.data, indent=1, sort_keys=True) + "\n"

    def ok(staged):
        try:
            return os.path.isfile(staged) and not os.path.islink(staged) and edits.read_text(staged) == want
        except ValueError:
            return False
    return ok


def _data_dir(path):
    """Afterparty's settings and state folders, what lies inside them and the folders on the way to them."""
    return any(path == d or path.startswith(d + os.sep) or d.startswith(path + os.sep)
               for d in (config.config_dir(), config.state_dir()))


def _drop_server_state():
    """Ruling P3: a plain uninstall also removes the server's state file and its launchd plist (which holds the
    per-launch key), each only through its write receipt and unchanged (checked on the staged copy, R5). Returns
    lines for the "left in place" list."""
    from cleanup import launcher
    rec = trusted_receipts(config.state_dir())
    out = []
    for path in (launcher.default_state(), os.path.join(config.state_dir(), launcher.SERVER_LABEL + ".plist")):
        want = rec.get(os.path.basename(path))
        try:
            r = owned.take_if(path, lambda s, want=want: want is not None and os.path.isfile(s)
                              and not os.path.islink(s) and fsops.file_sum(s) == want)
        except owned.Changed as e:
            out.append(str(e))
            continue
        if r == "restored":
            out.append("%s (no receipt shows Afterparty wrote it as it is now; it may hold the server's key, so "
                       "delete it yourself)" % tilde(path))
    return out


def _remove_dirs(items, keep, man):
    """Folders the installer created (their `dir` receipts), deepest first, if empty (rmdir: atomic, never removes
    anything else), except those in `keep`. Never any other folder."""
    for it in reversed(items):
        if it["kind"] == "dir" and it["path"] not in keep:
            try:
                os.rmdir(it["path"])
            except OSError:
                pass  # not empty: something else lives there now
            man.forget("dir", it["path"])


# ---------------------------------------------------------------- update
def _reinstall(repo, lock_fd):
    try:
        py = config.load()[0]["python"] or sys.executable
    except config.ConfigError as e:
        print("update: the settings are invalid (%s); fix them, then run ./install.sh --yes." % e, file=sys.stderr)
        return BAD
    env = dict(os.environ, PYTHONPATH=repo, AFTERPARTY_INSTALL_LOCK_FD=str(lock_fd))  # the child shares our lock
    return subprocess.run([py, "-B", "-m", "cleanup.installer", "install", "--yes", "--reuse-answers", "--python", py],
                          cwd=repo, env=env, pass_fds=(lock_fd,)).returncode


def update(repo=REPO, official=None, reinstall=None):
    """Fast-forward from the official repository's `main`, fetched explicitly from the validated `origin` (never
    whatever remote the branch happens to track), then re-run the installer with the same answers. The install
    lock is held from the first git check through the re-install."""
    official = official or official_origins()
    try:
        with owned.lock() as lock_fd:
            url = _git(repo, "remote", "get-url", "origin")
            origin = url.stdout.strip()
            if url.returncode != 0 or origin not in official:
                print("update: %s didn't come from the official repository (origin: %s). Nothing changed."
                      % (tilde(repo), origin or "none"), file=sys.stderr)
                return BAD
            branch = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if branch != "main":
                print("update: %s is on branch %r, not main. Nothing changed." % (tilde(repo), branch or "detached"),
                      file=sys.stderr)
                return BAD
            st = _git(repo, "status", "--porcelain")
            if st.returncode != 0 or st.stdout.strip():
                print("update: %s has local changes, so it wasn't updated. Nothing changed." % tilde(repo),
                      file=sys.stderr)
                return BAD
            from cleanup.plan import bridge
            try:
                with cleanup_lock():
                    pass  # the re-install would refuse to restart jobs now; refuse before the code changes
            except bridge.Busy:
                print("update: nothing changed: " + CLEANUP_RUNNING, file=sys.stderr)
                return BUSY
            # the URL just validated, not the remote's name: nothing in the repository's config can redirect it
            fetch = _git(repo, "fetch", "--no-tags", "--quiet", origin, "+refs/heads/main:refs/remotes/origin/main")
            if fetch.returncode != 0:
                print("update: couldn't fetch (%s). Nothing changed." % (fetch.stderr.strip() or "git fetch failed"),
                      file=sys.stderr)
                return FAILED
            merge = _git(repo, "merge", "--ff-only", "--quiet", "refs/remotes/origin/main")
            if merge.returncode != 0:
                print("update: couldn't fast-forward (%s). Nothing changed."
                      % (merge.stderr.strip() or "git merge failed"), file=sys.stderr)
                return FAILED
            return (reinstall or _reinstall)(repo, lock_fd)
    except owned.Busy as e:
        print("update: %s" % e, file=sys.stderr)
        return BUSY


def build_parser():
    ap = argparse.ArgumentParser(prog="install.sh", description="Install Afterparty. Installing never deletes your files.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--python", required=True)
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--yes", action="store_true")
    i.add_argument("--adopt", action="store_true")
    i.add_argument("--root", action="append", default=[])
    i.add_argument("--keep", action="append", default=[])
    i.add_argument("--hygiene-rule", choices=("yes", "no"), default="no")
    i.add_argument("--agent", choices=("yes", "no"))
    i.add_argument("--app", choices=("yes", "no"), default="no")
    i.add_argument("--reuse-answers", action="store_true")
    i.add_argument("--detect-roots", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true")
    u.add_argument("--yes", action="store_true")
    u.add_argument("--dry-run", action="store_true")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "install":
        if a.detect_roots:
            for r in detect_roots():
                print(r)
            return OK
        return install(a)
    return uninstall(purge=a.purge, yes=a.yes, dry_run=a.dry_run)
