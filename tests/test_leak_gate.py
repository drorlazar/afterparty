# tests/test_leak_gate.py
"""The leak gate (spec 6.2). Every planted leak below is assembled at run time, so this file passes the gate."""
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.conftest import REPO

GATE = REPO / ".github" / "scripts" / "leak_gate.py"
_spec = importlib.util.spec_from_file_location("leak_gate", GATE)
lg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lg)

PUBLIC = ["cleanup", "web", "bin", "app", "skills/tidy", "skills/afterparty", "launchd/templates", "install.sh",
          "uninstall.sh", "INSTALL.md", "README.md", "LICENSE", "NOTICE", "site", "tests", "tests-e2e", ".github",
          "pyproject.toml", ".gitignore"]
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def _tree(tmp_path, files):
    root = tmp_path / "tree"
    root.mkdir(exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body) if isinstance(body, bytes) else p.write_text(body)
    return root


def _gate(root, *args, env=None):
    r = subprocess.run([sys.executable, str(GATE), str(root), *args], capture_output=True, text=True, timeout=300,
                       env=env)
    return r.returncode, r.stdout


def scan(tmp_path, files, *args):
    return _gate(_tree(tmp_path, files), "--media-tools", "skip", *args)


def _stub_tools(tmp_path, exiftool=None, ffprobe=None):
    d = tmp_path / "tools"
    d.mkdir(exist_ok=True)
    for name, body in (("exiftool", exiftool), ("ffprobe", ffprobe)):
        if body is not None:
            (d / name).write_text("#!/bin/sh\n" + body + "\n")
            (d / name).chmod(0o755)
    return dict(os.environ, PATH=str(d) + ":/usr/bin:/bin")


def _commit(root, name, email, msg="c"):
    e = dict(GIT_ENV, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=e)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", msg], check=True, env=e)


def test_clean_tree_passes(tmp_path):
    code, out = scan(tmp_path, {"a.py": "print('hello')\n",
                                "README.md": "See https://github.com/%s/afterparty\n" % lg.USER})
    assert code == 0, out


@pytest.mark.parametrize("make,rule", [
    (lambda: "by %s\n" % lg.USER, "identity"),
    (lambda: "hi %s\n" % lg.FIRST.capitalize(), "identity"),
    (lambda: "mail jane.doe@" + "corp-mail.com\n", "email"),
    (lambda: "path /Users/" + "janedoe/x\n", "home-path"),
    (lambda: "host build-box" + ".local\n", "hostname"),
    (lambda: "ip 192" + ".168.1.20\n", "private-ip"),
    (lambda: "k = 'AKIA" + "Q" * 16 + "'\n", "aws-key"),
    (lambda: "-----BEGIN " + "RSA PRIVATE KEY-----\n", "private-key"),
    (lambda: 'api_key = "' + "a1" * 14 + '"\n', "generic-secret"),
])
def test_each_rule_blocks(tmp_path, make, rule):
    code, out = scan(tmp_path, {"src/x.py": make()})
    assert code == 1 and (": %s:" % rule) in out, out


def test_allowed_identity_and_placeholders_pass(tmp_path):
    lines = ["https://github.com/%s/afterparty" % lg.USER, "io.github.%s.afterparty.weekly" % lg.USER,
             "https://%s.github.io/afterparty/" % lg.USER, "12345+" + lg.USER + "@users.noreply.github.com",
             "git@github.com:%s/afterparty.git" % lg.USER, "/Users/u/Projects", "/Users/Shared/x",
             "t@example.invalid", "a@example.com", "~/.local/bin", "127.0.0.1", ".env.local",
             "com.apple.TimeMachine.2026-10-07-010000.local"]
    code, out = scan(tmp_path, {"a.txt": "\n".join(lines) + "\n", "LICENSE": "Copyright (c) 2026 %s\n" % lg.OWNER})
    assert code == 0, out


def test_owner_name_only_in_license_and_notice(tmp_path):
    code, out = scan(tmp_path, {"README.md": "Made by %s\n" % lg.OWNER})
    assert code == 1 and ": identity:" in out
    code, out = scan(tmp_path, {"src/LICENSE": "Copyright (c) 2026 %s\n" % lg.OWNER})   # root files only
    assert code == 1 and "src/LICENSE:1: identity:" in out


