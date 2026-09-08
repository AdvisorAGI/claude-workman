# Workman Fleet v1.1

A personal Codex plugin with one explicit device entry point, local outcome
learning and a DGX graph memory. Fleet setup v1.1 is separate from desktop
claude-workman 0.3.0 and macOS helper 0.2.0.

## Use

MCP tools: `fleet_control`, `fleet_recall`, `fleet_report`, `fleet_memory`,
`fleet_verify`, `fleet_onboard`, `fleet_test_mode`, `fleet_observe`.
Supported nodes: `dgx`, `mini`, `air`, `machome`.
Read the bundled fleet-control skill before desktop work.

The equivalent CLI is `scripts/fleet-wm NODE ACTION`; supply action arguments
as JSON on stdin. `shot --output /absolute/new-file.png` writes a private
capture. `all report` collects outstanding journal rows. No broadcast input.

Call `active` and `pointer` before a task and keep their original restoration
values in the task, not in the learning store. On macOS use the original app
name as `restore_focus`; on X11 use its window ID. At the end, call `finish`
with `restore_focus`, `restore_x`, `restore_y`. It attempts restoration and
releases this session's lease even if restoration is disabled or fails; inspect
its `restoration` results. Do not restore over another user's active work.

## Mouse and keyboard switches

`input {"mouse":false,"keyboard":false}` stops new automation on that device.
Omit either key to leave that switch unchanged; omit both to read the state.
An off setting persists across sessions and restarts. Only re-enable it when
the user requests it. `panel` opens the local STOP BOTH / individual-toggle
window. Closing the panel leaves the chosen settings in effect. The Mac Desktop “Workman Input Controls.command” and DGX application launcher can reopen it. The panel observes only these two Workman booleans.

Your physical mouse and keyboard always remain usable; Workman never grabs or
disables them. Already posted atomic input can finish. Typing checks STOP and
focus every four characters. There is no persistent button-down or key-down
fleet API. Drag uses paired input with release on recoverable motion failures.
A process/OS crash or a legacy tool remains outside those guarantees.

Different devices can run in parallel. Independent sessions on one physical
desktop share a 120-second input lease (reserve explicitly for 15–300 seconds).
Pass stable, non-sensitive `context {project,task,session}` from clients that
have their own identity. `release` gives up ownership without moving focus or
pointer. `finish` restores then releases. Busy input never triggers takeover.
These protections cover Workman Fleet v1.1 clients. Preserved legacy direct
MCP/CLI tools do not honor its switches or leases; use the fleet entry point in
all participating sessions, and coordinate older sessions manually.

## Local learning and graph

Only explicit Workman task outcomes enter the existing
`~/.grok/workman-learn/journal.jsonl`, marked `workman_fleet_v1.1`.
The existing `~/fleet-report.py --workman-json` exports that allowlisted schema
over existing SSH. Other reporter modes remain unchanged. No reporter token,
HTTP listener, model API, transcript scan or new scheduler is used by this path.
DGX receipt is separate from action success. STOP returns before delivery to avoid waiting for reporter I/O; the next fleet call/report collects its durable local event.
`fleet_report` retries delivery, never the desktop action.
Journal files rotate into private append-only segments after 4 MiB. Segments are
retained and read as one evidence family, so rotation bounds individual files
without deleting history. API batches remain limited to 500 events.

DGX stores per-device evidence under `~/autonomy/learnings/by-device`, lessons
under `knowledge/WORKMAN-FLEET-V1.1.md`, and a SQLite graph projection in
`workman-v1.1-graph.sqlite3`. Existing journal rows are preserved and only the
new schema is exported. Stable entity IDs link devices, projects, sessions,
tasks, decisions, outcomes, permission observations and lessons. Event identity
is immutable; conflicting deliveries are rejected. Permission changes retain
supersession/conflict edges. Stale (over 300 seconds), future-dated and
conflicting permissions return no usable value. Memory never grants permission.

Every fleet action recalls lessons and relevant graph entities first. A backend
success alone is not a verified lesson: view a later screenshot and then call
`fleet_verify` with its event ID and the action event ID. Later independent
`fleet_memory`/`fleet_recall` calls can retrieve the linked evidence.
No typed content, key chords, window titles, raw screens, transcripts, passwords,
tokens or authentication codes are retained in this memory path.

## Installation and maintenance

