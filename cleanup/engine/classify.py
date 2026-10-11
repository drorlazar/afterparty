"""classify(path) → verdict + evidence. First matching rule wins; keep list always wins."""
import os
import re
import time

from cleanup.engine.core import git_ignored, git_root, kept, tracked
from cleanup.engine.leftovers import (busy, cache_stale, group_busy, in_use, is_cache_entry, is_clone_group,
                                      is_code_sign_clone, is_old_claude_version, is_temp_entry, open_paths, running_ids,
                                      temp_stale)

SYSTEM = ("/System", "/usr", "/bin", "/sbin", "/private", "/Library", "/opt", "/cores")
DEPENDENCY = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              ".turbo", ".parcel-cache", ".vite", "DerivedData", ".scratch"}
BUILD = {"dist", "build", ".next", "target"}
FRAMES = re.compile(r"(?i)^(renders?|frames?|out|outputs?|final-frames|final-native)$")
CACHE_NAMES = {"Caches", "Cache", "CachedData", "Code Cache", "GPUCache", "ShaderCache", "DawnCache",
               "crx_cache", "CachedExtensionVSIXs"}  # browser-updater / editor-extension download caches
APP_DATA = ("Application Support", "Containers", "Group Containers", "Preferences", "Keychains", "Mail",
            "Messages", "Photos", "Calendars", "Safari")


def _under(path, base):
    # case-insensitive (APFS default): "/system" is "/System", ".GIT" is ".git"
    p, b = path.casefold(), base.casefold().rstrip("/")
    return p == b or p.startswith(b + "/")


def _is_app_cache(path, parts, lib):
    if _under(path, lib + "/Caches"):
        return True
    for base in ("/Application Support/", "/Containers/", "/Group Containers/"):
        if path.startswith(lib + base) and any(p in CACHE_NAMES for p in parts):
            return True
    return False


def _weak_dependency_ok(path, name, lib):
    """Names that are only *usually* disposable need proof before they count as regenerable."""
    if name in ("venv", ".venv"):
        return os.path.isfile(os.path.join(path, "pyvenv.cfg"))
    if name == "DerivedData":
        return _under(path, lib + "/Developer/Xcode") or bool(git_root(path))
    return True


def _v(path, cls, label, reason, rule, action):
    return {"path": path, "class": cls, "label": label, "reason": reason, "rule": rule, "action": action}


def _is_system(path, home):
    # anything inside the user's home is never a "system" path, wherever home lives
    return not _under(path, home) and any(_under(path, s) for s in SYSTEM)


