#!/usr/bin/env python3
"""Writes assets/film/chapters.json from the film's timeline.json (scenes counted in beats).

    python3 site/tools/chapters-from-timeline.py path/to/film/timeline.json

The page reads chapters.json at runtime, so a re-cut only needs this script re-run. It also lists which
film files exist (media), and the page attaches only those, so a missing file is never requested.
Scene ids without an entry in CHIPS keep the timeline title and are drawn as story chapters.
Stdlib only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "assets", "film", "chapters.json")

# timeline scene id -> (chip id, chip title, kind[, habit number]). kind colours the chip dot:
# mess = dusty ochre, afterparty = mint, story = neutral. Habit chips are what the accordion links seek to.
CHIPS = {
    "opener": ("laptop", "The laptop", "story"),
    "laptop": ("laptop", "The laptop", "story"),
    "dive": ("dive", "Into the drive", "story"),
    "intro": ("venue", "The party starts", "story"),
    "outro": ("sunrise", "Sunrise", "story"),
    "hook": ("hook", "Disk full", "mess"),
    "rewind": ("rewind", "Rewind", "story"),
    "party": ("venue", "The venue", "story"),
    "guests": ("guests", "Render, install, branch", "mess"),
    "mess": ("mess", "The mess", "mess"),
    "scratch": ("scratch", "Record scratch", "mess"),
    "title": ("afterparty", "Meet Afterparty", "afterparty"),
    "bin": ("habit-1", "One bin", "afterparty", 1),
    "tidy": ("habit-2", "/tidy", "afterparty", 2),
    "sunday": ("habit-3", "Sunday sweep", "afterparty", 3),
    "map": ("habit-4", "The map", "afterparty", 4),
    "promise": ("promise", "The promise", "afterparty"),
    "encore": ("encore", "Encore", "story"),
}


FILM_DIR = os.path.dirname(OUT)


def media():
    """Lists only the media files that exist, so the page never requests a missing file."""
    have = lambda n: os.path.exists(os.path.join(FILM_DIR, n))
    film = [n for n in ("afterparty.webm", "afterparty.mp4") if have(n)]
    clips = {}
    for i in range(1, 5):
        c = [n for n in (f"habit-{i}.webm", f"habit-{i}.mp4") if have(n)]
        if c:
            clips[str(i)] = c
    return {"film": film, "captions": "afterparty.vtt" if have("afterparty.vtt") else None, "clips": clips}


def main(path):
    with open(path) as f:
        tl = json.load(f)
    beat = float(tl["music"]["beatSec"])
    first = float(tl["music"].get("firstBeatSec", 0))
    t = 0
    chapters = []
    for sc in tl["scenes"]:
        start = first + t * beat
        t += sc["beats"]
        end = first + t * beat
        spec = CHIPS.get(sc["id"], (sc["id"], sc.get("title", sc["id"]), "story"))
        ch = {"id": spec[0], "start": round(start, 2), "end": round(end, 2), "title": spec[1], "kind": spec[2]}
        if len(spec) > 3:
            ch["habit"] = spec[3]
        chapters.append(ch)
    data = {
        "version": 1,
        "sample": False,
        "note": "Generated from the film timeline by site/tools/chapters-from-timeline.py. Re-run after every re-cut.",
        "bpm": tl["music"].get("bpm"),
        "duration": round(first + t * beat, 2),
        "chapters": chapters,
        "media": media(),
    }
    own = tl.get("chapters") or []   # cross-check against the timeline's own chapter list, when it has one
    for mine, theirs in zip(chapters, own):
        if mine["id"] != theirs.get("id") or abs(mine["start"] - float(theirs.get("start", 0))) > 0.01:
            print(f"warning: {mine['id']} @ {mine['start']} differs from the timeline's {theirs.get('id')} @ {theirs.get('start')}")
    if own and len(own) != len(chapters):
        print(f"warning: {len(chapters)} chapters here, {len(own)} in the timeline")
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"wrote {len(chapters)} chapters, {data['duration']} s")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
