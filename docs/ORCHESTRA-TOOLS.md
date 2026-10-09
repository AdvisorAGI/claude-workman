# Orchestra tools: one place to see and steer every Claude session

Owner order 2026-10-08: the lead manages sessions from one place, hears at once
when a session needs approval or input, compacts other sessions at a good turn,
and sees each session's usage live. These tools do that with **zero model
calls**: everything is read from files the CLI and the desktop app already
write.

| Tool (MCP) | CLI | Does |
|---|---|---|
| `sessions_list(machine="local", active_within_min=180, include_subagents=False)` | `workman orchestra-list [--json] [--within M] [--machine mac-one]` | every session: account, model/effort, context, state, front end, pid |
| `session_needs(machine="local", silent_min=25)` | `workman orchestra-needs` | only the sessions waiting on someone |
| `session_usage(window_h=24, by="session"\|"account", top=25)` | `workman orchestra-usage [--by account]` | tokens, images, turns; repeated first prompts |
| `session_events(since_cursor="", kinds="", limit=50)` | `workman orchestra-events [--since N] [--kinds a,b] [--no-collect]` | cursor feed of new events |
| (collector) | `workman orchestra-collect` | one zero-model pass, safe every 30-60 s |
| `session_compact(session, mode="dry_run", min_tokens=300000, max_tokens=400000, force=False, resume="")` | `workman orchestra-compact <session> [--run] [--force]` | compact ANOTHER session from its front end |
| `session_set_model_effort(session, model="", effort="", mode="dry_run", allow_fable=False, max_tokens=400000, force=False)` | `workman orchestra-set-model <session> [--model M] [--effort E] [--allow-fable] [--run]` | change ANOTHER session's model/effort through its picker |

`session` is a session id or a prefix of at least 6 hex chars, a desktop
`local_<uuid>`, or an exact title.

Code layout (one concern per module, each under 400 lines):

