import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time

from tests.conftest import REPO, fake_app_builder, make_installer, names, snapshot

AP = "io.github.drorlazar.afterparty"


def _manifest(installer):
    return json.loads((installer.home / ".local/state/afterparty/install-manifest.json").read_text())


def _in_process(installer, monkeypatch, *args):
    for k in ("AFTERPARTY_CONFIG_DIR", "AFTERPARTY_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in installer.env.items():
        monkeypatch.setenv(k, v)
    from cleanup.installer import main as inst
    return inst.main(["install", "--python", sys.executable, *args])


def test_dry_run_lists_everything_and_changes_nothing(installer):
    before = snapshot(installer.home)
    r = installer.run("--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("~/.local/bin/afterparty -> ", "~/.local/bin/afterparty-session-end",
                   "~/.config/afterparty/config.json", AP + ".weekly", AP + ".spacecheck",
                   "~/.claude/skills/tidy", "~/.claude/skills/afterparty", "~/.local/state/afterparty",
                   "SessionEnd hook in ~/.claude/settings.json", "project folders: ~/Projects",
                   "installing never deletes your files", "Run again with --yes"):
        assert needle in r.stdout, needle
    assert "CLAUDE.md" not in r.stdout          # the .scratch rule only with consent
    assert snapshot(installer.home) == before and not installer.log.exists()


def test_detect_roots_prints_project_folders(installer):
    (installer.home / "Developer").mkdir()
    r = installer.run("--detect-roots")
    assert r.returncode == 0 and r.stdout.split() == ["~/Projects", "~/Developer"]


def test_install_creates_what_the_plan_listed(installer):
    r = installer.run("--yes", "--root", "~/Projects", "--keep", "~/Projects/keepme")
    assert r.returncode == 0, r.stdout + r.stderr
    h = installer.home
    assert os.readlink(h / ".local/bin/afterparty") == os.path.join(str(REPO), "bin", "afterparty")
    cfg = json.loads((h / ".config/afterparty/config.json").read_text())
    assert cfg["python"] == sys.executable and cfg["roots"] == ["~/Projects"] and cfg["keep"] == ["~/Projects/keepme"]
    assert cfg["autopilot"] == "report" and cfg["agent"]["enabled"] is False
    for name in ("tidy", "afterparty"):
        assert (h / ".claude/skills" / name / "SKILL.md").read_text() == (REPO / "skills" / name / "SKILL.md").read_text()
    settings = json.loads((h / ".claude/settings.json").read_text())
    cmd = shlex.quote(str(h / ".local/bin/afterparty-session-end"))
    assert settings["hooks"]["SessionEnd"] == [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]
    for job in ("weekly", "spacecheck"):
        plist = h / "Library/LaunchAgents" / (AP + "." + job + ".plist")
        assert plistlib.loads(plist.read_bytes())["ProgramArguments"][:2] == [sys.executable, "-B"]
        assert (installer.launchd / (AP + "." + job)).exists()          # loaded, as launchctl print reports
    man = _manifest(installer)
    assert sorted({i["kind"] for i in man["items"]}) == ["config", "dir", "hook", "job", "link", "skill"]
    assert {i["state"] for i in man["items"]} == {"done"}
    assert not (h / ".claude/CLAUDE.md").exists()
    assert "Nothing was deleted" in r.stdout and "Full Disk Access" in r.stdout


def test_reinstall_is_idempotent(installer):
    assert installer.run("--yes").returncode == 0
    first, snap = _manifest(installer), snapshot(installer.home)
    r = installer.run("--yes")
    assert r.returncode == 0, r.stderr
    assert snapshot(installer.home) == snap and _manifest(installer) == first
    assert "  + " not in r.stdout and "  ~ " not in r.stdout
    assert len(json.loads((installer.home / ".claude/settings.json").read_text())["hooks"]["SessionEnd"]) == 1


def test_existing_config_values_are_kept(installer):
    cdir = installer.home / ".config/afterparty"
    cdir.mkdir(parents=True)
    (installer.home / "Work").mkdir()
    (cdir / "config.json").write_text(json.dumps({"version": 1, "roots": ["~/Work"], "autopilot": "clean",
                                                  "low_space_gb": 20}))
    assert installer.run("--yes", "--root", "~/Projects", "--agent", "yes").returncode == 0
    cfg = json.loads((cdir / "config.json").read_text())
    assert cfg["roots"] == ["~/Work", "~/Projects"] and cfg["autopilot"] == "clean" and cfg["low_space_gb"] == 20
    assert cfg["agent"]["enabled"] is True and cfg["python"] == sys.executable


def test_install_keeps_a_protection_added_after_planning(installer, monkeypatch):
    from cleanup import config
    from cleanup.installer import main as inst

    def user_protects_then_says_yes(question):
        config.add_keep("~/Projects/client")    # e.g. the map's Protect button while the prompt waits
        return True
    monkeypatch.setattr(inst, "confirm", user_protects_then_says_yes)
    assert _in_process(installer, monkeypatch, "--root", "~/Projects") == 0
    cfg = json.loads((installer.home / ".config/afterparty/config.json").read_text())
    assert cfg["keep"] == ["~/Projects/client"] and cfg["roots"] == ["~/Projects"]


def test_protect_during_an_installer_settings_write_is_never_lost_to_a_cleanup(tmp_path, monkeypatch):
    """Codex probe: Protect pressed while the installer writes settings, with a cleanup trying to start meanwhile.
    Never "Protect pending and last_gate says go": the cleanup is refused, and afterwards the protection is in
    the settings that last_gate reads fresh."""
    import os as _os
    import time as _time
    from cleanup import config
    from cleanup.installer import main as inst
    from cleanup.plan import bridge
    home = tmp_path / "home"
    (home / "P").mkdir(parents=True)
    for k, v in (("HOME", str(home)), ("AFTERPARTY_CONFIG_DIR", str(tmp_path / "cfg")),
                 ("AFTERPARTY_STATE_DIR", str(tmp_path / "state"))):
        monkeypatch.setenv(k, v)
    from cleanup.installer import owned
    with owned.lock():  # ruling P10: the state and settings folders exist, with their receipts, before any settings write
        owned.bootstrap()
        man = owned.Manifest()
        man.intend("dir", config.config_dir(), sum="dir")
        _os.mkdir(config.config_dir(), 0o700)
        man.done("dir", config.config_dir())
    config.save({"version": 1, "roots": ["~/P"]})
    seen = {}

    def slow_installer_write(cfg):
        seen["protect"] = subprocess.Popen([sys.executable, "-c", "from cleanup import config; config.add_keep('~/P/precious')"],
                                           env=dict(_os.environ, PYTHONPATH=str(REPO)))
        _time.sleep(0.5)
        try:
            with bridge.lock(config.state_dir()):
                seen["cleanup"] = "started"
        except bridge.Busy:
            seen["cleanup"] = "refused"
        cfg["roots"].append("~/Q")

    inst.settings_transaction(slow_installer_write)
    assert seen["protect"].wait(timeout=30) == 0
    assert seen["cleanup"] == "refused"
    assert config.current_keep() == [_os.path.realpath(str(home / "P" / "precious"))]


def test_installer_settings_write_refuses_while_a_cleanup_runs(installer, monkeypatch):
    from cleanup.plan import bridge
    (installer.home / ".local/state/afterparty").mkdir(parents=True)
    with bridge.lock(str(installer.home / ".local/state/afterparty")):
        r = installer.run("--yes")
    assert r.returncode == 5 and "cleanup is running" in r.stderr     # refused up front (I4): retryable
    assert not (installer.home / ".config/afterparty/config.json").exists()


def test_skill_changed_after_planning_stops_the_install(installer, monkeypatch):
    from cleanup.installer import main as inst

    def user_makes_their_own_tidy(question):
        d = installer.home / ".claude/skills/tidy"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("mine")
        return True
    monkeypatch.setattr(inst, "confirm", user_makes_their_own_tidy)
    assert _in_process(installer, monkeypatch) == 4
    assert (installer.home / ".claude/skills/tidy/SKILL.md").read_text() == "mine"


def test_conflicts_stop_before_any_change(installer):
    h = installer.home
    (h / ".claude/skills/tidy").mkdir(parents=True)
    (h / ".claude/skills/tidy/SKILL.md").write_text("my own tidy")
    (h / ".local/bin").mkdir(parents=True)
    os.symlink("/somewhere/else", h / ".local/bin/afterparty")
    before = snapshot(h)
    r = installer.run("--yes")
    assert r.returncode == 3
    assert "! skill" in r.stdout and "! link" in r.stdout and "Stopped before changing anything" in r.stdout
    assert snapshot(h) == before and not installer.log.exists()


def test_identical_items_need_explicit_adoption(installer):
    shutil.copytree(REPO / "skills" / "tidy", installer.home / ".claude/skills/tidy")
    r = installer.run("--yes")
    assert r.returncode == 3 and "--adopt" in r.stdout
    assert "? skill" in installer.run("--dry-run", "--adopt").stdout
    assert installer.run("--yes", "--adopt").returncode == 0
    (item,) = [i for i in _manifest(installer)["items"] if i["path"].endswith(".claude/skills/tidy")]
    assert item["adopted"] is True


def test_user_files_at_old_staging_names_are_never_touched(installer):
    h = installer.home
    spots = [h / ".claude/skills/tidy.afterparty-new", h / ".claude/skills/tidy.afterparty-old",
             h / "Applications/.Afterparty.app.new"]
    for p in spots:
        p.mkdir(parents=True)
        (p / "mine.txt").write_text("keep")
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    assert installer.run("--yes", "--app", "yes", extra=ex).returncode == 0
    assert all((p / "mine.txt").read_text() == "keep" for p in spots)
    assert installer.run("--yes", "--purge", script="uninstall.sh", extra=ex).returncode == 0
    assert all((p / "mine.txt").read_text() == "keep" for p in spots)


def test_app_with_another_bundle_id_is_a_conflict(installer):
    contents = installer.home / "Applications/Afterparty.app/Contents"
    contents.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.example.other"}, f)
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    r = installer.run("--yes", "--app", "yes", extra=ex)
    assert r.returncode == 3 and "isn't Afterparty's app" in r.stdout


def test_a_same_id_app_we_didnt_install_is_a_conflict(installer):
    contents = installer.home / "Applications/Afterparty.app/Contents"
    contents.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": AP}, f)
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    r = installer.run("--yes", "--app", "yes", extra=ex)
    assert r.returncode == 3 and "! app" in r.stdout


def test_app_is_recorded_by_its_full_contents(installer):
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(installer.tmp))}
    assert installer.run("--yes", "--app", "yes", extra=ex).returncode == 0
    (item,) = [i for i in _manifest(installer)["items"] if i["kind"] == "app"]
    assert item["sum"].startswith("tree:")


