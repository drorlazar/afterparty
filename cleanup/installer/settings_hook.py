"""Afterparty's one SessionEnd handler group in ~/.claude/settings.json. The whole group is ours; a group that
contains our command but differs in any way was edited by hand and is never touched. Preservation is semantic
(spec 5.3, rev 5): every key, value and key order and the file's indentation width are kept; the exact original
bytes are in the backup."""
import json

from cleanup.installer import edits


def entry(command):
    return {"hooks": [{"type": "command", "command": command, "timeout": 10}]}


def parse(text):
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError("~/.claude/settings.json isn't valid JSON (%s)" % e)
    if not isinstance(data, dict):
        raise ValueError("~/.claude/settings.json isn't a JSON object")
    return data


def _groups(data):
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in ~/.claude/settings.json isn\'t an object')
    groups = hooks.get("SessionEnd", [])
    if not isinstance(groups, list):
        raise ValueError('"hooks.SessionEnd" in ~/.claude/settings.json isn\'t a list')
    return groups


def _commands(group):
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return []
    return [h.get("command") for h in group["hooks"] if isinstance(h, dict)]


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def status(text, group):
    groups = _groups(parse(text))
    if any(_same(g, group) for g in groups):
        return "same"
    command = group["hooks"][0]["command"]
    return "edited" if any(command in _commands(g) for g in groups) else "absent"


def commands(text):
    return [c for g in _groups(parse(text)) for c in _commands(g) if isinstance(c, str)]


def _indent(text):
    """One level of the file's indentation: a number of spaces, or the tab run of a tab-indented file."""
    for line in (text or "").splitlines()[1:]:
        if line.startswith("\t"):
            return line[:len(line) - len(line.lstrip("\t"))]
        body = line.lstrip(" ")
        if body and len(line) > len(body):
            return len(line) - len(body)
    return 2


def _dump(data, like):
    out = json.dumps(data, indent=_indent(like), ensure_ascii=False)
    return out + ("\n" if like is None or like.endswith("\n") or not like.strip() else "")


def add(path, group, backup_dir=None, before_check=None):
    def transform(text):
        state = status(text, group)
        if state == "same":
            return None
        if state == "edited":
            raise ValueError("~/.claude/settings.json has an Afterparty hook entry that was edited by hand")
        data = parse(text)
        data.setdefault("hooks", {}).setdefault("SessionEnd", _groups(data)).append(group)
        return _dump(data, text)
    return edits.edit(path, transform, backup_dir=backup_dir, before_check=before_check)


def remove(path, group, backup_dir=None):
    """Remove our exact group. "removed", "absent", or "edited" (a changed copy is left in place)."""
    result = {}

    def transform(text):
        result["r"] = "absent"
        if text is None:
            return None
        data = parse(text)
        groups = _groups(data)
        keep = [g for g in groups if not _same(g, group)]
        if len(keep) == len(groups):
            if any(group["hooks"][0]["command"] in _commands(g) for g in groups):
                result["r"] = "edited"
            return None
        result["r"] = "removed"
        if keep:
            data["hooks"]["SessionEnd"] = keep
        else:
            del data["hooks"]["SessionEnd"]
            if not data["hooks"]:
                del data["hooks"]
        return _dump(data, text)
    edits.edit(path, transform, backup_dir=backup_dir)
    return result.get("r", "absent")
