import json
import os
import subprocess
import sys
import time

import pytest

from cleanup import config
from tests.conftest import REPO


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(d))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (d / "config.json").write_text(json.dumps({"version": 1, "roots": ["~/Work"], "autopilot": "clean"}))
    return d / "config.json"


def _child(code):
    return subprocess.Popen([sys.executable, "-c", "from cleanup import config\n" + code],
                            env=dict(os.environ, PYTHONPATH=str(REPO)))


def test_settings_writers_take_turns(cfg):
    """The map app's Protect button, pressed while the installer is mid-update, waits and then lands."""
    child = {}

    def map_app_protects():
        if not child:
            child["p"] = _child("config.add_keep('~/Work/client')")
            time.sleep(0.5)
            assert child["p"].poll() is None          # it is waiting for our lock

    config.update(lambda c: c["roots"].append("~/Projects"), before_replace=map_app_protects)
    assert child["p"].wait(timeout=30) == 0
    saved = json.loads(cfg.read_text())
    assert saved["keep"] == ["~/Work/client"] and saved["roots"] == ["~/Work", "~/Projects"]
    assert saved["autopilot"] == "clean"


def test_save_takes_the_same_lock(cfg):
    child = {}

    def someone_saves():
        if not child:
            child["p"] = _child("config.save({'version': 1, 'roots': ['~/Saved']})")
            time.sleep(0.5)
            assert child["p"].poll() is None

    config.update(lambda c: c["roots"].append("~/Projects"), before_replace=someone_saves)
    assert child["p"].wait(timeout=30) == 0


def test_update_never_drops_a_concurrent_keep_entry(cfg):
    calls = {"n": 0}

    def other_program_saves():      # a writer that doesn't take our lock, between our read and our replace
        calls["n"] += 1
        if calls["n"] == 1:
            d = json.loads(cfg.read_text())
            d["keep"] = ["~/Work/client"]
            cfg.write_text(json.dumps(d))

    out = config.update(lambda c: c["roots"].append("~/Projects"), before_replace=other_program_saves)
    saved = json.loads(cfg.read_text())
    assert saved["keep"] == ["~/Work/client"] and saved["roots"] == ["~/Work", "~/Projects"]
    assert saved["autopilot"] == "clean" and out == config.validate(saved) and calls["n"] == 2


def test_update_gives_up_and_leaves_the_other_writers_content(cfg):
    n = {"i": 0}

    def always():
        n["i"] += 1
        cfg.write_text(json.dumps({"version": 1, "keep": ["~/k%d" % n["i"]]}))

    with pytest.raises(config.ConfigError, match="kept changing"):
        config.update(lambda c: c["roots"].append("~/Elsewhere"), before_replace=always)
    assert json.loads(cfg.read_text()) == {"version": 1, "keep": ["~/k3"]}


def test_a_failed_update_leaves_the_sidecar_matching_the_settings(cfg):
    n = {"i": 0}

    def always():
        n["i"] += 1
        cfg.write_text(json.dumps({"version": 1, "python": "/other/python3", "keep": ["~/k%d" % n["i"]]}))

    with pytest.raises(config.ConfigError):
        config.update(lambda c: c.update(python="/mine/python3"), before_replace=always)
    assert (cfg.parent / "python").read_text() == "/other/python3\n"


def test_update_never_overwrites_malformed_settings(cfg):
    cfg.write_text("{nope")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.update(lambda c: c.update(autopilot="report"))
    assert cfg.read_text() == "{nope"


def test_what_the_settings_writer_writes_is_receipted(cfg):
    from cleanup import fsops
    config.update(lambda c: c["roots"].append("~/Projects"))
    rec = fsops.receipts(str(cfg.parent))
    assert rec["config.json"] == fsops.file_sum(str(cfg)) and rec["python"] == fsops.file_sum(str(cfg.parent / "python"))
    assert rec[".lock"] == fsops.file_sum(str(cfg.parent / ".lock"))


def test_update_starts_from_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(tmp_path / "new"))
    out = config.update(lambda c: c.update(python="/opt/py/bin/python3"))
    assert out["autopilot"] == "report" and (tmp_path / "new" / "python").read_text() == "/opt/py/bin/python3\n"


