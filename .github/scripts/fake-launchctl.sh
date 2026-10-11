#!/bin/sh
PATH=/usr/bin:/bin
log='@LOG@'
st='@STATE@'
echo "$@" >> "$log"
case "$1" in
  bootstrap) lbl=$(basename "$3" .plist) ;;
  submit) lbl=$3 ;;
  *) lbl=${2##*/} ;;
esac
case "$1" in
  bootstrap)
    if [ -e "$st/fail-bootstrap" ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    if [ -r "$HOME/.config/afterparty/config.json" ]; then cat "$HOME/.config/afterparty/config.json" >> "$log.config"; echo >> "$log.config"; fi
    : > "$st/$lbl"; exit 0 ;;
  bootout)
    if [ -e "$st/stuck-$lbl" ]; then exit 0; fi
    if [ -e "$st/$lbl" ]; then rm -f "$st/$lbl"; exit 0; fi
    echo "Boot-out failed: 3: No such process" >&2; exit 3 ;;
  print)
    if [ -e "$st/fail-print" ]; then echo "Could not access the domain: Operation not permitted" >&2; exit 1; fi
    if [ -e "$st/$lbl" ]; then exit 0; fi
    echo "Could not find service \"$lbl\" in domain" >&2; exit 113 ;;
  submit)
    # a one-off Full Disk Access probe: binaries listed in $st/fda-granted get "fda:yes", others "fda:no";
    # $st/fda-hang writes nothing (a probe that times out), $st/fail-submit fails the submit,
    # $st/hang-submit makes launchctl itself hang (the caller's time bound must end it)
    if [ -e "$st/hang-submit" ]; then exec sleep 30; fi
    if [ -e "$st/fail-submit" ]; then echo "submit failed" >&2; exit 5; fi
    out=""; bin=""
    while [ $# -gt 0 ]; do
      case $1 in -o) out=$2; shift ;; --) bin=$2; break ;; esac
      shift
    done
    echo "$bin" >> "$st/fda-probed"
    : > "$st/$lbl"
    if [ -e "$st/fda-hang" ]; then exit 0; fi
    # $st/fda-run: really run the submitted program (the generated probe payload) and keep what it prints
    if [ -e "$st/fda-run" ]; then shift; "$@" > "$out" 2>&1; exit 0; fi
    if [ -e "$st/fda-granted" ] && grep -qxF "$bin" "$st/fda-granted"; then echo "fda:yes" > "$out"; else echo "fda:no" > "$out"; fi
    exit 0 ;;
  remove)
    # $st/fail-remove: removal fails and the job stays; $st/stuck-remove: removal "succeeds" but the job stays;
    # $st/hang-remove: launchctl hangs
    if [ -e "$st/hang-remove" ]; then exec sleep 30; fi
    if [ -e "$st/fail-remove" ]; then echo "Remove failed: 5: Input/output error" >&2; exit 5; fi
    if [ -e "$st/stuck-remove" ]; then exit 0; fi
    if [ -e "$st/$2" ]; then rm -f "$st/$2"; exit 0; fi
    exit 3 ;;
  list)
    if [ -n "$2" ]; then
      if [ -e "$st/fail-list" ]; then echo "Could not access the domain" >&2; exit 1; fi
      if [ -e "$st/$2" ]; then printf -- '{\n\t"Label" = "%s";\n};\n' "$2"; exit 0; fi
      echo "Could not find service \"$2\" in domain for port" >&2; exit 113
    fi
    for f in "$st"/*; do
      b=$(basename "$f")
      case $b in fail-*|stuck-*|hang-*|fda-*|'*') ;; *) printf -- '-\t0\t%s\n' "$b" ;; esac
    done ;;
esac
