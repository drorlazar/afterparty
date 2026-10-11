"""Full Disk Access detection: TCC-protected folders raise PermissionError without it.

One policy, shared by the map app (this process) and the launchd probe (`cleanup.fda`, which runs the source of
`first_outcome` verbatim in the probed interpreter): the folders are tried in order and the FIRST relevant outcome
decides. A readable folder means "yes", a PermissionError means "no", a missing folder or any other error is not
relevant and the next folder is tried; no relevant outcome at all is "none" (couldn't tell)."""
import inspect
import os
import sys

PROBES = ("~/Library/Safari", "~/Library/Mail", "/Library/Application Support/com.apple.TCC")


def first_outcome(probes):
    """"yes" / "no" / "none" for the first probe folder that exists and gives a relevant answer. Self-contained
    (only `os`), because its source is also the launchd probe's payload."""
    import os
    for p in probes:
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            continue
        try:
            os.listdir(p)
            return "yes"
        except PermissionError:
            return "no"
        except OSError:
            continue
    return "none"


def probe_payload(probes=PROBES):
    """The program the launchd probe runs: `first_outcome` itself, then one `fda:<outcome>` line."""
    return "%s\nprint('fda:' + first_outcome(%r), flush=True)\n" % (inspect.getsource(first_outcome),
                                                                   tuple(probes))


def has_full_disk_access(probes=PROBES):
    # nothing to probe: assume access, the scan will report unreadable folders
    return first_outcome(probes) != "no"


def interpreter():
    return os.path.realpath(sys.executable)
