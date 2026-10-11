import fcntl
import json
import os
import stat
import subprocess
import sys

import pytest

from cleanup.installer import blocks, edits, owned, settings_hook
from tests.conftest import REPO

CMD = "/Users/u/.local/bin/afterparty-session-end"
GROUP = settings_hook.entry(CMD)


# ---- edits: re-read before rename, backups first
def test_a_write_during_the_backup_is_never_lost(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"model": "a"}, indent=2) + "\n")
    calls = {"n": 0}

    def meanwhile():   # runs after our backup copy, right before the final check
        calls["n"] += 1
        if calls["n"] == 1:
            d = json.loads(s.read_text())
            d["theme"] = "dark"
            s.write_text(json.dumps(d, indent=2) + "\n")

    assert settings_hook.add(str(s), GROUP, backup_dir=str(tmp_path / "b"), before_check=meanwhile) is True
    d = json.loads(s.read_text())
    assert d["theme"] == "dark" and d["model"] == "a" and settings_hook.status(s.read_text(), GROUP) == "same"
    assert calls["n"] == 2


def test_gives_up_after_three_tries_and_leaves_the_other_writers_file(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("{}")
    n = {"i": 0}

    def always():
        n["i"] += 1
        s.write_text(json.dumps({"edit": n["i"]}))

    with pytest.raises(edits.EditConflict, match="kept changing"):
        settings_hook.add(str(s), GROUP, before_check=always)
    assert json.loads(s.read_text()) == {"edit": 3}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]   # no temp or staging folders left


def test_backups_have_unique_names_the_original_bytes_and_a_receipt(tmp_path):
    from cleanup import fsops
    s = tmp_path / "settings.json"
    s.write_text('{"a": 1}')
    settings_hook.add(str(s), GROUP, backup_dir=str(tmp_path / "b"))
    settings_hook.remove(str(s), GROUP, backup_dir=str(tmp_path / "b"))
    rec = fsops.receipts(str(tmp_path / "b"))
    backups = sorted(p for p in (tmp_path / "b").iterdir() if p.name != fsops.RECEIPTS)
    assert sorted(rec) == [b.name for b in backups] and all(rec[b.name] == fsops.file_sum(str(b)) for b in backups)
    assert len(backups) == 2 and backups[0].name != backups[1].name
    assert '{"a": 1}' in [b.read_text() for b in backups]
    assert all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups)


def test_edit_follows_a_symlinked_file(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    settings_hook.add(str(link), GROUP)
    assert link.is_symlink() and settings_hook.status(real.read_text(), GROUP) == "same"


def test_edit_re_resolves_a_retargeted_link(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{}")
    b.write_text('{"b": true}')
    link = tmp_path / "settings.json"
    link.symlink_to(a)
    flips = {"n": 0}

    def retarget():
        flips["n"] += 1
        if flips["n"] == 1:
            link.unlink()
            link.symlink_to(b)

    settings_hook.add(str(link), GROUP, before_check=retarget)
    assert a.read_text() == "{}" and json.loads(b.read_text())["b"] is True
    assert settings_hook.status(b.read_text(), GROUP) == "same"


def test_edit_keeps_the_file_mode(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text("{}")
    s.chmod(0o600)
    settings_hook.add(str(s), GROUP)
    assert stat.S_IMODE(s.stat().st_mode) == 0o600


# ---- settings hook: the whole group is ours, semantic preservation (spec 5.3, rev 5)
def test_add_keeps_other_hooks_key_order_and_indentation(tmp_path):
    s = tmp_path / "settings.json"
    mine = {"model": "opus", "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/mine.sh"}]}],
                                       "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}, "z": "é"}
    s.write_text(json.dumps(mine, indent=4, ensure_ascii=False) + "\n")
    assert settings_hook.add(str(s), GROUP) is True and settings_hook.add(str(s), GROUP) is False
    text = s.read_text()
    d = json.loads(text)
    assert list(d) == ["model", "hooks", "z"] and d["z"] == "é" and text.endswith("\n")
    assert '\n    "model"' in text                       # 4-space indentation kept
    assert d["hooks"]["SessionEnd"] == mine["hooks"]["SessionEnd"] + [GROUP]
    assert d["hooks"]["Stop"] == mine["hooks"]["Stop"]


