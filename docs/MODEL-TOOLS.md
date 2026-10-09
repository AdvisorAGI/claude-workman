# Model tools: non-Claude CLI agents, images and video from Workman

Owner order 2026-10-08: the orchestrator drives the other CLI agents (Codex / ChatGPT
models, Grok CLI, Cursor CLI) and their image and video generation from Workman tools,
and none of that work counts against Claude usage.

Package `workman/model_tools/` (controller) with the target-side subpackage
`workman/model_tools/jobctl/`. MCP tools are registered in `workman/server.py` under
"NON-CLAUDE MODEL TOOLS". CLI: `python -m workman.model_tools ...` or `bin/workman-models ...`.

| module | concern |
|---|---|
| `model_tools/__init__.py` | public names the server block and callers use |
| `transport.py` | machines, job ids, the jobctl bundle over `python3 -I -` / ssh, job records, `_launch` |
| `agents.py` | `agent_run`, `agent_status` (+ reconcile), `agent_result`, `agent_cancel` |
| `media.py` | `image_generate`, `video_generate`, `media_quota`, the codex subject guard |
| `catalog.py` | `models_available` and its 10-minute cache |
| `cli.py`, `__main__.py` | command line |
| `jobctl/common.py` | paths, file helpers, worker env allow-list, run-model header parsing |
| `jobctl/procs.py` | process identity (`owned`) and signalling |
| `jobctl/sandbox.py` | the bwrap filesystem sandbox of codex image runs |
| `jobctl/launch.py` | preflight, job dir, systemd unit or detached session |
| `jobctl/supervise.py` | the per-job supervisor (run-model direct/role, codex image, grok) |
| `jobctl/status.py`, `cancel.py` | state, bounded result, cancel, `units` listing |
| `jobctl/catalog.py`, `grok_media.py`, `main.py` | models, grok Imagine, subcommand dispatch |

The target needs one program, so `jobctl.bundle()` concatenates the jobctl modules in
`MODULE_ORDER` and drops their intra-package imports (each marked `# jobctl:local`); the
modules still import as a normal package for tests. A test compiles the bundle under the
Python 3.8 grammar (the Macs run 3.9.6) and runs it standalone.

## Why it costs no Claude usage

Every agent runs through `~/bin/run-model` (direct mode, or one of its router roles) as its
own process, detached from the calling session, exactly as the lead's own shell would start
it. Every tool returns at once with a job id (`image_generate` and `video_generate` too,
unless the caller passes `wait_s` > 0). Model output never streams into the caller:
`agent_status` returns a small state record and `agent_result` returns at most `max_lines`
lines and 4000 characters. Claude pays for the launch call and the reads, nothing else. No
Claude subagent drives a CLI (that is the forbidden wrapper); the MCP server launches a
detached process, which is the sanctioned path.

Money rules carried in code, on the target, not only in the caller:

- Subscription CLIs only. Nothing is read from `~/ENV`. The worker environment is an
  allow-list (`jobctl/common.py` `ENV_ALLOW`: PATH, HOME, USER, LOGNAME, SHELL, LANG,
  LANGUAGE, TERM, TZ, TMPDIR, XDG_*_HOME, XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS,
  SSH_AUTH_SOCK, WORK_TASK_ID, WORK_ROLE, CODEX_HOME, MODEL_ROUTER, plus the prefixes LC_,
  RUN_MODEL_, MODEL_ROUTER_, SESSION_ESCAPE_, WM_JOBCTL_, WORKMAN_GROK_). A prefixed name
  that contains KEY, TOKEN, SECRET, PASSW, CREDENTIAL, COOKIE, CLAUDE or ANTHROPIC, in any
  letter case, is still dropped. So `OPENAI_API_KEY`, `openai_api_key`, `OPENAI_ADMIN_KEY`,
  `REPLICATE_API_TOKEN`, `BASH_ENV`, `ENV`, `NODE_OPTIONS`, `PYTHONSTARTUP`, `PYTHONPATH`
  and `LD_PRELOAD` never reach a worker. Loader and injection variables (`BASH_ENV`, `ENV`,
  `NODE_OPTIONS`, `NODE_PATH`, `PYTHON*`, `LD_*`, `DYLD_*`, `GIT_*`, `PERL5LIB`, `RUBYOPT`, ...)
  are refused unconditionally, even when the test-only `WM_JOBCTL_KEEP_ENV` names them. The
  same scrub applies on the Mac (nohup) path. The controller runs jobctl with `python3 -I`,
  and the grok interpreter with `-E`.