def test_invalid_settings_json_stops_and_changes_nothing(installer):
    s = installer.home / ".claude/settings.json"
    s.write_text('{"model": "x",}')
    before = snapshot(installer.home)
    r = installer.run("--yes")
    assert r.returncode == 3 and "isn't valid JSON" in r.stdout
    assert snapshot(installer.home) == before


def test_hook_merge_keeps_the_users_other_hooks(installer):
    s = installer.home / ".claude/settings.json"
    mine = {"model": "opus", "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/my-hook.sh"}]}],
                                       "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
    s.write_text(json.dumps(mine, indent=2) + "\n")
    assert installer.run("--yes").returncode == 0
    d = json.loads(s.read_text())
    assert d["model"] == "opus" and d["hooks"]["Stop"] == mine["hooks"]["Stop"]
    assert d["hooks"]["SessionEnd"][0] == mine["hooks"]["SessionEnd"][0] and len(d["hooks"]["SessionEnd"]) == 2
    (backup,) = (installer.home / ".local/state/afterparty/backups").glob("*.bak")
    assert json.loads(backup.read_text()) == mine


def test_hook_runs_from_a_home_folder_with_spaces(tmp_path):
    ins = make_installer(tmp_path, "home with space")
    assert ins.run("--yes").returncode == 0
    groups = json.loads((ins.home / ".claude/settings.json").read_text())["hooks"]["SessionEnd"]
    (cmd,) = [g["hooks"][0]["command"] for g in groups]
    assert cmd == shlex.quote(str(ins.home / ".local/bin/afterparty-session-end")) and cmd.startswith("'")
    run = ins.home / "Projects/p/.scratch/run1"
    run.mkdir(parents=True)
    (run / "f.bin").write_bytes(b"x" * 4096)
    r = subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps({"cwd": str(ins.home / "Projects/p"), "session_id": "s"}),
                       env=dict(ins.env, DISKCLEAN_LEDGER_MIN_BYTES="0"), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    ledger = ins.home / ".local/state/afterparty/sessions.jsonl"
    deadline = time.time() + 15
    while not ledger.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert json.loads(ledger.read_text().splitlines()[0])["session"] == "s"