def test_an_unreadable_folder_fails_closed(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n", "locked/secret.txt": "by %s\n" % lg.USER})
    (root / "locked").chmod(0)
    try:
        code, out = _gate(root, "--media-tools", "skip")
    finally:
        (root / "locked").chmod(0o755)
    assert code == 1 and "locked:0: unreadable:" in out


def test_file_and_folder_names_are_checked(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n"})
    (root / ("notes-" + lg.FIRST)).mkdir()                       # an empty folder with a private name
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 1 and ":name: identity:" in out


def test_links_are_checked_by_target_and_may_not_leave_the_tree(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), root / "docs")                      # a folder link pointing out of the export
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 1 and ": link-escape:" in out


def test_a_file_too_big_to_check_fails_closed(tmp_path):
    root = _tree(tmp_path, {"big.txt": "x" * 2000, "small.txt": "x" * 800 + " by %s\n" % lg.USER})
    code, out = _gate(root, "--media-tools", "skip", env=dict(os.environ, AFTERPARTY_LEAK_MAX_BYTES="1024"))
    assert code == 1 and "big.txt:0: too-large:" in out and "small.txt:1: identity:" in out


def test_private_deny_list(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("# private names\nAcmeCorp\nre:proj(ect)?-zeta\n")
    code, out = scan(tmp_path, {"a.md": "Built for acmecorp and project-zeta\n", "b.md": "acmecorporation is fine\n"},
                     "--deny", str(deny))
    assert code == 1 and out.count(": deny:") == 2 and "b.md" not in out


def test_binary_strings_are_checked(tmp_path):
    code, out = scan(tmp_path, {"img.bin": b"\x00\x01PNG" + ("/Users/" + "janedoe/secret").encode() + b"\x00"})
    assert code == 1 and ": home-path:" in out


def test_missing_media_tool_fails_when_required(tmp_path):
    root = _tree(tmp_path, {"poster.png": PNG_1PX})
    code, out = _gate(root, env=dict(os.environ, PATH="/nonexistent"))
    assert code == 1 and ": media-tool:" in out and "exiftool" in out


def test_a_failing_metadata_tool_fails_closed(tmp_path):
    root = _tree(tmp_path, {"poster.png": PNG_1PX, "clip.mp4": b"\x00\x00\x00\x18ftypmp42"})
    env = _stub_tools(tmp_path, exiftool="exit 1", ffprobe="exit 1")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and "poster.png:meta: metadata-unreadable:" in out and "clip.mp4:meta: metadata-unreadable:" in out


def test_a_partial_metadata_answer_fails_closed(tmp_path):
    root = _tree(tmp_path, {"a.png": PNG_1PX, "b.png": PNG_1PX})
    env = _stub_tools(tmp_path, exiftool="printf '[{\"SourceFile\": \"%s\", \"PNG:ImageWidth\": 1}]' \"$6\"")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and out.count(": metadata-unreadable:") == 1


def test_pins_cover_unreadable_files_only_while_unchanged(tmp_path):
    root = _tree(tmp_path, {"web/fonts/F.woff2": b"wOF2\x00font"})
    digest = hashlib.sha256((root / "web/fonts/F.woff2").read_bytes()).hexdigest()
    (root / ".github").mkdir()
    (root / ".github/leak-gate-pins.txt").write_text("%s  web/fonts/F.woff2  upstream font, checked by hand\n" % digest)
    env = _stub_tools(tmp_path, exiftool="exit 1")
    assert _gate(root, "--media-tools", "required", env=env)[0] == 0
    (root / "web/fonts/F.woff2").write_bytes(b"wOF2\x00changed")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and ": pin-mismatch:" in out


@pytest.mark.skipif(not shutil.which("exiftool"), reason="needs exiftool")
def test_image_metadata_is_flagged(tmp_path):
    root = _tree(tmp_path, {"poster.png": PNG_1PX})
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Artist=Jane Example", str(root / "poster.png")],
                   check=True)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and ": metadata:" in out and "Artist" in out


def test_commit_identity_and_history(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n"})
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=GIT_ENV)
    ok_id = (lg.USER, "1+" + lg.USER + "@users.noreply.github.com")
    _commit(root, *ok_id)
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    (root / "old.txt").write_text("contact jane@" + "corp-mail.com\n")
    _commit(root, *ok_id)
    (root / "old.txt").unlink()
    _commit(root, *ok_id)                                        # removed later: still in the history
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "history:old.txt@" in out and ": email:" in out
    _commit(root, "someone", "s@example.invalid")
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert ": commit-identity:" in out


def test_history_gives_the_copyright_exemption_to_root_files_only(tmp_path):
    root = _tree(tmp_path, {"LICENSE": "Copyright (c) 2026 %s\n" % lg.OWNER})
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=GIT_ENV)
    ok_id = (lg.USER, "1+" + lg.USER + "@users.noreply.github.com")
    _commit(root, *ok_id)
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    (root / "src").mkdir()
    (root / "src" / "LICENSE").write_text("Copyright (c) 2026 %s\n" % lg.OWNER)
    _commit(root, *ok_id)
    (root / "src" / "LICENSE").unlink()
    _commit(root, *ok_id)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "history:src/LICENSE@" in out