- Codex image jobs do not go through run-model. run-model cannot pass a sandbox flag, and
  the codex config here is `danger-full-access`. The supervisor runs
  `codex exec --skip-git-repo-check -s read-only -m <NEWEST_SOL> -c model_reasoning_effort=..
  -C <job>/work -o <job>/final.txt -` (prompt on stdin) inside an OS-level filesystem sandbox
  (`jobctl/sandbox.py`, bubblewrap). The prompt is not the boundary.
  - **Filesystem:** `/` is read-only, and `$HOME`, `/tmp`, `/var/tmp` and `/run/user/<uid>`
    are empty tmpfs. So `~/ENV`, `~/.ssh`, `~/.config/gh`, `~/.claude*`, every repo and note,
    the session bus and the keyring socket are unreachable, by `~` or by absolute path.
  - **Codex home:** the child gets a fresh CODEX_HOME at `/tmp/wm-sbx/codex-home`, built
    from an allow-list. `~/.codex` itself is never bound, so its history, sessions, sqlite
    DBs, `AGENTS.md`, backups, symlinks and the real `config.toml` do not exist in there.
    - `auth.json` is the real login file, bound alone and read-write, so a token refresh
      lands in the real file. It is resolved first and refused if it is not a regular file
      directly inside `~/.codex`.
    - `config.toml` is a minimal private config: read-only sandbox, no MCP servers, and the
      features that reach outside the job off (apps/connectors, browser_use including full
      CDP access to the logged-in Chrome, computer_use).
    - `generated_images` is `<job>/generated`.
  - **Executables:** only the exact `@openai/codex` package dir (found by its `package.json`
    name) is bound, read-only, to `/tmp/wm-sbx/codex`. The node binary file alone goes to
    `/tmp/wm-sbx/bin/node`. A codex that is not a package is bound as the single file.
    Anything that is, or holds, the home dir is refused, so `~/.local` and `~` are never
    re-mounted.
  - **Job dir:** it is bound by its real path only after checking it is the plain directory
    launch created under the jobs root, and the same for `work/` and `generated/`. A symlink
    to anything is refused.
  - **Pid namespace:** the sandbox has its own, so killing bwrap ends every process inside.
  - **Environment:** HOME is the empty work dir. PATH is `/tmp/wm-sbx/bin:/usr/local/bin:/usr/bin:/bin`
    (never `~/bin` or `~/.local/bin`). LANG/LC_*, TERM, TZ, USER, LOGNAME, WORK_TASK_ID and
    CODEX_HOME pass. Nothing else.
  - **Network:** it stays shared, because codex has to reach OpenAI. Shell commands the model
    runs are network-blocked by codex's own read-only sandbox, and the outward features are
    off.
  - **Why bwrap:** systemd `--user` units on this host silently ignore `ProtectHome=` and
    `InaccessiblePaths=` (probed: every path stayed visible).
  - **Where it runs:** a machine without a working bwrap (the Macs) refuses codex image jobs
    with `sandbox_unavailable`.

  The prompt fences the subject in `<subject>...</subject>` and forbids shell commands, file
  access and the imagegen API-key fallback. A subject with `/ \ ~ $` backtick `.. ; | && < >`,
  or one of the words env, cat, secret(s), key(s), token(s), password(s), credential(s) or
  apikey (whole tokens, any case), is refused (`unsafe_subject`). That is a second line; the
  sandbox is the boundary. Each run writes one run-model-shaped line to `RUNS.jsonl`
  (`"via": "workman-image"`).
- `agent_run` never picks a model; run-model does. No Claude model can be started.

## Tools

