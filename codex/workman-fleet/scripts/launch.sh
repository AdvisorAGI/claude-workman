#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY=${WORKMAN_FLEET_PYTHON:-/home/monzurul/workman/.venv/bin/python}
if [ ! -x "$PY" ]; then
  echo 'Workman Fleet: configured Python runtime is missing; see README.md. No automatic installation attempted.' >&2
  exit 2
fi
exec "$PY" "$ROOT/scripts/server.py"
