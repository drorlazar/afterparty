# Installing Afterparty (runbook for Claude Code and Codex)

You are installing Afterparty for the person you're working with. Follow these steps in order. Before each step,
say in one or two plain sentences what it does. Never run a command that deletes, moves or cleans anything:
installing Afterparty never deletes your files, and the person decides about cleaning later.

Source: `https://github.com/drorlazar/afterparty`, unless the person's message names another git URL or local
folder to install from; then use that one and say so.

## 1. Preflight

Run these checks and stop at the first one that fails, telling the person exactly what to do:

```sh
sw_vers -productVersion          # needs 13 or newer
/usr/bin/xcode-select -p         # prints a folder when the Command Line Tools are installed
```

- macOS older than 13: stop. "Afterparty needs macOS 13 or newer."
- If `xcode-select -p` fails, the Command Line Tools are missing: never run `/usr/bin/git`, `/usr/bin/python3` or
  `xcrun` (without the tools they open an install dialog instead of running). Look for a `git` that works on its
  own: `/opt/homebrew/bin/git` (Apple silicon Homebrew) or `/usr/local/bin/git` (Intel Homebrew). The installer
  itself finds a Python at `/opt/homebrew/bin/python3`, `/usr/local/bin/python3` or the python.org location. If
  there is no such `git`, stop: "Run `xcode-select --install`, wait for it to finish, then paste the install
  prompt again." If the installer later stops with the same message, there is no usable Python either.
- For the optional app, only if `xcode-select -p` succeeded: `xcrun --find swiftc` and `xcrun --show-sdk-path`
  both succeed → the app can be built.

Use the `git` you found in every step below (written as `git`).

## 2. Get the code

If `~/.afterparty` doesn't exist:

```sh
git clone https://github.com/drorlazar/afterparty ~/.afterparty
```

If it exists, check that it's ours and unchanged before updating it:

```sh
git -C ~/.afterparty remote get-url origin     # must be the source above
git -C ~/.afterparty status --porcelain        # must print nothing
git -C ~/.afterparty pull --ff-only origin main
```

If the origin is different or there are local changes, stop and ask the person what to do.

## 3. Show the plan

```sh
cd ~/.afterparty && ./install.sh --dry-run
```

The installer picks a Python 3.9+ that actually runs, then lists every folder, link, scheduled job, skill, hook and
setting it would add. Show the person that list, grouped, in plain words. Lines starting with `!` are conflicts
(something already there that isn't Afterparty's): explain each one and ask what to do. Never move or delete it
yourself. If a line says an item is identical to Afterparty's copy but not recorded as ours, the person may choose
to let Afterparty own it: then add `--adopt` in step 5 (uninstalling would then remove it too). Be clear that for an
existing `Afterparty.app`, `--adopt` accepts any app bundle that carries Afterparty's bundle id, not only one this
installer built, so only suggest it for an app the person recognises.

## 4. Ask

Ask these together, with the defaults:

1. **Project folders.** `./install.sh --dry-run --detect-roots` prints the ones it found. Which hold their coding
   projects? Afterparty only cleans project leftovers inside these.
2. **Folders to protect.** Anything Afterparty must never clean (default: none).
3. **The `.scratch` rule.** Add a short rule to `~/.claude/CLAUDE.md` (and `~/.codex/AGENTS.md`) that tells agents
   to keep throwaway files in `<project>/.scratch/<run>/`? Say plainly: idle `.scratch` runs are cleared for good,
   whatever they contain, when the person runs a clean or asks `/tidy` to clear a project's whole `.scratch` (or on
   Sundays if they turn autopilot on). The rule goes to `~/.codex/AGENTS.md` only if `~/.codex` exists. It sits between
   `<!-- afterparty:begin -->` and `<!-- afterparty:end -->`, and uninstalling removes it. (default: no)
4. **Ask Claude in the map.** Turn on the optional helper that explains folders? It uses their own Claude Code and
   sees names and sizes only. (default: no)

## 5. Install

Pass the answers as flags, one `--root` or `--keep` per folder:

```sh
cd ~/.afterparty && ./install.sh --yes --root ~/Projects --keep ~/Projects/client-work --hygiene-rule no --agent no
```

If it stops with `!` conflicts, nothing was changed: go back to step 3 with the person. If it stops part-way (for
example a scheduled job couldn't start), run the same command again; it picks up where it stopped.

## 6. App

If step 1 found Swift and the SDK, offer the disk-map app: run the step 5 command again with `--app yes`. It
builds `~/Applications/Afterparty.app` from source in about a minute. The build never replaces an app it didn't
install; if one exists, the installer lists it as a conflict in step 3. Without Swift, `afterparty map` opens
the same map in the browser.

## 7. Full Disk Access

macOS hides some folders until an app has Full Disk Access; without it the report names the folders it couldn't
read. Explain that, then open the settings pane:

```sh
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
```

The installer's last lines name what to add (the exact Python path it resolved, which on a Mac with the Command Line Tools is the framework `Python.app`, not `/usr/bin/python3`; and `Afterparty.app` if it was built). Ask the
person to add exactly those paths; don't change any settings yourself.
With both Xcode and the Command Line Tools installed, System Settings can list two entries both named `Python.app`; they are different copies, so add (or enable) the one at the exact path printed (`afterparty doctor` prints it again and checks whether access works).

## 8. First look

```sh
~/.local/bin/afterparty report
```

It deletes nothing and takes a few minutes; run it in the background. Then summarise in plain words: the total, the
biggest groups, and what would happen to each. Caches, dependency folders and idle `.scratch` runs are cleared for
good. A few tool caches and local models are removed by the tools themselves, and only after asking one by one. Old
render versions, temp files and personal items go to the Trash with undo; worktrees go only when every commit is on
the remote. Mention:

- `/tidy` after a task, to clear that task's leftovers (it asks first);
- the Sunday run only reports; `afterparty autopilot on` lets it clean on its own (it asks to confirm).

Call Afterparty by its full path (`~/.local/bin/afterparty`): their shell may not have `~/.local/bin` on its PATH.

## Removing it later

Only if the person asks. Tell them to run the uninstall command from the README themselves; don't run it for them.
A plain uninstall keeps a small install record, plus their settings, history and logs, so that a later purge can
still find everything.
Neither removes the native app's own data (WebKit storage with the map's preferences, caches, its preferences plist
and saved window state, all named after `io.github.drorlazar.afterparty` under `~/Library`); the uninstall prints the
exact paths present, and the person can remove them if they want.
