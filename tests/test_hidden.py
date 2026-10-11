import plistlib
from types import SimpleNamespace

from cleanup.scanner import hidden

GB = 2**30
APFS = plistlib.dumps({"Containers": [{"Volumes": [
    {"Name": "Macintosh HD - Data", "Roles": ["Data"], "CapacityInUse": 800 * GB},
    {"Name": "Macintosh HD", "Roles": ["System"], "CapacityInUse": 11 * GB},
    {"Name": "VM", "Roles": ["VM"], "CapacityInUse": 3 * GB},
    {"Name": "Preboot", "Roles": ["Preboot"], "CapacityInUse": 6 * GB},
]}, {"Volumes": [{"Name": "Other disk", "Roles": ["Data"], "CapacityInUse": 99 * GB}]}]})


def fake_run(cmd):
    if cmd[:3] == ["diskutil", "apfs", "list"]:
        return APFS
    if cmd[0] == "osascript":
        return str(70 * GB).encode()  # available for important usage
    if cmd[0] == "tmutil":
        return b"Snapshots for disk /:\ncom.apple.TimeMachine.2026-10-07-010000.local\ncom.apple.os.update-ABC\n"
    return None


def statvfs(_):
    return SimpleNamespace(f_blocks=1000 * GB // 4096, f_frsize=4096, f_bavail=60 * GB // 4096)


def test_probe_breaks_down_hidden_space():
    r = hidden.probe(mapped=700 * GB, counts={"unreadable": 4, "cloud": 2, "timeout": 0},
                     run=fake_run, statvfs=statvfs)
    assert r["used"] == 940 * GB and r["free"] == 60 * GB
    items = {i["key"]: i for i in r["items"]}
    assert items["volume:Macintosh HD"]["bytes"] == 11 * GB
    assert items["volume:VM"]["bytes"] == 3 * GB
    assert "volume:Other disk" not in items
    assert items["purgeable"]["bytes"] == 10 * GB
    assert items["snapshots"]["detail"].startswith("2 local")
    assert items["unreadable"]["detail"].startswith("4 folders")
    assert r["unattributed"] == (940 - 700 - 11 - 3 - 6 - 10) * GB


def test_probe_degrades_when_tools_fail():
    r = hidden.probe(mapped=100 * GB, counts={"unreadable": 0, "cloud": 0, "timeout": 0},
                     run=lambda cmd: None, statvfs=statvfs)
    assert r["unattributed"] == 840 * GB
    assert all(i["key"] in ("snapshots", "unreadable", "cloud") for i in r["items"])
