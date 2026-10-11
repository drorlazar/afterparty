import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
OLD = time.time() - 100 * 86400  # "idle" timestamp: 100 days ago


# What an installed Afterparty's own jobs and server keep writing in the real state folder while the suite runs
# (job logs, the SessionEnd ledger, server state and log, the scan database, the agent cache, preview thumbnails,
# Mole's state, the run lock and the receipts file those writes update). Top-level names only.
RUNTIME_DIRS = {"logs", "agent", "thumbs"}
RUNTIME_FILES = {"sessions.jsonl", "server.json", "server.log", "io.github.drorlazar.afterparty.server.plist",
                 "scan.db", "scan.db-wal", "scan.db-shm", "scan.db-journal", "mole.json", "run.lock",
                 ".afterparty-receipts.json"}


def home_fingerprint(home):
    """What Afterparty could leave in a home folder: its settings and state folders (every file with size and
    mtime, every folder by name), the Afterparty mentions in ~/.claude/settings.json and its LaunchAgents plists.
    Only Afterparty's own traces count, so a real Claude Code session or another app changing its own files never
    trips the guard, and neither do a real install's runtime files (RUNTIME_*); settings, the install record, a
    pending protection or anything else new still do. Entries that vanish mid-walk are skipped."""
    home = Path(home)
    out = []
    for rel in (".config/afterparty", ".local/state/afterparty"):
        root = home / rel
        if not os.path.lexists(root):
            out.append((rel, "absent"))
            continue
        state = rel == ".local/state/afterparty"
        for d, dirs, files in os.walk(root):
            if state and d == str(root):
                dirs[:] = [n for n in dirs if n not in RUNTIME_DIRS]
                files = [n for n in files if n not in RUNTIME_FILES]
            for n in [""] + sorted(dirs + files):
                p = os.path.join(d, n) if n else d
                try:
                    st = os.lstat(p)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(st.st_mode):  # a folder's size/mtime change with any entry, ignored ones too
                    out.append((os.path.relpath(p, home), stat.S_IFDIR))
                else:
                    out.append((os.path.relpath(p, home), stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns))
    try:
        text = (home / ".claude" / "settings.json").read_text(errors="replace")
        out.append(("settings.json", tuple(l for l in text.splitlines() if "afterparty" in l.lower())))
    except OSError:
        out.append(("settings.json", "unreadable or absent"))
    agents = home / "Library" / "LaunchAgents"
    try:
        for n in sorted(os.listdir(agents)):
            if "afterparty" in n.lower():
                try:
                    st = os.lstat(agents / n)
                except FileNotFoundError:
                    continue
                out.append((n, st.st_size, st.st_mtime_ns))
    except OSError:
        out.append(("LaunchAgents", "unreadable or absent"))
    return out


def _real_homes():
    import pwd
    homes = {pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME") or ""}
    return sorted(h for h in homes if h and os.path.isdir(h))


@pytest.fixture(scope="session", autouse=True)
def real_home_guard():
    """Global constraint: tests never touch the real home folder. Fails the run if anything Afterparty-shaped
    appeared or changed there while the suite ran (the guard only reads; it never repairs or deletes)."""
    before = {h: home_fingerprint(h) for h in _real_homes()}
    yield
    changed = [h for h, fp in before.items() if home_fingerprint(h) != fp]
    if changed:
        pytest.fail("the test run changed Afterparty files in the real home folder: %s (compare "
                    "~/.config/afterparty, ~/.local/state/afterparty, ~/.claude/settings.json and "
                    "~/Library/LaunchAgents/*afterparty*)" % ", ".join(changed), pytrace=False)


@pytest.fixture(scope="session")
def _default_xcode_select(tmp_path_factory):
    """An xcode-select stand-in outside every test's tmp_path (tests list and size their tmp trees): it succeeds and
    reports the (absent) stand-in Command Line Tools folder, so installer checks pass and FDA discovery finds no
    Xcode."""
    xs = tmp_path_factory.mktemp("default-bin") / "xcode-select"
    xs.write_text('#!/bin/sh\necho "${AFTERPARTY_CLT_DIR:-/Library/Developer/CommandLineTools}"\n')
    xs.chmod(0o755)
    return xs


@pytest.fixture(autouse=True)
def _default_afterparty_dirs(tmp_path, monkeypatch, _default_xcode_select):
    """Every test reads empty tmp config/state folders by default, never the real install; a test or the env
    fixture can still override them."""
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "_default_config"))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "_default_state"))
    monkeypatch.setenv("AFTERPARTY_CLT_DIR", str(tmp_path / "_no_clt"))  # never the real Command Line Tools copy
    monkeypatch.setenv("AFTERPARTY_XCODE_APPS", str(tmp_path / "_no_xcode" / "Xcode*.app"))  # nor an installed Xcode
    # nor the Xcode the host's xcode-select names (discovery tests set their own selector)
    monkeypatch.setenv("AFTERPARTY_XCODE_SELECT", str(_default_xcode_select))


