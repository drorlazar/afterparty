import os
import time

from cleanup.engine import leftovers as L
from cleanup.engine.apfs import SharedCounter
from cleanup.engine.classify import classify
from cleanup.plan import manifest as M
from cleanup.scanner import scan as S
from tests.conftest import OLD, age, state
from tests.test_apfs import clone

X = "/private/var/folders/l1/abc_0000gn/X"
CLONE = X + "/com.google.Chrome.code_sign_clone/code_sign_clone.0BcHWf"


def clone_dir(x, browser="com.google.Chrome", name="code_sign_clone.aaaaaa", ts=OLD):
    c = x / f"{browser}.code_sign_clone" / name / "Google Chrome.app.bundle" / "Contents"
    c.mkdir(parents=True)
    (c / "Info.plist").write_text("x")
    age(c.parents[1], ts)
    return str(c.parents[1])


def test_clone_paths_are_regenerable_before_the_system_rule():
    for p in (CLONE, CLONE.replace("/private", "", 1), os.path.dirname(CLONE)):
        v = classify(p, home="/Users/u", fast=True)
        assert (v["class"], v["rule"], v["action"]) == ("regenerable", "code-sign-clone", "permanent")
        assert v["evidence"][0]["status"] == "passed"


def test_other_temp_paths_stay_system():
    for p in (X, X + "/com.apple.something", X.replace("/X", "/T") + "/com.google.Chrome.code_sign_clone/code_sign_clone.a",
              CLONE + "/Google Chrome.app.bundle"):
        assert classify(p, home="/Users/u", fast=True)["rule"] == "system"


def test_stale_clones_skip_in_use_and_fresh(tmp_path):
    old = clone_dir(tmp_path, name="code_sign_clone.old")
    used = clone_dir(tmp_path, name="code_sign_clone.used")
    clone_dir(tmp_path, name="code_sign_clone.new", ts=time.time())
    clone_dir(tmp_path, browser="com.microsoft.edgemac", name="code_sign_clone.edge")
    (tmp_path / "com.apple.other").mkdir()
    got = L.stale_clones(str(tmp_path), opened=[used + "/Google Chrome.app.bundle/Contents/Info.plist"])
    assert got == {str(tmp_path / "com.google.Chrome.code_sign_clone"): [old],
                   str(tmp_path / "com.microsoft.edgemac.code_sign_clone"):
                       [str(tmp_path / "com.microsoft.edgemac.code_sign_clone/code_sign_clone.edge")]}


def test_unknown_open_files_mean_busy(tmp_path):
    c = clone_dir(tmp_path)
    assert L.busy(c, None) and not L.busy(c, [])
    assert L.stale_clones(str(tmp_path), opened=[]) and not L.stale_clones(str(tmp_path / "missing"), opened=[])


def test_group_is_busy_if_any_clone_is_or_it_holds_something_else(tmp_path):
    c = clone_dir(tmp_path)
    g = os.path.dirname(c)
    assert not L.group_busy(g, [])
    assert L.group_busy(g, [c + "/x"])
    (tmp_path / "com.google.Chrome.code_sign_clone" / "notes.txt").write_text("x")
    assert L.group_busy(g, [])


