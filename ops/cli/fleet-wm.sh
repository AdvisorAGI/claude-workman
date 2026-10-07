#!/usr/bin/env bash
# fleet-wm.sh <device|all> <verb> [args...]   (2026-09-12)
#
# Another machine's screen and input from this shell, through wm.py's persistent
# remote channel (workman.remote: one ssh session per host, JSON lines, the ssh
# path picked by ~/.ssh/fleet-connect.sh: cable, LAN, Headscale, Cloudflare).
#
#   fleet-wm.sh mac-mini shot                 -> ~/.claude/skills/workman/shots/mac-mini-shot-<UTC>.png
#   fleet-wm.sh mac-mini zoom - 0 0 800 600   -> ~/.claude/skills/workman/shots/mac-mini-zoom-<UTC>.png
#   fleet-wm.sh md windows
#   fleet-wm.sh all ping                      -> every device in turn
#   fleet-wm.sh mac-mini status               -> transport in use + round trip
#   fleet-wm.sh mac-mini pause status | resume confirmed | release
#   fleet-wm.sh mac-mini handoff              -> release, close what we opened, park, verify
#
# Devices: dgx mac-mini md air wind1 (aliases: mini, tariqul, spark, windows).
set -u
WM="${WM_PY_SOURCE:-$HOME/.claude/tools/wm.py}"
SHOTS="${FLEET_WM_OUT:-$HOME/.claude/skills/workman/shots}"
ALL="dgx mac-mini md air"

dev="${1:-}"; [ $# -gt 0 ] && shift
verb="${1:-}"; [ $# -gt 0 ] && shift
if [ -z "$dev" ] || [ -z "$verb" ]; then
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
devices="$dev"
[ "$dev" = all ] && devices="$ALL"
# Fan-out allows observation and the safety verbs. Input must target one
# explicitly named device and dispatches through Workman's owner gate.
if [ "$dev" = all ]; then
  case "$verb" in
    shot|zoom|region|windows|active|platform|status|ping|elements|tree|focused|pause|resume|handoff) ;;
    *) echo 'fleet-wm: input requires one device; all allows observation and safety verbs' >&2; exit 2 ;;
  esac
fi
mkdir -p "$SHOTS"

rc=0
for d in $devices; do
  [ "$dev" = all ] && echo "== $d =="
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  case "$verb" in
    shot|zoom|region)
      path="${1:-}"
      if [ -z "$path" ] || [ "$path" = "-" ]; then
        path="$SHOTS/$d-$verb-$stamp.png"
      fi
      [ $# -gt 0 ] && shift
      python3 "$WM" --host "$d" "$verb" "$path" "$@" || rc=$?
      # put the path back for the next device when running 'all'
      set -- "-" "$@"
      ;;
    *)
      python3 "$WM" --host "$d" "$verb" "$@" || rc=$?
      ;;
  esac
done
exit $rc
