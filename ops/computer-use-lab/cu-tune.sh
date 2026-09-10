#!/usr/bin/env bash
# cu-tune.sh — daily DGX computer-use session.
# One task, finish it, remember it. 30 minutes is a daily ceiling, not a quota.
# If nothing is left to learn, the session exits without spending a seat.
#
#   cu-tune.sh [--probe] [--date YYYY-MM-DD] [--force]
#
# Leader (learn): Fable, else ChatGPT Astra (Codex), else Grok 4.6, else Cursor, else Qwen.
# Recheck of a known skill: cheaper seat, short cap.
# Verifier: a different available CLI.

set -uo pipefail

_src="${BASH_SOURCE[0]}"; _res="$(readlink -f "$_src" 2>/dev/null || true)"
[ -n "$_res" ] && _src="$_res"
LAB_DIR="$(cd "$(dirname "$_src")" && pwd)"
REPO_DIR="${WORKMAN_ROOT:-$(cd "$LAB_DIR/../.." && pwd)}"
if [ ! -d "$REPO_DIR/workman" ]; then
  REPO_DIR="${WORKMAN_ROOT:-$HOME/workman}"
fi

PROBE=0
FORCE=0
DATE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --probe) PROBE=1; shift ;;
    --force) FORCE=1; shift ;;
    --date) DATE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$_src"; exit 0 ;;
    *) echo "cu-tune: unknown argument: $1" >&2; exit 2 ;;
  esac
done

DATE="${DATE:-$(date -u +%F)}"
LEARN_ROOT="${WORKMAN_LEARN_ROOT:-$HOME/.grok/workman-learn}"
OUT_DIR="$LAB_DIR/runs/$DATE"
mkdir -p "$OUT_DIR" "$LEARN_ROOT"
STOP="$LAB_DIR/STOP"
# 30 minutes is the daily max. Recheck uses a shorter cap. Grace is only to
# finish the click already in flight, not to start a second task.
MAX_SEC="${CU_TUNE_MAX_SEC:-1800}"
GRACE_SEC="${CU_TUNE_GRACE_SEC:-300}"
RECHECK_SEC="${CU_TUNE_RECHECK_SEC:-480}"
MAX_TURNS="${CU_TUNE_MAX_TURNS:-40}"
RECHECK_TURNS="${CU_TUNE_RECHECK_TURNS:-12}"

for _p in "$HOME/dgx-ai-lab/bin" "$HOME/.local/bin" "$HOME/bin" "$HOME/autonomy/bin" "$REPO_DIR/.venv/bin"; do
  case ":$PATH:" in *":$_p:"*) ;; *) PATH="$_p:$PATH" ;; esac
done
export PATH PYTHONPATH="$LAB_DIR:$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export WORKMAN_LEARN_ROOT="$LEARN_ROOT"
export WORKMAN_ROOT="$REPO_DIR"

py() {
  if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    "$REPO_DIR/.venv/bin/python" "$@"
  else
    python3 "$@"
  fi
}

run_with_cap() {
  timeout --signal=TERM --kill-after="$GRACE_SEC" "$MAX_SEC" "$@"
}

if [ -f "$STOP" ]; then
  echo "cu-tune: STOP file present ($STOP)" >&2
  exit 0
fi

# One invocation at a time for this date.
exec 9>"$OUT_DIR/.lock"
if ! flock -n 9; then
  echo "cu-tune: already running for $DATE" >&2
  exit 0
fi

if [ "$PROBE" -eq 1 ]; then
  py "$LAB_DIR/seats.py" --json
  exit 0
fi

PICK_ARGS=(--tasks "$LAB_DIR/tasks.jsonl" --date "$DATE" --learn-root "$LEARN_ROOT" --out-dir "$OUT_DIR")
if [ "$FORCE" -eq 1 ]; then
  PICK_ARGS+=(--force)
fi
DECISION_JSON="$OUT_DIR/decision.json"
py "$LAB_DIR/pick_task.py" "${PICK_ARGS[@]}" >"$DECISION_JSON"
ACTION=$(py -c "import json; print(json.load(open('$DECISION_JSON')).get('action') or '')")
MODE=$(py -c "import json; print(json.load(open('$DECISION_JSON')).get('mode') or '')")
REASON=$(py -c "import json; print(json.load(open('$DECISION_JSON')).get('reason') or '')")
export REASON