def test_public_parts_of_this_repo_pass_the_public_rules(tmp_path):
    root = tmp_path / "public"
    for part in PUBLIC:
        src, dst = REPO / part, root / part
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        else:
            shutil.copy2(src, dst)
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 0, out


def test_public_gitignore_keeps_generated_files_out_of_the_checkout():
    rules = (REPO / ".gitignore").read_text().split()
    for needle in ("__pycache__/", "*.pyc", ".scratch/", ".venv/", ".pytest_cache/"):
        assert needle in rules, needle


def test_ci_runs_tests_installer_and_leak_gate():
    wf = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    for needle in ("runs-on: macos-15", "PY=/usr/bin/python3", '"$PY" -m pytest -o addopts= -q -p no:cacheprovider',
                   "= 3.9", "fake-launchctl.sh", "AFTERPARTY_LAUNCHCTL", "./install.sh --yes",
                   "./uninstall.sh --purge --yes", "brew install exiftool", ".github/scripts/leak_gate.py"):
        assert needle in wf, needle
    assert "macos-14" not in wf
    assert "uv python install 3.9" in wf and "uv venv --python 3.9" in wf   # a 3.9 that isn't Apple's, too
    gate = wf[wf.index("  leak-gate:"):]
    assert "fetch-depth: 0" in gate                                         # the gate refuses a shallow clone
    assert "leak_gate.py . --check-commits" in gate                         # history too, not only the tree
    # G0 S2 / ruling: the history scan runs on push events only; pull requests scan the tree (GitHub's merge commit
    # and contributors' identities would always fail the identity rule)
    steps = [s for s in gate.split("\n      - ")[1:] if "leak_gate.py" in s]
    history = [s for s in steps if "--check-commits" in s]
    tree_only = [s for s in steps if "--check-commits" not in s]
    assert len(history) == 1 and "if: github.event_name == 'push'" in history[0], steps
    assert len(tree_only) == 1 and "if: github.event_name != 'push'" in tree_only[0], steps
    assert "leak_gate.py .\n" in tree_only[0] + "\n"


# --- fix round 1: probes that passed the first version (each value is assembled at run time) ---------------------

def _gz(text):
    import gzip
    return gzip.compress(text.encode())


def _zip(text):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", text)
    return buf.getvalue()


def _bplist():
    import plistlib
    return plistlib.dumps({"k": "v"}, fmt=plistlib.FMT_BINARY)


HOME_LEAK = "/Users/" + "janedoe/x"


@pytest.mark.parametrize("name,make,rule", [
    ("utf16-bom.txt", lambda: HOME_LEAK.encode("utf-16"), "home-path"),
    ("utf16-le.txt", lambda: ("path " + HOME_LEAK + "\n").encode("utf-16-le"), "home-path"),
    ("utf16-be.txt", lambda: ("path " + HOME_LEAK + "\n").encode("utf-16-be"), "home-path"),
    ("a.json", lambda: '{"p": "' + HOME_LEAK.replace("/", "\\/") + '"}\n', "home-path"),
    ("lower.txt", lambda: HOME_LEAK.lower() + "\n", "home-path"),
    ("short.bin", lambda: b"\x89\x01\x00" + lg.FIRST.encode() + b"\x00\xff", "identity"),
    ("wide.bin", lambda: b"\x89\x01\x02\x03" * 64 + ("by " + lg.USER).encode("utf-16-le") + b"\xff\xfe\x01" * 64,
     "identity"),
])
def test_text_in_any_encoding_is_checked(tmp_path, name, make, rule):
    code, out = scan(tmp_path, {name: make()})
    assert code == 1 and (": %s:" % rule) in out, out


