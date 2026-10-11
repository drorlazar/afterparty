"""Explain the gap between disk 'used' and what the map can see."""
import os
import plistlib
import subprocess

JXA_IMPORTANT = ('ObjC.import("Foundation");var u=$.NSURL.fileURLWithPath("/");'
                 'var k="NSURLVolumeAvailableCapacityForImportantUsageKey";'
                 'var r=u.resourceValuesForKeysError([k],null);ObjC.unwrap(r.objectForKey(k))')


def default_run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _apfs_other_volumes(run):
    out = run(["diskutil", "apfs", "list", "-plist"])
    if not out:
        return []
    try:
        data = plistlib.loads(out)
    except Exception:
        return []
    for c in data.get("Containers", []):
        vols = c.get("Volumes", [])
        if any("Data" in v.get("Roles", []) and v.get("Name", "").endswith("Data") for v in vols) or \
                any(v.get("Name") == "Macintosh HD - Data" for v in vols):
            return [(v.get("Name", "?"), int(v.get("CapacityInUse", 0))) for v in vols
                    if "Data" not in v.get("Roles", [])]
    return []


def probe(mapped, counts, run=None, statvfs=os.statvfs):
    run = run or default_run
    st = statvfs("/System/Volumes/Data")
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    used = total - free
    items = []
    vol_bytes = 0
    for name, b in _apfs_other_volumes(run):
        vol_bytes += b
        items.append({"key": f"volume:{name}", "label": f"{name} volume", "bytes": b,
                      "detail": "separate APFS volume (macOS system, swap, boot files)"})
    purgeable = 0
    out = run(["osascript", "-l", "JavaScript", "-e", JXA_IMPORTANT])
    try:
        purgeable = max(0, int(float(out.decode().strip())) - free) if out else 0
    except ValueError:
        purgeable = 0
    if out:
        items.append({"key": "purgeable", "label": "Purgeable", "bytes": purgeable,
                      "detail": "caches macOS frees automatically when space runs low"})
    snaps = run(["tmutil", "listlocalsnapshots", "/"])
    n_snaps = len([l for l in (snaps or b"").decode(errors="replace").splitlines() if l.startswith("com.apple")])
    items.append({"key": "snapshots", "label": "Local snapshots", "bytes": None,
                  "detail": f"{n_snaps} local snapshots (Time Machine / updates); size not reported by macOS"})
    items.append({"key": "unreadable", "label": "Unreadable folders", "bytes": None,
                  "detail": f"{counts.get('unreadable', 0)} folders the scan could not read"})
    items.append({"key": "cloud", "label": "Cloud folders", "bytes": None,
                  "detail": f"{counts.get('cloud', 0)} cloud mounts skipped (files mostly not on disk)"})
    unattributed = max(0, used - mapped - vol_bytes - purgeable)
    return {"total": total, "used": used, "free": free, "mapped": mapped, "items": items,
            "unattributed": unattributed}
