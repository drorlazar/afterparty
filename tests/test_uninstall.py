import fcntl
import json
import os
import shlex
import subprocess

import pytest

from cleanup.installer import main as inst
from tests.conftest import REPO, fake_app_builder, names, snapshot

AP = "io.github.drorlazar.afterparty"


def _pretend_user(installer):
    s = installer.home / ".claude/settings.json"
    s.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/mine.sh"}]}]}},
                            indent=2) + "\n")
    (installer.home / ".claude/CLAUDE.md").write_text("# Mine\n")
    return s


def test_uninstall_leaves_only_settings_history_and_logs(installer):
    s = _pretend_user(installer)
    before, before_settings = names(snapshot(installer.home)), s.read_text()
    assert installer.run("--yes", "--hygiene-rule", "yes").returncode == 0
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    h = installer.home
    assert s.read_text() == before_settings and (h / ".claude/CLAUDE.md").read_text() == "# Mine\n"
    extra = sorted(set(names(snapshot(h))) - set(before))
    allowed = (".config", ".config/afterparty", ".local", ".local/state", ".local/state/afterparty")
    assert [p for p in extra if p not in allowed
            and not p.startswith((".config/afterparty/", ".local/state/afterparty/"))] == []
    assert (h / ".config/afterparty/config.json").exists()
    # ruling P2: a reduced install record stays (the data folders and the settings item), so --purge works later
    man = json.loads((h / ".local/state/afterparty/install-manifest.json").read_text())
    assert man["items"] and {i["kind"] for i in man["items"]} <= {"dir", "config"}
    assert all(i["path"].startswith((str(h / ".config"), str(h / ".local"))) for i in man["items"])
    assert not (h / ".local/state/afterparty/server.json").exists()          # ruling P3
    assert not list(installer.launchd.iterdir())                    # no job of ours is loaded any more
    assert "delete it yourself" in r.stdout and str(REPO) in r.stdout   # the code checkout stays (approved A6)


def test_purge_leaves_nothing(installer):
    _pretend_user(installer)
    before = snapshot(installer.home)
    assert installer.run("--yes", "--hygiene-rule", "yes").returncode == 0
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert snapshot(installer.home) == before         # names, modes and contents, byte for byte


def test_purge_names_but_never_deletes_the_native_apps_own_data(installer):
    """WebKit storage, caches, preferences and saved window state of Afterparty.app (what launching the app
    leaves) are listed with exact paths; nothing is deleted because its name holds our bundle id."""
    _pretend_user(installer)
    assert installer.run("--yes", "--hygiene-rule", "yes").returncode == 0
    lib = installer.home / "Library"
    made = [lib / "WebKit" / AP / "WebsiteData" / "LocalStorage" / "x.localstorage", lib / "Caches" / AP / "c",
            lib / "Preferences" / (AP + ".plist"), lib / "Saved Application State" / (AP + ".savedState") / "w"]
    for p in made:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("native")
    def native():
        return [e for e in snapshot(lib) if not e[0].startswith("LaunchAgents")]
    before = native()
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert native() == before                          # all of it is still there, unchanged
    for p in (lib / "WebKit" / AP, lib / "Caches" / AP, lib / "Preferences" / (AP + ".plist"),
              lib / "Saved Application State" / (AP + ".savedState")):
        assert "rm -rf %s" % shlex.quote(str(p)) in r.stdout
    assert "HTTPStorages" not in r.stdout              # only what is actually present


def test_purge_after_a_plain_uninstall_still_leaves_nothing(installer):
    """Ruling P2: the README's promise holds: uninstall first, --purge later, and the home folder is as it was."""
    _pretend_user(installer)
    before = snapshot(installer.home)
    assert installer.run("--yes", "--hygiene-rule", "yes").returncode == 0
    assert installer.run("--yes", script="uninstall.sh").returncode == 0
    assert (installer.home / ".config/afterparty/config.json").exists()
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert snapshot(installer.home) == before


