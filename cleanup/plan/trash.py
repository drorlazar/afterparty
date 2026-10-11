"""Move items to the macOS Trash and report where each one landed (needed for undo).

Uses NSFileManager (via JXA), not Finder: Finder plays its trash sound once per item and
times out on large batches. Finder's "Put Back" may not be offered for these
items, so Afterparty's own undo (which uses the returned Trash path) is the way back."""
import subprocess

BATCH = 20

SCRIPT = '''ObjC.import("Foundation");
function run(argv) {
  const fm = $.NSFileManager.defaultManager, out = [];
  for (const p of argv) {
    const res = $(), err = $();
    if (fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), res, err)) out.push(ObjC.unwrap(res.path));
    else out.push("ERR:" + (ObjC.unwrap(err.localizedDescription) || "could not move to Trash"));
  }
  return out.join(String.fromCharCode(30));
}'''


def move_to_trash(paths):
    results = {}
    for i in range(0, len(paths), BATCH):
        batch = paths[i:i + BATCH]
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-", *batch], input=SCRIPT, capture_output=True, text=True, timeout=300)
            # record separator (not newline): file names may legally contain line breaks
            recs = r.stdout[:-1].split("\x1e") if r.returncode == 0 and r.stdout.endswith("\n") else \
                (r.stdout.split("\x1e") if r.returncode == 0 else [])
        except (OSError, subprocess.TimeoutExpired):
            recs, r = [], None
        if len(recs) != len(batch):  # can't trust positional mapping → let the caller verify each path
            recs = ["ERR:" + ((r.stderr.strip() if r else "") or "the Trash call did not answer")] * len(batch)
        for p, rec in zip(batch, recs):
            results[p] = rec
    return results