@pytest.mark.parametrize("name,make", [
    ("a.gz", lambda: _gz("by " + lg.USER)), ("a.zip", lambda: _zip("x")), ("a.plist", _bplist),
    ("a.psd", lambda: b"8BPS\x00\x01" + bytes(20)), ("a.flac", lambda: b"fLaC\x00\x00\x00\x22" + bytes(34)),
    ("a.mkv", lambda: b"\x1aE\xdf\xa3" + bytes(20)), ("a.avif", lambda: b"\x00\x00\x00\x1cftypavif" + bytes(10)),
    ("Assets.car", lambda: b"BOMStore" + bytes(20)), ("poster.dat", lambda: PNG_1PX),
    ("latin1.txt", lambda: "caf\xe9\n".encode("latin-1")),
    ("bom.dat", lambda: b"\xff\xfe" + bytes(range(1, 31)) * 4),          # a UTF-16 BOM on binary content
    ("ctl.dat", lambda: bytes(range(1, 9)) * 20 + b"text"),              # valid UTF-8, but control bytes
])
def test_a_binary_no_tool_checks_must_be_pinned(tmp_path, name, make):
    code, out = scan(tmp_path, {name: make()})
    assert code == 1 and "%s:0: unknown-binary:" % name in out, out


def test_a_pinned_unknown_binary_passes_only_while_unchanged(tmp_path):
    root = _tree(tmp_path, {"a.zip": _zip("x")})
    digest = hashlib.sha256((root / "a.zip").read_bytes()).hexdigest()
    _tree(tmp_path, {".github/leak-gate-pins.txt": "%s  a.zip  test archive, checked by hand\n" % digest})
    assert _gate(root, "--media-tools", "skip")[0] == 0
    (root / "a.zip").write_bytes(_zip("y"))
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 1 and "a.zip:meta: pin-mismatch:" in out


@pytest.mark.parametrize("make,rule", [
    (lambda: "io.github.%s.afterparty.jane@" % lg.USER + "corp-mail.com\n", "email"),
    (lambda: "io.github.%s.afterparty.acmecorp\n" % lg.USER, "deny"),
    (lambda: "/Users/%s/afterparty/notes\n" % lg.USER, "home-path"),
    (lambda: "https://github.com/%s/afterparty.jane@" % lg.USER + "corp-mail.com\n", "email"),
])
def test_an_approved_form_never_hides_a_leak_next_to_it(tmp_path, make, rule):
    deny = tmp_path / "deny.txt"
    deny.write_text("acmecorp\n")
    code, out = scan(tmp_path, {"a.txt": make()}, "--deny", str(deny))
    assert code == 1 and (": %s:" % rule) in out, out


def test_the_real_job_labels_pass(tmp_path):
    lines = ["io.github.%s.afterparty.%s.plist" % (lg.USER, n) for n in ("weekly", "spacecheck", "server")]
    code, out = scan(tmp_path, {"a.txt": "\n".join(lines) + "\n"})
    assert code == 0, out


def _repo(tmp_path, files):
    root = _tree(tmp_path, files)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=GIT_ENV)
    return root


def _ok_id():
    return (lg.USER, "1+" + lg.USER + "@users.noreply.github.com")


def _tag(root, name, email, *args):
    e = dict(GIT_ENV, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    subprocess.run(["git", "-C", str(root), "tag", "-a", *args], check=True, env=e)


def test_annotated_tags_are_checked_like_commits(tmp_path):
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, *_ok_id())
    _tag(root, *_ok_id(), "v1", "-m", "first release")
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    _tag(root, "someone", "s@example.invalid", "v2", "-m", "second")
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "tag:v2: commit-identity:" in out
    subprocess.run(["git", "-C", str(root), "tag", "-d", "v2"], check=True, capture_output=True, env=GIT_ENV)
    _tag(root, *_ok_id(), "v3", "-m", "contact jane@" + "corp-mail.com")
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "tag:v3:msg1: email:" in out


def test_a_tag_pointing_straight_at_a_blob_is_checked(tmp_path):
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, *_ok_id())
    loose = tmp_path / "loose.txt"
    loose.write_text("contact jane@" + "corp-mail.com\n")
    blob = subprocess.run(["git", "-C", str(root), "hash-object", "-w", str(loose)], check=True, capture_output=True,
                          text=True, env=GIT_ENV).stdout.strip()
    _tag(root, *_ok_id(), "blobtag", blob, "-m", "a blob")
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and ": email:" in out and "history:" in out, out


def test_replace_objects_cannot_hide_history(tmp_path):
    root = _repo(tmp_path, {"old.txt": "contact jane@" + "corp-mail.com\n"})
    _commit(root, *_ok_id())
    leaky = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD:old.txt"], check=True, capture_output=True,
                           text=True, env=GIT_ENV).stdout.strip()
    (root / "old.txt").write_text("clean\n")
    _commit(root, *_ok_id())
    clean = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD:old.txt"], check=True, capture_output=True,
                           text=True, env=GIT_ENV).stdout.strip()
    subprocess.run(["git", "-C", str(root), "replace", leaky, clean], check=True, env=GIT_ENV)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "history:old.txt@" in out and ": email:" in out, out