def test_purge_after_a_plain_uninstall_still_stops_for_a_changed_entry_point(installer):
    """R7 with P2: a shim the user changed, left by a plain uninstall, stays in the reduced record, so a later
    --purge still stops before any data while it is there."""
    assert installer.run("--yes").returncode == 0
    shim = installer.home / ".local/bin/afterparty"
    shim.unlink()
    shim.write_text("#!/bin/sh\necho mine\n")
    assert installer.run("--yes", script="uninstall.sh").returncode == 0
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 4 and "~/.local/bin/afterparty" in r.stderr
    assert (installer.home / ".config/afterparty/config.json").exists()
    shim.unlink()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert not (installer.home / ".config/afterparty").exists()


def test_purge_after_a_plain_uninstall_stops_for_anything_at_an_entry_path(installer):
    """R9 ruling: the fixed entry paths count whatever the record says. After a plain uninstall forgot the shim it
    removed, a file someone puts at ~/.local/bin/afterparty still stops --purge before any data or lock."""
    assert installer.run("--yes").returncode == 0
    assert installer.run("--yes", script="uninstall.sh").returncode == 0
    cfgd = installer.home / ".config/afterparty"
    before = {p.name: p.read_bytes() for p in cfgd.iterdir() if p.is_file()}
    shim = installer.home / ".local/bin/afterparty"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\necho someone else's\n")
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 4 and "~/.local/bin/afterparty" in r.stderr and "stopped before touching" in r.stderr
    assert {p.name: p.read_bytes() for p in cfgd.iterdir() if p.is_file()} == before
    assert not (installer.tmp / "trash" / ".Trash").exists() or not list((installer.tmp / "trash" / ".Trash").iterdir())
    shim.unlink()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert not cfgd.exists()


def test_a_plain_uninstall_removes_the_servers_state_files_through_their_receipts(installer):
    """Ruling P3: server.json and the server plist (it holds the per-launch key) go with a plain uninstall, each only
    through its write receipt; one that changed since Afterparty wrote it stays and is listed."""
    import sys
    assert installer.run("--yes").returncode == 0
    state = installer.home / ".local/state/afterparty"
    code = ("from cleanup import launcher; launcher._write_private(launcher.default_state(), "
            "{'mode': 'launchd', 'label': launcher.SERVER_LABEL, 'port': 1, 'token': 't'}); "
            "from cleanup import fsops; p = launcher.os.path.join(launcher.config.state_dir(), "
            "launcher.SERVER_LABEL + '.plist'); fsops.replace_owned(p, b'<plist/>', mode=0o600); "
            "fsops.note_written(p, b'<plist/>')")
    subprocess.run([sys.executable, "-c", code], env=dict(installer.env, PYTHONPATH=str(REPO)), check=True)
    plist = state / (AP + ".server.plist")
    assert (state / "server.json").exists() and plist.exists()
    plist.write_text("<plist>edited</plist>")
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (state / "server.json").exists()
    assert plist.read_text() == "<plist>edited</plist>" and AP + ".server.plist" in r.stdout


def test_purge_never_deletes_a_file_a_forged_receipt_names(installer):
    """Ruling P4: a receipts file that no longer certifies itself vouches for nothing. A forged entry with the real
    checksum of the user's file doesn't make it deletable: it goes to the Trash, intact."""
    from cleanup import fsops
    assert installer.run("--yes").returncode == 0
    state = installer.home / ".local/state/afterparty"
    mine = state / "logs" / "my-notes.txt"
    mine.write_text("mine")
    rf = state / "logs" / fsops.RECEIPTS
    data = json.loads(rf.read_text()) if rf.exists() else {}
    data["my-notes.txt"] = fsops.file_sum(str(mine))
    rf.write_text(json.dumps(data))
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    (landed,) = [p for p in (installer.tmp / "trash" / ".Trash").iterdir() if p.name.endswith("-my-notes.txt")]
    assert landed.read_text() == "mine" and not mine.exists()


