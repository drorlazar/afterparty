#!/bin/sh
# Afterparty uninstaller: removes only what the installer added and nobody changed since.
# --purge also removes Afterparty's settings, cleanup history and logs (and the code folder, only if it is an
# unchanged clone of the official repository); --dry-run lists what it would do.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset CDPATH  # with CDPATH set, `cd relative/dir` prints the folder into our command substitutions
here=$(cd "$(dirname "$0")" && pwd -P)
cfg=${AFTERPARTY_CONFIG_DIR:-$HOME/.config/afterparty}
py=""
[ -r "$cfg/python" ] && py=$(head -n 1 "$cfg/python")
if [ -z "$py" ] || [ ! -x "$py" ]; then
  py=$("$here/bin/afterparty-pick-python") || exit $?
fi
entry='import sys; sys.path.insert(0, sys.argv.pop(1)); from cleanup.installer.main import main; sys.exit(main())'
exec "$py" -I -B -c "$entry" "$here" uninstall "$@"
