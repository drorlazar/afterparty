"""The agent can't promote an unknown item to permanent, and can't unlock a protected one — whatever it says."""
from cleanup.agent import runner
from cleanup.agent.schema import sanitize, validate
from cleanup.plan import manifest as M
from tests.test_manifest import fake_classify, tree


def test_agent_delete_proposal_for_unknown_item_still_goes_to_trash(tmp_path):
    t = tree(tmp_path)
    ans = {"verdict": "safe-to-delete", "confidence": 1, "summary": "junk", "evidence": [],
           "proposed": [{"path": str(t / "doc.txt"), "action": "delete", "reason": "junk"}]}
    assert validate(ans) is None
    paths = [p["path"] for p in sanitize(ans, str(t))["proposed"]]
    m = M.build(paths, classify_fn=fake_classify)
    assert m["items"][0]["mode"] == "trash"


def test_agent_cannot_unlock_protected(tmp_path):
    t = tree(tmp_path)
    prot = t / "sysstuff"; prot.mkdir(exist_ok=True)   # fake_classify: names starting with 'sys' are protected
    m = M.build([str(prot)], classify_fn=fake_classify)
    assert m["items"][0]["mode"] == "skip"


def test_schema_forbids_permanent_action():
    ans = {"verdict": "safe-to-delete", "confidence": 1, "summary": "", "evidence": [],
           "proposed": [{"path": "/x", "action": "permanent", "reason": ""}]}
    assert validate(ans)


def test_names_only_by_default():
    cmd = runner.command()
    tools = cmd[cmd.index("--tools") + 1]
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" not in tools.split(",") and "Grep" not in tools.split(",")
    assert "Read" not in allowed.split() and "Bash(ls:*)" in allowed.split()


def test_read_files_needs_opt_in():
    cmd = runner.command(read_files=True)
    assert "Read" in cmd[cmd.index("--tools") + 1].split(",")


def test_secrets_are_generalised():
    joined = " ".join(runner.SECRETS)
    for pat in ("**/*credentials*", "**/*secret*", "**/*token*", "**/.netrc", "~/.config/**/auth*"):
        assert pat in joined
    assert "harness" not in joined


def test_every_dotenv_file_is_denied():
    # Final review MUST-FIX 11: `.env*` covers .env, .env.local and .envrc (spec 4.3).
    import fnmatch
    assert "**/.env*" in runner.SECRETS and "**/.envrc" in runner.SECRETS
    for name in (".env", ".env.local", ".envrc"):
        assert any(fnmatch.fnmatch("proj/" + name, p.replace("**/", "*/")) for p in runner.SECRETS), name
    assert any(d == "Read(**/.envrc)" for d in runner.DENIED)