write_tune() {
  {
    echo "# Computer-use tune $DATE"
    echo
    echo "- host: DGX"
    echo "- action: ${1:-}"
    echo "- mode: ${MODE:-}"
    echo "- reason: ${REASON:-}"
    echo "- task: ${TASK_ID:-}"
    echo "- leader: ${LEADER_ID:-none} (${LEADER_MODEL:-})"
    echo "- verifier: ${VERIFY_ID:-none} (${VERIFY_MODEL:-})"
    echo "- caps: ${MAX_SEC:-}s + ${GRACE_SEC}s finish-grace, max_turns=${MAX_TURNS:-}"
    echo "- finished: ${FINISHED:-}"
    echo "- outcome: ${OUTCOME:-}"
    echo "- seats: ${SEATS_JSON:-}"
  } >"$OUT_DIR/TUNE.md"
  echo "$OUT_DIR/TUNE.md"
}

if [ "$ACTION" != "do" ]; then
  cp "$DECISION_JSON" "$OUT_DIR/skip.json"
  OUTCOME=skipped
  py -c '
from workman import memory_graph as mg
import os
mg.work("tune_skip", os.environ.get("REASON") or "skip")
' 2>/dev/null || true
  write_tune skip
  exit 0
fi

if [ -z "${DISPLAY:-}" ]; then
  _gs=$(pgrep -f gnome-session-binary | head -1 || true)
  if [ -n "$_gs" ] && [ -r "/proc/$_gs/environ" ]; then
    eval "$(tr '\0' '\n' < /proc/$_gs/environ \
      | grep -E '^(DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)=' \
      | sed 's/^/export /')"
  fi
fi

SEATS_JSON="$OUT_DIR/seats.json"
py "$LAB_DIR/seats.py" --json --mode "$MODE" >"$SEATS_JSON" || true
LEADER_ID=$(py -c "import json; d=json.load(open('$SEATS_JSON')); l=d.get('leader') or {}; print(l.get('id') or '')")
VERIFY_ID=$(py -c "import json; d=json.load(open('$SEATS_JSON')); v=d.get('verifier') or {}; print(v.get('id') or '')")
LEADER_MODEL=$(py -c "import json; d=json.load(open('$SEATS_JSON')); l=d.get('leader') or {}; print(l.get('model') or '')")
VERIFY_MODEL=$(py -c "import json; d=json.load(open('$SEATS_JSON')); v=d.get('verifier') or {}; print(v.get('model') or '')")

if [ -z "$LEADER_ID" ]; then
  echo "cu-tune: no leader seat" >&2
  cat "$SEATS_JSON"
  exit 2
fi

if [ "$MODE" = "recheck" ]; then
  MAX_SEC="$RECHECK_SEC"
  MAX_TURNS="$RECHECK_TURNS"
  GRACE_SEC="${CU_TUNE_RECHECK_GRACE_SEC:-120}"
fi