def test_add_keep_keeps_what_another_writer_saved(cfg):
    d = json.loads(cfg.read_text())
    d["roots"] = ["~/Work", "~/Other"]
    cfg.write_text(json.dumps(d))
    config.add_keep("~/Work/keepme")
    saved = json.loads(cfg.read_text())
    assert saved["keep"] == ["~/Work/keepme"] and saved["roots"] == ["~/Work", "~/Other"]


def test_every_settings_writer_goes_through_the_lock():
    for f in ("cleanup/commands.py", "cleanup/server/app.py", "cleanup/installer/main.py",
              "scripts/migrate_legacy.py"):
        p = REPO / f
        if p.exists():
            assert "config.save(" not in p.read_text() and "_atomic_write(" not in p.read_text(), f


def _wait_for_pending(child):
    deadline = time.time() + 30
    while True:
        d = config.pending_dir()
        if os.path.isdir(d) and [n for n in os.listdir(d) if n.endswith(".json")]:
            return
        assert time.time() < deadline and child.poll() is None
        time.sleep(0.05)


def test_a_pending_protect_is_seen_by_last_gate_before_it_gets_the_settings_lock(cfg):
    """Codex probe (R8): Protect is pressed while the settings lock is held (as across the run-lock handoff of a
    settings transaction). It publishes its intent before waiting, so a cleanup's last_gate, reading the settings
    fresh, never says go for that path."""
    from cleanup.plan import executor
    target = os.path.join(os.environ["HOME"], "Work", "client", "render.mov")
    with config.writers_lock():
        child = _child("config.add_keep('~/Work/client')")
        _wait_for_pending(child)
        assert child.poll() is None                                   # still waiting for the settings lock
        assert "keep" not in json.loads(cfg.read_text())                # nothing merged yet
        status, blocked = executor.last_gate([target], config.current_keep, None)
        assert status == "go" and blocked == {target: "now in your keep list"}
    assert child.wait(timeout=30) == 0
    assert json.loads(cfg.read_text())["keep"] == ["~/Work/client"] and os.listdir(config.pending_dir()) == []


def test_a_malformed_pending_protection_makes_deleting_refuse(cfg):
    from cleanup.plan import executor
    os.makedirs(config.pending_dir())
    with open(os.path.join(config.pending_dir(), "0123.json"), "w") as f:
        f.write("{not json")
    with pytest.raises(config.ConfigError, match="pending protection"):
        config.load()
    assert executor.last_gate(["/somewhere"], config.current_keep, None) == ("halt", {})


def test_a_protect_that_died_before_merging_still_protects(cfg):
    os.makedirs(config.pending_dir())
    with open(os.path.join(config.pending_dir(), "0123.json"), "w") as f:
        json.dump({"keep": "~/Work/client"}, f)
    assert config.current_keep() == [os.path.realpath(os.path.join(os.environ["HOME"], "Work", "client"))]
    assert "keep" not in json.loads(cfg.read_text())                    # load() never writes


def test_a_protect_published_during_the_read_is_never_missed(cfg, monkeypatch):
    """Codex probe (R10): Protect publishes after load() listed the pending files but before it read the settings.
    The stable read sees the list change and reads again, so last_gate still blocks the path."""
    from cleanup.plan import executor
    target = os.path.join(os.environ["HOME"], "Work", "client", "render.mov")
    real, calls = config._read, []

    def publish_then_read(path):
        if not calls:
            os.makedirs(config.pending_dir(), exist_ok=True)
            with open(os.path.join(config.pending_dir(), "0123.json"), "w") as f:
                json.dump({"keep": "~/Work/client"}, f)
        calls.append(path)
        return real(path)
    monkeypatch.setattr(config, "_read", publish_then_read)
    assert executor.last_gate([target], config.current_keep, None) == ("go", {target: "now in your keep list"})
    assert len(calls) == 2


def test_a_protect_withdrawn_during_the_read_is_still_seen(cfg, monkeypatch):
    """R10: the pending file is merged and withdrawn between the listing and its read: read again, find it merged."""
    os.makedirs(config.pending_dir())
    pending = os.path.join(config.pending_dir(), "0123.json")
    with open(pending, "w") as f:
        json.dump({"keep": "~/Work/client"}, f)
    real_entries, calls = config._pending_entries, []

    def merged_meanwhile(names):
        if not calls:
            calls.append(1)
            d = json.loads(cfg.read_text())
            d["keep"] = ["~/Work/client"]
            cfg.write_text(json.dumps(d))
            os.unlink(pending)
        return real_entries(names)
    monkeypatch.setattr(config, "_pending_entries", merged_meanwhile)
    assert config.current_keep() == [os.path.realpath(os.path.join(os.environ["HOME"], "Work", "client"))]


