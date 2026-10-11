---
name: tidy
description: Clean up after a finished task — list the throwaway files this task left (.scratch runs, render frame sequences, dependency and build folders) with sizes and offer to clear them. Use when the user says "tidy", "clean up after yourself", "delete the frames/cache", or when a task you just finished left more than 500 MB of intermediates.
---

# Tidy

Clears one finished task's folder with Afterparty. Nothing is deleted until the user says yes.

1. Pick the folder the task worked in: the project root, or for a video task the video's folder. Never the home folder.
2. Report (deletes nothing): `~/.local/bin/afterparty tidy "<folder>"`. Every line has an id such as `#3`.
3. Tell the user the total, the biggest items, and anything that looks like a deliverable. Say which items go to the Trash (frames, anything personal) and which are cleared for good (caches, dependency folders, `.scratch` runs).
4. Ask. Only after a yes: `~/.local/bin/afterparty tidy "<folder>" --apply --yes`. To keep some items add `--skip 2,5`; to clear only some use `--only 1,3`.
5. Report the space freed and the undo command Afterparty printed (`~/.local/bin/afterparty undo`).

Never delete files yourself with `rm`, `trash` or Finder. Every deletion goes through Afterparty so it is checked against the user's protections. Trash moves can be undone; caches, dependency folders and .scratch runs are cleared for good.

Skipped automatically: anything tracked by git, open in an app, or in a protected folder; caches and dependency folders holding a video or project file (.mp4 .mov .webm .blend .aep); frame folders and other Trash items holding those or .glb .fbx .md files; frame folders under `assets/`, `public/`, `src/`, `static/`, `sprites/` and `textures/`. `.scratch` runs are throwaway by convention: they are cleared for good whatever they contain, so tell the user before clearing one that holds finished work.

While you work, keep intermediates in `<project>/.scratch/<run>/`.