def test_a_shallow_clone_fails(tmp_path):
    src = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(src, *_ok_id())
    (src / "a.txt").write_text("y\n")
    _commit(src, *_ok_id())
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + str(src), str(clone)], check=True, env=GIT_ENV)
    code, out = _gate(clone, "--media-tools", "skip", "--check-commits")
    assert code == 1 and ".git:0: history: a shallow" in out, out


def test_binary_history_the_tree_no_longer_holds_fails(tmp_path):
    root = _repo(tmp_path, {"a.txt": "x\n", "poster.png": PNG_1PX})
    _commit(root, *_ok_id())
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    (root / "poster.png").unlink()
    _commit(root, *_ok_id())
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "history:poster.png@" in out and ": history: binary content" in out, out


@pytest.mark.parametrize("make,rule", [
    (lambda: "API_KEY=" + "a1b2" * 8 + "\n", "generic-secret"),
    (lambda: "api_token: " + "Zx9" * 9 + "\n", "generic-secret"),
    (lambda: "MY_SERVICE_SECRET='" + "Q7w" * 9 + "'\n", "generic-secret"),
    (lambda: "k = sk-" + "proj-" + "Ab_9-" * 8 + "\n", "openai-key"),
    (lambda: "hf" + "_" + "Ab9" * 12 + "\n", "huggingface-token"),
    (lambda: "sk" + "_live_" + "Ab9" * 9 + "\n", "stripe-key"),
    (lambda: "GOCSPX" + "-" + "Ab9_" * 7 + "\n", "google-oauth-secret"),
    (lambda: "e" + "yJhbGciOiJIUzI1NiJ9." + "e" + "yJzdWIiOiIxMjM0In0." + "Ab9" * 6 + "\n", "jwt"),
])
def test_more_secret_shapes_are_blocked(tmp_path, make, rule):
    code, out = scan(tmp_path, {"cfg/.env": make()})
    assert code == 1 and (": %s:" % rule) in out, out


# --- Codex G0 review: synthetic values, assembled at run time ----------
LETTERS40 = ("wJalrXUtnFEMIKMDENGbPxRfiCYzEXAMPLEKEYab" + "")[:40]


@pytest.mark.parametrize("make,rule", [
    (lambda: "aws_secret_access_key = " + LETTERS40 + "\n", "aws-secret"),
    (lambda: "AWS_SECRET_ACCESS_KEY='" + LETTERS40[:20] + "/+" + LETTERS40[22:] + "'\n", "aws-secret"),
    (lambda: "OPENROUTER_API_KEY=" + "QwErTyUiOpAsDfGhJkLzXcVbNmQwErTy" + "\n", "generic-secret"),
    (lambda: 'service_token: "' + "PoIuYtReWqLkJhGfDsAmNbVcXzPoIuYtRe" + '"\n', "generic-secret"),
])
def test_g0_m6_letters_only_secrets_are_blocked(tmp_path, make, rule):
    code, out = scan(tmp_path, {"cfg/.env": make()})
    assert code == 1 and (": %s:" % rule) in out, out


@pytest.mark.parametrize("line", [
    "token = getTokenFromLocalStorageOrCookieFallback()\n",       # code, not a value
    "password_reset_link_expiry_message_template = x\n",
])
def test_g0_m6_long_identifiers_are_not_secrets(tmp_path, line):
    code, out = scan(tmp_path, {"a.py": line})
    assert code == 0, out


def _need(*tools):
    for t in tools:
        if not shutil.which(t):
            pytest.skip("%s isn't installed" % t)


def test_g0_m7_a_jpeg_file_comment_is_flagged(tmp_path):
    _need("exiftool", "sips")
    root = _tree(tmp_path, {"a.png": PNG_1PX})
    subprocess.run(["sips", "-s", "format", "jpeg", str(root / "a.png"), "--out", str(root / "a.jpg")],
                   check=True, capture_output=True)
    (root / "a.png").unlink()
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Comment=a private annotation", str(root / "a.jpg")],
                   check=True)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "File:Comment" in out, out


def _m4a(path):
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "1", "-c:a", "aac",
                    str(path)], check=True)


