from tests.conftest import REPO


def test_runtime_files_live_in_the_afterparty_folders(tmp_path, monkeypatch):
    cfg, st = tmp_path / "c", tmp_path / "s"
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(st))
    from cleanup import launcher
    from cleanup.scanner import store
    assert launcher.default_state() == str(st / "server.json")
    assert store.default_db() == str(st / "scan.db")
    from cleanup.agent import setup
    assert setup.settings_file() == str(cfg / "agent.json")


def test_no_legacy_state_paths_in_code():
    for p in (REPO / "cleanup").rglob("*.py"):
        text = p.read_text()
        for legacy in ("state/cleanup", ".config/cleanup", "state/diskclean"):
            assert legacy not in text, (p.name, legacy)