def test_purge_moves_crash_leftovers_to_the_trash_and_lists_them(installer):
    """Leftovers of a write cut short (`.afterparty-*.tmp`, a pending protection's `.tmp-<uuid>`) carry no receipt:
    in folders the installer created they go to the Trash, listed; never deleted for good."""
    assert installer.run("--yes").returncode == 0
    tmp = installer.home / ".config/afterparty/.afterparty-abc123.tmp"
    tmp.write_text("half a settings file")
    pending = installer.home / ".local/state/afterparty/keep-pending/.tmp-0123abcd"
    pending.write_text('{"keep": "~/Projects/x"')
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    landed = {p.name.split("-", 1)[1]: p.read_text() for p in (installer.tmp / "trash" / ".Trash").iterdir()}
    assert landed[".afterparty-abc123.tmp"] == "half a settings file"
    assert landed[".tmp-0123abcd"] == '{"keep": "~/Projects/x"'
    assert ".afterparty-abc123.tmp" in r.stdout and ".tmp-0123abcd" in r.stdout
    assert not (installer.home / ".config/afterparty").exists()


def test_purge_on_a_bare_home_leaves_nothing(installer):
    before = snapshot(installer.home)
    assert installer.run("--yes").returncode == 0
    assert (installer.home / ".claude/settings.json").exists()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert snapshot(installer.home) == before          # the settings file we created is gone too


def test_purge_moves_unknown_files_in_our_folders_to_the_trash(installer):
    assert installer.run("--yes").returncode == 0
    mine = installer.home / ".local/state/afterparty/my-notes.txt"
    mine.write_text("mine")
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    (landed,) = [p for p in (installer.tmp / "trash" / ".Trash").iterdir() if p.name.endswith("-my-notes.txt")]
    assert r.returncode == 0 and not mine.exists() and landed.read_text() == "mine" and "my-notes.txt" in r.stdout


def test_purge_refuses_folders_that_arent_afterpartys(installer):
    assert installer.run("--yes").returncode == 0
    odd = installer.home / "Documents"
    odd.mkdir()
    (odd / "config.json").write_text("the user's")
    r = installer.run("--yes", "--purge", script="uninstall.sh", extra={"AFTERPARTY_CONFIG_DIR": str(odd)})
    assert r.returncode == 2 and (odd / "config.json").read_text() == "the user's"


def test_items_changed_since_install_are_left_and_listed(installer):
    assert installer.run("--yes").returncode == 0
    f = installer.home / ".claude/skills/tidy/SKILL.md"
    f.write_text("edited")
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 0 and "~/.claude/skills/tidy (changed since install)" in r.stdout
    assert f.read_text() == "edited" and not (installer.home / ".claude/skills/afterparty").exists()