def test_g0_m7_av_chapter_tags_are_flagged(tmp_path):
    _need("ffmpeg", "ffprobe")
    root = _tree(tmp_path, {})
    _m4a(tmp_path / "plain.m4a")
    meta = tmp_path / "meta.txt"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=a private annotation\n")
    subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(tmp_path / "plain.m4a"), "-i", str(meta), "-map", "0",
                    "-map_chapters", "1", "-c", "copy", str(root / "a.m4a")], check=True)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "chapter" in out, out


def test_g0_m7_av_program_tags_are_flagged(tmp_path):
    root = _tree(tmp_path, {"a.mp4": b"\x00\x00\x00\x18ftypmp42"})
    info = {"format": {"tags": {}}, "streams": [], "chapters": [],
            "programs": [{"tags": {"service_name": "a private annotation"}, "streams": []}]}
    env = _stub_tools(tmp_path, ffprobe="cat <<'EOF'\n%s\nEOF" % json.dumps(info))
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and "program0" in out and "service_name" in out, out


def test_g0_m7_mp4_xmp_is_flagged(tmp_path):
    _need("ffmpeg", "ffprobe", "exiftool")
    root = _tree(tmp_path, {})
    _m4a(root / "a.m4a")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-dc:Description=a private annotation",
                    str(root / "a.m4a")], check=True)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "XMP" in out, out


def test_g0_m7_a_clean_m4a_still_passes(tmp_path):
    _need("ffmpeg", "ffprobe")
    root = _tree(tmp_path, {})
    _m4a(root / "a.m4a")
    code, out = _gate(root, "--media-tools", "required")
    assert code == 0, out


def test_g0_m8_a_historical_gitlink_path_is_name_checked(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("AcmeCorp\n")
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, *_ok_id())
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    subprocess.run(["git", "-C", str(root), "update-index", "--add", "--cacheinfo", "160000,%s,vendor/acmecorp-sdk" % sha],
                   check=True, env=GIT_ENV)
    e = dict(GIT_ENV, GIT_AUTHOR_NAME=_ok_id()[0], GIT_AUTHOR_EMAIL=_ok_id()[1], GIT_COMMITTER_NAME=_ok_id()[0],
             GIT_COMMITTER_EMAIL=_ok_id()[1])
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True, env=e)
    subprocess.run(["git", "-C", str(root), "rm", "-q", "--cached", "vendor/acmecorp-sdk"], check=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"], check=True, env=e)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits", "--deny", str(deny))
    assert code == 1 and "history:vendor/acmecorp-sdk" in out and ": deny:" in out, out



# --- Codex G0 recheck (round 2) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("make", [
    lambda: 'API_KEY="' + "abcdefghijklmnopqrst" * 2 + '"\n',          # 40 chars, one case, no digits
    lambda: 'API_KEY="' + "ABCDEFGHIJKLMNOPQRST" * 2 + '"\n',
    lambda: "SECRET_TOKEN='" + "AbCdEfGhIjKlMnOpQrStUvWx" + "'\n",       # 24 chars, mixed case
    lambda: "api_key: " + "abc_def_ghi_jkl_mno_pqr_stu" + "\n",          # digit-free, with _
    lambda: "password = " + "correct-horse-battery-staple" + "\n",
])
def test_g0r2_m6_any_long_value_assigned_to_a_secret_like_key_is_blocked(tmp_path, make):
    code, out = scan(tmp_path, {"cfg/.env": make()})
    assert code == 1 and ": generic-secret:" in out, out


XMP_UUID = bytes.fromhex("BE7ACFCB97A942E89C71999491E3AFAC")


def test_g0r2_m7_bare_rdf_xmp_in_an_mp4_is_flagged(tmp_path):
    _need("ffmpeg", "ffprobe")
    root = _tree(tmp_path, {})
    _m4a(root / "a.m4a")
    rdf = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<rdf:Description dc:description="a private annotation"/></rdf:RDF>').encode()
    box = (8 + 16 + len(rdf)).to_bytes(4, "big") + b"uuid" + XMP_UUID + rdf
    with open(root / "a.m4a", "ab") as f:
        f.write(box)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "XMP" in out, out


def test_g0r2_m7_any_xmp_in_an_image_is_flagged(tmp_path):
    _need("exiftool")
    root = _tree(tmp_path, {"a.png": PNG_1PX})
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:Rating=3", str(root / "a.png")], check=True)
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "XMP" in out, out


