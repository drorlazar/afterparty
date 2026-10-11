import json
import os

import pytest

from cleanup import config


@pytest.fixture
def cdir(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("AFTERPARTY_CONFIG_DIR", str(d))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return d


def test_missing_file_gives_defaults(cdir):
    cfg, present = config.load()
    assert present is False
    assert cfg["autopilot"] == "report"
    assert cfg["roots"] == [] and cfg["keep"] == []
    assert cfg["agent"] == {"enabled": False, "read_files": False}


def test_malformed_json_raises(cdir):
    cdir.mkdir()
    (cdir / "config.json").write_text("{nope")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load()


@pytest.mark.parametrize("raw,msg", [
    ({"autopilot": "yolo"}, "autopilot"),
    ({"roots": "~/x"}, "roots"),
    ({"keep": [""]}, "keep"),
    ({"idle_days": -1}, "idle_days"),
    ({"idle_days": True}, "idle_days"),
    ({"scratch_days": 0}, "scratch_days"),
    ({"bogus": 1}, "unknown"),
    ({"agent": {"enabled": "yes"}}, "agent"),
    ({"version": 2}, "version"),
    ([], "object"),
])
def test_invalid_values_raise(cdir, raw, msg):
    with pytest.raises(config.ConfigError, match=msg):
        config.validate(raw)


def test_non_utf8_file_raises_config_error(cdir):
    cdir.mkdir()
    (cdir / "config.json").write_bytes(b"\xff\xfe{}")
    with pytest.raises(config.ConfigError, match="can't be read as text"):
        config.load()


def test_deeply_nested_json_raises_config_error(cdir):
    cdir.mkdir()
    (cdir / "config.json").write_text("[" * 100000 + "]" * 100000)
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load()


@pytest.mark.parametrize("python", ["/usr/bin/python3\n", "/usr/bin/python3\r", "a\x00b", "a\x7fb", "a\tb"])
def test_python_with_control_characters_raises(cdir, python):
    with pytest.raises(config.ConfigError, match="python"):
        config.validate({"python": python})


def test_save_roundtrip_and_python_sidecar(cdir):
    config.save({"roots": ["~/Projects"], "python": "/opt/py/bin/python3"})
    cfg, present = config.load()
    assert present and cfg["roots"] == ["~/Projects"]
    assert (cdir / "python").read_text() == "/opt/py/bin/python3\n"
    assert json.loads((cdir / "config.json").read_text())["version"] == 1


def test_save_refuses_invalid_and_keeps_old(cdir):
    config.save({"roots": ["~/a"]})
    with pytest.raises(config.ConfigError):
        config.save({"autopilot": "x"})
    assert config.load()[0]["roots"] == ["~/a"]


def test_roots_expand_dedupe_and_skip_missing(cdir):
    home = os.environ["HOME"]
    os.makedirs(os.path.join(home, "Projects"))
    cfg = config.validate({"roots": ["~/Projects", "~/Projects/", os.path.join(home, "Projects"), "~/Missing"]})
    found, missing = config.roots(cfg)
    assert found == [os.path.realpath(os.path.join(home, "Projects"))]
    assert missing == ["~/Missing"]


def test_keep_paths_are_realpaths_even_if_missing(cdir):
    cfg = config.validate({"keep": ["~/Exports", "~/Exports/"]})
    assert config.keep_paths(cfg) == [os.path.realpath(os.path.join(os.environ["HOME"], "Exports"))]


def test_add_keep_appends_once(cdir):
    config.save({})
    config.add_keep("~/Exports")
    config.add_keep(os.path.join(os.environ["HOME"], "Exports"))
    assert config.load()[0]["keep"] == ["~/Exports"]


def test_state_dir_env(cdir, monkeypatch, tmp_path):
    monkeypatch.setenv("AFTERPARTY_STATE_DIR", str(tmp_path / "st"))
    assert config.state_dir() == str(tmp_path / "st")


def test_save_writes_the_python_sidecar_first_through_the_owned_writer(cdir, monkeypatch):
    # Final review SHOULD-FIX: a crash between the two writes never leaves config.json pointing at a missing python.
    from cleanup import fsops
    order, real = [], fsops.write_owned

    def spy(path, data, expected, mode=None):
        order.append(os.path.basename(path))
        return real(path, data, expected, mode)
    monkeypatch.setattr(fsops, "write_owned", spy)
    config.save({"version": 1, "python": "/usr/bin/python3"})
    assert [o for o in order if o != ".afterparty-receipts.json"] == ["python", "config.json"]
