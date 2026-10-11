#!/bin/bash
# Build Afterparty.app (a native window around the local server).
#   AFTERPARTY_APP_BUILD_DIR=<empty dir>  build into that folder only (the installer places it itself, after
#                                         checking the installed copy against its install record)
#   otherwise                             build, then place it at ${AFTERPARTY_APP_DEST:-~/Applications} only
#                                         if nothing is there yet
# This script never replaces or removes an existing Afterparty.app (a bundle id proves nothing about who made
# or changed a bundle): to update an installed app, run ./install.sh --yes --app yes. Staging happens in a folder
# it creates with mktemp; it never removes a path because of its name.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset CDPATH  # with CDPATH set, `cd relative/dir` prints the folder into our command substitutions
cd "$(dirname "$0")"
REPO="$(cd .. && pwd -P)"
BUNDLE_ID="io.github.drorlazar.afterparty"
DEST="${AFTERPARTY_APP_DEST:-$HOME/Applications}"
APP="$DEST/Afterparty.app"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO/cleanup/__init__.py")"
REPO_XML="$(printf '%s' "$REPO" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"

bundle_id() { /usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$1/Contents/Info.plist" 2>/dev/null || true; }
not_ours() { [ -L "$APP" ] || { [ -e "$APP" ] && [ "$(bundle_id "$APP")" != "$BUNDLE_ID" ]; }; }

if [ -z "${AFTERPARTY_APP_BUILD_DIR:-}" ] && { [ -e "$APP" ] || [ -L "$APP" ]; }; then
  if not_ours; then
    echo "build-app: $APP exists and isn't Afterparty's app; leaving it alone. Move it elsewhere, then build again." >&2
  else
    echo "build-app: $APP is already there. To replace it, run ./install.sh --yes --app yes (it checks the" >&2
    echo "installed copy against its install record first)." >&2
  fi
  exit 3
fi
if ! /usr/bin/xcode-select -p >/dev/null 2>&1 || ! /usr/bin/xcrun --find swiftc >/dev/null 2>&1 \
   || ! /usr/bin/xcrun --show-sdk-path >/dev/null 2>&1; then
  echo "build-app: Swift and the macOS SDK are needed. Run xcode-select --install, then build again." >&2
  exit 69
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/afterparty-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT          # our own mktemp folder
OUT="$WORK/Afterparty.app"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
/usr/bin/xcrun swiftc -O -swift-version 5 -module-name Afterparty -framework Cocoa -framework WebKit \
  -framework UserNotifications Afterparty.swift -o "$OUT/Contents/MacOS/Afterparty"
cp AppIcon.icns "$OUT/Contents/Resources/AppIcon.icns"
cat > "$OUT/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Afterparty</string>
  <key>CFBundleDisplayName</key><string>Afterparty</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>Afterparty</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>AfterpartyRepo</key><string>$REPO_XML</string>
</dict></plist>
EOF
codesign --force --sign - "$OUT" >/dev/null 2>&1   # ad-hoc signature so Gatekeeper/TCC can identify it

if [ -n "${AFTERPARTY_APP_BUILD_DIR:-}" ]; then
  [ -d "$AFTERPARTY_APP_BUILD_DIR" ] && [ ! -e "$AFTERPARTY_APP_BUILD_DIR/Afterparty.app" ] || {
    echo "build-app: AFTERPARTY_APP_BUILD_DIR must be an existing folder without Afterparty.app in it" >&2; exit 2; }
  cp -R "$OUT" "$AFTERPARTY_APP_BUILD_DIR/Afterparty.app"
  echo "built $AFTERPARTY_APP_BUILD_DIR/Afterparty.app ($VERSION)"
  exit 0
fi

mkdir -p "$DEST"
STAGE="$(mktemp -d "$DEST/.afterparty-stage.XXXXXX")"
cp -R "$OUT" "$STAGE/Afterparty.app"
/bin/mv -n "$STAGE/Afterparty.app" "$DEST/" || true   # -n: never replaces; whatever appeared meanwhile wins
if [ -e "$STAGE/Afterparty.app" ]; then
  echo "build-app: $APP appeared while building; it was left alone." >&2
  rm -rf "$STAGE"; exit 3           # our mktemp folder, holding only our new copy
fi
rmdir "$STAGE"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
echo "installed $APP ($VERSION)"