def test_an_edited_hook_entry_is_left_and_listed(installer):
    assert installer.run("--yes").returncode == 0
    s = installer.home / ".claude/settings.json"
    d = json.loads(s.read_text())
    d["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = 60
    s.write_text(json.dumps(d, indent=2) + "\n")
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 0 and "edited by hand" in r.stdout
    assert json.loads(s.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 60


def test_an_edited_app_is_left_and_listed(installer):
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    assert installer.run("--yes", "--app", "yes", extra=ex).returncode == 0
    app = installer.home / "Applications/Afterparty.app"
    (app / "Contents/MacOS/Afterparty").write_text("#!/bin/sh\necho edited\n")
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/note.txt").write_text("mine")
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 0 and "~/Applications/Afterparty.app (changed since install)" in r.stdout
    assert (app / "Contents/Resources/note.txt").read_text() == "mine"


def test_a_manifest_naming_other_paths_is_refused(installer):
    assert installer.run("--yes").returncode == 0
    victim = installer.tmp / "victim.txt"
    victim.write_text("keep")
    m = installer.home / ".local/state/afterparty/install-manifest.json"
    data = json.loads(m.read_text())
    data["items"].append({"kind": "link", "path": str(victim), "sum": "link:x", "state": "done"})
    m.write_text(json.dumps(data))
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 3 and victim.read_text() == "keep"
    assert (installer.home / ".claude/skills/tidy").exists()     # nothing at all was removed


def test_uninstall_asks_first_and_dry_run_changes_nothing(installer):
    assert installer.run("--yes").returncode == 0
    snap = snapshot(installer.home)
    assert installer.run(script="uninstall.sh").returncode == 1
    r = installer.run("--dry-run", "--purge", script="uninstall.sh")
    assert r.returncode == 0 and "--purge" in r.stdout and snapshot(installer.home) == snap


def test_uninstall_keeps_other_files_in_shared_folders(installer):
    b = installer.home / ".local/bin"
    b.mkdir(parents=True)
    (b / "mytool").write_text("#!/bin/sh\n")
    assert installer.run("--yes").returncode == 0
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert sorted(p.name for p in b.iterdir()) == ["mytool"]


def test_failed_activation_is_not_success_and_completes_on_rerun(installer):
    before = snapshot(installer.home)
    (installer.launchd / "fail-bootstrap").write_text("")
    r = installer.run("--yes")
    assert r.returncode == 4 and "couldn't start" in r.stderr and "Run the installer again" in r.stderr
    assert "Afterparty is installed" not in r.stdout
    (installer.launchd / "fail-bootstrap").unlink()
    assert installer.run("--yes").returncode == 0
    assert (installer.launchd / (AP + ".weekly")).exists()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert snapshot(installer.home) == before


def test_a_job_that_wont_stop_keeps_its_receipt(installer):
    assert installer.run("--yes").returncode == 0
    (installer.launchd / ("stuck-" + AP + ".weekly")).write_text("")
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 4 and AP + ".weekly" in r.stderr
    assert (installer.home / ".local/state/afterparty/install-manifest.json").exists()
    (installer.launchd / ("stuck-" + AP + ".weekly")).unlink()
    assert installer.run("--yes", script="uninstall.sh").returncode == 0


def test_busy_while_another_run_holds_the_lock(installer):
    assert installer.run("--yes").returncode == 0
    fd = os.open(str(installer.home), os.O_RDONLY)          # the install lock is the home folder itself
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert installer.run("--yes", script="uninstall.sh").returncode == 5
        assert installer.run("--yes").returncode == 5
    finally:
        os.close(fd)


def test_purge_touches_no_data_while_a_cleanup_holds_its_lock(installer):
    from cleanup.plan import bridge
    assert installer.run("--yes").returncode == 0
    cfg = installer.home / ".config/afterparty/config.json"
    with bridge.lock(str(installer.home / ".local/state/afterparty")):   # a cleanup or undo is running
        r = installer.run("--yes", "--purge", script="uninstall.sh")
        assert cfg.exists()                                             # settings and history untouched
    assert r.returncode == 5 and "cleanup is running" in r.stderr
    assert (installer.home / ".local/bin/afterparty").exists()          # refused before stopping any job (I4)
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0   # finishes once it's free
    assert not cfg.exists()


def test_purge_touches_no_data_while_the_settings_lock_is_held(installer):
    from cleanup import fsops
    assert installer.run("--yes").returncode == 0
    with fsops.held(str(installer.home / ".config/afterparty/.lock")):   # a settings write in progress
        r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 4 and (installer.home / ".config/afterparty/config.json").exists()


def _purge_in_process(installer, monkeypatch, hook):
    from cleanup.installer import main as inst
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR", "AFTERPARTY_INSTALL_LOCK_FD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in installer.env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(inst, "_purge_hook", hook)
    return inst.uninstall(purge=True, yes=True)


def _protect():
    import sys
    return subprocess.Popen([sys.executable, "-c", "from cleanup import config; config.add_keep('~/Projects/new')"],
                            env=dict(os.environ, PYTHONPATH=str(REPO)))


def test_a_new_settings_writer_during_the_purge_waits_and_its_write_survives(installer, monkeypatch):
    """Codex probe: a NEW arrival while purge holds every lock (not an existing waiter). It must wait, and what it
    writes after the purge must never be deleted by it."""
    import time
    assert installer.run("--yes").returncode == 0
    seen = {}

    def hook(stage):
        if stage == "locked":
            seen["p"] = _protect()
            time.sleep(0.5)
            seen["waited"] = seen["p"].poll() is None
    assert _purge_in_process(installer, monkeypatch, hook) == 0
    assert seen["p"].wait(timeout=30) == 0 and seen["waited"] is True
    cfg = json.loads((installer.home / ".config/afterparty/config.json").read_text())
    assert cfg["keep"] == ["~/Projects/new"]                              # written after the purge, kept


def test_a_writer_arriving_after_the_lock_files_go_is_never_undone(installer, monkeypatch):
    assert installer.run("--yes").returncode == 0
    seen = {}

    def hook(stage):
        if stage == "unlinked":
            seen["p"] = _protect()
            assert seen["p"].wait(timeout=30) == 0                        # it got a fresh lock and wrote at once
    assert _purge_in_process(installer, monkeypatch, hook) == 0
    cfg = json.loads((installer.home / ".config/afterparty/config.json").read_text())
    assert cfg["keep"] == ["~/Projects/new"]


def test_purge_deletes_only_what_a_receipt_names_unchanged(installer):
    """Codex probe: a file named exactly like Afterparty's backups, but with no receipt, is never deleted: inside a
    folder the installer created it goes to the Trash (recoverable), and so does any other unreceipted file."""
    (installer.home / ".claude/settings.json").write_text('{"model": "x"}\n')   # gives the install a real backup
    assert installer.run("--yes").returncode == 0
    state = installer.home / ".local/state/afterparty"
    backups = state / "backups"
    ours = [p for p in backups.iterdir() if p.name.endswith(".bak")]
    assert len(ours) == 1
    lookalike = backups / "holiday.1700000000-0123abcd.bak"
    lookalike.write_text("the user's file with a backup-like name")
    (state / "journal").mkdir()
    (state / "journal" / "20261011-101010-abcd.jsonl").write_text("{}\n")   # engine-style name, no receipt
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0
    trash = installer.tmp / "trash" / ".Trash"
    landed = {p.name.split("-", 1)[1]: p for p in trash.iterdir()}
    assert landed["holiday.1700000000-0123abcd.bak"].read_text().startswith("the user's")   # in the Trash, intact
    assert "20261011-101010-abcd.jsonl" in landed
    assert not lookalike.exists() and not state.exists()               # nothing left in place
    assert not ours[0].exists() and ours[0].name not in landed         # receipted, unchanged: removed for good
    assert "Moved to the Trash" in r.stdout and "holiday.1700000000-0123abcd.bak" in r.stdout


def test_files_in_a_folder_afterparty_didnt_create_are_never_touched(installer):
    state = installer.home / ".local/state/afterparty"
    state.mkdir(parents=True)                                            # existed before the install
    (state / "mine.txt").write_text("the user's")
    assert installer.run("--yes").returncode == 0
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0 and (state / "mine.txt").read_text() == "the user's"
    assert not (installer.tmp / "trash" / ".Trash").exists() or not list((installer.tmp / "trash" / ".Trash").iterdir())
    assert "mine.txt" in r.stdout


def test_a_receipted_file_changed_since_goes_to_the_trash(installer):
    assert installer.run("--yes").returncode == 0
    cfg = installer.home / ".config/afterparty/config.json"
    edited = cfg.read_text().replace('"autopilot": "report"', '"autopilot": "report" ')   # edited by hand
    cfg.write_text(edited)
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    landed = [p for p in (installer.tmp / "trash" / ".Trash").iterdir() if p.name.endswith("-config.json")]
    assert r.returncode == 0 and not cfg.exists() and len(landed) == 1 and landed[0].read_text() == edited


def test_a_file_recreated_right_after_purge_stages_it_survives(installer, monkeypatch):
    """Codex probe (R5): no path is hashed and then unlinked. A replacement written at the path the instant after
    --purge moved the receipted file aside survives; only the staged, checked copy is deleted."""
    from cleanup import fsops
    assert installer.run("--yes").returncode == 0
    cfg = str(installer.home / ".config/afterparty/config.json")
    real = fsops.rename_exclusive

    def racing(src, dst):
        err = real(src, dst)
        if err is None and src == cfg:
            with open(src, "w") as f:
                f.write("written by someone else meanwhile\n")
        return err
    monkeypatch.setattr(fsops, "rename_exclusive", racing)
    assert _purge_in_process(installer, monkeypatch, None) == 0
    with open(cfg) as f:
        assert f.read() == "written by someone else meanwhile\n"


def test_an_edited_receipts_file_goes_to_the_trash(installer):
    """Codex probe (R6): a receipts file is never deleted because of its name; a hand-edited one is moved to the
    Trash and listed."""
    assert installer.run("--yes").returncode == 0
    rf = installer.home / ".config/afterparty/.afterparty-receipts.json"
    data = json.loads(rf.read_text())
    data["notes.txt"] = "sha256:" + "0" * 64
    rf.write_text(json.dumps(data))
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    landed = [p for p in (installer.tmp / "trash" / ".Trash").iterdir() if p.name.endswith("-.afterparty-receipts.json")]
    assert r.returncode == 0 and len(landed) == 1, r.stdout + r.stderr
    assert json.loads(landed[0].read_text())["notes.txt"] == "sha256:" + "0" * 64
    assert not rf.exists() and ".afterparty-receipts.json" in r.stdout


def test_purge_stops_before_any_data_while_a_changed_entry_point_remains(installer):
    """Codex probe (R7): a shim the user changed stays in place, so Afterparty could still be started. --purge stops
    before touching settings, history or locks, names the shim and exits non-zero; once it's gone, purge finishes."""
    assert installer.run("--yes").returncode == 0
    shim = installer.home / ".local/bin/afterparty"
    shim.unlink()
    shim.write_text("#!/bin/sh\necho mine\n")
    cfgd, state = installer.home / ".config/afterparty", installer.home / ".local/state/afterparty"
    keep = [cfgd / "config.json", cfgd / ".lock", cfgd / ".afterparty-receipts.json", cfgd / "python",
            state / "install-manifest.json"]
    before = {p: p.read_bytes() for p in keep if p.exists()}
    assert cfgd / "config.json" in before and cfgd / ".lock" in before
    locks = sorted(p.name for p in state.iterdir() if p.name.endswith(".lock"))
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 4 and "~/.local/bin/afterparty" in r.stderr and "stopped before touching" in r.stderr
    assert all(p.exists() for p in before) and (cfgd / "config.json").read_bytes() == before[cfgd / "config.json"]
    assert sorted(p.name for p in state.iterdir() if p.name.endswith(".lock")) == locks
    assert shim.read_text() == "#!/bin/sh\necho mine\n"
    assert not (installer.home / ".claude/skills/afterparty").exists()   # the unchanged entry points are gone
    assert not (installer.tmp / "trash" / ".Trash").exists() or not list((installer.tmp / "trash" / ".Trash").iterdir())
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 4   # still there: still stops
    assert (cfgd / "config.json").exists()
    shim.unlink()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0
    assert not cfgd.exists()


def test_a_shim_recreated_while_purge_removes_it_stops_the_purge(installer, monkeypatch):
    """Codex probe (R9): purge moves the shim aside and something writes a new executable shim at that path. The
    new shim is preserved, so an entry point remains: exit 4, settings intact, the record kept, the path named."""
    from cleanup import fsops
    assert installer.run("--yes").returncode == 0
    shim = str(installer.home / ".local/bin/afterparty")
    real = fsops.rename_exclusive

    def racing(src, dst):
        err = real(src, dst)
        if err is None and src == shim:
            with open(src, "w") as f:
                f.write("#!/bin/sh\necho someone else's\n")
            os.chmod(src, 0o755)
        return err
    monkeypatch.setattr(fsops, "rename_exclusive", racing)
    assert _purge_in_process(installer, monkeypatch, None) == 4          # never 0 with the settings deleted
    monkeypatch.setattr(fsops, "rename_exclusive", real)
    assert (installer.home / ".config/afterparty/config.json").exists() and os.access(shim, os.X_OK)
    man = json.loads((installer.home / ".local/state/afterparty/install-manifest.json").read_text())
    assert shim in [i["path"] for i in man["items"]]
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 4   # still there: still stops
    os.unlink(shim)
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0


def test_a_shim_recreated_while_purge_takes_its_locks_stops_the_purge(installer, monkeypatch):
    """Codex probe (R12): the shim reappears while purge acquires the settings lock (after the early check). The
    decisive check runs with every lock held, right before the data: exit 4, shim and settings intact, record kept."""
    assert installer.run("--yes").returncode == 0
    shim = installer.home / ".local/bin/afterparty"

    def hook(stage):
        if stage == "locked":
            shim.write_text("#!/bin/sh\necho back again\n")
            os.chmod(str(shim), 0o755)
    assert _purge_in_process(installer, monkeypatch, hook) == 4
    assert (installer.home / ".config/afterparty/config.json").exists() and os.access(str(shim), os.X_OK)
    man = json.loads((installer.home / ".local/state/afterparty/install-manifest.json").read_text())
    assert str(shim) in [i["path"] for i in man["items"]]
    shim.unlink()
    assert installer.run("--yes", "--purge", script="uninstall.sh").returncode == 0


def test_a_shim_recreated_during_purge_recovery_stops_the_purge(installer, monkeypatch):
    """Codex probe (R15): the shim reappears while purge runs its recovery and preparation under the locks. The
    decisive check comes after all of that, right before the first deletion: exit 4, settings intact, record kept."""
    from cleanup.installer import edits
    assert installer.run("--yes").returncode == 0
    shim = installer.home / ".local/bin/afterparty"
    phase, real_sweep = {}, edits.sweep

    def hook(stage):
        phase[stage] = True

    def sweep(parent):
        if phase.get("locked") and not shim.exists():
            shim.write_text("#!/bin/sh\necho back during recovery\n")
            os.chmod(str(shim), 0o755)
        return real_sweep(parent)
    monkeypatch.setattr(edits, "sweep", sweep)
    assert _purge_in_process(installer, monkeypatch, hook) == 4
    assert (installer.home / ".config/afterparty/config.json").exists() and os.access(str(shim), os.X_OK)
    man = json.loads((installer.home / ".local/state/afterparty/install-manifest.json").read_text())
    assert str(shim) in [i["path"] for i in man["items"]]


def test_the_server_is_stopped_before_anything_else(installer, monkeypatch):
    """R7: the server goes first; if it won't stop, nothing else is removed."""
    assert installer.run("--yes").returncode == 0
    monkeypatch.setattr(inst, "_stop_server", lambda: (False, "still answering"))
    assert _purge_in_process(installer, monkeypatch, None) == 4
    assert os.path.lexists(installer.home / ".local/bin/afterparty")
    assert (installer.home / ".claude/skills/afterparty").exists()
    assert (installer.home / ".config/afterparty/config.json").exists()


def test_purge_never_removes_an_empty_folder_it_didnt_make(installer):
    state = installer.home / ".local/state/afterparty"
    (state / "empty-mine").mkdir(parents=True)                           # the state folder existed before
    assert installer.run("--yes").returncode == 0
    (installer.home / ".config/afterparty/empty-too").mkdir()           # inside a folder the installer created
    r = installer.run("--yes", "--purge", script="uninstall.sh")
    assert r.returncode == 0 and (state / "empty-mine").is_dir()
    landed = [p.name for p in (installer.tmp / "trash" / ".Trash").iterdir()]
    assert [n for n in landed if n.endswith("-empty-too")] and not (installer.home / ".config/afterparty").exists()


def test_a_receipt_naming_another_job_is_refused_and_that_job_untouched(installer):
    assert installer.run("--yes").returncode == 0
    (installer.launchd / "com.example.other").write_text("")             # someone else's loaded job
    m = installer.home / ".local/state/afterparty/install-manifest.json"
    data = json.loads(m.read_text())
    for it in data["items"]:
        if it["kind"] == "job":
            it["label"] = "com.example.other"
    m.write_text(json.dumps(data))
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 3 and (installer.launchd / "com.example.other").exists()


def test_no_install_record_means_nothing_is_removed(installer):
    r = installer.run("--yes", script="uninstall.sh")
    assert r.returncode == 1 and "nothing was removed" in r.stdout


def test_afterparty_uninstall_command(installer):
    assert installer.run("--yes").returncode == 0
    ap = installer.home / ".local/bin/afterparty"
    r = subprocess.run(["/bin/sh", str(ap), "uninstall", "--yes"], env=installer.env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.path.lexists(ap)


# ---- update
def _git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _commit(clone, text, msg):
    (clone / "a.txt").write_text(text)
    _git("commit", "-qam", msg, cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)


@pytest.fixture
def origin_and_copy(tmp_path, monkeypatch):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))       # update locks the home folder: a pretend one
    monkeypatch.delenv("AFTERPARTY_INSTALL_LOCK_FD", raising=False)
    bare, seed, copy = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "copy"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("clone", "-q", str(bare), str(seed))
    (seed / "a.txt").write_text("1")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "one", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    _git("clone", "-q", str(bare), str(copy))
    return bare, seed, copy


def test_update_fast_forwards_from_the_validated_remote_then_reinstalls(origin_and_copy):
    bare, seed, copy = origin_and_copy
    _commit(seed, "2", "two")
    seen = []
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: seen.append(r) or 0) == 0
    assert (copy / "a.txt").read_text() == "2" and seen == [str(copy)]