def test_an_edited_entry_is_never_ours(tmp_path):
    s = tmp_path / "settings.json"
    edited = {"hooks": [{"type": "command", "command": CMD, "timeout": 60}]}
    s.write_text(json.dumps({"hooks": {"SessionEnd": [edited]}}))
    assert settings_hook.status(s.read_text(), GROUP) == "edited"
    with pytest.raises(ValueError, match="edited by hand"):
        settings_hook.add(str(s), GROUP)
    assert settings_hook.remove(str(s), GROUP) == "edited"
    assert json.loads(s.read_text())["hooks"]["SessionEnd"] == [edited]


def test_remove_takes_only_our_exact_group(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/mine.sh"}]},
                                                      GROUP]}, "model": "x"}))
    assert settings_hook.remove(str(s), GROUP) == "removed"
    assert json.loads(s.read_text()) == {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "~/mine.sh"}]}]},
                                         "model": "x"}
    s.write_text(json.dumps({"hooks": {"SessionEnd": [GROUP]}, "model": "x"}))
    settings_hook.remove(str(s), GROUP)
    assert json.loads(s.read_text()) == {"model": "x"}
    assert settings_hook.remove(str(s), GROUP) == "absent"


@pytest.mark.parametrize("bad", ['{"a": 1,}', "[1, 2]", '{"hooks": []}', '{"hooks": {"SessionEnd": {}}}'])
def test_unusable_settings_are_an_error_never_a_rewrite(tmp_path, bad):
    s = tmp_path / "settings.json"
    s.write_text(bad)
    with pytest.raises(ValueError):
        settings_hook.add(str(s), GROUP)
    assert s.read_text() == bad


# ---- marked block: byte-for-byte round trip
@pytest.mark.parametrize("original", [None, "", "# Mine", "# Mine\n", "# Mine\n\n"])
def test_block_round_trip_restores_the_file_exactly(tmp_path, original):
    md = tmp_path / "CLAUDE.md"
    if original is not None:
        md.write_text(original)
    block = blocks.render(3)
    pad = blocks.pad_for(original)
    blocks.put(str(md), block, None, pad)
    text = md.read_text()
    assert blocks.current(text) == block and text.endswith(blocks.END + "\n")
    assert blocks.take(str(md), owned.text_sum(block), pad) is True
    assert md.read_text() == (original or "")