TASK_JSON="$OUT_DIR/task.json"
py -c "import json; json.dump(json.load(open('$DECISION_JSON')).get('task') or {}, open('$TASK_JSON','w'))"
TASK_ID=$(py -c "import json; print(json.load(open('$TASK_JSON')).get('task_id') or '')")
export CU_TUNE_TASK_JSON="$TASK_JSON"
export LEADER_ID LEADER_MODEL
SID=$(py -c '
import json, os
from workman import memory_graph as mg
row = json.load(open(os.environ["CU_TUNE_TASK_JSON"]))
title = (row.get("task_id") or row.get("task") or "cu-tune")[:80]
print(mg.session_open(
    title, source="cu-tune",
    model=os.environ.get("LEADER_MODEL") or "",
    rung=os.environ.get("LEADER_ID") or "",
).get("session_id") or "")
')
export WORKMAN_SESSION_ID="$SID"
EPISODE_UID=$(py -c '
import json, os
from workman import episode
row = json.load(open(os.environ["CU_TUNE_TASK_JSON"]))
post = row.get("post") or ""
checklist = [x for x in (row.get("task"), post) if x]
print(episode.start(
    row.get("task") or "",
    app=row.get("app") or "",
    model=os.environ.get("LEADER_MODEL") or "",
    rung=os.environ.get("LEADER_ID") or "",
    source="cu-tune",
    task_id=row.get("task_id") or "",
    checklist=checklist,
).get("episode_uid") or "")
')
export WORKMAN_EPISODE_UID="$EPISODE_UID"
export WORKMAN_TASK="$(py -c 'import json,os; print(json.load(open(os.environ["CU_TUNE_TASK_JSON"])).get("task") or "")')"
export WORKMAN_TASK_ID="$TASK_ID"

KNOWN="$OUT_DIR/known-skill.json"
export KNOWN_PATH="$KNOWN"
py -c '
import json, os
from pathlib import Path
row = json.load(open(os.environ["CU_TUNE_TASK_JSON"]))
tid = row.get("task_id") or ""
hits = []
p = Path(os.environ["WORKMAN_LEARN_ROOT"]) / "skills.jsonl"
if p.is_file():
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("task_id") == tid:
            hits.append(r)
Path(os.environ["KNOWN_PATH"]).write_text(json.dumps(hits[:1]))
'

BRIEF="$OUT_DIR/brief.txt"
py "$LAB_DIR/brief.py" --task "$TASK_JSON" --known "$KNOWN" \
  --journal "$LEARN_ROOT/journal.jsonl" --out "$BRIEF" >/dev/null || true

REMEMBER="$OUT_DIR/remembered.json"
PROMPT="$OUT_DIR/leader-prompt.txt"
{
  cat "$LAB_DIR/prompts/leader.md"
  echo
  echo "Leader: $LEADER_ID mode=$MODE cap=${MAX_SEC}s turns=$MAX_TURNS"
  echo
  echo "## brief"
  cat "$BRIEF" 2>/dev/null || true
} >"$PROMPT"

LEADER_OUT="$OUT_DIR/leader.out"
run_leader() {
  case "$LEADER_ID" in
    fable)
      run_with_cap claude -p --model "$LEADER_MODEL" --max-turns "$MAX_TURNS" \
        --output-format text \
        --append-system-prompt "30 min is a max, not a target. Finish the one task you started, remember it if new, then stop. Do not invent extra work." \
        < "$PROMPT" >"$LEADER_OUT" 2>"$OUT_DIR/leader.err"
      ;;
    astra)
      run_with_cap codex exec --sandbox workspace-write -m "$LEADER_MODEL" \
        "$(cat "$PROMPT")" >"$LEADER_OUT" 2>"$OUT_DIR/leader.err"
      ;;
    grok)
      run_with_cap grok -m grok-4.6 --effort xhigh --max-turns "$MAX_TURNS" \
        --deny "Bash(ssh *)" --deny "Bash(gh *)" --deny "Bash(git push *)" \
        --deny "Bash(rm -rf *)" --deny "Bash(docker *)" \
        -p "$(cat "$PROMPT")" >"$LEADER_OUT" 2>"$OUT_DIR/leader.err"
      ;;
    cursor)
      run_with_cap cursor-agent -p --model "${LEADER_MODEL:-}" \
        "$(cat "$PROMPT")" >"$LEADER_OUT" 2>"$OUT_DIR/leader.err"
      ;;
    qwen)
      run_with_cap python3 "$LAB_DIR/qwen_once.py" --prompt-file "$PROMPT" --out "$LEADER_OUT"
      ;;
    *)
      echo "cu-tune: unknown leader $LEADER_ID" >&2
      return 2
      ;;
  esac
}

set +e
run_leader
LEADER_EC=$?

python3 - "$LEADER_OUT" "$OUT_DIR/proposal.json" "$REMEMBER" <<'PY'
import json, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace") if Path(sys.argv[1]).exists() else ""
row = None
if Path(sys.argv[3]).exists():
    try:
        row = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    except ValueError:
        row = None
if row is None:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            row = json.loads(m.group(0))
        except ValueError:
            row = None
    Path(sys.argv[2]).write_text(json.dumps(row or {"error": "no-json", "raw": text[-400:]}) + "\n")
PY