@pytest.fixture
def env(tmp_path):
    """Isolated environment: HOME redirected, tiny frame thresholds, deterministic git."""
    home = tmp_path / "home"
    home.mkdir()
    e = dict(os.environ)
    e.update({
        "HOME": str(home),
        # system dirs only: keeps brew/uv/pnpm/npm/ollama out of reach of a test that forgets --root
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(REPO),
        "DISKCLEAN_SEQ_MIN_BYTES": "1024",
        "DISKCLEAN_SEQ_MIN_FILES": "30",
        "DISKCLEAN_CODE_SIGN_DIR": "",  # never look at the real browser code-sign clones or temp folder
        "DISKCLEAN_TEMP_DIR": "",
        "DISKCLEAN_CACHE_DIR": "",
        "AFTERPARTY_LAUNCHCTL": "/usr/bin/false",  # any launchd call in a test fails instead of reaching the real domain
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    cfg_dir, state_dir, trash_dir = tmp_path / "config", tmp_path / "state", tmp_path / "trash"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"version": 1, "roots": ["~/Projects"]}))
    e["AFTERPARTY_CONFIG_DIR"] = str(cfg_dir)
    e["AFTERPARTY_STATE_DIR"] = str(state_dir)
    e["AFTERPARTY_TEST_TRASH_DIR"] = str(trash_dir)
    e["AFTERPARTY_NO_NOTIFY"] = "1"  # tests never post real macOS notifications
    return e


def set_config(env, **values):
    path = Path(env["AFTERPARTY_CONFIG_DIR"]) / "config.json"
    cfg = json.loads(path.read_text()) if path.exists() else {"version": 1}
    cfg.update(values)
    path.write_text(json.dumps(cfg))


def state(env):
    return Path(env["AFTERPARTY_STATE_DIR"])


@pytest.fixture
def run_cli(env):
    def _run(*args, python=sys.executable, stdin=subprocess.DEVNULL):
        return subprocess.run([python, "-m", "cleanup.cli", "diskclean", *map(str, args)],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=180, stdin=stdin)
    return _run


def age(path, ts=OLD):
    """Set mtime of path and everything under it (incl. .git) to ts."""
    path = Path(path)
    for p in [path, *path.rglob("*")]:
        os.utime(p, (ts, ts), follow_symlinks=False)


