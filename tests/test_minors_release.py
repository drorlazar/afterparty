"""Deferred release-tooling minors (Plan B, tasks 1, 2 and 12): the leak gate's remaining gaps, each pinned by a
test. Every private-looking value is assembled at run time so this file passes the gate's own rules."""
import os
import subprocess

import pytest

from tests.test_leak_gate import GIT_ENV, PNG_1PX, _commit, _gate, _ok_id, _repo, _stub_tools, _tree, lg
from tests.test_wording import TEXT_SUFFIXES


def scan(tmp_path, files, *args):
    return _gate(_tree(tmp_path, files), "--media-tools", "skip", *args)


@pytest.mark.parametrize("make", [
    lambda: "100" + ".64.1.2",                  # carrier-grade NAT (Tailscale and friends)
    lambda: "100" + ".127.255.1",
    lambda: "169" + ".254.10.20",               # IPv4 link-local
    lambda: "fd" + "12:3456:789a::1",           # IPv6 unique-local
    lambda: "fc" + "00::5",
    lambda: "fe" + "80::1",                     # IPv6 link-local
])
def test_more_private_ranges_are_blocked(tmp_path, make):
    code, out = scan(tmp_path, {"cfg.txt": "peer " + make() + "\n"})
    assert code == 1 and ": private-ip:" in out, out


@pytest.mark.parametrize("line", ["100.63.1.1", "100.128.0.1", "169.253.1.1", "fc: 1", "{fd: 3, fe: 4}",
                                  "color #fd8800", "127.0.0.1"])
def test_public_or_non_address_text_is_not_a_private_ip(tmp_path, line):
    code, out = scan(tmp_path, {"a.txt": line + "\n"})
    assert code == 0, out


@pytest.mark.parametrize("host", ["nas" + ".lan", "printer" + ".home.arpa", "db" + ".internal",
                                  "box" + ".localdomain", "wiki" + ".intranet"])
def test_private_network_hostnames_are_blocked(tmp_path, host):
    code, out = scan(tmp_path, {"a.txt": "ssh " + host + "\n"})
    assert code == 1 and ": hostname:" in out, out


def test_a_fifo_is_a_finding_and_never_hangs_the_gate(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n"})
    os.mkfifo(str(root / "pipe"))
    code, out = _gate(root, "--media-tools", "skip")         # _gate's timeout fails the test if it blocks
    assert code == 1 and "pipe:0: special-file:" in out, out


def test_an_exiftool_warning_is_not_a_full_read(tmp_path):
    root = _tree(tmp_path, {"a.png": PNG_1PX})
    env = _stub_tools(tmp_path, exiftool="printf '[{\"SourceFile\": \"%s\", \"ExifTool:Warning\": \"odd header\"}]' "
                                         "\"$6\"")
    code, out = _gate(root, "--media-tools", "required", env=env)
    assert code == 1 and "a.png:meta: metadata-unreadable:" in out, out


def test_utf16be_strings_in_a_binary_are_checked(tmp_path):
    # big-endian only: the little-endian scan of these bytes (shifted by one) loses the last letter of the path
    blob = b"\xff\x01" + ("/Users/" + "j").encode("utf-16-be") + b"\xff\x01"
    code, out = scan(tmp_path, {"data.bin": blob})
    assert code == 1 and (": home-path: /" + "Users/j") in out, out


@pytest.mark.parametrize("make,rule", [
    (lambda: "sk-" + "ant-" + "Ab9_" * 8, "anthropic-key"),
    (lambda: "sk-" + "Ab9x" * 10, "openai-key"),
    (lambda: "gh" + "p_" + "Ab9x" * 9, "github-token"),
    (lambda: "xo" + "xb-" + "1234567890-abcdef", "slack-token"),
    (lambda: "AI" + "za" + "Ab9_-" * 7, "google-key"),
])
def test_provider_keys_are_blocked(tmp_path, make, rule):
    code, out = scan(tmp_path, {"cfg/x.txt": "value " + make() + "\n"})
    assert code == 1 and (": %s:" % rule) in out, out


def test_a_link_target_is_checked_even_inside_the_tree(tmp_path):
    root = _tree(tmp_path, {"a.txt": "x\n"})
    os.symlink("notes-" + lg.FIRST, str(root / "shortcut"))  # dangling, but inside the tree: only its text leaks
    code, out = _gate(root, "--media-tools", "skip")
    assert code == 1 and "shortcut:link: identity:" in out and "link-escape" not in out, out


def test_a_deleted_files_path_in_history_is_checked(tmp_path):
    root = _repo(tmp_path, {"notes-" + lg.FIRST + "/a.txt": "x\n"})
    _commit(root, *_ok_id())
    subprocess.run(["git", "-C", str(root), "rm", "-q", "-r", "notes-" + lg.FIRST], check=True, env=GIT_ENV)
    _commit(root, *_ok_id())
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and ("history:notes-%s" % lg.FIRST) in out and ":name: identity:" in out, out


def test_a_commit_message_is_checked(tmp_path):
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, *_ok_id(), msg="moved from /Users/" + "janedoe/src")
    code, out = _gate(root, "--media-tools", "skip", "--check-commits")
    assert code == 1 and ".git:msg1: home-path:" in out, out


def test_an_inherited_git_dir_never_redirects_the_history_check(tmp_path):
    root = _repo(tmp_path, {"a.txt": "x\n"})
    _commit(root, "someone", "jane@" + "corp-mail.com")      # the tree's own history is bad
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(["git", "-C", str(decoy), "init", "-q"], check=True, env=GIT_ENV)
    _commit(decoy, *_ok_id())                                 # a clean history elsewhere
    env = dict(os.environ, GIT_DIR=str(decoy / ".git"), GIT_WORK_TREE=str(decoy))
    code, out = _gate(root, "--media-tools", "skip", "--check-commits", env=env)
    assert code == 1 and ": commit-identity:" in out, out


def test_wording_check_covers_icons_and_data_files():
    assert {".svg", ".json"} <= set(TEXT_SUFFIXES)