| tool | what it does |
|---|---|
| `agent_run(cli, prompt, effort="high", cwd="", machine="mac-one", role="", out="", timeout_s=2700)` | Launch. `cli`: codex, grok, cursor, astra, luna, terra (run-model direct mode, `effort` max/xhigh/high/medium/low). Or `role`: a run-model router role (cheap, mid, review, review-deep, build-sol, review-grok, quick, build, ...); then the router picks the lane and effort. `cwd` and `out` must be absolute (or `~/`). Returns job id, unit, pid, log path. |
| `agent_status(job="", limit=20, reconcile=False)` | One job, or the last `limit` jobs started from this machine: state, model, effort, machine, host, age, runtime, exit code, notes (not logged in, usage limit). `reconcile=True` adds the `run-model-wm-*` units that have no job record (orphans), other run-model units, and launches never confirmed. |
| `agent_result(job, max_lines=40)` | Bounded result. Order of preference: the report file run-model found in the prompt, the role router's answer, the CLI's final message (codex `-o` file, or the text after `tokens used`), then the log tail. Hard cap 4000 characters, 200 lines. |
| `agent_cancel(job)` | Stops only what is provably the job's: exactly `run-model-<job>.service` via `systemctl --user stop`, and a TERM to the supervisor only when `owned()` proves the pid is the job's (alive, and its start time equals the one recorded at spawn; a command line alone is no proof). The worker tree goes down with its process group, and the run-model comeback file is parked in `active/stopped/` so `run-comeback` does not relaunch it. A `lost` job is never signalled. A unit or pid that is not the job's is refused (`not_owned`). |
| `image_generate(prompt, provider="codex", out_dir="", n=1, size="", machine="", wait_s=0)` | Returns a job id at once; file paths (never bytes) via `agent_result`. See below. |
| `video_generate(prompt, aspect_ratio="9:16", duration="15s", refs=[], provider="grok", resolution="", audio="on", out_dir="", wait_s=0)` | grok Imagine video. Returns a job id at once unless `wait_s` > 0. |
| `media_quota(machine="")` | Read-only grok Imagine quota (image and video buckets). |
| `models_available(machine="all", refresh=False)` | Per machine: codex version and login, grok models and login, cursor-agent models, run-model direct CLIs, role table, router presence, `models.env` newest ids. Cached 10 minutes (memory and `~/.cache/workman/model-tools/`). |

States: `starting`, `running`, `finished` (exit 0), `failed`, `timed_out`, `cancelled`,
`lost` (the supervisor died without writing an exit code, for example a reboot),
`launch_failed` (systemd-run failed and nothing started), `unknown` (no such job on that
machine), and `unreachable` (list view only: ssh failed).

## How a job runs

1. **Record first.** The controller writes the job record in
   `~/orchestra/runs/workman-jobs/<job>.json` with `launch: pending`. Then it pipes the jobctl
   bundle to `python3 -I -` on the target: locally, or `ssh <alias> python3 -I -` through the
   fleet aliases (`dgx`, `mac-one`, `mac-air`, all via `~/.ssh/fleet-connect.sh`).
   - A definite refusal from the target removes the record.
   - If the outcome is open (a timeout, no reply, or a failed systemd-run), the record stays
     as `launch: unconfirmed`. The reply carries the job id and `unit_if_systemd`, so
     `agent_status(job)`, `agent_status(reconcile=True)` and `agent_cancel(job)` can still
     find the job.
   - If ssh fails, the reply is `error: unreachable, layer: ssh` with fleet-connect's own stderr.
2. **Checks on the target.** `launch` checks that run-model is there and supports the CLI.
   It parses the `case "${1:-}" in ...) direct` line, so a Mac copy without luna/terra
   refuses them. For role mode it checks that `model-router` exists; for image jobs, that
   `codex` exists.
3. **Job dir and supervisor.** `launch` writes `~/orchestra/runs/workman/<job>/` (`job.json`
   naming the unit, `prompt.txt`, a runnable `jobctl.py`) and starts the supervisor:
   - Linux: `systemd-run --user --collect --service-type=exec --property=ExitType=cgroup
     --unit=run-model-<job> --property=RuntimeMaxSec=<timeout + 690>`. This is the same escape
     run-model's `session-escape.sh` uses, so the job outlives the Claude session. Inside
     that unit run-model runs in place (`SESSION_ESCAPE_DONE=1`) and records the unit in
     `RUNS.jsonl`. If systemd-run fails, the job is `launch_failed`. Nothing else is started:
     there is never a nohup fallback after a systemd attempt, because the unit may exist anyway.
   - Mac: a detached session (`start_new_session`) with the scrubbed environment.