def test_put_refuses_when_the_file_changed_since_the_plan(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Mine\n")
    pad = blocks.pad_for("# Mine\n")
    md.write_text("# Mine")                     # someone edits it after planning
    with pytest.raises(ValueError, match="changed"):
        blocks.put(str(md), blocks.render(3), None, pad)
    assert md.read_text() == "# Mine"


def test_an_edited_block_is_left_in_place(tmp_path):
    md = tmp_path / "CLAUDE.md"
    block = blocks.render(3)
    blocks.put(str(md), block, None, "")
    md.write_text(md.read_text().replace("suggest `/tidy`", "suggest `/tidy` (my edit)"))
    assert blocks.take(str(md), owned.text_sum(block), "") is False
    assert "(my edit)" in md.read_text()


def test_put_replaces_our_previous_block_in_place(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("top\n")
    old = blocks.render(3)
    blocks.put(str(md), old, None, blocks.pad_for("top\n"))
    md.write_text(md.read_text() + "bottom\n")
    blocks.put(str(md), blocks.render(5), old, None)
    text = md.read_text()
    assert text.startswith("top\n\n" + blocks.BEGIN) and text.endswith(blocks.END + "\nbottom\n")
    assert "idle for 5 days" in text


def test_broken_markers_are_an_error():
    with pytest.raises(ValueError):
        blocks.current(blocks.BEGIN + "\nno end\n")
    with pytest.raises(ValueError):
        blocks.current(blocks.render(3) + "\n" + blocks.render(3))


# ---- manifest, receipts, validation
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("AFTERPARTY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AFTERPARTY_STATE_DIR", raising=False)
    monkeypatch.delenv("AFTERPARTY_INSTALL_LOCK_FD", raising=False)
    return h


def test_bootstrap_creates_the_state_folder_with_its_receipt_in_one_step(home):
    made = owned.bootstrap()
    state = home / ".local/state/afterparty"
    assert made == [str(home / ".local"), str(home / ".local/state"), str(state)]
    man = owned.Manifest()
    assert [(i["kind"], i["path"], i["state"]) for i in man.items()] == [("dir", p, "done") for p in made]
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert not [n for n in os.listdir(home) if n.startswith(edits.STAGE_PREFIX)]
    assert owned.bootstrap() == []


def test_the_install_record_is_never_written_over_a_replacement(home):
    """Codex probe (R14): the record is replaced after it was read; the next receipt must not destroy that."""
    from cleanup import fsops
    owned.bootstrap()
    man = owned.Manifest()
    with open(man.path, "w") as f:
        f.write("the user's replacement\n")
    with pytest.raises(fsops.Conflict):
        man.forget("dir", "/nowhere")
    assert open(man.path).read() == "the user's replacement\n"


def test_intent_then_done_receipts(home):
    owned.bootstrap()
    man = owned.Manifest()
    path = str(home / ".claude/skills/tidy")
    man.intend("skill", path, sum="tree:x", prior=None)
    assert owned.Manifest().get("skill", path)["state"] == "pending"
    man.done("skill", path)
    assert owned.Manifest().get("skill", path) == {"kind": "skill", "path": path, "sum": "tree:x", "prior": None,
                                                    "state": "done"}
    man.forget("skill", path)
    assert owned.Manifest().get("skill", path) is None


AP_JOB = "Library/LaunchAgents/io.github.drorlazar.afterparty.weekly.plist"


@pytest.mark.parametrize("item", [
    {"kind": "link", "path": "/etc/hosts", "sum": "link:x", "state": "done"},          # outside its only place
    {"kind": "shell", "path": "/x", "state": "done"},                                     # unknown kind
    {"kind": "skill", "path": "SKILL", "sum": "tree:x", "state": "done"},                # not absolute
    {"kind": "hook", "path": "HOME/.claude/settings.json", "sum": "s", "state": "done"},  # no group recorded
    {"kind": "job", "path": "HOME/" + AP_JOB, "sum": "sha256:x", "label": "com.example.other", "state": "done"},
    {"kind": "hook", "path": "HOME/.claude/settings.json", "sum": "sha256:x", "state": "done",
     "group": {"hooks": [{"type": "command", "command": "/bin/sh -c anything", "timeout": 10}]}},
    {"kind": "skill", "path": "HOME/.claude/skills/tidy", "sum": 7, "state": "done"},  # wrong type
    {"kind": "link", "path": "HOME/.local/bin/afterparty", "sum": "link:/etc/passwd", "state": "done"},
    {"kind": "block", "path": "HOME/.claude/CLAUDE.md", "sum": "sha256:x", "pad": "\n\n\n", "state": "done"},
    {"kind": "skill", "path": "HOME/.claude/skills/tidy", "sum": "tree:x", "state": "done", "adopted": "yes"},
])
def test_a_manifest_naming_anything_else_is_refused(home, item):
    owned.bootstrap()
    p = home / ".local/state/afterparty" / owned.MANIFEST
    data = json.loads(p.read_text())
    data["items"].append(json.loads(json.dumps(item).replace("HOME", str(home))))
    p.write_text(json.dumps(data))
    with pytest.raises(owned.ManifestError):
        owned.Manifest()


def test_our_own_items_are_valid(home):
    for item in ({"kind": "job", "path": str(home / AP_JOB), "sum": "sha256:x", "prior": None, "state": "pending",
                  "label": "io.github.drorlazar.afterparty.weekly"},
                 {"kind": "hook", "path": str(home / ".claude/settings.json"), "sum": "sha256:x", "created": True,
                  "state": "done", "group": settings_hook.entry(owned.hook_command())},
                 {"kind": "link", "path": str(home / ".local/bin/afterparty"), "sum": "link:/any/checkout/bin/afterparty",
                  "prior": None, "state": "done"}):
        assert owned.valid_item(item), item


def test_the_install_lock_is_the_home_folder_and_can_be_handed_to_a_child(home):
    code = ("import sys\nsys.path.insert(0, %r)\nfrom cleanup.installer import owned\n"
            "try:\n    owned.lock().__enter__()\nexcept owned.Busy:\n    sys.exit(5)\n") % str(REPO)
    with owned.lock() as fd:
        assert subprocess.run([sys.executable, "-c", code], env=dict(os.environ)).returncode == 5
        env = dict(os.environ, AFTERPARTY_INSTALL_LOCK_FD=str(fd))
        assert subprocess.run([sys.executable, "-c", code], env=env, pass_fds=(fd,)).returncode == 0
    assert os.listdir(home) == []                       # no lock file anywhere
    fd = os.open(str(home), os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # released afterwards
    os.close(fd)


# ---- staging: names prove nothing, no-clobber placement, re-check before every replace and removal
def _build_skill(text):
    def build(p):
        os.mkdir(p)
        with open(os.path.join(p, "SKILL.md"), "w") as f:
            f.write(text)
    return build


def test_put_stops_when_the_destination_changed_since_the_plan(tmp_path):
    dst = tmp_path / "skills" / "tidy"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("ours")
    planned = owned.tree_sum(str(dst))
    (dst / "SKILL.md").write_text("edited after the plan")
    with pytest.raises(owned.Changed):
        owned.put(str(dst), _build_skill("new"), planned)
    assert (dst / "SKILL.md").read_text() == "edited after the plan"
    assert os.listdir(tmp_path / "skills") == ["tidy"]


def test_put_never_overwrites_something_that_arrives_late(tmp_path):
    dst = tmp_path / "link"

    def user_arrives():
        dst.write_text("the user's file, created after our check")
    with pytest.raises(owned.Changed):
        owned.put(str(dst), lambda p: os.symlink("/x", p), None, before_place=user_arrives)
    assert dst.read_text().startswith("the user's file") and os.listdir(tmp_path) == ["link"]


def test_put_replaces_our_own_unchanged_copy(tmp_path):
    dst = tmp_path / "tidy"
    _build_skill("v1")(str(dst))
    owned.put(str(dst), _build_skill("v2"), owned.tree_sum(str(dst)))
    assert (dst / "SKILL.md").read_text() == "v2" and os.listdir(tmp_path) == ["tidy"]


def test_take_puts_a_changed_item_back(tmp_path):
    d = tmp_path / "tidy"
    d.mkdir()
    (d / "SKILL.md").write_text("ours")
    s = owned.tree_sum(str(d))
    (d / "extra.txt").write_text("the user's")
    assert owned.take(str(d), s) is False
    assert (d / "extra.txt").read_text() == "the user's" and sorted(os.listdir(tmp_path)) == ["tidy"]
    (d / "extra.txt").unlink()
    assert owned.take(str(d), s) is True and os.listdir(tmp_path) == []


def test_sweep_restores_an_unverified_item_and_ignores_unmarked_folders(tmp_path):
    parent = tmp_path / "skills"
    parent.mkdir()
    stage = edits.make_stage(str(parent), "take", orig=str(parent / "tidy"))
    os.mkdir(os.path.join(stage, "item"))                        # a crash right after moving it aside
    open(os.path.join(stage, "item", "SKILL.md"), "w").write("whatever it was")
    lookalike = parent / (edits.STAGE_PREFIX + "0123")           # the user's own folder with our prefix
    lookalike.mkdir()
    (lookalike / "keep.txt").write_text("mine")
    (parent / "tidy.afterparty-new").mkdir()                     # an old fixed staging name: means nothing now
    edits.sweep(str(parent))
    assert (parent / "tidy" / "SKILL.md").read_text() == "whatever it was"
    assert (lookalike / "keep.txt").read_text() == "mine" and (parent / "tidy.afterparty-new").is_dir()
    assert not os.path.exists(stage)


def test_sweep_never_removes_a_marked_stage_whose_contents_dont_match(tmp_path):
    parent = tmp_path / "bin"
    parent.mkdir()
    stage = edits.make_stage(str(parent), "put", orig=str(parent / "afterparty"))
    os.symlink("/x", os.path.join(stage, "new"))
    edits.update_mark(stage, new_sum=owned.link_sum("/x"))
    (open(os.path.join(stage, "notes.txt"), "w")).write("someone put this here")   # not something we make
    edits.sweep(str(parent))
    assert os.path.exists(os.path.join(stage, "notes.txt"))
    os.unlink(os.path.join(stage, "notes.txt"))
    os.unlink(os.path.join(stage, "new"))
    os.symlink("/y", os.path.join(stage, "new"))                 # not the copy the marker recorded
    edits.sweep(str(parent))
    assert os.path.exists(stage)


def test_sweep_removes_our_own_leftover_copy(tmp_path):
    parent = tmp_path / "bin"
    parent.mkdir()
    stage = edits.make_stage(str(parent), "put", orig=str(parent / "afterparty"))
    os.symlink("/x", os.path.join(stage, "new"))
    edits.update_mark(stage, new_sum=owned.link_sum("/x"))
    edits.sweep(str(parent))
    assert os.listdir(parent) == []


def test_tree_sum_sees_every_change(tmp_path):
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("a")
    first = owned.tree_sum(str(d))
    (d / "SKILL.md").write_text("b")
    assert owned.tree_sum(str(d)) != first
    (d / "SKILL.md").write_text("a")
    assert owned.tree_sum(str(d)) == first
    (d / "extra.txt").write_text("")
    assert owned.tree_sum(str(d)) != first


# ---- an exception mid-operation never leaves anything out of place
def _unreadable_skill(tmp_path):
    d = tmp_path / "tidy"
    d.mkdir()
    (d / "SKILL.md").write_text("ours")
    s = owned.tree_sum(str(d))
    (d / "SKILL.md").chmod(0)                    # checking the staged copy will raise PermissionError
    return d, s


def test_take_puts_the_item_back_when_its_check_raises(tmp_path):
    d, s = _unreadable_skill(tmp_path)
    try:
        with pytest.raises(PermissionError):
            owned.take(str(d), s)
        assert os.listdir(tmp_path) == ["tidy"] and os.listdir(d) == ["SKILL.md"]
    finally:
        (d / "SKILL.md").chmod(0o600)
    assert (d / "SKILL.md").read_text() == "ours"


def test_put_puts_the_old_copy_back_when_its_check_raises(tmp_path):
    d, s = _unreadable_skill(tmp_path)
    try:
        with pytest.raises(PermissionError):
            owned.put(str(d), _build_skill("new"), s)
        assert os.listdir(tmp_path) == ["tidy"] and os.listdir(d) == ["SKILL.md"]
    finally:
        (d / "SKILL.md").chmod(0o600)
    assert (d / "SKILL.md").read_text() == "ours"


def test_put_clears_its_stage_when_the_build_fails(tmp_path):
    def broken(p):
        os.mkdir(p)
        (open(os.path.join(p, "half.txt"), "w")).write("partial")
        raise OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        owned.put(str(tmp_path / "tidy"), broken, None)
    assert os.listdir(tmp_path) == []