def test_shared_counter_counts_clone_data_once(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(os.urandom(2_000_000))
    clone(a, tmp_path / "b.bin")
    c = SharedCounter()
    sizes = [c.size(str(p), os.lstat(p)) for p in (a, tmp_path / "b.bin")]
    assert sizes[0] > 1_900_000 and sizes[1] == 0


def test_folder_of_clones_is_sized_like_one_copy(tmp_path):
    src = tmp_path / "app"
    src.mkdir()
    (src / "big.bin").write_bytes(os.urandom(2_000_000))
    for i in range(5):
        d = tmp_path / "x" / f"c{i}"
        d.mkdir(parents=True)
        clone(src / "big.bin", d / "big.bin")
    assert 1_900_000 < M._size(str(tmp_path / "x")) < 2_200_000
    r = S.scan(str(tmp_path))
    assert 1_900_000 < r.stats["bytes"] < 2_200_000  # original + 5 clones share one copy, not 6 × 2 MB


def test_stale_clone_is_removed_and_busy_one_kept(env, run_cli, tmp_path):
    # Final review promoted coverage: classify honours the DISKCLEAN_CODE_SIGN_DIR seam (as it does the temp-folder
    # one), so a stale launch copy really goes end to end, while a brand-new (busy) one stays.
    x = tmp_path / "X"
    old = clone_dir(x, name="code_sign_clone.old")
    new = clone_dir(x, name="code_sign_clone.new", ts=time.time())
    env["DISKCLEAN_CODE_SIGN_DIR"] = str(x)
    r = run_cli("--tier", "1")
    assert "1 stale com.google.Chrome launch copies" in r.stdout, r.stdout + r.stderr
    assert os.path.isdir(old)
    r = run_cli("--tier", "1", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(old) and os.path.isdir(new)
    log = next(state(env).glob("log-*.tsv")).read_text()
    assert any(l.split("\t")[1] == "tier1" and l.split("\t")[4] == str(old) for l in log.splitlines())


def test_clone_seam_matches_only_its_own_folder(tmp_path, monkeypatch):
    x = tmp_path / "X"
    c = clone_dir(x)
    monkeypatch.setenv("DISKCLEAN_CODE_SIGN_DIR", str(x))
    assert L.is_code_sign_clone(c) and L.is_clone_group(os.path.dirname(c))
    assert not L.is_code_sign_clone(str(tmp_path / "Y" / "a.code_sign_clone" / "code_sign_clone.1"))
    assert not L.is_code_sign_clone(c + "/Google Chrome.app.bundle")
    monkeypatch.setenv("DISKCLEAN_CODE_SIGN_DIR", "")
    assert not L.is_code_sign_clone(c)


def test_old_versions_keep_newest_and_kept(tmp_path):
    for v in ("2.1.9", "2.1.77", "2.1.289", "notes"):
        (tmp_path / v).mkdir()
    assert L.old_versions(str(tmp_path)) == [str(tmp_path / "2.1.9"), str(tmp_path / "2.1.77")]
    assert L.old_versions(str(tmp_path), keep=[str(tmp_path / "2.1.9")]) == [str(tmp_path / "2.1.77")]
    assert L.old_versions(str(tmp_path / "2.1.9")) == []


def test_old_claude_version_is_regenerable_unless_in_use(tmp_path):
    home = str(tmp_path)
    d = tmp_path / "Library/Application Support/Claude/claude-code"
    (d / "2.1.289").mkdir(parents=True)
    (d / "2.1.293").mkdir()
    old, new = str(d / "2.1.289"), str(d / "2.1.293")
    assert classify(old, home=home, fast=True)["rule"] == "old-version"
    assert classify(old, home=home, fast=True)["action"] == "permanent"
    assert classify(new, home=home, fast=True)["rule"] == "app-data"


def test_claude_command_target_is_kept(tmp_path):
    v = tmp_path / ".local/share/claude/versions"
    v.mkdir(parents=True)
    (v / "2.1.1").write_text("old")
    (v / "2.1.2").write_text("new")
    (tmp_path / ".local/bin").mkdir()
    os.symlink(v / "2.1.1", tmp_path / ".local/bin/claude")
    assert not L.is_old_claude_version(str(v / "2.1.1"), str(tmp_path))


def test_temp_entries(tmp_path, monkeypatch):
    t = tmp_path / "T"
    t.mkdir()
    monkeypatch.setenv("DISKCLEAN_TEMP_DIR", str(t))
    old = t / "spark-1"
    (old / "sub").mkdir(parents=True)
    (old / "sub" / "f").write_text("x")
    age(old)
    recent = t / "pytest-of-u"
    recent.mkdir()
    touched = t / "mixed"
    (touched / "sub").mkdir(parents=True)
    age(touched)
    (touched / "sub" / "new").write_text("x")  # one fresh file keeps the whole folder
    apple = t / "com.apple.thing"
    apple.mkdir()
    age(apple)
    assert L.stale_temp(str(t), opened=[]) == [str(old)]
    assert L.stale_temp(str(t), opened=[str(old / "sub" / "f")]) == []
    assert classify(str(old), home="/Users/u", fast=True)["rule"] == "temp-stale"
    assert classify(str(recent), home="/Users/u", fast=True)["rule"] == "temp-recent"
    assert classify(str(old / "sub"), home="/Users/u", fast=True)["rule"] != "temp-stale"


def test_diskclean_clears_temp_old_versions_and_download_caches(env, run_cli, tmp_path):
    home = tmp_path / "home"
    t = tmp_path / "T"
    (t / "spark-1").mkdir(parents=True)
    age(t / "spark-1")
    (t / "fresh").mkdir()
    env["DISKCLEAN_TEMP_DIR"] = str(t)
    cc = home / "Library/Application Support/Claude/claude-code"
    (cc / "2.1.289").mkdir(parents=True)
    (cc / "2.1.293").mkdir()
    vsix = home / "Library/Application Support/Code/CachedExtensionVSIXs"
    vsix.mkdir(parents=True)
    (vsix / "ext-1.0").write_text("x")
    crx = home / "Library/Application Support/Google/GoogleUpdater/crx_cache"
    crx.mkdir(parents=True)
    (crx / "old").write_text("x")
    age(crx / "old")
    (crx / "new").write_text("x")
    r = run_cli("--tier", "1", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert not (t / "spark-1").exists() and (t / "fresh").exists()
    assert not (cc / "2.1.289").exists() and (cc / "2.1.293").exists()
    assert not (vsix / "ext-1.0").exists() and vsix.exists()
    assert not (crx / "old").exists() and (crx / "new").exists()


def test_cache_owner_strips_helper_suffixes():
    assert L.cache_owner("com.google.Chrome.helper") == "com.google.chrome"
    assert L.cache_owner("com.adobe.cep.CEPHtmlEngine Helper (GPU)") == "com.adobe.cep.cephtmlengine"
    assert L.cache_owner("org.blenderfoundation.blender") == "org.blenderfoundation.blender"
    assert L.owner_running("com.google.Chrome.helper", {"com.google.chrome"})
    assert L.owner_running("org.blenderfoundation.blender", {"org.blenderfoundation.blender.thumbnailer"})
    assert not L.owner_running("org.blenderfoundation.blender", {"com.google.chrome", "clang"})
    assert L.owner_running("clang", {"clang"}) and L.owner_running("x", None)


def _cache(c, name, ts=OLD, size=1000):
    d = c / name / "com.apple.metal"
    d.mkdir(parents=True)
    (d / "shaders").write_bytes(os.urandom(size))
    age(c / name, ts)
    return str(c / name)


def test_stale_caches(tmp_path, monkeypatch):
    c = tmp_path / "C"
    old = _cache(c, "org.blenderfoundation.blender")
    _cache(c, "com.apple.FontRegistry")                                   # Apple's: never
    _cache(c, "com.google.Chrome.helper")                                 # app running
    _cache(c, "clang", ts=time.time() - 3 * 86400)                        # small and only 3 days idle
    big = _cache(c, "com.big.app", ts=time.time() - 2 * 86400, size=64 * 1024)
    monkeypatch.setattr(L, "BIG_CACHE", 32 * 1024)  # allocated bytes: the 1 KB caches take one 4 KB block
    got = L.stale_caches(str(c), opened=[], running={"com.google.chrome"})
    assert got == [big, old]
    assert L.stale_caches(str(c), opened=[old + "/com.apple.metal/shaders"], running={"com.google.chrome"}) == [big]
    monkeypatch.setattr(L, "running_ids", lambda: None)  # ps failed: every app counts as running
    assert L.stale_caches(str(c), opened=[]) == []
    monkeypatch.setenv("DISKCLEAN_CACHE_DIR", str(c))
    assert classify(old, home="/Users/u", fast=True)["rule"] == "system-app-cache"
    assert classify(str(c / "com.apple.FontRegistry"), home="/Users/u", fast=True)["rule"] == "system"


def test_diskclean_clears_idle_app_caches(env, run_cli, tmp_path):
    c = tmp_path / "C"
    old = _cache(c, "org.example.idleapp")
    fresh = _cache(c, "org.example.busyapp", ts=time.time())
    apple = _cache(c, "com.apple.metal")
    env["DISKCLEAN_CACHE_DIR"] = str(c)
    r = run_cli("--tier", "1", "--apply", "--yes")
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(old) and os.path.isdir(fresh) and os.path.isdir(apple)


def test_app_extensions_dont_count_as_the_app_running(tmp_path):
    import plistlib
    app = tmp_path / "Blender.app"
    ext = app / "Contents/PlugIns/blender-thumbnailer.appex"
    for d, bid in ((app, "org.blenderfoundation.blender"), (ext, "org.blenderfoundation.blender.thumbnailer")):
        (d / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
        with open(d / "Contents/Info.plist", "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bid}, f)
    only_ext = L.ids_of([str(ext / "Contents/MacOS/blender-thumbnailer")])
    assert not L.owner_running("org.blenderfoundation.blender", only_ext)
    app_running = L.ids_of([str(app / "Contents/MacOS/Blender")])
    assert "org.blenderfoundation.blender" in app_running and "blender" in app_running
    assert L.owner_running("org.blenderfoundation.blender", app_running)


def test_copy_made_since_the_browser_started_is_kept(tmp_path):
    before = clone_dir(tmp_path, name="code_sign_clone.before", ts=OLD)
    since = clone_dir(tmp_path, name="code_sign_clone.since", ts=time.time() - 3600)
    started = time.time() - 2 * 3600
    got = L.stale_clones(str(tmp_path), opened=[], starts={"com.google.chrome": started})
    assert got == {str(tmp_path / "com.google.Chrome.code_sign_clone"): [before]}
    assert L.busy(since, [], starts={"com.google.chrome": started})
    assert not L.busy(since, [], starts={})          # browser not running: only lsof / freshness decide
    starts = L.process_starts()
    assert starts is None or all("." in k for k in starts)


def test_one_locked_temp_item_doesnt_stop_the_rest(env, run_cli, tmp_path):
    t = tmp_path / "T"
    locked = t / "a-locked"
    (locked / "inner").mkdir(parents=True)
    (t / "b-free").mkdir()
    age(t)
    os.chmod(locked, 0o500)  # can't remove "inner" from it
    env["DISKCLEAN_TEMP_DIR"] = str(t)
    try:
        r = run_cli("--tier", "1", "--apply", "--yes")
    finally:
        os.chmod(locked, 0o700)
    assert not (t / "b-free").exists() and (locked / "inner").exists(), r.stdout + r.stderr
    assert "1 failed" in r.stdout  # EACCES is a real failure; only EPERM (macOS-protected) is skipped quietly