def test_protections_that_never_settle_make_deleting_refuse(cfg, monkeypatch):
    from cleanup.plan import executor
    real, n = config._read, []

    def always_publishing(path):
        os.makedirs(config.pending_dir(), exist_ok=True)
        with open(os.path.join(config.pending_dir(), "%04d.json" % len(n)), "w") as f:
            json.dump({"keep": "~/Work/x%d" % len(n)}, f)
        n.append(1)
        return real(path)
    monkeypatch.setattr(config, "_read", always_publishing)
    with pytest.raises(config.ConfigError, match="kept changing"):
        config.load()
    assert executor.last_gate(["/somewhere"], config.current_keep, None) == ("halt", {})


def test_a_failed_protect_keeps_protecting(cfg, monkeypatch):
    """Codex probe (R13): the merge fails; the pending protection stays, so last_gate still blocks the path."""
    from cleanup.plan import executor
    target = os.path.join(os.environ["HOME"], "Work", "client", "render.mov")

    def failing_merge(mutate, path=None, **kw):
        raise config.ConfigError("disk full (injected)")
    monkeypatch.setattr(config, "update", failing_merge)
    with pytest.raises(config.ConfigError, match="injected"):
        config.add_keep("~/Work/client")
    assert len(os.listdir(config.pending_dir())) == 1
    assert executor.last_gate([target], config.current_keep, None) == ("go", {target: "now in your keep list"})


def test_a_protect_whose_folder_fsync_fails_keeps_protecting(cfg, monkeypatch):
    def failing_fsync(folder):
        raise OSError(5, "Input/output error (injected)")
    monkeypatch.setattr(config, "_fsync_dir", failing_fsync)
    with pytest.raises(config.KeepPending) as e:   # still protecting, settings not updated (M2: the map's 409)
        config.add_keep("~/Work/client")
    assert isinstance(e.value.__cause__, OSError)
    assert len(os.listdir(config.pending_dir())) == 1
    assert os.path.realpath(os.path.join(os.environ["HOME"], "Work", "client")) in config.current_keep()


def test_a_receipts_file_afterparty_cant_vouch_for_never_fails_a_settings_write(cfg, capsys):
    """Ruling (Task 7 review): the settings are already saved when the receipt is written; a receipts file that
    isn't Afterparty's leaves them unreceipted (Trash treatment at --purge) instead of raising."""
    from cleanup import fsops
    rf = cfg.parent / fsops.RECEIPTS
    rf.write_bytes(b"the user's own notes\n")
    out = config.update(lambda c: c["roots"].append("~/Projects"))
    assert out["roots"] == ["~/Work", "~/Projects"] and json.loads(cfg.read_text())["roots"] == ["~/Work", "~/Projects"]
    config.save({"version": 1, "roots": ["~/Saved"], "python": "/opt/py/bin/python3"})
    assert json.loads(cfg.read_text())["roots"] == ["~/Saved"]
    assert (cfg.parent / "python").read_text() == "/opt/py/bin/python3\n"
    config.add_keep("~/Work/client")                                     # the Protect path is unchanged
    assert json.loads(cfg.read_text())["keep"] == ["~/Work/client"] and os.listdir(config.pending_dir()) == []
    assert rf.read_bytes() == b"the user's own notes\n"
    assert capsys.readouterr().err.count("no write receipt") == 1        # warned once


def test_dot_files_in_the_pending_folder_are_ignored(cfg):
    os.makedirs(config.pending_dir())
    for name in (".DS_Store", ".tmp-0123"):
        with open(os.path.join(config.pending_dir(), name), "w") as f:
            f.write("\0 not a protection")
    assert config.current_keep() == []


@pytest.mark.parametrize("name, body", [("notes.txt", '{"keep": "~/Work/client"}'), ("0123.json", '{"keep": 1}')])
def test_any_other_stray_or_malformed_pending_entry_makes_deleting_refuse(cfg, name, body):
    from cleanup.plan import executor
    os.makedirs(config.pending_dir())
    with open(os.path.join(config.pending_dir(), name), "w") as f:
        f.write(body)
    with pytest.raises(config.ConfigError):
        config.load()
    assert executor.last_gate(["/somewhere"], config.current_keep, None) == ("halt", {})
