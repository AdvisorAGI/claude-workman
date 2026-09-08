# Workman fleet v1.1 verification

Updated 2026-09-08 UTC. The implementation is packaged and installed locally.
Fleet acceptance is still partial: machome needs human privacy grants, the Air
workflow benchmark was interrupted and released for credential work, and the
latest taskboard refinements are compiled but not deployed. Nothing was pushed
or published. Fleet v1.1 is separate from upstream package versions.

## Device readiness and actual proof

Live status was rechecked at 00:21:00-01 UTC. Permissions are observations at
that time, not lasting authorization. All four devices recorded the status
locally and delivered it to the existing DGX hub. A second collection returned
zero new events on all four devices, confirming no duplicate delivery.

| Device | Fresh grants/helper | Actual screen/input evidence | Current gate |
|---|---|---|---|
| DGX | X11 ready | Capture, click, exact typing, focus, minimize, title-bar drag, local STOP | Desktop released; preserve owner's split view |
| Mac mini | Screen Recording and Accessibility true | Capture, click, exact replacement and multiline typing/paste, focus restoration, STOP | Released; no further UI actions under Air-only routing |
| MacBook Air | Screen Recording and Accessibility true | Capture, click and exact 26-character input; 13 completed workflow trials | Released for coordinator's credential workflow; do not take over |
| machome | App/helper reachable; both grants false | Local recording and DGX receipt only | Human grants required; no capture/input proof |

On machome, the owner must enable **Workman** in System Settings > Privacy &
Security > Screen Recording and in Privacy & Security > Accessibility. The
installed app is `/Users/amg/Applications/Workman.app`. Only the owner approves
these OS controls. After that desktop is authorized for testing, use onboarding
`wait` to recheck; follow any OS-required relaunch and verify capture and input
separately. Do not switch to machome UI while the Air-only instruction applies.

Evidence: `evidence/readiness-final.json`, `report-dedup-final.json`,
`screens/dgx-final-fixture.png`, `screens/mini-exact-final.png`,
`screens/air-input-verified.png`. Capture files are private, excluded from Git
and the release source archive. The learning graph stores their hashes/event
references, not pixels or typed content.

## Installed package and controls

Maintained source: `/home/monzurul/workman/codex/workman-fleet`.
Installed version: `1.1.0+codex.20260908001438`, in the personal Codex marketplace.
The installed stdio process exposed and exercised the seven tools:
`fleet_control`, `fleet_onboard`, `fleet_recall`, `fleet_report`, `fleet_memory`,
`fleet_verify`, `fleet_test_mode`. The installed process also refused an unknown
device. This is an actual tool-call check, not a handshake count alone.

One explicit-device entry point reuses the inspected runtime paths and existing
authenticated SSH. Capture, pointer, click, literal input, focus and window
operations have scoped guards. No new listener, background recorder, scheduler,
remote-access service or model client was added. The browser companion remains
optional and disabled for this task.

Per-device leases coordinate participating sessions; distinct devices can work
concurrently. A second session was refused on the owned DGX desktop. Persistent
mouse and keyboard OFF switches, a local control panel, guarded finish/release,
fresh focus identity and bounded typing checks are implemented. Legacy direct
tools remain installed and do not honor these new fleet leases or switches.
Use the fleet entry point in participating sessions. Physical user input stays
usable. An already posted atomic paste may complete after STOP.

The Mac Desktop `Workman Input Controls.command` and DGX application launcher
open the local control panel. They do not start a monitoring service. Task finish
restored DGX/mini/preflight Air focus and pointer. The later Air interruption
released ownership without restoring over the changed focus.

## Learning and graph memory

The existing Workman journal, fleet reporter and DGX learning hub are reused.
The local SQLite graph is a projection of allowlisted task events. It links
devices, projects, tasks, sessions, decisions, permission observations, outcomes
and verified lessons with stable IDs and source/evidence references. No raw
typed text, key chords, window titles, secrets, screens or transcripts enter
this path. Recall happens before ordinary fleet actions; urgent STOP bypasses
recall/report collection and explicitly reports delivery as pending.

Independent later queries retrieved four verified DGX lessons, five mini
lessons and three Air lessons. Machome has permission-failure evidence and
receipt, but no verified screen/input lesson. Example end-to-end evidence:

| Device | Real action event | Later capture event | Recalled lesson |
|---|---|---|---|
| DGX | `1fe9377ba197444caeb2103025567872` | `25be2a5ce26a4f678422005f472cc92e` | `lesson:dgx:verified_click` |
| mini | `81e21bfdca8546e09a987d149f5c87c9` | `72d38c96e16f4068a746a6d11a8991bc` | `lesson:mini:verified_macos-fixture-edit-menu` |
| Air | `b92d582b2692430685fc909499c7c7cc` | `dbd327064ac441558d7021c50b7e36a0` | `lesson:air:verified_type` |

The original mini typing claim was wrong: a bare Cocoa fixture lacked the Edit
menu needed for Command-A. Its premature verification was retracted, preserving
history. After adding the normal Select All responder action, the 26-character
replacement and SHA-256 passed, and the recovery lesson was separately recalled.
The exact later digest is
`36fbb991815fc06155b7415d572cfc45680b9875cb4ed736721864817ef55897`.

Automated tests cover duplicate/conflicting events, evidence ordering and device
matching, retraction and dependent lessons, permission supersession, expiry,
future dates and same-time conflicts. Stale/conflicting permission queries return
no usable value. Memory never supplies an OS grant or overrides live checks.
See `evidence/independent-final-*-memory.json` and the earlier independent mini
and Air query files. The project memory index links this report using the
existing file-memory convention.

## Measured performance and workflow results

These are small local-fixture measurements, not general performance guarantees.
No human baseline has been measured. No model token or cost comparison has run.