4. **The run.** The supervisor runs `run-model <cli> --effort .. --cwd .. --out .. --
   "<prompt>"` with `RUN_MODEL_TIMEOUT=timeout_s` and `WORK_TASK_ID=<job>`, so run-model's
   RUNS.jsonl line carries the job id. For role mode it runs
   `run-model <role> <cwd> <prompt.txt> <job>`, then waits for the router pid it reads from
   the role's `.meta` file. That pid is adopted (its start time recorded in `role.json`)
   only while its command line names the job. An unadopted pid is waited for but never
   signalled.
5. **Bookkeeping.** The supervisor writes `pid`, `pid_start`, `child_pid`, `child_start`,
   `started`, `ended`, `rc`, and the `timed_out` / `cancelled` markers. It never reaps its
   child before it has finished signalling it (`waitid(WNOWAIT)`), so the child's pid and
   process group cannot be someone else's while it kills them. Any other signal goes
   through `owned()`, which needs the recorded start time. A process group is only signalled
   when its leader is the owned pid. A backstop stops the tree 90 s after `timeout_s` if
   run-model's own watchdog did not.
6. **Job ids.** The job id names its machine (`wm-<UTC>-<dgx|m1|air>-<cli>-<hex>`), so any
   controller can query any job.

`cwd` defaults to the job directory itself, a scratch dir outside any git tree. run-model's
own worker contract still applies when `cwd` is a repo: a successful write run is committed
and pushed by `~/bin/work-commit` unless the prompt says read-only. Media jobs never commit.

Heat: `agent_run` defaults to `machine="mac-one"` (owner order 17:14Z: builds and suites off
the DGX). Image jobs default to the local machine because the generation happens in the
cloud; grok jobs run on the DGX because that is where `~/grok-web-mcp` and its Chrome are.

### Role mode per machine (read live 2026-10-08)

| machine | run-model direct CLIs | role mode | router writes |
|---|---|---|---|
| dgx | cursor grok codex astra luna terra | yes, `model_router.py` | `<out>.lane` on success, `<out>.route` always |
| mac-one | cursor grok codex astra | yes, bash `model-router` | `model-router: done seat=.. exit=N` in `<name>.err`; the whole codex transcript in `.out` |
| mac-air | cursor grok codex astra | **no router: role mode is refused** (`role_mode_unavailable`) | |

The router is not the supervisor's child, so its exit code is read back from those files
(`jobctl/supervise.py` `role_exit`). For the Mac router the result is the codex final
message cut out of the copied transcript (`source: role_final_message`).

## Images

- `provider="codex"`: Codex CLI's built-in `image_gen` tool (feature `image_generation`,
  stable, on the ChatGPT plan login; `codex login status` = "Logged in using ChatGPT"), run
  read-only in a throwaway dir as described above. image_gen itself writes into
  `$CODEX_HOME/generated_images/<session id>/`; the supervisor copies those files into
  `out_dir` under `<slug>-1.png` ... and never overwrites an existing file (it uses `-v2`
  instead). A run that ends with no image is `failed` with exit 3. Files are checked by magic
  bytes. `n` is 1..4. `size` is passed as text; the tool picks the real pixel size (asked
  1024x1024, got 1254x1254). `out_dir` defaults to `<job dir>/media`.
- `provider="grok"`: grok Imagine through `~/grok-web-mcp/server.py` on the DGX.
  - Its functions are imported and called with `/usr/bin/python3 -E`, which has
    `websocket-client` in its user site. No browser automation is reimplemented.
  - The caller reads `imagine_quota` first. It refuses with `quota_exhausted`, or with
    `quota_unknown` when the reply cannot be read (fail closed).
  - The job checks quota again just before generating, and holds a lock file
    (`~/grok-web-mcp/.workman-grok.lock`) so two jobs never drive the one Imagine tab.
  - `n` in {2, 4, 8, 12} and `size` as an aspect ratio set the UI's sticky controls through
    `imagine_set_image_controls`. `n=1` leaves the sticky Image Count unchanged, so more
    than one image can come back.
  - Downloads are copied from `~/grok-web-mcp/out/` into `out_dir`. `imagine_image` names
    files with only the first 8 characters of the post id, so images return
    `post_id_prefixes`; videos return full `post_ids`.

## Video