def _match(path, home, fast=False):
    parts = path.split("/")
    lib = home + "/Library"
    if is_code_sign_clone(path) or is_clone_group(path):  # before the system check: under /private/var/folders
        if not fast and is_clone_group(path) and group_busy(path, open_paths()):
            return _v(path, "protected", "Browser copies, one in use",
                      "Your running browser uses one of these copies — select the others, or quit the browser first.",
                      "code-sign-clone-in-use", "none")
        if not fast and busy(path, open_paths()):
            return _v(path, "protected", "Browser copy in use", "A running browser uses (or just made) this copy.",
                      "code-sign-clone-in-use", "none")
        return _v(path, "regenerable", "Browser update leftover",
                  "A copy Chrome-style browsers make of themselves at launch; stale copies are never reused.",
                  "code-sign-clone", "permanent")
    if is_temp_entry(path):  # top level of your own $TMPDIR (…/T): disposable once nobody uses it
        try:
            stale = time.time() - os.lstat(path).st_mtime >= 3 * 86400 if fast else temp_stale(path, open_paths())
        except OSError:
            stale = False
        if stale:
            return _v(path, "review", "Stale temp file", "Left in your temp folder; untouched for 3+ days and open in no "
                      "app. Age doesn't prove it's worthless, so it goes to the Trash where you can undo.",
                      "temp-stale", "trash")
        return _v(path, "protected", "Temp file in use", "Recently used or open in an app — temp files go after 3 idle days.",
                  "temp-recent", "none")
    if is_cache_entry(path):  # an app's GPU-shader / compiler cache in your system cache folder (…/C)
        if fast or cache_stale(path, open_paths(), running_ids()):
            return _v(path, "regenerable", "App GPU / compiler cache", "The app rebuilds it (first launch may be slower). "
                      "Removed once the app isn't running and it sat unused 7+ days (1+ day if over 1 GB).",
                      "system-app-cache", "permanent")
        return _v(path, "protected", "App cache in use", "Its app is running, or it was used recently.",
                  "system-app-cache-recent", "none")
    if _is_system(path, home):
        return _v(path, "protected", "System", "macOS or system-wide data. Shown for understanding; never deleted here.",
                  "system", "none")
    if re.search(r"(?i)\.app/", path):  # inside any app bundle, wherever it lives
        return _v(path, "protected", "Inside an app", "App bundle internals can't be removed piecemeal — uninstall the app.",
                  "app-internals", "none")
    if re.match(r"^/Applications/.*\.app$", path):
        return _v(path, "review", "Application", "Uninstalling moves the whole app to the Trash.", "application", "trash")
    if _under(path, lib + "/Application Support/Google/Chrome"):
        return _v(path, "protected", "Chrome profile", "On your never-touch list.", "chrome", "none")
    if ".git" in [x.casefold() for x in parts]:
        return _v(path, "protected", "Git internals", "Repository data — never deleted here.", "git-internal", "none")
    if _under(path, lib + "/CloudStorage") or _under(path, lib + "/Mobile Documents"):
        return _v(path, "review", "Cloud-synced files", "Your synced files (iCloud Drive, Dropbox…) — manage them in that app.",
                  "cloud", "none")
    if is_old_claude_version(path, home):
        if not fast and in_use(path, open_paths()):
            return _v(path, "protected", "Old version in use", "A running process still uses this version.",
                      "old-version-in-use", "none")
        return _v(path, "regenerable", "Old app version", "A newer version sits next to it; the old one is never used again.",
                  "old-version", "permanent")
    if _is_app_cache(path, parts, lib):
        return _v(path, "regenerable", "App cache", "Apps rebuild their caches automatically.", "app-cache", "permanent")
    if any(_under(path, lib + "/" + d) for d in APP_DATA):
        return _v(path, "protected", "App data", "Live application data — remove apps through their uninstaller.",
                  "app-data", "none")
    name = parts[-1]
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    if is_dir and len(parts) >= 2 and parts[-2] == ".scratch":
        if not fast and tracked(path):
            return _v(path, "protected", "Tracked by git", "Committed to a repository — not a scratch copy.",
                      "tracked", "none")
        return _v(path, "regenerable", "Agent scratch run", "Throwaway files an agent kept in .scratch while it worked.",
                  "scratch", "permanent")
    if name in DEPENDENCY and not is_dir and os.path.lexists(path):
        return _v(path, "review", "Not a folder", "Named like a dependency folder, but it's a file.", "dependency-not-dir", "trash")
    if name in DEPENDENCY and not _weak_dependency_ok(path, name, lib):
        return _v(path, "review", "Looks like a dependency folder", "Named like a disposable folder, but nothing proves it is.",
                  "dependency-unproven", "trash")
    if name in DEPENDENCY:
        if not fast and tracked(path):
            return _v(path, "protected", "Tracked by git", "Committed to a repository — not a disposable copy.",
                      "tracked", "none")
        return _v(path, "regenerable", "Dependencies / build cache", "Recreated by the package manager or build tool.",
                  "dependency", "permanent")
    if _under(path, home + "/.cache") or _under(path, home + "/.npm"):
        return _v(path, "regenerable", "Tool cache", "Download caches re-fill on demand.", "tool-cache", "permanent")
    if name in BUILD:
        if not fast and git_root(path) and git_ignored(path) and not tracked(path):
            return _v(path, "regenerable", "Build output", "Git-ignored build output — rebuilt by the project.",
                      "build-output", "permanent")
        return _v(path, "review", "Build folder", "Not git-ignored, so it may hold real files.", "build-unignored", "trash")
    if "worktrees" in parts or ".worktrees" in parts:
        return _v(path, "review", "Git worktree", "Removable only if clean and pushed — checked at review time.",
                  "worktree", "none")
    if any(FRAMES.match(p) for p in parts[-4:]):
        return _v(path, "review", "Render / frame output", "Older versions are usually safe; the latest is kept.",
                  "frames", "trash")
    if _under(path, home + "/Downloads"):
        return _v(path, "review", "Downloads", "Personal files — go to the Trash, never deleted outright.",
                  "downloads", "trash")
    if _under(path, lib):
        return _v(path, "protected", "Library", "User library data.", "library", "none")
    return _v(path, "unknown", "Unclassified", "No rule matched. Ask the agent what this is.", "unmatched", "trash")


def classify(path, mtime=None, keep=(), home=None, fast=False):
    """fast=True: no git subprocess calls (used to color thousands of map tiles); the inspector uses the full check."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    path = os.path.normpath(path)
    if keep and kept(path, list(keep)):
        v = _v(path, "protected", "Protected by you", "In your keep list.", "keep-list", "none")
    else:
        v = _match(path, home, fast)
    sys_ok = not _is_system(path, home) or is_code_sign_clone(path) or is_clone_group(path) or is_temp_entry(path) \
        or is_cache_entry(path)
    ev = [
        {"check": "not a system path", "status": "passed" if sys_ok else "failed", "detail": ""},
        {"check": "not in keep list", "status": "failed" if v["rule"] == "keep-list" else "passed", "detail": ""},
    ]
    if mtime:
        days = int((time.time() - mtime) // 86400)
        ev.append({"check": "idle", "status": "passed" if days >= 14 else "failed",
                   "detail": f"idle {days} days" + ("" if days >= 14 else " — recently used")})
    else:
        ev.append({"check": "idle", "status": "unavailable", "detail": "no scan data"})
    v["evidence"] = ev
    return v