| Measurement | Fresh result | Limit |
|---|---|---|
| Mini batched inspect | Median 2825.845 to 1100.993 ms; 61.038% less elapsed time | Five pairs; status/active/pointer only |
| Mini multiline input | 6/6 exact, 63 characters and digest; zero retries | Three type and three paste trials |
| Mini multiline latency | Median type 7099.812 ms; paste 1295.463 ms | Clipboard fast preset; 81.754% reduction in this fixture only |
| Mini STOP acknowledgement | 402.525, 347.724, 301.872 ms | Three trials, not a robust p95 estimate |
| Mini STOP enforcement | In-flight move aborted before endpoint; next input refused in all three | Move-result receipt 802.430-820.015 ms after request; actual pointer-stop instant not measured |
| Installed plugin idle | 10.010 s; CPU delta 0.0 s at Linux tick resolution; RSS 57,102,336 bytes unchanged | Added stdio process only; no helper/panel incremental baseline |

Mini fast-paste digest:
`c31b9d038627dba218ce4604f1e10d8cc0f20dac75788250acbf01af8b97f3d7`.
STOP's local switch write took 1-2 ms. Its event was received by DGX on the next
collection. Before this optimization, three STOP acknowledgements took
580.780-750.090 ms. The in-flight guard result now reports `input_disabled`.

Air workflows use harmless coder, marketing-draft and funding-plan fields in
a local native fixture. No ads, investments, payments or external submissions
were performed. Direct and optional smooth pointer modes use the same typing
path. The measured difference must not be attributed to prose style.

**Air denominator: 18 planned; 14 attempted; 13 completed successfully; one
interrupted; four not started.** All 13 completed trials matched exact text and
digest, had zero click error in logical points and zero retries. Direct had
seven completed trials with median 9493.594 ms; smooth mode had six with median
9916.796 ms. Unequal partial groups do not establish a winner or a human score.

Trial 14 stopped on `focus_changed` at 23:59:08 UTC on September 7. Cleanup
released ownership at 23:59:14 instead of stealing focus back. The cause of the
focus change is unconfirmed. Original results are retained, with the journal-
backed interruption added in `air-workflows/results-with-interruption.json`.
The harness now records interruptions and unattempted counts. A later authorized
recovery may use `--start-at 13` with a new output directory after checking the
screen and any remaining owned fixture. No recovery was run while Air was held
for credentials. Air STOP/load/idle measurements and a person-run baseline are
still outstanding. Earlier planner-only motion results are synthetic.

Evidence: `evidence/fast-inspect-benchmark.json`, `mini-fast-paste-benchmark.json`,
`mini-stop-benchmark-final.json`, `mini-fast-restored.json`,
`installed-plugin-final-check.json`, `air-workflows/results-with-interruption.json`.

## Onboarding and task panel

The consolidated per-installation flow inventories only enabled view/input
requirements, reasons and live states. It remembers prompts, waits with bounded
backoff, and handles denial/unanswered prompts, revocation, cancellation,
unavailable helpers, relaunch and timeout. A human approved Air's grants.
A static numbered callout was visually verified beside the freshly observed
Accessibility control, without covering it, and expired. Without capture the
flow provides plain text. No privacy database was edited.

The two mini overlays were the same taskboard, one LaunchAgent instance and
one manual instance, reading the same three task JSON files. At 23:31 UTC the
verified duplicate was consolidated after backing up source/binary/plist.
All three task-file hashes stayed unchanged. A screenshot verified one readable
panel with task/status/device/freshness, adjustable text and collapse/live
controls: `evidence/screens/taskboard-clean.png`.

The source additionally includes opacity and stricter verified-result display
refinements. That final candidate compiled successfully and passed valid and
retracted evidence model checks. **It was not deployed or given a final UI
interaction check after Air-only routing.** The existing installed revision
remains distinct from this newer source. Do not restart mini UI without release.

## Raw Caveman is only a Workman test provider

The pinned GitHub MIT skill is packaged as unmodified test data, default OFF.
The installed tool/resource check selected it for one explicit test, read all
7,022 exact bytes through MCP, proved another test remained OFF, then disabled
it and confirmed resource reads were refused. No doctrine, normal-session style
or model setting changed. No model API or desktop input was used by this check.

The registry supports reviewed packaged Markdown skill/plugin providers and
references to already configured MCP resources. External MCP references are
declarative handoffs, not proof of a connected server. No executor/installer is
included. A matched live raw-Caveman model comparison remains unrun; no token
savings, human equivalence or zero-detectability claim is made.
See [CAVEMAN_TEST.md](CAVEMAN_TEST.md) and `evidence/caveman-installed-check.json`.

## Automated checks and recovery

- Desktop plus fleet pytest: **432 passed in 2.05 seconds**, exit 0.
- Browser companion: **38 passed, 0 failed**, unchanged companion, exit 0.
- Mini helper selection: **209 tests, 16 skipped**, exit 0.
- Air helper selection: **59 tests**, exit 0.
- Taskboard final source compile: exit 0. Valid and retracted proof cases each
  passed four model checks and one evidence check, without changing task files.
- Codex plugin validator and supported local installation: exit 0.
- Installed MCP calls/resource pin/default-OFF/isolation/OFF checks: exit 0.
- All ten helper/reporter recovery backups and current hashes verified; all six
  original v1.0 checkpoint hashes still match.

Existing dirty changes, runtimes, source folders, browser companion and working
caches were preserved. No virtual environment was moved. See [INVENTORY.md](INVENTORY.md)
and [ROLLBACK.md](ROLLBACK.md), including the exact rollback manifest.
The `workman-releases/v1.1` checkpoint is an implementation candidate with these
explicit acceptance gates, not a claim that every device is fully verified.