grok Imagine only: `imagine_video` (aspect 9:16, 16:9, 1:1, 2:3, 3:2 or Auto; 6s, 10s or
15s; resolution 480p/720p/1080p; audio on/off), or `imagine_video_with_refs` when `refs`
(image paths on the DGX) are given; with refs grok caps the clip at 720p/15s and ignores the
aspect. Quota (`video` bucket) is checked first. A video takes minutes; `agent_result(job)`
gives the mp4 path and post id.

**Codex CLI has no video generation.** `codex features list` (codex-cli 0.161.0) shows
`image_generation` and no video feature, so `video_generate(provider="codex")` is refused
with `unsupported`. Nothing is faked.

## Protocol and SDK

- MCP specification: the current published revision is **2026-07-28**
  (modelcontextprotocol.io/specification/versioning, read 2026-10-08).
- The Workman server runs on the official Python SDK `mcp` **1.29.1**, `mcp.server.fastmcp`
  (FastMCP). Its `LATEST_PROTOCOL_VERSION` is **2025-11-25**; it supports 2024-11-05,
  2025-03-26, 2025-06-18 and 2025-11-25, so a 2026-07-28 client falls back to 2025-11-25
  through the spec's backward-compatibility path. The newest SDK on PyPI is 2.3.0.
  `pyproject.toml` pins `mcp>=1.2.0,<2` because 2.0 removed `mcp.server.fastmcp`. Moving the
  whole server to 2.x (`mcp.server.mcpserver`) is a server-wide port outside this PR.
- These tools add no protocol framing of their own. They are plain FastMCP tools with no UI
  resource, so the MCP Apps extension does not apply.

## Known gaps

- **Cursor on the Macs:** `cursor-agent` over ssh fails on both Macs. `models_available`
  read live "Your macOS login keychain is locked. Run security unlock-keychain and try
  again." A `cli="cursor"` job on a Mac fails the same way until the keychain is unlocked in
  that login context, which is an owner action and not done here. Cursor works on the DGX.
- **Mac Air role mode:** Mac Air has no `model-router`, so role mode is refused there; use
  `cli` + `effort`.
- **Mac run-model copies:** they have no luna/terra direct mode, no `--dry-run` and no
  manifest file.
- **Sandboxed image path not run with a model:** the sandboxed codex image path has not
  generated an image live. It was proven with zero-model probes (below) and with tests using a
  fake codex. The live image proof ran the earlier path at `3e75ebf`.
- **auth.json refresh:** `auth.json` is bound as a single file. That is safe as long as codex
  rewrites it in place, which is what codex-cli 0.161 is believed to do (not verified: no token
  refresh can be forced without a model run). If a future codex replaced the file by rename,
  a refresh inside an image job would fail to persist the rotated token.
- **Network left open:** the image sandbox keeps the network (codex must reach OpenAI).
  Localhost services stay reachable in principle. Shell commands are blocked from the network
  by codex's own read-only sandbox (not re-verified nested inside bwrap), and the
  outward-reaching codex features are off.
- **Image jobs need Linux:** they are Linux-only (bwrap). The Macs refuse them.
- **grok generation not run live:** grok image and video generation are covered by tests with
  a fake grok-web server only.
- **Status listing:** `agent_status()` without a job lists jobs started from this
  controller's registry. Jobs started from another machine's Workman are reachable by id
  (the id names the machine) but are not listed. Reconcile only reads units on Linux (here
  and the DGX); a Mac supervisor orphaned by a lost launch reply is not listed.
- **Role-mode effort:** run-model role mode ignores `effort`; `agent_status` reports the
  router's real effort when the codex header shows it, else `router`.

## Tests

| file | covers |
|---|---|
| `tests/model_fakes.py` | shared fakes and the `fleet` fixture |
| `tests/test_model_agents.py` | agent_run validation, direct/role runs, env scrub, cancel, timeout, backstop, lost, ownership refusals, result caps, record-before-launch |
| `tests/test_model_transport.py` | ssh path, unreachable host, systemd unit, systemctl cancel, no nohup after a failed systemd-run, reconcile, job ids |
| `tests/test_model_sandbox.py` | real-bwrap stand-ins: home secrets unreachable by any path; a symlinked `config.toml` and a symlink to a secret inside `~/.codex` never reach the child; an `auth.json` symlink pointing outside is refused; a codex package and node under `~/.local` are bound alone to `/tmp/wm-sbx` (nothing else of `~/.local` visible); a package that holds the home dir is refused; a symlinked job or work dir is refused |
| `tests/test_model_media.py` | sandboxed codex image path through real bwrap (planted secrets hidden, stand-in history and config, auth shared), refusal without a sandbox, naming, no-image failure, unsafe subjects, grok quota refusals, grok image/video calls |
| `tests/test_model_catalog.py`, `test_model_cli.py` | models cache, MCP registration and delegation, CLI |
| `tests/test_model_jobctl.py` | pure helpers, env allow-list, loader variables refused even when kept, image env (HOME isolation, PATH), a real-bwrap stand-in that cannot read planted secrets, `owned`/`adopt` by start time, the bundle (3.8 grammar, standalone run), the 600-line file and 60-line function caps |

