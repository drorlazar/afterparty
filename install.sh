#!/bin/sh
# Afterparty installer. `./install.sh --dry-run` lists every change; `./install.sh --yes` makes them.
# Installing never deletes your files. Answers come as flags (see INSTALL.md). It never depends on your shell's PATH.
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset CDPATH  # with CDPATH set, `cd relative/dir` prints the folder into our command substitutions
here=$(cd "$(dirname "$0")" && pwd -P)
if [ "$(uname -s)" != Darwin ]; then
  echo "Afterparty runs on macOS only." >&2
  exit 69
fi
major=$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)
case $major in ''|*[!0-9]*) major=0 ;; esac
if [ "$major" -lt 13 ]; then
  echo "Afterparty needs macOS 13 or newer." >&2
  exit 69
fi
want=""
prev=""
for arg in "$@"; do
  [ "$prev" = "--python" ] && want=$arg
  case $arg in --python=*) want=${arg#--python=} ;; esac
  prev=$arg
done
py=$("$here/bin/afterparty-pick-python" ${want:+"$want"}) || exit $?
entry='import sys; sys.path.insert(0, sys.argv.pop(1)); from cleanup.installer.main import main; sys.exit(main())'
exec "$py" -I -B -c "$entry" "$here" install --python "$py" "$@"
