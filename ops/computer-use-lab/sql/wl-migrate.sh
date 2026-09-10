#!/usr/bin/env bash
# Apply additive wl/*.sql. Refuses any file that looks destructive.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DSN_FILE="${WORKMAN_LEARN_DSN_FILE:-}"
if [ -z "${DATABASE_URL:-}" ] && [ -z "$DSN_FILE" ]; then
  echo "wl-migrate: set DATABASE_URL (sourced, never echoed) or skip; JSONL is enough" >&2
  exit 0
fi
bad=0
for f in "$DIR"/*.sql; do
  if grep -Eiq 'drop[[:space:]]+(table|schema|index|extension)|truncate[[:space:]]|delete[[:space:]]+from' "$f"; then
    echo "wl-migrate: refused $f (destructive SQL)" >&2
    bad=1
  fi
done
if [ "$bad" -ne 0 ]; then
  exit 2
fi
if [ -n "${DATABASE_URL:-}" ]; then
  for f in "$DIR"/[0-9]*.sql; do
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
  done
fi