`.codex-plugin/plugin.json` and `.mcp.json` package the plugin. The latter uses
`mcpServers` and `${PLUGIN_ROOT}` with an executable stdio launcher. The default
hub runtime is `/home/monzurul/workman/.venv/bin/python`; override explicitly
with `WORKMAN_FLEET_PYTHON` for a relocated hub. Paths in `fleet.json` identify
inspected working runtimes and sources. Virtual environments are not portable.

`install_support.py` deploys the small adapter/reporter extension with verified
per-file backups, leaving runtimes, services and privacy settings alone.
`patch_helper.py` and `patch_ops.py` accept only the inspected helper source
patterns and preserve verified backups. They fix Cocoa activation notifications,
request actual application focus through the already granted Accessibility API,
and ensure drag release on motion failure. Restart only the existing helper
service after such a patch. A human must grant missing macOS privacy access.

Current verification, device gates and exact rollback paths are documented in
`/home/monzurul/workman/docs/fleet-v1.1/RESULTS.md` and `ROLLBACK.md`. The v1.0
checkpoint is preserved. No push or publication has been performed.

## Permission onboarding and input presets

`fleet_onboard(node)` inventories enabled view/input features together, with
reasons, live state and exact Settings paths. `begin` opens only a missing grant;
`wait` observes changes with bounded backoff (at most 60 seconds). `retry` is
explicit; `cancel` stops setup. Denial/unanswered prompts, revocation, helper
relaunch and timeout remain distinct from readiness. A human approves each OS
prompt. Browser permissions are not requested while the companion is disabled.
An optional `permission_hint` uses a just-observed screenshot target, stays
outside its control, is click-through/static and expires within 20 seconds.
Without capture, use the numbered plain-text requirements. No TCC writes.

`inspect` batches status, fresh active identity and pointer without capture.
`move`/`click` accept `motion:direct|human` and `speed:0.25..4`; direct is default.
Documented choices are direct at 1.0, careful human motion at 0.7, smooth at
1.0, and responsive human motion at 1.5. These are per-call choices. Successful
and failed move/click events retain only the requested motion and bounded speed;
when the backend supplies them, sanitized step count, duration and humanized
outcome also reach the journal and DGX graph. Coordinates are never retained.
The `paste` action requires `preset:first_party_fast`, an explicitly authorized
`surface` and fresh `expect_focus`. It uses the existing clipboard backend for
ordinary nonsecret multiline text. Prior clipboard contents are replaced and
never read. Exact readback is required separately. STOP/lease/focus guards still
apply; a posted paste is atomic. This is not the coding model's Fast mode.
`wait_window` is a bounded window-presence check, not page-load proof.
See the project `FAST_MODE.md` for scope, measurements and regression fixtures.

`correct {verification_id}` preserves and retracts mistaken verification.
`lesson {kind,verification_id}` attaches reviewed device-specific recovery
knowledge to verified evidence; correcting that evidence retracts its lessons.

## Isolated style-provider tests

`fleet_test_mode` is a declarative skill/plugin/MCP-resource registry scoped only
to explicit Workman test IDs. Default off; one provider per test. Raw Caveman
v2.6.0 is byte-pinned with MIT attribution and returned only on an explicit test
payload request. The resource `workman://tests/{test_id}/raw-style` serves that
same exact Markdown through the existing stdio MCP server. No hooks, proxy,
telemetry, model calls, global style setting or doctrine changes are installed.
Registering an MCP reference does not launch/connect its server. See the
`test-providers` schema and project `CAVEMAN_TEST.md`. No raw-Caveman model-token
or cost improvement is claimed before matched live measurement.

## Compact observations

`fleet_observe` is a read-only projection of `inspect`. Its task-scoped
`profile_action` supports status, set, on, off and health. New task IDs default
OFF. OFF makes `view:auto` return the full observation; ON restores the last
Low-Max level and makes automatic reads compact. The setting uses an atomic
private file, a per-task lock and 32 rotating revision slots, so parallel
sessions see changes without restart. It stores no observation payload.
A nonsecret
`query` filters active-application windows only. It preserves candidate counts,
identity, ambiguity, focus, permissions, input switches, lease and report receipts.
Field revision, page load and exact field readback stay explicitly unmeasured.
Independent global visual checks remain necessary. New diagnostics, failures or
unavailable readiness retain the full original. `view:full` and the returned
`observation_id` retrieve the original without another remote call for 30 seconds.
The bounded process-memory cache holds at most eight 256 KiB encoded observations;
access expires after 30 seconds, with eviction on requests or process exit. No
raw observation is persisted. Missing information never authorizes input.
See project `OBSERVATION.md` for the versioned interface and measurements. Payload
byte reduction is not total-provider token reduction or a promised speedup.
