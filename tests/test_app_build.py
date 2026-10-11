import os
import plistlib
import subprocess

import pytest

from tests.conftest import REPO

SCRIPT = REPO / "app" / "build-app.sh"


def _bundle(path, bundle_id):
    (path / "Contents").mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)


def _run(dest, extra=None):
    env = {"HOME": str(dest.parent), "AFTERPARTY_APP_DEST": str(dest), "PATH": "/usr/bin:/bin",
           "PYTHONDONTWRITEBYTECODE": "1"}   # CI checks that no .pyc lands in the checkout
    env.update(extra or {})
    return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=900)


def test_refuses_to_replace_another_apps_bundle(tmp_path):
    dest = tmp_path / "Applications"
    _bundle(dest / "Afterparty.app", "com.example.other")
    (dest / "Afterparty.app" / "keep.txt").write_text("theirs")
    r = _run(dest)
    assert r.returncode == 3 and "isn't Afterparty's app" in r.stderr
    assert (dest / "Afterparty.app" / "keep.txt").read_text() == "theirs"


def test_refuses_a_plain_folder_or_a_link(tmp_path):
    dest = tmp_path / "Applications"
    (dest / "Afterparty.app").mkdir(parents=True)
    assert _run(dest).returncode == 3
    other = tmp_path / "Other.app"
    _bundle(other, "io.github.drorlazar.afterparty")
    os.rmdir(dest / "Afterparty.app")
    os.symlink(other, dest / "Afterparty.app")
    assert _run(dest).returncode == 3 and os.path.islink(dest / "Afterparty.app")


def test_never_deletes_a_user_folder_at_an_old_staging_name(tmp_path):
    dest = tmp_path / "Applications"
    (dest / ".Afterparty.app.new").mkdir(parents=True)
    (dest / ".Afterparty.app.new" / "mine.txt").write_text("keep")
    _run(dest, {"AFTERPARTY_APP_BUILD_DIR": str(tmp_path / "out")})
    assert (dest / ".Afterparty.app.new" / "mine.txt").read_text() == "keep"


def test_build_script_has_one_public_bundle_id_and_no_python():
    s = SCRIPT.read_text()
    assert 'BUNDLE_ID="io.github.drorlazar.afterparty"' in s and s.count("CFBundleIdentifier") == 2
    assert "python3" not in s and "<string>$BUNDLE_ID</string>" in s and "mktemp -d" in s
    assert ".Afterparty.app.new" not in s and "/bin/mv -n" in s and 'rm -rf "$APP"' not in s
    assert 'mv "$APP"' not in s                      # the standalone build never moves an existing bundle


def test_standalone_build_never_replaces_even_our_own_bundle(tmp_path):
    dest = tmp_path / "Applications"
    _bundle(dest / "Afterparty.app", "io.github.drorlazar.afterparty")
    (dest / "Afterparty.app" / "edited.txt").write_text("changed by the user")
    r = _run(dest)
    assert r.returncode == 3 and "install.sh --yes --app yes" in r.stderr
    assert (dest / "Afterparty.app" / "edited.txt").exists()


def test_swift_app_is_afterparty_and_writes_no_bytecode():
    assert not (REPO / "app" / "CleanUp.swift").exists() and (REPO / "app" / "AppIcon.icns").exists()
    src = (REPO / "app" / "Afterparty.swift").read_text()
    assert "CleanUp" not in src and '"AfterpartyRepo"' in src and 'window.title = "Afterparty"' in src
    assert '["-B", "-m", "cleanup.cli", "app", "--launchd", "--no-open"]' in src


@pytest.mark.skipif(os.environ.get("AFTERPARTY_SWIFT_TEST") != "1", reason="compiles Swift (slow); opt in")
def test_build_only_then_a_standalone_first_install(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    r = _run(tmp_path / "Applications", {"AFTERPARTY_APP_BUILD_DIR": str(out)})
    assert r.returncode == 0, r.stderr
    info = plistlib.loads((out / "Afterparty.app/Contents/Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "io.github.drorlazar.afterparty"
    assert info["CFBundleExecutable"] == "Afterparty" and info["AfterpartyRepo"] == str(REPO)
    assert (out / "Afterparty.app/Contents/MacOS/Afterparty").is_file()
    assert not (tmp_path / "Applications").exists()            # build-only installs nothing
    dest = tmp_path / "Applications"
    assert _run(dest).returncode == 0                          # nothing there yet: placed
    assert _run(dest).returncode == 3                          # never replaced by the standalone build
