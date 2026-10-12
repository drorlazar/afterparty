"""What ships publicly: licence files, neutral metadata, nothing that only makes sense on one person's Mac."""
import os

from tests.conftest import REPO

OWNER = bytes.fromhex("44726f72204c617a6172").decode()   # the copyright holder, spelled so the leak gate's name rule stays simple
MIGRATION, RECORDER = "Mac" + "Migration", "Kap" + "tures"


def test_license_is_mit():
    text = (REPO / "LICENSE").read_text()
    assert text.startswith("MIT License\n\nCopyright (c) 2026 %s\n" % OWNER)
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_notice_credits_fonts_mole_icons_and_media():
    text = (REPO / "NOTICE").read_text()
    for needle in ("Inter", "JetBrains Mono", "SIL Open Font License", "web/fonts/OFL-Inter.txt", "Mole",
                   "--dry-run", "## Icons", "## Film and page media"):
        assert needle in text, needle


def test_pyproject_is_generic():
    text = (REPO / "pyproject.toml").read_text()
    assert 'name = "afterparty"' in text
    assert 'description = "Clean up after your coding agents: a safety-first disk cleaner and disk map for macOS"' in text


def test_diskclean_skill_is_retired():
    assert not (REPO / "skills" / "diskclean").exists()
    # the skill folders only: a stray Finder .DS_Store or other dotfile isn't a skill
    skills = sorted(p.name for p in (REPO / "skills").iterdir() if p.is_dir() and not p.name.startswith("."))
    assert skills == ["afterparty", "tidy"]


def test_tier4_offers_nothing_from_one_persons_mac(env, monkeypatch):
    from cleanup.engine import core
    home = env["HOME"]
    monkeypatch.setattr(core, "HOME", home)
    monkeypatch.setattr(core.shutil, "which", lambda n: None)
    for rel in (MIGRATION, MIGRATION + "HDD", "Movies/" + RECORDER):
        os.makedirs(os.path.join(home, rel))
    offered = [i.path for i in core.tier4()]
    assert not [p for p in offered if MIGRATION in p or RECORDER in p]
    assert not [p for p in core.off_root_allowed(home) if MIGRATION in p or RECORDER in p]
    src = (REPO / "cleanup" / "engine" / "core.py").read_text()
    for name in (MIGRATION, RECORDER, "Coty" + "pist", "pais.handy", "(Beta)", "iMovie"):
        assert name not in src, name


def test_no_private_plan_links_in_tests():
    marker = "plans" + "/"
    for p in (REPO / "tests").glob("*.py"):
        assert marker not in p.read_text(), p.name


def test_core_usage_names_the_public_command():
    from cleanup.engine import core
    assert "afterparty clean" in core.__doc__ and "afterparty --apply" not in core.__doc__
    assert "or merged" not in core.__doc__   # worktrees: every commit on a remote, not "merged"
