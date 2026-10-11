"""Persist manifests and locate journals in the Afterparty state folder (config.state_dir(): journal/ and plans/),
the same journal folder the engine writes, so `afterparty undo` finds map-app cleanups too. The legacy state
folder from before Afterparty is no longer read."""
import json
import os

from cleanup import config, fsops


def _dir(sub):
    d = os.path.join(config.state_dir(), sub)
    os.makedirs(d, exist_ok=True)
    return d


def save_manifest(m):
    p = os.path.join(_dir("plans"), m["id"] + ".json")
    fsops.replace_owned(p, json.dumps(m).encode("utf-8"), mode=0o600)  # R14
    return p


def load_manifest(pid):
    if not pid.isalnum():
        raise ValueError("bad plan id")
    with open(os.path.join(_dir("plans"), pid + ".json")) as f:
        return json.load(f)


def journal_path(pid):
    if not pid.isalnum():
        raise ValueError("bad plan id")
    return os.path.join(_dir("journal"), pid + ".jsonl")


def history():
    out = []
    for name in sorted(os.listdir(_dir("plans")), reverse=True)[:50]:
        try:
            m = json.load(open(os.path.join(_dir("plans"), name)))
        except (OSError, ValueError):
            continue
        out.append({"id": m["id"], "created": m["created"], "totals": m["totals"],
                    "executed": os.path.exists(journal_path(m["id"]))})
    return sorted(out, key=lambda x: -x["created"])
