# Computer-use lab — teach Workman like a person, every day

Workman already has a static shortcut table and a recipe store. This lab is the
**research & development** lane that makes him keep learning computer-use the
way a human does: try a habit, keep what works, never forget the chord.

It mirrors the nightly vendor-research lane on the DGX:

| Stage | Zone | What runs |
|---|---|---|
| F harvest / fetch | **yellow** | Read allowlisted public shortcut/docs sources; never execute fetched bytes. On a Mac, live AX menu harvest is preferred and needs no fetch. |
| D propose | **green** (host) | Model proposes new skills / chords from journal gaps + harvest. Seats cannot run inside red/yellow (tmpfs home). |
| G gate | host | Deterministic checks: no secrets, has steps_v2, app+platform set, chord parseable. |
| V verify | **red** | Offline eval of the proposed skill against a frozen checklist (no network). Reject if it requires DOM injection or teleports. |
| A apply | host | Upsert into `~/.grok/workman-learn/shortcuts.jsonl` and `skills.jsonl`. |
| R report | host | Short REPORT line for the owner. |

## Everyday loop (any session, any OS)

1. `cu_memory op=working` with a short checklist, or let `episode.start` open one.
2. `cu_memory op=recall` / `cu_skill_recall` before inventing a mouse path.
3. `shortcut(action, app=...)` then act. Screenshot to confirm.
4. `cu_memory op=tick` as items finish. Desk facts: `op=fact q='subj | pred | obj'`.
5. After a verified win: `cu_skill_teach`. Do not paste the journal into the model.

## DGX

```bash
export PATH=$HOME/dgx-ai-lab/bin:$PATH
# from a checkout of this repo, or ~/ops/computer-use-lab after deploy:
./ops/computer-use-lab/cu-lab.sh --date $(date -u +%F)
```

Yellow fetch uses `dgxlab run -z yellow`. Red verify uses `dgxlab run -z red`.
Propose stays on the host green audit chain (same rule as nightly-research).

## Daily 5AM tuner (DGX only)

One session per day at 05:00 Asia/Dhaka. It **does** a real desktop task on this
box, remembers a verified win, then stops.

| Rule | Meaning |
|---|---|
| 15 minutes is a **ceiling**, not a quota | Stop as soon as the one task is done. Do not fill the slot. |
| One task, finish it | No second task. SIGTERM at 15 min so in-flight work can land; short grace, then SIGKILL. |
| Skip if nothing to learn | If every task already has a skill and a recent success, spend **no seat**. |
| Recheck is cheap | A stale skill is replayed by Grok/Qwen for a few minutes, not Fable. |
| Learn uses the owner ladder | Fable, else ChatGPT Astra (Codex), else Grok 4.6, else Cursor, else Qwen. |
| Remember | `cu_skills.teach` into `~/.grok/workman-learn/skills.jsonl` with `task_id`. |

```bash
# on the DGX
~/ops/computer-use-lab/cu-tune.sh --probe   # seats only, no spend
systemctl --user list-timers cu-tune.timer
# halt: touch ~/ops/computer-use-lab/STOP
```

Caps: `CU_TUNE_MAX_SEC=900`, `CU_TUNE_GRACE_SEC=60`, recheck `CU_TUNE_RECHECK_SEC=480`.

## Files

| Path | Role |
|---|---|
| `$HOME/.grok/workman-learn/shortcuts.jsonl` | Durable chords, never deleted |
| `$HOME/.grok/workman-learn/shortcuts-meta.json` | Harvest TTL per app |
| `$HOME/.grok/workman-learn/skills.jsonl` | Taught computer-use skills |
| `$HOME/.grok/workman-learn/recipes.jsonl` | Existing GUI recipes (unchanged) |
| `$HOME/.grok/workman-learn/episodes.jsonl` | Judged episodes (task + outcome) |
| `$HOME/.grok/workman-learn/working.json` | In-task checklist (remaining items only) |
| `$HOME/.grok/workman-learn/facts.jsonl` | Temporal desk facts (`valid_from` / `valid_to`) |
| `$HOME/.grok/workman-learn/work-history.jsonl` | One-line work log |
| `$HOME/.grok/workman-learn/session-history.jsonl` | Tune/session open/close |
| fleet mirrors | Read-only copies from other devices |

## Memory (four stores, token-light)

Computer-use memory is **not** a chat vector store. Models from local Qwen through
frontier see **one MCP tool**, `cu_memory`, and always get `{ok, op, n, lines}` under
~1600 characters. Skill recall is the same packed shape. The journal stays on disk.

| Layer | File | What the model sees |
|---|---|---|
| Working | `working.json` | Remaining checklist (`TODO …`), not history |
| Episodic | `episodes.jsonl` | Closed after a judged outcome; counts in `op=status` |
| Procedural | `skills.jsonl` | `cu_skill_recall` → one-line `s:title \| app \| steps` |
| Semantic | `facts.jsonl` | `op=fact` `q='subj \| pred \| obj'`; old values get `valid_to` |

Do not embed screenshots or typed text. Do not dump the journal into a prompt.
`ops/computer-use-lab/brief.py` caps the daily tuner brief at 1200 characters
(5 journal *kinds*, not 40 raw lines). Qwen also hard-caps the prompt at 4000
chars (`CU_TUNE_QWEN_PROMPT_MAX`) with `enable_thinking=false`.

`cu_memory` ops: `status | working | tick | recall | fact | forget | history | board`.
Aliases exist for smaller models (`done` = tick, `remember` = fact, `Recall HDMI hz`
puts the query in `op`).

Postgres `wl.*` (optional ingest) lives in `sql/001_wl.sql`. File JSONL is the
source of truth. Never mix this store with advisor pgvector or the 24h pinned
`board_*` window.