VERDICT="$OUT_DIR/verifier.json"
export VERDICT
export LAB_DIR
export PROPOSAL="$OUT_DIR/proposal.json"
if [ -n "$VERIFY_ID" ]; then
  VPROMPT="$OUT_DIR/verifier-prompt.txt"
  {
    cat "$LAB_DIR/prompts/verifier.md"
    echo
    echo "## brief"
    py -c '
import json, os
import brief
task = json.load(open(os.environ["CU_TUNE_TASK_JSON"]))
prop = json.load(open(os.environ["PROPOSAL"]))
print(brief.verify_text(task, prop))
' 
  } >"$VPROMPT"
  case "$VERIFY_ID" in
    grok)
      timeout 180 grok -m grok-4.6 --effort high --max-turns 3 \
        --deny "Write(*)" --deny "Edit(*)" \
        -p "$(cat "$VPROMPT")" >"$OUT_DIR/verifier.out" 2>"$OUT_DIR/verifier.err" || true
      ;;
    sonnet|fable)
      timeout 180 claude -p --model "${VERIFY_MODEL:-sonnet}" --max-turns 3 \
        --output-format text --disallowedTools "Bash,Edit,Write" \
        < "$VPROMPT" >"$OUT_DIR/verifier.out" 2>"$OUT_DIR/verifier.err" || true
      ;;
    astra)
      timeout 180 codex exec --sandbox read-only -m "$VERIFY_MODEL" \
        "$(cat "$VPROMPT")" >"$OUT_DIR/verifier.out" 2>"$OUT_DIR/verifier.err" || true
      ;;
    qwen)
      python3 "$LAB_DIR/qwen_once.py" --prompt-file "$VPROMPT" --out "$OUT_DIR/verifier.out" || true
      ;;
    *)
      echo '{"verdict":"drop","finished":false,"reason":"no verifier"}' >"$OUT_DIR/verifier.out"
      ;;
  esac
  python3 - "$OUT_DIR/verifier.out" "$VERDICT" <<'PY'
import json, re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace") if Path(sys.argv[1]).exists() else ""
row = {"verdict": "drop", "finished": False, "reason": "unparsed"}
m = re.search(r"\{.*\}", text, re.S)
if m:
    try:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, dict) and parsed.get("verdict") in ("keep", "drop"):
            row = parsed
    except ValueError:
        pass
Path(sys.argv[2]).write_text(json.dumps(row) + "\n")
PY
else
  echo '{"verdict":"drop","finished":false,"reason":"no verifier seat"}' >"$VERDICT"
fi

KEEP=$(py -c "import json; print(json.load(open('$VERDICT')).get('verdict')=='keep')")
FINISHED=$(py -c "import json; d=json.load(open('$OUT_DIR/proposal.json')); print(bool(d.get('finished')))")
UNCHANGED=$(py -c "import json; d=json.load(open('$OUT_DIR/proposal.json')); print(bool(d.get('unchanged')))")
if [ "$KEEP" = "True" ] && [ "$UNCHANGED" != "True" ]; then
  py -c "import json; p=json.load(open('$OUT_DIR/proposal.json')); open('$OUT_DIR/proposal.jsonl','w').write(json.dumps(p)+'\n')"
  py "$LAB_DIR/gate.py" --in "$OUT_DIR/proposal.jsonl" --out "$OUT_DIR/gated.jsonl" || true
  py "$LAB_DIR/verify_red.py" --in "$OUT_DIR/gated.jsonl" --out "$OUT_DIR/verified.jsonl" || true
  py "$LAB_DIR/apply.py" --in "$OUT_DIR/verified.jsonl" --learn-root "$LEARN_ROOT" || true
  OUTCOME=success
elif [ "$KEEP" = "True" ]; then
  OUTCOME=success
elif [ "$FINISHED" = "True" ]; then
  OUTCOME=partial
else
  OUTCOME=failed
fi

export CU_OUTCOME="$OUTCOME"
export VERIFY_ID
py -c '
from workman import episode, memory_graph as mg
import os
print(episode.close(
    outcome=os.environ.get("CU_OUTCOME") or "unknown",
    judged_by=os.environ.get("VERIFY_ID") or "none",
    judge_note=open(os.environ["VERDICT"], encoding="utf-8").read()[:200],
))
mg.session_close(outcome=os.environ.get("CU_OUTCOME") or "unknown")
'

write_tune do