def test_update_ignores_a_branch_tracking_another_remote(origin_and_copy, tmp_path):
    bare, seed, copy = origin_and_copy
    evil = tmp_path / "evil.git"
    _git("clone", "-q", "--bare", str(bare), str(evil))
    other = tmp_path / "other"
    _git("clone", "-q", str(evil), str(other))
    _commit(other, "evil", "not from the official repo")
    _git("remote", "add", "evil", str(evil), cwd=copy)
    _git("fetch", "-q", "evil", cwd=copy)
    _git("branch", "-q", "--set-upstream-to=evil/main", "main", cwd=copy)
    _commit(seed, "2", "two")
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: 0) == 0
    assert (copy / "a.txt").read_text() == "2"


def test_update_refuses_an_unofficial_origin_and_local_changes(origin_and_copy):
    bare, _, copy = origin_and_copy
    assert inst.update(repo=str(copy), official=("https://github.com/drorlazar/afterparty",), reinstall=lambda r, fd: 0) == 2
    (copy / "a.txt").write_text("mine")
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: 0) == 2
    assert (copy / "a.txt").read_text() == "mine"


def test_update_hands_its_lock_to_the_reinstall(origin_and_copy):
    import fcntl as _f
    bare, seed, copy = origin_and_copy
    _commit(seed, "2", "two")
    seen = {}

    def reinstall(repo, fd):
        probe = os.open(os.path.expanduser("~"), os.O_RDONLY)
        try:
            _f.flock(probe, _f.LOCK_EX | _f.LOCK_NB)
            seen["free"] = True
        except OSError:
            seen["free"] = False                            # still held: nothing can slip in between
        finally:
            os.close(probe)
        seen["fd"] = fd
        return 0
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=reinstall) == 0
    assert seen["free"] is False and isinstance(seen["fd"], int)


def test_update_refuses_another_branch(origin_and_copy):
    bare, _, copy = origin_and_copy
    _git("checkout", "-q", "-b", "mine", cwd=copy)
    assert inst.update(repo=str(copy), official=(str(bare),), reinstall=lambda r, fd: 0) == 2


def test_official_origins_are_the_public_repository():
    assert "https://github.com/drorlazar/afterparty.git" in inst.OFFICIAL
    assert all("drorlazar/afterparty" in o for o in inst.OFFICIAL)
