#!/usr/bin/env bash
# cu-lab.sh — computer-use R&D lane (red / yellow / green), like nightly-research.
#
#   cu-lab.sh [--date YYYY-MM-DD] [--skip-harvest] [--no-apply]
#
# Stages:
#   H harvest   local (+ yellow docs)  menu chords + gap scan
#   D propose   green (host)           propose skills from gaps
#   G gate      host                   deterministic reject
#   V verify    red                    offline checklist (no network)
#   A apply     host                   write shortcuts.jsonl / skills.jsonl
#   R report    host                   REPORT.md

set -uo pipefail

_src="${BASH_SOURCE[0]}"; _res="$(readlink -f "$_src" 2>/dev/null || true)"
[ -n "$_res" ] && _src="$_res"
LAB_DIR="$(cd "$(dirname "$_src")" && pwd)"
REPO_DIR="$(cd "$LAB_DIR/../.." && pwd)"
DATE=""; SKIP_HARVEST=0; NOAPPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --date) DATE="${2:-}"; shift 2 ;;
    --skip-harvest) SKIP_HARVEST=1; shift ;;
    --no-apply) NOAPPLY=1; shift ;;
    -h|--help) sed -n '2,16p' "$_src"; exit 0 ;;
    *) echo "cu-lab: unknown argument: $1" >&2; exit 2 ;;
  esac
done
DATE="${DATE:-$(date -u +%F)}"
LEARN_ROOT="${WORKMAN_LEARN_ROOT:-$HOME/.grok/workman-learn}"
OUT_DIR="$LAB_DIR/runs/$DATE"
mkdir -p "$OUT_DIR" "$LEARN_ROOT"
REPORT="$OUT_DIR/REPORT.md"
PROPOSALS="$OUT_DIR/proposals.jsonl"
: >"$PROPOSALS"

for _p in "$HOME/dgx-ai-lab/bin" "$HOME/.local/bin" "$REPO_DIR/.venv/bin"; do
  case ":$PATH:" in *":$_p:"*) ;; *) PATH="$_p:$PATH" ;; esac
done
export PATH WORKMAN_LEARN_ROOT="$LEARN_ROOT" PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"

py() {
  if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    "$REPO_DIR/.venv/bin/python" "$@"
  else
    python3 "$@"
  fi
}

{
  echo "# Computer-use lab $DATE"
  echo
} >"$REPORT"

if [ "$SKIP_HARVEST" -eq 0 ]; then
  py "$LAB_DIR/harvest_local.py" --out "$OUT_DIR/harvest.json" >>"$REPORT" 2>&1 \
    || echo "- harvest: failed (continued)" >>"$REPORT"
  if command -v dgxlab >/dev/null 2>&1; then
    dgxlab run -z yellow -- python3 "$LAB_DIR/harvest_docs.py" \
      --out /tmp/cu-harvest-docs.json >>"$OUT_DIR/yellow.log" 2>&1 \
      && cp /tmp/cu-harvest-docs.json "$OUT_DIR/harvest-docs.json" 2>/dev/null \
      || echo "- yellow docs: failed or unavailable (continued)" >>"$REPORT"
  fi
else
  echo "- harvest: skipped by flag" >>"$REPORT"
fi

py "$LAB_DIR/propose.py" --learn-root "$LEARN_ROOT" --out "$PROPOSALS" \
  --harvest "$OUT_DIR/harvest.json" >>"$REPORT" 2>&1 \
  || echo "- propose: failed (continued)" >>"$REPORT"

py "$LAB_DIR/gate.py" --in "$PROPOSALS" --out "$OUT_DIR/gated.jsonl" \
  >>"$REPORT" 2>&1 || echo "- gate: failed (continued)" >>"$REPORT"

if command -v dgxlab >/dev/null 2>&1; then
  cp "$OUT_DIR/gated.jsonl" /tmp/cu-gated.jsonl
  if dgxlab run -z red -- python3 "$LAB_DIR/verify_red.py" \
      --in /tmp/cu-gated.jsonl --out /tmp/cu-verified.jsonl >>"$OUT_DIR/red.log" 2>&1; then
    cp /tmp/cu-verified.jsonl "$OUT_DIR/verified.jsonl"
    echo "- red verify: ok" >>"$REPORT"
  else
    cp "$OUT_DIR/gated.jsonl" "$OUT_DIR/verified.jsonl"
    echo "- red verify: fell back to gated" >>"$REPORT"
  fi
else
  py "$LAB_DIR/verify_red.py" --in "$OUT_DIR/gated.jsonl" --out "$OUT_DIR/verified.jsonl" \
    >>"$REPORT" 2>&1 || cp "$OUT_DIR/gated.jsonl" "$OUT_DIR/verified.jsonl"
  echo "- red verify: in-process (no dgxlab)" >>"$REPORT"
fi

if [ "$NOAPPLY" -eq 0 ]; then
  py "$LAB_DIR/apply.py" --in "$OUT_DIR/verified.jsonl" --learn-root "$LEARN_ROOT" \
    >>"$REPORT" 2>&1 || echo "- apply: failed" >>"$REPORT"
else
  echo "- apply: skipped (--no-apply)" >>"$REPORT"
fi

echo "- done: $REPORT" >>"$REPORT"
echo "$REPORT"