def test_g0r2_new3_a_tree_reachable_only_through_a_tag_is_name_checked(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("AcmeCorp\n")
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, *_ok_id())
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    tree = subprocess.run(["git", "-C", str(root), "mktree"], input="160000 commit %s\tvendor-acmecorp\n" % head,
                          capture_output=True, text=True, check=True, env=GIT_ENV).stdout.strip()
    subprocess.run(["git", "-C", str(root), "tag", "treetag", tree], check=True, env=GIT_ENV)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits", "--deny", str(deny))
    assert code == 1 and "vendor-acmecorp" in out and ": deny:" in out, out


# --- Codex G0 recheck 2 (round 3) ----------------------------------------------------------------------------------
@pytest.mark.parametrize("make", [
    lambda: 'clientSecret = "' + "abcdefghij" * 4 + '"\n',
    lambda: '"accessToken": "' + "ABCDEFGHIJ" * 4 + '"\n',
    lambda: "encryptionKey: " + "AbCdEfGhIjKlMnOpQrStUvWx" + "\n",
    lambda: "dbCredentials=" + "QwErTyUiOpAsDfGhJkLzXc" + "\n",
])
def test_g0r3_p2_3_camelcase_secret_keys_are_blocked(tmp_path, make):
    code, out = scan(tmp_path, {"cfg/app.json": make()})
    assert code == 1 and ": generic-secret:" in out, out


@pytest.mark.parametrize("line", [
    "monkeyBusinessDescription = aVeryLongValueThatIsNotASecretAtAll\n",   # "key" inside a word isn't a key
    "author_display_name = someone_with_a_long_handle_x\n",
    'cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")\n',   # a Swift argument label
])
def test_g0r3_p2_3_words_that_only_contain_key_or_auth_are_not_secret_keys(tmp_path, line):
    code, out = scan(tmp_path, {"a.py": line})
    assert code == 0, out


def _xmp_m4a(root, payload):
    _m4a(root / "a.m4a")
    box = (8 + 16 + len(payload)).to_bytes(4, "big") + b"uuid" + XMP_UUID + payload
    with open(root / "a.m4a", "ab") as f:
        f.write(box)


RDF_R = ('<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dc="http://purl.org/dc/elements/1.1/">'
         '<r:Description dc:description="a private annotation"/></r:RDF>')


@pytest.mark.parametrize("payload", [
    lambda: RDF_R.encode(),
    lambda: ('<?xml version="1.0" encoding="UTF-16"?>' + RDF_R.replace("r:", "rdf:")).encode("utf-16"),
    lambda: ('<?xml version="1.0" encoding="UTF-16BE"?>' + RDF_R).encode("utf-16-be"),
])
def test_g0r3_p2_4_av_xmp_in_any_prefix_or_encoding_is_flagged(tmp_path, payload):
    _need("ffmpeg", "ffprobe", "exiftool")
    root = _tree(tmp_path, {})
    _xmp_m4a(root, payload())
    code, out = _gate(root, "--media-tools", "required")
    assert code == 1 and "XMP" in out, out


def test_g0r3_p2_5_a_tree_reused_under_a_tag_only_root_is_checked_at_every_path(tmp_path):
    root = _repo(tmp_path, {"Users/alice": "x\n"})
    _commit(root, *_ok_id())
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True,
                         check=True).stdout.strip()
    other = subprocess.run(["git", "-C", str(root), "mktree"], input="040000 tree %s\tbackup\n" % top,
                           capture_output=True, text=True, check=True, env=GIT_ENV).stdout.strip()
    subprocess.run(["git", "-C", str(root), "tag", "a-root", top], check=True, env=GIT_ENV)   # names the shared tree first
    subprocess.run(["git", "-C", str(root), "tag", "b-backup", other], check=True, env=GIT_ENV)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "backup/" + "Users/alice" in out and ": home-path:" in out, out


def test_g0r3_s2_the_owner_exemption_needs_every_path_including_tag_aliases(tmp_path):
    root = _repo(tmp_path, {"LICENSE": "Copyright (c) 2026 %s\n" % lg.OWNER})
    _commit(root, *_ok_id())
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    blob = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD:LICENSE"], capture_output=True, text=True,
                          check=True).stdout.strip()
    subprocess.run(["git", "-C", str(root), "tag", "notes", blob], check=True, env=GIT_ENV)
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and ": identity:" in out, out


# Pins and the printable strings of compressed media (2026-10-11): random bytes in an encoded film look like
# 6-character "addresses" or a deny word in random case. Every planted string is assembled at run time.
FAKE_ADDR = "9" + "@v.z"                    # no real domain
FAKE_ADDR2 = "QA" + "@E.cFu"                # domain-shaped random bytes (a pin is what clears these)
REAL_ADDR = "jane.doe" + "@" + "corp-mail.com"
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42"


