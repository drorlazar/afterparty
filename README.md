# Afterparty

![Afterparty](web/icons/icon-512.png)

Clean up after your coding agents. Claude Code and Codex leave render frames, `node_modules`, worktrees and temp
files behind. Afterparty finds them, tells you what each one is, and clears them safely, so you can use agents
freely without disk anxiety. macOS 13 or newer.

## Install

Paste this into Claude Code:

```
Install Afterparty from https://github.com/drorlazar/afterparty — read INSTALL.md and follow it.
```

The agent shows you everything it will add before adding it, asks which folders hold your projects and which to
protect, and never deletes your files during the install. Prefer to do it yourself? See
[Manual install](#manual-install).

## What it does

- `afterparty report`: a dry run. What can be cleaned and why; nothing is deleted.
- `afterparty clean`: shows the exact list, asks, then cleans.
- `/tidy`: after a task, your agent clears that task's leftovers (it asks first).
- `/afterparty`: your agent runs the report and explains it in plain words.
- Every Sunday it only reports and notifies you. It cleans on its own only after `afterparty autopilot on`.
- `afterparty map`: a map of the whole disk, coloured by what each item is.
- `afterparty undo`: puts back what the last cleanup moved to the Trash.

## Safety model

Caches and scraps are cleared. Everything else goes to the Trash, with undo. In detail:

- **Cleared for good** (they rebuild themselves): dependency folders (`node_modules`, virtual environments,
  `__pycache__`), tool and app caches, git-ignored build output, older Claude Code versions, stale launch copies.
  Any of these that holds a video or project file (`.mp4 .mov .webm .blend .aep`) is skipped.
- **`.scratch` runs are scraps by convention:** once a run has been idle for 3 days it is cleared for good,
  whatever they contain, with no file-type check, so keep finished work out of `.scratch`. Files still open in an app
  or tracked by git are skipped.
- **Removed for good by the tools themselves:** `uv cache prune`, `pnpm store prune` and `brew cleanup` (never while
  the tool is running); and, only after asking you one by one, `uv cache clean --force`, `npm cache clean --force`
  and `ollama rm <model>`. These are removed for good; they re-download when needed.
- **Moved to the Trash, with undo:** old render versions (the newest is kept), stale temp files and personal items
  (asked one by one). Any of these holding `.mp4 .mov .webm .blend .aep .glb .fbx .md` files is skipped.
- **Anything you pick yourself in the map** goes to the Trash with undo, unless the rules recognise it as rebuildable
  (a cache, dependency folder or build output) inside your project folders or a few fixed places Afterparty knows (app caches, Codex's
  folders, `~/Downloads` and the Hugging Face cache); then it is cleared for good, like the rebuildable items above. Map picks get no file-type check: they
  are your own choice. Protected items are never offered.
- **Worktrees** are removed with git, only when they're clean and every commit is on your remote. There is no undo
  for these.
- **Never touched:** anything git tracks, anything open in an app, symlinks, macOS system areas, and every folder on
  your keep list.
- **On a schedule** the Sunday run only reports. After `afterparty autopilot on` it also clears the "cleared for
  good" items above, runs the tool prunes (`uv cache prune`, `pnpm store prune`, `brew cleanup`) and moves old
  render versions and stale temp files to the Trash, on its own.
- Every item is checked again right before it's touched; if it changed, it's skipped. A failed Trash move is
  skipped, never deleted instead. Every action is written to a journal.
- The optional "Ask Claude" helper in the map is off until you turn it on, sees names and sizes only unless you
  allow more, and can only suggest items for the normal review.

## Manual install

```sh
git clone https://github.com/drorlazar/afterparty ~/.afterparty
cd ~/.afterparty
./install.sh --dry-run                  # lists every file, link, job and setting it would add
./install.sh --yes --root ~/Projects
```

Options: `--root DIR` (once per project folder), `--keep PATH` (never clean this), `--hygiene-rule yes` (adds the
`.scratch` rule to `~/.claude/CLAUDE.md`, and to `~/.codex/AGENTS.md` if `~/.codex` exists, between markers), `--agent yes`, `--app yes`
(builds `Afterparty.app`; needs Swift from `xcode-select --install`; it never replaces an app it didn't install), `--adopt` (lets Afterparty own identical copies it finds; for the app, any bundle with Afterparty's bundle id
is accepted). Update later with `~/.local/bin/afterparty update`.

## Uninstall

```sh
~/.local/bin/afterparty uninstall       # or ./uninstall.sh
```

This removes only what the installer added and you haven't changed since; anything you edited is left and listed.
Your settings, cleanup history and logs stay, along with a small install record that lets a later `--purge` find
everything. The code folder stays too (the command prints how to delete it). To remove all of that as well:

```sh
./uninstall.sh --purge
```

Files Afterparty recorded writing are deleted; anything else in its own folders, such as cleanup history, goes to the
Trash and is listed, so you can still get it back. The code folder `~/.afterparty` also goes to the Trash, but only
if it is an unchanged clone of this repository; otherwise it stays and the command prints how to remove it. If you
changed something the installer added (a command in `~/.local/bin`, the app or the Claude Code hook), `--purge`
stops with exit code 4 before touching any data and lists what to remove first; run it again afterwards. The same
holds for the scheduled-job files in `~/Library/LaunchAgents` and for any Afterparty entry point at its fixed path,
whoever put it there. While an Afterparty cleanup (or an undo) is running, installing, updating and uninstalling
stop with exit code 5 before stopping or restarting any scheduled job; run them again when it has finished.

Once you have opened Afterparty.app, macOS and WebKit keep some data for the app itself: the map's own preferences
(theme, text size, pane widths) in `~/Library/WebKit/io.github.drorlazar.afterparty`, and possibly
`~/Library/Caches/io.github.drorlazar.afterparty`, `~/Library/HTTPStorages/io.github.drorlazar.afterparty`, the
window size in `~/Library/Preferences/io.github.drorlazar.afterparty.plist` and
`~/Library/Saved Application State/io.github.drorlazar.afterparty.savedState`. Uninstalling never deletes these
(Afterparty didn't write them, and a name proves nothing); it prints the ones present so you can remove them.

## Credits

Fonts: Inter and JetBrains Mono (SIL Open Font License). The map can show suggestions from
[Mole](https://github.com/tw93/mole) when it's installed. Details in [NOTICE](NOTICE).

## Licence

MIT. See [LICENSE](LICENSE).
