"""The only shape an agent answer may have. Anything else is discarded with an error."""
import os

VERDICTS = ("safe-to-delete", "review", "keep")
ACTIONS = ("delete", "keep")  # deliberately no "permanent": only the rules engine decides how something is deleted

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "confidence", "summary", "evidence", "proposed"],
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string", "maxLength": 1200},
        "evidence": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 400}},
        "proposed": {"type": "array", "maxItems": 50, "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "action", "reason"],
            "properties": {"path": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
                           "reason": {"type": "string", "maxLength": 400}}}},
    },
}


def validate(obj):
    """Return an error message, or None if `obj` matches SCHEMA (stdlib re-check — never trust the CLI alone)."""
    if not isinstance(obj, dict):
        return "answer is not an object"
    props = SCHEMA["properties"]
    if set(obj) - set(props):
        return f"unexpected keys: {sorted(set(obj) - set(props))}"
    for k in SCHEMA["required"]:
        if k not in obj:
            return f"missing {k}"
    if obj["verdict"] not in VERDICTS:
        return "bad verdict"
    c = obj["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1:
        return "bad confidence"
    if not isinstance(obj["summary"], str) or len(obj["summary"]) > 1200:
        return "bad summary"
    ev = obj["evidence"]
    if not isinstance(ev, list) or len(ev) > 12 or not all(isinstance(e, str) and len(e) <= 400 for e in ev):
        return "bad evidence"
    pr = obj["proposed"]
    if not isinstance(pr, list) or len(pr) > 50:
        return "bad proposed"
    for p in pr:
        if not isinstance(p, dict) or set(p) != {"path", "action", "reason"}:
            return "bad proposal"
        if not isinstance(p["path"], str) or p["action"] not in ACTIONS or not isinstance(p["reason"], str) \
                or len(p["reason"]) > 400:
            return "bad proposal"
    return None


def _inside(path, target):
    t = os.path.normpath(target).casefold()
    p = path.casefold()
    return p == t or p.startswith(t.rstrip("/") + "/")


def sanitize(answer, target):
    """Keep only proposals for real, non-symlink paths at or under `target`; list what was dropped and why."""
    out = dict(answer, proposed=[], dropped=[])
    for p in answer["proposed"]:
        path = p["path"]
        why = None
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            why = "not a clean absolute path"
        elif not _inside(path, target):
            why = "outside the folder you asked about"
        elif os.path.islink(path) or (os.path.lexists(path) and os.path.realpath(path) != os.path.normpath(os.path.join(
                os.path.realpath(target), os.path.relpath(path, target)))):  # a symlink *below* the target
            why = "a symlink"
        elif not os.path.lexists(path):
            why = "doesn't exist"
        if why:
            out["dropped"].append({"path": path, "reason": why})
        else:
            out["proposed"].append(dict(p))
    return out
