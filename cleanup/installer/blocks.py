"""The optional .scratch rule in ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md, between Afterparty markers. Edits are
text-surgical: removing the block restores the file byte for byte."""
from cleanup.installer import edits, owned

BEGIN = "<!-- afterparty:begin -->"
END = "<!-- afterparty:end -->"
RULE = ("## Disk hygiene (Afterparty)\n\n"
        "Put throwaway intermediates (extracted frames, render image sequences, preview stills, temp downloads, "
        "probe output) in `<project>/.scratch/<run>/`, and add `.scratch/` to `.gitignore` in git repos. "
        "Afterparty treats `.scratch` runs as throwaway: once one has been idle for {days} days or more it is "
        "cleared for good, whatever it contains (no Trash, no file-type check; files still open or tracked by git "
        "are skipped). That happens when you run `afterparty clean` or `/tidy`, or on Sundays only if autopilot is "
        "on; by default the Sunday run only reports. Never put deliverables there. When a finished task left more "
        "than 500 MB of intermediates, suggest `/tidy`.")


def render(scratch_days):
    return BEGIN + "\n" + RULE.format(days=scratch_days) + "\n" + END


def span(text):
    if text is None:
        return None
    b, e = text.count(BEGIN), text.count(END)
    if b == 0 and e == 0:
        return None
    start, stop = text.find(BEGIN), text.find(END)
    if b != 1 or e != 1 or stop < start:
        raise ValueError("has a broken or repeated Afterparty block; fix the markers by hand")
    return start, stop + len(END)


def current(text):
    s = span(text)
    return None if s is None else text[s[0]:s[1]]


def pad_for(text):
    """The separator written before an appended block, decided from the text the plan saw."""
    base = text or ""
    return "" if not base else ("\n" if base.endswith("\n") else "\n\n")


def put(path, block, old, pad, backup_dir=None):
    """Append our block (old is None; `pad` is what the plan computed) or replace our block `old` in place.
    ValueError if the file changed since the plan in a way that matters."""
    def transform(text):
        s = span(text)
        if s is None:
            if old is not None or pad_for(text) != pad:
                raise ValueError("changed since the plan; run the installer again")
            return (text or "") + pad + block + "\n"
        if text[s[0]:s[1]] != old:
            raise ValueError("has an Afterparty block that changed since the plan")
        return None if old == block else text[:s[0]] + block + text[s[1]:]
    edits.edit(path, transform, backup_dir=backup_dir)


def take(path, block_sum, pad, backup_dir=None):
    """Remove our block if it is still exactly what we wrote. True when removed (or already gone), False when it
    was edited by hand (then it stays)."""
    verdict = {}

    def transform(text):
        verdict["ok"] = True
        s = span(text)
        if s is None:
            return None
        if owned.text_sum(text[s[0]:s[1]]) != block_sum:
            verdict["ok"] = False
            return None
        before, after = text[:s[0]], text[s[1]:]
        if after.startswith("\n"):
            after = after[1:]
        if pad and before.endswith(pad):
            before = before[:-len(pad)]
        return before + after
    edits.edit(path, transform, backup_dir=backup_dir)
    return verdict.get("ok", True)