| Module | Concern |
|---|---|
| `workman/orchestra_tools.py` | read-side MCP handlers, `machine=` over ssh, CLI |
| `workman/orchestra_control.py` | control re-exports and CLI (incl. the `rc-compact` runner entry) |
| `workman/orchestra/common.py` | policy constants, paths, time, `redact` |
| `workman/orchestra/transcripts.py` | transcript records and `parse_transcript` |
| `workman/orchestra/registry.py` | accounts, live registry, desktop store, /proc, tmux, caller ids |
| `workman/orchestra/state.py` | `classify`, `front_end`, `turn_ready`, `Scan`, needs |
| `workman/orchestra/usage.py` | usage per session/account, repeats |
| `workman/orchestra/events.py` | collector and cursor feed |
| `workman/orchestra/guards.py` | shared guards, band, target lookup, logs |
| `workman/orchestra/drivers.py` | Chrome verbs, page and focus checks |
| `workman/orchestra/compact.py` | `session_compact`, guarded `session_front_compact`, `RemoteCompact` |
| `workman/orchestra/picker.py` | model ids/labels, desktop and Chrome pickers |
| `workman/orchestra/model_effort.py` | `session_set_model_effort` |
| `workman/front_compact.py` | the desktop runner (PR #6): reads, header guard, `FrontCompact` |
| `workman/front_compact_cli.py` | `spawn`, `learn_header`, the runner CLI and its guard chain |

Tests mirror them: `tests/test_orchestra_<module>.py`, shared fakes in
`tests/orchestra_fakes.py`.

## Sources (read-only)

| Source | Gives |
|---|---|
| `~/.claude*/projects/<slug>/<session>.jsonl` | the transcript: one config dir per account seat |
| `~/.claude*/sessions/<pid>.json` | the CLI's live registry: pid, status (busy/idle/waiting), `waitingFor`, entrypoint, the desktop `hostSessionId` |
| `<config>/.claude.json`, `~/.claude.json` | `oauthAccount.emailAddress` and `accountUuid` only; nothing else is kept |
| `~/.config/Claude/claude-code-sessions/<account>/<org>/local_*.json` | desktop sessions: local id, title, model, effort (allow-listed keys) |
| `/proc/<pid>/{cmdline,stat,cgroup}` | `--model`/`--effort`, print mode, the parent `claude rc --name`, the systemd unit |
| `~/.claude/fleet-session-events.jsonl` | the Stop/Notification hook feed (permission prompts) |
| `~/orchestra/runs/RUNS.jsonl` | which session ids were headless `run-claude-seat` runs |

Never printed: tokens, bridge session ids, socket paths, tool inputs, command
lines. Prompts, titles and questions pass through `redact` (the agent-watch
patterns plus `session_`/`cse_` ids, plain or hyphenated).

## Fields and states

- **context** = last main-chain assistant usage `input + cache_read + cache_creation`;
  `ctx_pct` is of 1M. A `compact_boundary` after that usage marks it stale (0).
- **model / effort** = what the last turn ran with (transcript); `front_model` /
  `front_effort` = what the front end is set to (desktop store, or `--model` /
  `--effort` of the live process).
- **account** = the desktop store's account uuid when there is one, else the
  config dir's `.claude.json`; the default `~/.claude` dir uses `~/.claude.json`
  (the CLI's file when `CLAUDE_CONFIG_DIR` is unset). `account_source` says which.
- **state**, in precedence order:
  `limit_hit` (last record is a rate-limit API error whose reset time has not
  passed) > `needs_input` (open AskUserQuestion, or the registry says `waiting`)
  > `blocked` (a classifier or permission-rule denial in the current turn, and
  the turn ended or went quiet >= 60 s; the owner's own reject/abort is not a
  denial) > `working` > `silent` (>= 25 min inside a tool call, after a tool
  result, or after a prompt) > `idle` (turn ended, interrupted, a local slash
  command, or nothing to do).
- **front end**: `desktop` (store entry or `hostSessionId`), `remote_control`
  (parent `claude rc`, a `claude-rc-*` unit, or a bridged registry entry),
  `headless` (`claude -p` without `--sdk-url`, or a RUNS.jsonl seat, or an SDK
  entrypoint with no other front end), `tmux` (a pane pid among the ancestors),
  `terminal`, `unknown`.

## Events

`workman orchestra-collect` appends to `~/orchestra/session-events.jsonl`
(`seq`, `ts`, `key`, `kind`, `session`, `account`, `title`, `front_end`,
`detail`, `epoch`, ...). State in `session-events.state.json`; a second
collector returns `busy` instead of double-appending; a lost state file is
rebuilt from the feed (seq and epoch) so nothing replays. Dedupe keys are
trimmed to the newest 20000, but a key whose condition is still live is never
trimmed, so a long-open question is never re-announced. Kinds:

| kind | fires once per |
|---|---|
| `question` | open AskUserQuestion (question text and option labels) |
| `permission` | registry `waiting` with no question; the hook feed's own prompt texts only ("Tool permission requested", "Claude needs your permission to use X"; not AskUserQuestion), one per session per 10 min |
| `blocked` | each classifier / permission-rule denial in the current turn, at once, even while the session keeps working |
| `limit_hit` | rate-limit API error |
| `silent` | silence inside a turn, only in [25, 115] min |
| `compact_ready` | context in 300k-400k at a clean turn (end_turn, quiet >= 60 s, no question) per 50k step |
| `finished` | a turn that ended within the last 30 min |

`session_events` runs one collector pass first (so it works without a timer),
then returns events with `seq > since_cursor` and the new `cursor`, which is
`<epoch>:<seq>`. The epoch changes when the feed is started afresh; a cursor
from another epoch, or a plain number past the end, has seen none of the
current feed, so the reply starts from its first event (`cursor_reset: true`,
paged by `limit` with `more`) and nothing is skipped. Example timer (not
installed by anything): `ops/orchestra-collect/`.

It consolidates `~/orchestra/watcher-20261008/watch.py` (questions, classifier
blocks, COMPACT-READY, stuck in a tool), `~/.claude/skills/orchestra/bin/watch.py`
(stale context after a compact boundary), `question.py` (tool_use without
tool_result) and `~/bin/agent-watch` (denial phrases, redaction).

## Front-end control

Guards, all checked before anything moves:

- **caller known**: the calling session's id comes from `CLAUDE_SESSION_ID`,
  `CLAUDE_CODE_SESSION_ID`, a Claude process in this process's ancestry that
  the live registry knows, or `self_id`. The MCP wrappers pass what the server
  knows. With none, every control action refuses: an unknown caller could be
  the target. Every id must be a session id (at least 8 hex characters);
  a name like `owner` is refused, never treated as "no caller".
- **not self**: the target is not the caller.
- the last **main-chain** assistant `stop_reason` is `end_turn`, quiet >= 60 s,
  no open question, and the live registry says neither `busy` nor `waiting`.
- no vlc/mpv; `/tmp/workman-frontend.lock` free (a dry run only probes it and
  never creates it).
- **band**: compaction only inside the policy band 300k-400k. `min_tokens` /
  `max_tokens` can narrow it, never widen it (clamped); only `force` skips it.
  A model switch re-reads the whole context uncached, so it is refused above
  400k (`max_tokens` can lower that; `force` skips it). Effort-only changes
  have no band.

Right before the first click or keystroke, every runner (desktop
`FrontCompact`, Remote Control `RemoteCompact`, the pickers) runs the same turn
check again (`orchestra_tools.turn_ready`: caller known and not the target,
main chain, end_turn, quiet >= 60 s, no question, registry neither busy nor
waiting), not just the transcript's last line. The resume line after a
compaction gets the same check (up to 3 min for it to pass); there the main
chain may also end in /compact's own echo, but a new prompt, a tool call or a
question means someone is using the session, and the resume is skipped and
logged, never typed. Escape is never pressed: a menu opened by mistake is
closed by pressing its opener again.

Runners started from a shell or by `spawn` (`python -m workman.front_compact`,
`orchestra_control rc-compact`) run the same guard chain first
(`orchestra.compact.runner_check`, logged as `tool: runner_check`), band
included. `--force` skips the band only with a `--reason`, which is logged.

| Front end | session_compact | session_set_model_effort |
|---|---|---|
| desktop | `front_compact` runner (PR #6, kept with its tests): deep link, header-hash guard (learn once with `python -m workman.front_compact <local_id> --learn`), `/compact`, new `compact_boundary`, resume line | deep link + header guard, then the composer-footer picker through AT-SPI; verified by the footer label or the desktop store |
| remote_control | detached runner: open `https://claude.ai/code/session_<id>` in Chrome on :0, abort unless the page shows the session title, click the composer, abort unless an editable field has focus, `/compact` + Return, new `compact_boundary` | same page guard, then the picker by its labels via `chrome.click_text`, verified by reading the page |
| headless | refused: no front end | refused, with the `run-claude-seat <seat> --model M --effort E --resume <id>` line |
| tmux / terminal / unknown | refused | refused |

Models: `opus`, `sonnet`, `haiku`, `fable` or a `claude-<family>-<ver>` id.
Fable only by the owner's explicit choice: `allow_fable=True` (`--allow-fable`);
without it the request is refused with that reason. Efforts:
`low|medium|high|xhigh|max`.
Logs: compaction in `~/orchestra/compact-log.jsonl` (the `front_compact` log,
same `{ts,target,result}` shape plus `tool`, `mode`), model/effort in
`~/orchestra/model-effort-log.jsonl`. Dry runs are logged too.

`session_front_compact` (the PR #6 low-level tool) is kept, but now runs
through `session_compact`'s guards (caller known and not the target, the turn
checks, the band, and the transcript must be that `local_id`'s session). The
runner CLI `python -m workman.front_compact` (in `workman/front_compact_cli.py`)
refuses when no caller session id is known.

## Known gaps

- Live front-end actions are **not proven**: no run-mode compaction or picker
  change was executed by the builder. The Remote Control URL
  (`WORKMAN_RC_URL_TEMPLATE`), the composer's accessible name
  (`WORKMAN_RC_COMPOSER`, default `Reply`), the claude.ai page showing the
  registry name as its title, and the desktop picker's AT-SPI roles/labels are
  assumptions that fail closed; the orchestrator proves them.
- Desktop compaction and picker need a learned header hash per session
  (`--learn` fronts the session once).
- `machine="mac-one"` etc. runs the CLI over ssh; the Macs need this branch
  deployed (today: `No module named workman.orchestra_tools`, see below).
- The `~/.claude` default dir's account is read from `~/.claude.json`; a CLI
  run with `CLAUDE_CONFIG_DIR=~/.claude` would use `~/.claude/.claude.json`.
- A desktop session's pid is not resolvable from the transcript (the app's
  spawned CLI has no session flag); `pid` is the registry's.

## Live proof

Read-only on the DGX, 2026-10-08, from the worktree
`~/wt/workman-orchestra-tools-20261008`. Emails masked, titles trimmed.
The collector proof is from a fresh feed (the builder's own earlier test feed
was removed before it).

### sessions_list  (`bin/workman orchestra-list --json`, exit 0, 2026-10-08T21:07:17Z)
```
count 31  states {"working": 3, "idle": 18, "limit_hit": 6, "needs_input": 2, "blocked": 2}
id       state       age   ctx    pct  model             effort front          rc/unit                     pid
017937e7 working         1s    116k 11.7 claude-opus-5-5   medium remote_control DGX Tariqul                 544598
84d7b920 working         1s    213k 21.4 claude-opus-5-5   medium remote_control DGX Tariqul                 544818
b0edf8af working         8s    107k 10.7 claude-opus-5-5   xhigh  remote_control DGX Tariqul                 3687775
5b70de27 idle           38s    180k 18.1 claude-opus-5-5   medium remote_control DGX Tariqul                 544330
a59dfb58 idle          122s    200k 20.1 claude-fable-5-1  high   headless       run-claude-seat-untracked-  261809
fd35ef91 limit_hit     274s      0k  0.0                          headless                                   None
3fef1a07 needs_input   392s    167k 16.7 claude-fable-5-1  high   remote_control DGX Tariqul                 3564079
1c447a76 limit_hit    1206s    287k 28.8 claude-fable-5-1  xhigh  remote_control DGX Support                 1370094
bf644709 needs_input  1716s    386k 38.7 claude-fable-5-1  xhigh  remote_control DGX Support                 731291
accounts: h***@advisorppc.com, s***@advisorppc.com, t***@gmail.com
```

### session_needs  (`bin/workman orchestra-needs`, exit 0)
```
count 10
3fef1a07 question   remote_control age   393s  Q: Two Codex Sol workers are running on the DGX aga... options=['Stop both, move to Macs (Recommended)', 'Let pricing-card finish, move agents-sdk', 'Leave both running']
bf644709 question   remote_control age  1717s  Q: Merge map PR #6 (Agents section, verified by me:... options=['Approve this class (Recommended)', 'Approve once', 'Hold']
40362483 blocked    remote_control age  7695s  Bash denied (classifier) [Production Reads]
dfde67ae blocked    remote_control age  9828s  Edit denied (classifier) [Credential Exploration]
fd35ef91 limit_hit  headless       age   275s  You've hit your session limit · resets 11:10pm (UTC)
1c447a76 limit_hit  remote_control age  1207s  You've hit your session limit · resets 11:10pm (UTC)
f895fdaa limit_hit  remote_control age  2020s  You've hit your weekly limit · resets Oct 11, 5am (UTC)
```

### session_usage  (`bin/workman orchestra-usage --window-h 24 --top 6`, exit 0)
```
sessions 116  totals {"input": 41710, "cache_read": 731685991, "cache_creation": 15242530, "output": 3166151, "total": 750136382, "images": 206, "turns": 536, "api_calls": 4296}
bdb54eed s***@advisorppc.com      total    69349272  cache_read    68560927  out   247446  img   0  turns  56  calls  236  repeat 1
95165be9 s***@advisorppc.com      total    60174453  cache_read    59677404  out   135488  img  12  turns  26  calls  240  repeat 1
d13b93d2 t***@gmail.com           total    54808936  cache_read    54335244  out    87414  img  42  turns  12  calls  201  repeat 1
b0edf8af t***@gmail.com           total    53171316  cache_read    52460125  out   152056  img   7  turns  17  calls  223  repeat 2
3fef1a07 t***@gmail.com           total    44399507  cache_read    43253100  out   330881  img   8  turns  56  calls  231  repeat 1
repeats (same first prompt, >= 2 sessions):
   2 sessions    63545399 tokens  Hi, how are you doing?
   4 sessions    45081293 tokens  Read /home/advisorppc/handoffs/map-refresh-20261008/BRIEF-
   4 sessions     5859180 tokens  READ-ONLY disk survey on the DGX (3.6T disk, 2.2T used, ho
  13 sessions     2829087 tokens  # Principal Architect — one run (Fable 5.1, high or xhigh 
by=account:
  s***@advisorppc.com      sessions  95  total   452379723  out  1947932  img  61  turns 372
  t***@gmail.com           sessions  19  total   297756659  out  1218219  img 145  turns 162
  h***@advisorppc.com      sessions   2  total           0  out        0  img   0  turns   2
```

### collector  (`bin/workman orchestra-collect` twice, both exit 0)
```
pass 1: ok True sessions 31 new 19 cursor 19 kinds {"finished": 1, "limit_hit": 6, "question": 2, "blocked": 6, "permission": 4}
pass 2: ok True sessions 31 new 0 cursor 19   (dedupe: nothing re-emitted)
  seq  1 finished    5b70de27 remote_control turn ended 1 min ago
  seq  2 limit_hit   fd35ef91 headless       You've hit your session limit · resets 11:10pm (UTC)
  seq  3 question    3fef1a07 remote_control Two Codex Sol workers are running on the DGX against your Ma
  seq  4 limit_hit   1c447a76 remote_control You've hit your session limit · resets 11:10pm (UTC)
  seq  5 question    bf644709 remote_control Merge map PR #6 (Agents section, verified by me: 45 tests gr
  seq  6 blocked     bf644709 remote_control Bash denied (classifier): [Merge Without Review]
session_events --since 15: count 4 cursor 19 seqs [16, 17, 18, 19]
session_events --since 20: count 0 cursor 19 cursor_clamped True
```

### session_compact dry runs  (`bin/workman orchestra-compact <id> [...]`, 21:09-21:11Z)
```
9ae83593 --min-tokens 100000  exit 0  front remote_control ctx    200k  ok True  failed guards []
    would: detached runner: wait for the turn to end (<= 30 min), defer while vlc/mpv run
    would: hold /tmp/workman-frontend.lock
    would: open the session's claude.ai/code page in Chrome on :0 (session_[bridge id])
    would: abort unless the page shows the session title ['advisorppc-b1']
    would: re-check idle, click the composer, abort unless an editable field has focus
    would: type /compact, press Return, wait <= 6 min for a new compact_boundary
    would: log every outcome to /home/advisorppc/orchestra/compact-log.jsonl
bf644709                      exit 1  front remote_control ctx    386k  ok False  failed guards ['clean_turn', 'no_question']
b0edf8af                      exit 1  front remote_control ctx    130k  ok False  failed guards ['not_self', 'clean_turn', 'quiet', 'not_busy', 'band']
f4a20b25                      exit 1  front headless       ctx    368k  ok False  failed guards []
    refused: headless `claude -p` session: there is no front end to type /compact into; it compacts on its own at the windo
f93ff046 --force              exit 1  front desktop        ctx    287k  ok False  failed guards ['header_learned']
```

### session_set_model_effort dry runs  (`bin/workman orchestra-set-model <id> ...`)
```
9ae83593 --model sonnet --effort medium exit 0  ok True  current {'model': 'claude-fable-5-1', 'effort': 'high'}  target {'model': 'claude-sonnet-5-5', 'effort': 'medium'}  failed []
    dry_run: every guard passed; nothing was touched
f93ff046 --effort medium                exit 1  ok False  current {'model': 'claude-fable-5-1', 'effort': 'high'}  target {'model': 'claude-fable-5-1', 'effort': 'medium'}  failed ['header_learned']
    header_learned: run `python -m workman.front_compact local_16ea9334-c00c-479b-8ee2-494e7330173b --learn` first
f4a20b25 --model opus --effort high     exit 1  ok False  current {'model': 'claude-opus-5-5', 'effort': 'xhigh'}  target {'model': 'claude-opus-5-5', 'effort': 'high'}  failed []
    headless `claude -p` session: no front end, the change applies only at the next resume: run-claude-seat support --model claude-opus-5-5 --effort high 
fable    9ae83593 --model fable         exit 1  ok False  current None  target None  failed []
    refused: never Fable for a worker      (head 3118c98; now: refused unless allow_fable=True, see below)
```

### machine="mac-one"  (`bin/workman orchestra-list --machine mac-one --json`, exit 1)
```
{"ok": false, "machine": "mac-one", "rc": 1, "error": "fleet-connect[mac-mini]: via direct cable 169.254.81.2 .../python3: No module named workman.orchestra_tools"}
```

### Recheck after the review fixes (2026-10-08 21:39Z, read-only dry runs)
```
orchestra-compact b0edf8af                       exit 1  failed: not_self, clean_turn, quiet, registry_ready (busy), band
env -u CLAUDE_CODE_SESSION_ID -u CLAUDE_SESSION_ID orchestra-compact b0edf8af
                                                 exit 1  failed: not_self (caller found through the process tree), ...
orchestra-compact 9ae83593 --min-tokens 0 --max-tokens 999999999
                                                 exit 1  band: ctx 200421 outside [300000, 400000] (clamped to policy)
orchestra-set-model 9ae83593 --model fable       exit 1  refused: Fable is picked only by explicit choice; pass allow_fable=True
orchestra-set-model 9ae83593 --model fable --allow-fable
                                                 exit 0  no change: already at the target
orchestra-collect                                exit 0  new 24, cursor 6ac80d927514:43 (epoch:seq), 0 repeats of the 19 earlier events
```