All tests use fakes: a fake `~/bin/run-model` (direct and role mode), fake `model-router`,
`systemd-run`, `systemctl`, `ssh` (runs the command locally; `FAKE_SSH_DOWN` makes a host
unreachable), fake `codex` (`exec` included), `grok`, `cursor-agent`, and a fake
`grok-web-mcp/server.py`. HOME is a temp dir, and the fixture plants `OPENAI_API_KEY`,
`openai_api_key`, `OPENAI_ADMIN_KEY`, `REPLICATE_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
`NODE_OPTIONS`, `PYTHONSTARTUP`, `BASH_ENV` and `ENV` (the loaders also listed in
`WM_JOBCTL_KEEP_ENV`), so the scrub is tested end to end; the fake codex records HOME,
CODEX_HOME and every variable name it receives. No
test starts a real model, unit, ssh session or browser.

## Image sandbox probe (2026-10-09, DGX, zero model calls)

All checks ran inside the exact sandbox the supervisor builds (`sandbox.image_sandbox`, at the
allow-list version).

**Binds:** the binds were exactly:
- `~/.local/lib/node_modules/@openai/codex` to `/tmp/wm-sbx/codex`;
- `~/.local/node/current/bin/node` to `/tmp/wm-sbx/bin/node`;
- `~/.codex/auth.json` to `/tmp/wm-sbx/codex-home/auth.json`;
- the job's `codex-config.toml` and `generated/`;
- the job dir.

**Stand-in shell (`/bin/sh`):**
- Hidden: `~/ENV`, `~/.ssh`, `~/.config/gh`, `~/.claude`, `~/.claude-tariqul`, `~/.local`,
  `~/.local/bin`, `~/.local/share`, `~/.codex`, `~/.codex/history.jsonl`, `~/workman`,
  `~/orchestra/watcher-20261008` and `/run/user/1000/bus`.
- `cat /home/advisorppc/ENV/*` by absolute path failed.
- The child's codex home held only `auth.json config.toml generated_images`. The config
  was the minimal one (read-only, 0 mcp_servers), and `auth.json` was non-empty.
- `/tmp` held only `wm-sbx`.

**Codex itself:**
- `codex login status` (via `/tmp/wm-sbx/codex/bin/codex.js`): "Logged in using ChatGPT".
- `codex features list`: apps, browser_use, browser_use_full_cdp_access and computer_use
  false; image_generation true.
- `codex exec` with `--unshare-net` (no network, so no model call possible) got through
  startup (`workdir`, `model: gpt-6.1-sol`, `sandbox: read-only`, a session id).

**Afterwards:** killing bwrap alone left no process inside, the real `~/.codex/history.jsonl`,
`config.toml` and `auth.json` were unchanged, and the probe dir was removed.

**Earlier probe rounds:**
- The first version bound the whole `~/.codex` with stand-ins over each entry. Review found it
  let top-level symlinks through and could re-mount `~/.local` read-only through an install
  root, so it was replaced by the allow-list.
- The first exec probe without `--unshare-pid` left the native codex grandchild running, which
  led to that flag.

## Live proof (2026-10-08, DGX controller, 3 real runs, at commit 3e75ebf)

These runs predate the FIX-FIRST round. The agent path and the quota read are unchanged
since then. The codex image path has since moved to `codex exec -s read-only` (see Known
gaps).

Before any model run, the real `systemd-run --user` path was proven with a stand-in
run-model script (no model): unit `run-model-wm-...service` in
`/user.slice/user-1000.slice/user@1000.service/app.slice/` (outside the Claude cgroup),
`OPENAI_API_KEY` absent in the worker, finished exit 0; a second stand-in job cancelled
through real `systemctl --user stop` came back `cancelled`, exit 143. Those two preflight
records were deleted afterwards.

### 1. `agent_run(cli="codex", role="cheap")` on mac-one, read-only one-liner

```
$ python -m workman.model_tools run codex --role cheap --machine mac-one --timeout 600 \
    "Read-only: do not run commands and do not create or edit files. Reply with exactly one line: the word PONG, a space, then the sum of 17 and 25."
{"ok": true, "job": "wm-20261008T211207Z-m1-codex-e74c3b", "machine": "mac-one", "host": "room1-tariqul",
 "mode": "role", "role": "cheap", "how": "nohup", "pid": 87379, ...}
$ python -m workman.model_tools status wm-20261008T211207Z-m1-codex-e74c3b      # at 5 s
{"state": "running", "exit": null, "age_s": 5}
$ python -m workman.model_tools status wm-20261008T211207Z-m1-codex-e74c3b
{"state": "finished", "exit": 0, "model": "luna", "effort": "low", "runtime_s": 7, "machine": "mac-one",
 "out": "/Users/tariqul/handoffs/model-runs/2026-10-09/wm-20261008T211207Z-m1-codex-e74c3b.out", ...}
$ python -m workman.model_tools result wm-20261008T211207Z-m1-codex-e74c3b
{"state": "finished", "exit": 0, "source": "role_final_message", "text": "PONG 42", "truncated": false}
```

The copied transcript shows `model: gpt-6-luna`, `sandbox: read-only`, `reasoning effort: low`.

### 2. `image_generate(provider="codex")` on the DGX (systemd unit)

```
$ python -m workman.model_tools image --provider codex --machine dgx --size 1024x1024 \
    --out-dir ~/orchestra/watcher-20261008/model-tools-proof --wait 420 \
    "a plain flat red circle centred on a white background, simple test image"
{"ok": true, "job": "wm-20261008T211314Z-dgx-image-c59111", "provider": "codex", "how": "systemd",
 "unit": "run-model-wm-20261008T211314Z-dgx-image-c59111.service", "state": "finished",
 "files": [".../model-tools-proof/a-plain-flat-red-circle-centred-1.png"],
 "said": "a-plain-flat-red-circle-centred-1.png \u2014 A plain red circle centred on a white background."}
real 0m33.955s
```

File check: `PNG image data, 1254 x 1254, 8-bit/color RGB`; centre pixel (251, 1, 1), corner
(254, 253, 254). The codex log shows `model: gpt-6.1-sol`, `reasoning effort: low`, and the
model copying from `~/.codex/generated_images/<session>/exec-....png`. RUNS.jsonl:
`task=wm-20261008T211314Z-dgx-image-c59111 model_id=gpt-6.1-sol effort=low exit=0 seconds=33
unit=run-model-wm-20261008T211314Z-dgx-image-c59111.service`.

### 3. `media_quota()` (grok `imagine_quota`, read-only)

```
$ python -m workman.model_tools quota --machine dgx
{"ok": true, "raw": "{\"status\": 200, \"json\": {\"image\": {\"available\": true, \"windowSizeSeconds\": 64800},
  \"imagePro\": {...true}, \"imageEdit\": {...true}, \"video\": {...true}, \"video720p\": {...true}}, \"text\": null}",
 "image": {"ok": true, "bucket": "image", "available": true},
 "video": {"ok": true, "bucket": "video", "available": true}}
real 0m1.225s
```

The quota showed capacity, but no grok generation was run: it would have been a fourth real
run.

### `models_available("all")` (read-only, no model call)

```
dgx     amg-dgx                      codex-cli 0.161.0 / Logged in using ChatGPT | grok logged in, default grok-4.7, 7 models
        | cursor 120 models | run-model direct cursor grok codex astra luna terra, 15 roles, router yes
mac-one room1-tariqul                codex-cli 0.161.0 / Logged in using ChatGPT | grok logged in, 4 models
        | cursor: keychain locked | run-model direct cursor grok codex astra, 15 roles, router yes
mac-air Muhammads-MacBook-Air-13846  codex-cli 0.161.0 / Logged in using ChatGPT | grok logged in, 4 models
        | cursor: keychain locked | run-model direct cursor grok codex astra, 15 roles, router no
real 0m3.270s
```
