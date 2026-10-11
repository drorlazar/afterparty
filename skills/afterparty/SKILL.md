---
name: afterparty
description: Free disk space on this Mac with Afterparty — run its report, explain what can be cleaned (caches and dependency folders, idle worktrees, old render versions, personal items that are asked one by one) and clean it on approval. Use when the user says "disk is full", "free up space", "clean the disk", "afterparty", or a low-disk or Afterparty notification appears.
---

# Afterparty

1. Report (deletes nothing; takes a few minutes, so run it in the background): `~/.local/bin/afterparty report`. The full list is in `~/.local/state/afterparty/last-report-manifest.tsv`.
2. Summarise per tier for the user:
   - **Tier 1 — caches and dependency folders, expired `.scratch` runs, tool prunes:** cleared for good; they rebuild themselves or were throwaway. `uv cache prune`, `pnpm store prune` and `brew cleanup` are run by the tools themselves. `.scratch` runs are cleared whatever they contain.
   - **Tier 2 — idle worktrees:** removed only when clean and every commit is on the remote. "Needs you" worktrees are never forced.
   - **Tier 3 — old render versions:** moved to the Trash; the newest version is kept.
   - **Tier 4 — personal items:** asked one by one; personal items go to the Trash; local models and full package caches are removed for good (they re-download).
3. Ask. On approval: `~/.local/bin/afterparty clean --yes` (or `--tier 1,3`). Tier 4 runs only in the user's own terminal: `~/.local/bin/afterparty clean --tier 4`.
4. Report the space freed and the undo command (`~/.local/bin/afterparty undo`). Undo puts back Trash moves only.
5. Sunday runs only report until the user turns on autopilot: `~/.local/bin/afterparty autopilot on` (asks for confirmation). With autopilot on, the Sunday run clears tier 1 and moves tier 3 to the Trash on its own.

Never delete files yourself. Apps are uninstalled by the user. Chrome profiles are never touched.
Protect a folder: add it to `keep` in `~/.config/afterparty/config.json`, or use the map app's Protect button.
