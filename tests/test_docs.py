import re

from tests.conftest import REPO

PROMPT = "Install Afterparty from https://github.com/drorlazar/afterparty — read INSTALL.md and follow it."
POLICY = ("Caches and scraps are cleared. Everything else goes to the Trash, with undo.",
          "uv cache clean --force", "npm cache clean --force", "ollama rm", "they re-download",
          ".mp4 .mov .webm .blend .aep", ".glb .fbx .md", "whatever they contain", "you pick yourself in the map",
          "unless the rules recognise it as rebuildable", "~/Downloads", "Hugging Face", "it didn't install",
          "every commit is on your remote", "autopilot on", "only reports")


def _code(text):
    return re.findall(r"```.*?```", text, re.S) + re.findall(r"`[^`\n]+`", text)


def test_runbook_follows_the_spec_steps():
    text = (REPO / "INSTALL.md").read_text()
    for needle in ("## 1. Preflight", "## 2. Get the code", "## 3. Show the plan", "## 4. Ask", "## 5. Install",
                   "## 6. App", "## 7. Full Disk Access", "## 8. First look", "./install.sh --dry-run",
                   "./install.sh --yes", "--detect-roots", "~/.local/bin/afterparty report", "xcode-select --install",
                   "remote get-url origin", "status --porcelain", "pull --ff-only", "Privacy_AllFiles", "/tidy",
                   "afterparty autopilot on", "<!-- afterparty:begin -->", "/usr/local/bin/git",
                   "/usr/local/bin/python3", "/opt/homebrew/bin/git", "only if `xcode-select -p` succeeded",
                   "--adopt", "any app bundle that carries Afterparty's bundle id"):
        assert needle in text, needle


def test_runbook_never_runs_a_deleting_command():
    code = "\n".join(_code((REPO / "INSTALL.md").read_text()))
    for pattern in (r"\brm\b", r"afterparty clean", r"--apply", r"--purge", r"\buninstall", r"\btrash\b",
                    r"autopilot on --yes", r"\bmv\b"):
        assert not re.search(pattern, code), pattern


def test_readme_states_the_policy_as_built():
    text = (REPO / "README.md").read_text()
    for needle in ("# Afterparty", PROMPT, "## Safety model", "## Manual install", "./install.sh --dry-run",
                   "## Uninstall", "./uninstall.sh --purge", "## Credits", "NOTICE", "MIT",
                   "install record") + POLICY:
        assert needle in text, needle
    assert "CleanUp" not in text and "Delete anyway" not in text and "Never offered" not in text
    assert "goes to the Trash too, without a file-type check" not in text   # a map pick can be cleared for good


def test_skills_state_the_class_rules():
    tidy = (REPO / "skills" / "tidy" / "SKILL.md").read_text()
    assert "Never offered: final videos" not in tidy           # was false for .scratch runs and map picks
    for needle in (".mp4 .mov .webm .blend .aep", ".glb .fbx .md", "whatever they contain",
                   "Trash moves can be undone; caches, dependency folders and .scratch runs are cleared for good"):
        assert needle in tidy, needle
    ap = (REPO / "skills" / "afterparty" / "SKILL.md").read_text()
    for needle in ("uv cache prune", "pnpm store prune", "brew cleanup", "Sunday runs only report",
                   "personal items go to the Trash; local models and full package caches are removed for good "
                   "(they re-download)"):
        assert needle in ap, needle


def test_hygiene_block_explains_report_only_and_no_file_type_check():
    from cleanup.installer import blocks
    rule = blocks.render(3)
    assert "whatever it contains" in rule and "by default the Sunday run only reports" in rule


def test_site_landing_page():
    import re
    site = REPO / "site"
    html = (site / "index.html").read_text()
    assert "<title>Afterparty</title>" in html and "https://github.com/drorlazar/afterparty" in html
    assert 'name="viewport"' in html and "with undo" in html and "Trash" in html
    # Ruling P5: installing removes none of the user's files (it may replace its own).
    assert "Installing never deletes your files." in html
    assert "deletes anything" not in html
    # Review notes stay private; every local asset the page names is shipped.
    assert not (site / "SKELETON-REVIEW.md").exists()
    refs = set(re.findall(r'(?:src|href|poster)="((?:assets|css|js)/[^"#?]+)"', html))
    assert refs and all((site / r).is_file() for r in refs), sorted(r for r in refs if not (site / r).is_file())