def _pin(root, rel, note="film encode, reviewed by hand"):
    digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    (root / ".github").mkdir(exist_ok=True)
    with open(root / ".github/leak-gate-pins.txt", "a") as f:
        f.write("%s  %s  %s\n" % (digest, rel, note))


def test_a_pinned_binary_skips_the_strings_scan_but_not_the_metadata_checks(tmp_path):
    body = MP4_HEAD + b"\x00\x93" + FAKE_ADDR2.encode() + b"\x01" + REAL_ADDR.encode() + b"\xff\x00"
    root = _tree(tmp_path, {"site/clip.mp4": body})
    env = _stub_tools(tmp_path, exiftool="exit 1", ffprobe="exit 1")   # no tool can read it: the pin covers that
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and ": email: " + REAL_ADDR in out and "metadata-unreadable" in out, out
    _pin(root, "site/clip.mp4")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 0, out
    # the same pinned file with an XMP packet: re-pinned, it still fails the metadata check
    (root / "site/clip.mp4").write_bytes(body + b"<x:xmpmeta xmlns:x='adobe:ns:meta/'></x:xmpmeta>")
    (root / ".github/leak-gate-pins.txt").unlink()
    _pin(root, "site/clip.mp4")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and "site/clip.mp4:meta: metadata: an XMP" in out and ": email:" not in out, out


def test_a_pin_that_no_longer_matches_brings_back_the_strings_scan(tmp_path):
    root = _tree(tmp_path, {"a.bin": b"\x00\x01" + REAL_ADDR.encode() + b"\x00"})
    _pin(root, "a.bin")
    assert _gate(root, "--media-tools", "skip")[0] == 0
    (root / "a.bin").write_bytes(b"\x00\x02" + REAL_ADDR.encode() + b"\x00")
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 1 and "a.bin:meta: pin-mismatch:" in out and ": email: " + REAL_ADDR in out, out


def test_unpinned_binary_strings_need_a_real_domain_and_a_readable_run(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("zork\n")
    noise = b"\x00\x93" + FAKE_ADDR.encode() + b"\x01\xfe" + b"zOrK" + b"\x02\x88" + b"#ZoRk%" + b"\x03"
    root = _tree(tmp_path, {"clip.mp4": MP4_HEAD + noise})
    code, out = _gate(root, "--media-tools", "skip", "--deny", str(deny))
    assert code == 0, out
    # a real address, or a deny word in a readable run, still fails an unpinned binary
    for extra, rule in ((REAL_ADDR.encode(), "email"), (b"made for Zork tools", "deny"),
                        (b"x/zork_build.dat", "deny")):
        (root / "clip.mp4").write_bytes(MP4_HEAD + noise + b"\x00" + extra + b"\x00")
        code, out = _gate(root, "--media-tools", "skip", "--deny", str(deny))
        assert code == 1 and ": %s: " % rule in out, (extra, out)
    # inside a readable run but not on its word boundaries: not the word
    (root / "clip.mp4").write_bytes(MP4_HEAD + noise + b"\x00" + b"xxzorkxxyy" + b"\x00")
    assert _gate(root, "--media-tools", "skip", "--deny", str(deny))[0] == 0


def test_text_files_keep_the_strict_rules_pinned_or_not(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("zork\n")
    root = _tree(tmp_path, {"a.md": "contact " + FAKE_ADDR + "\n", "b.txt": "zork\n"})
    _pin(root, "a.md", "a pin never excuses text")
    code, out = _gate(root, "--media-tools", "skip", "--deny", str(deny))
    assert code == 1 and "a.md:1: email: " + FAKE_ADDR in out and "b.txt:1: deny: zork" in out, out


def test_history_content_identical_to_a_pinned_file_skips_the_strings_scan(tmp_path):
    root = _repo(tmp_path, {"site/clip.mp4": MP4_HEAD + b"\x00" + REAL_ADDR.encode() + b"\x00"})
    _pin(root, "site/clip.mp4")
    _commit(root, *_ok_id())
    assert _gate(root, "--media-tools", "skip", "--check-commits")[0] == 0
    # the same bytes at a new path aren't pinned there: the tree scan blocks them
    (root / "site/copy.mp4").write_bytes((root / "site/clip.mp4").read_bytes())
    _commit(root, *_ok_id())
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and "site/copy.mp4:bin: email:" in out, out