def seq(d, n=40, size=64, ts=OLD):
    """Create an image sequence of n numbered PNG files (tiny; thresholds lowered via env)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"f_{i:04d}.png").write_bytes(os.urandom(size))
    age(d, ts)
    return d


def git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def git_repo(env, tmp_path):
    def _make(rel="repo"):
        r = tmp_path / rel
        r.mkdir(parents=True)
        git(r, "init", "-q", "-b", "main", env=env)
        git(r, "commit", "-q", "--allow-empty", "-m", "init", env=env)
        return r
    return _make


def manifest(env, name="last-scoped-report"):
    """Parse the TSV manifest the CLI writes: [(tier, kind, size, auto|manual, path, reason)]."""
    p = state(env) / f"{name}-manifest.tsv"
    rows = []
    for line in p.read_text().splitlines():
        if line.startswith("#"):
            continue
        t, kind, size, mode, path, reason = line.split("\t")[:6]
        rows.append((int(t), kind, int(size or 0), mode, path, reason))
    return rows


def fake_launchctl(tmp_path):
    """A launchctl stand-in that remembers which labels are loaded (never the real launchd domain)."""
    state = tmp_path / "launchd-state"
    state.mkdir()
    log = tmp_path / "launchctl.log"
    stub = tmp_path / "fakebin" / "launchctl"
    stub.parent.mkdir(exist_ok=True)
    template = (REPO / ".github" / "scripts" / "fake-launchctl.sh").read_text()
    stub.write_text(template.replace("@LOG@", str(log)).replace("@STATE@", str(state)))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return SimpleNamespace(path=stub, log=log, state=state)


def make_installer(root, home_name="home"):
    """A pretend Mac for the installer: HOME under `root` (with ~/Projects and ~/.claude), a stateful launchctl
    stub, a passing xcode-select stub, this test's Python as the only candidate. Never touches the real home,
    launchd domain or Trash."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    home = root / home_name
    (home / "Projects").mkdir(parents=True)
    (home / ".claude").mkdir()
    lc = fake_launchctl(root)
    xcode = root / "fakebin" / "xcode-select"
    xcode.write_text("#!/bin/sh\nexit 0\n")
    xcode.chmod(0o755)
    e = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "AFTERPARTY_LAUNCHCTL": str(lc.path),
         "AFTERPARTY_XCODE_SELECT": str(xcode), "AFTERPARTY_PYTHON_CANDIDATES": sys.executable,
         "AFTERPARTY_NO_NOTIFY": "1", "AFTERPARTY_TEST_TRASH_DIR": str(root / "trash"),
         "DISKCLEAN_CODE_SIGN_DIR": "", "DISKCLEAN_TEMP_DIR": "", "DISKCLEAN_CACHE_DIR": "",
         "PYTHONDONTWRITEBYTECODE": "1"}   # CI checks that no .pyc lands in the checkout

    def run(*args, script="install.sh", repo=REPO, cwd=None, extra=None):
        return subprocess.run(["/bin/sh", str(Path(repo) / script), *map(str, args)], env=dict(e, **(extra or {})),
                              cwd=cwd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    return SimpleNamespace(home=home, env=e, log=lc.log, launchd=lc.state, run=run, tmp=root)


@pytest.fixture
def installer(tmp_path):
    return make_installer(tmp_path)


def snapshot(root):
    """Every entry under root: (path, "dir", mode) / (path, "file", mode, sha256) / (path, "link", target)."""
    out = []
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out.append((rel, "link", os.readlink(p)))
            elif stat.S_ISDIR(st.st_mode):
                out.append((rel, "dir", oct(stat.S_IMODE(st.st_mode))))
            else:
                with open(p, "rb") as f:
                    out.append((rel, "file", oct(stat.S_IMODE(st.st_mode)), hashlib.sha256(f.read()).hexdigest()))
    return sorted(out)


def names(snap):
    return [entry[0] for entry in snap]


def fake_app_builder(root, version="1"):
    """Stands in for app/build-app.sh's build-only mode: writes a tiny bundle with our bundle id (another
    `version` builds a different bundle, for upgrade tests)."""
    p = Path(root) / "fakebin" / ("build-app-" + version)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("""#!/bin/sh
d="$AFTERPARTY_APP_BUILD_DIR/Afterparty.app/Contents"
mkdir -p "$d/MacOS"
printf '#!/bin/sh\\necho afterparty\\n' > "$d/MacOS/Afterparty"
chmod 755 "$d/MacOS/Afterparty"
cat > "$d/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>io.github.drorlazar.afterparty</string></dict></plist>
EOF
""".replace("echo afterparty", "echo afterparty " + version))
    p.chmod(0o755)
    return p
