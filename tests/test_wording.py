from tests.conftest import REPO


def test_public_strings_say_afterparty():
    hits = []
    for base in ("cleanup", "web"):
        for p in (REPO / base).rglob("*"):
            if p.suffix in (".py", ".js", ".html", ".css") and "CleanUp" in p.read_text(encoding="utf-8"):
                hits.append(str(p.relative_to(REPO)))
    assert hits == []


def test_server_403_points_to_the_public_command():
    src = (REPO / "cleanup" / "server" / "app.py").read_text()
    assert "Open Afterparty with `afterparty map`." in src and "`cleanup` command" not in src


def test_session_hook_docstring_names_the_report():
    src = (REPO / "cleanup" / "hooks" / "session_end.py").read_text()
    assert "`diskclean` lists them" not in src and "the Afterparty report lists them" in src
