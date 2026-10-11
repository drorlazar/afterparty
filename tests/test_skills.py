import pytest

from tests.conftest import REPO

SKILLS = ["tidy", "afterparty"]


@pytest.mark.parametrize("name", SKILLS)
def test_skill_text_is_safe_and_public(name):
    text = (REPO / "skills" / name / "SKILL.md").read_text()
    assert text.startswith(f"---\nname: {name}\n")
    for banned in ("rm -rf", "rm -r ", "~/bin/", "/tmp/", "diskclean"):
        assert banned not in text, banned
    assert "~/.local/bin/afterparty" in text
    assert "Trash" in text


def test_skills_only_promise_undo_for_trash_moves():
    # Final review MUST-FIX 11: permanent actions can't be undone, and the copy says so.
    tidy = (REPO / "skills" / "tidy" / "SKILL.md").read_text()
    assert "can be undone." not in tidy
    assert "Trash moves can be undone; caches, dependency folders and .scratch runs are cleared for good" in tidy
    ap = (REPO / "skills" / "afterparty" / "SKILL.md").read_text()
    assert "personal items go to the Trash; local models and full package caches are removed for good " \
           "(they re-download)" in ap


def test_clean_prompt_never_says_worktrees_go_to_the_trash():
    from cleanup.engine import core
    src = (REPO / "cleanup" / "engine" / "core.py").read_text()
    assert "idle worktrees are removed only when every commit is on the remote" in src.lower()
    assert "the rest goes to the Trash" not in src