def test_scratch_rule_only_with_consent_and_between_markers(installer):
    (installer.home / ".codex").mkdir()
    md = installer.home / ".claude/CLAUDE.md"
    md.write_text("# My rules\n")
    assert installer.run("--yes", "--hygiene-rule", "yes").returncode == 0
    text = md.read_text()
    assert text.startswith("# My rules\n\n<!-- afterparty:begin -->\n## Disk hygiene (Afterparty)")
    assert text.endswith("<!-- afterparty:end -->\n") and "idle for 3 days or more" in text
    assert "by default the Sunday run only reports" in text
    assert (installer.home / ".codex/AGENTS.md").read_text().startswith("<!-- afterparty:begin -->")
    assert (installer.home / ".codex/skills/tidy/SKILL.md").exists()


def test_reinstall_keeps_a_skill_the_user_edited(installer):
    assert installer.run("--yes").returncode == 0
    f = installer.home / ".claude/skills/tidy/SKILL.md"
    f.write_text(f.read_text() + "\nmy note\n")
    r = installer.run("--yes")
    assert r.returncode == 3 and "! skill" in r.stdout and f.read_text().endswith("my note\n")


def test_runs_with_an_empty_environment(installer):
    keep = ("HOME", "AFTERPARTY_LAUNCHCTL", "AFTERPARTY_XCODE_SELECT", "AFTERPARTY_PYTHON_CANDIDATES")
    base = ["/usr/bin/env", "-i"] + ["%s=%s" % (k, installer.env[k]) for k in keep]
    r = subprocess.run(base + ["/bin/sh", str(REPO / "install.sh"), "--yes"], capture_output=True, text=True,
                       timeout=300, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    home = "HOME=" + str(installer.home)
    r = subprocess.run(["/usr/bin/env", "-i", home, "/bin/sh", str(installer.home / ".local/bin/afterparty"),
                        "autopilot", "status"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "Autopilot: reports every Sunday" in r.stdout, r.stderr


def test_scheduled_job_command_runs_as_rendered(installer):
    assert installer.run("--yes").returncode == 0
    d = plistlib.loads((installer.home / "Library/LaunchAgents" / (AP + ".spacecheck.plist")).read_bytes())
    env = dict(d["EnvironmentVariables"], AFTERPARTY_NO_NOTIFY="1")
    r = subprocess.run(d["ProgramArguments"], cwd=d["WorkingDirectory"], env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr


def test_install_from_a_folder_with_spaces(installer):
    repo = installer.tmp / "My Tools" / "after party"
    for part in ("cleanup", "bin", "skills/tidy", "skills/afterparty", "launchd", "install.sh"):
        src, dst = REPO / part, repo / part
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    r = installer.run("--yes", repo=repo)
    assert r.returncode == 0, r.stdout + r.stderr
    d = plistlib.loads((installer.home / "Library/LaunchAgents" / (AP + ".weekly.plist")).read_bytes())
    assert d["WorkingDirectory"] == os.path.realpath(repo)
    r = subprocess.run(["/bin/sh", str(installer.home / ".local/bin/afterparty"), "autopilot", "status"],
                       env=installer.env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not list(repo.rglob("__pycache__"))               # -B: the checkout stays clean


def test_installer_ignores_a_cleanup_folder_in_the_working_directory(installer):
    shadow = installer.tmp / "work" / "cleanup"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text('raise SystemExit("imported the wrong cleanup package")\n')
    assert installer.run("--dry-run", cwd=str(shadow.parent)).returncode == 0


def test_without_yes_nothing_changes(installer):
    before = snapshot(installer.home)
    assert installer.run().returncode == 1 and snapshot(installer.home) == before


def test_bad_answers_are_refused(installer):
    assert installer.run("--yes", "--root", "~/missing").returncode == 2
    assert installer.run("--yes", "--keep", "relative/path").returncode == 2
    assert installer.run("--yes", "--python", "/nonexistent/python3").returncode == 69


def _crash_case(root):
    ins = make_installer(root)
    (ins.home / ".codex").mkdir()
    (ins.home / ".claude/CLAUDE.md").write_text("# My rules\n")
    ex = {"AFTERPARTY_TEST_APP_BUILDER": str(fake_app_builder(ins.tmp)), "TMPDIR": str(ins.tmp)}
    return ins, ex


def test_a_crash_at_any_point_of_an_install_is_finished_by_the_next_run(tmp_path):
    args = ("--yes", "--app", "yes", "--hygiene-rule", "yes")
    ins, ex = _crash_case(tmp_path / "count")
    log = tmp_path / "checkpoints.log"
    assert ins.run(*args, extra=dict(ex, AFTERPARTY_TEST_CHECKPOINTS=str(log))).returncode == 0
    total = len(log.read_text().splitlines())
    assert total > 20
    for n in range(1, total + 1):
        ins, ex = _crash_case(tmp_path / ("c%d" % n))
        assert ins.run(*args, extra=dict(ex, AFTERPARTY_TEST_CRASH_AT=str(n))).returncode == 99, n
        r = ins.run(*args, extra=ex)
        assert r.returncode == 0, (n, r.stdout + r.stderr)
        stages = [p for p in names(snapshot(ins.home)) if ".afterparty-stage-" in p]
        assert stages == [], (n, stages)
        states = {(i["kind"], i["path"]): i["state"] for i in _manifest(ins)["items"]}
        assert set(states.values()) == {"done"}, (n, [k for k, v in states.items() if v != "done"])
