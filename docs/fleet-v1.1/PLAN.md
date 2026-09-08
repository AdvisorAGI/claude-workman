# Workman fleet setup v1.1

Status: implementation packaged locally, fleet acceptance partial, 2026-09-08.
See RESULTS.md for current proof, benchmark denominators and human gates. The
baseline below is historical. Fleet v1.1 is separate from
claude-workman 0.3.0 and the macOS helper 0.2.0.

## Definition of done

- Content inventory identifies source, browser companion, runtimes, skills,
  plugin caches, documentation and backups on DGX, mini, Air and machome.
- Existing paths, virtual environments, dirty changes and v1.0 remain intact.
- A validated Codex plugin exposes one node-addressed entry point and CLI for
  status, screenshots, windows, focus, pointer movement, click, literal typing,
  key chords, scroll and drag. Window management includes listing, focusing,
  minimizing and moving windows through observed title-bar drags.
- Explicit node allowlist, strict existing SSH authentication, no TCP listener,
  no credential copying, no model calls, bounded failures and no action retry.
- macOS control requires a reachable Workman.app daemon and its live privacy
  grants. An MCP handshake alone never counts as device control proof.
- Fresh automated tests cover dispatch, node validation, coordinate contracts,
  focus checks, permission denial, failures, and text-free event reporting.
- A persistent per-task observation switch supports OFF and Low-Max across
  parallel sessions, uses atomic recovery and bounded setting history, and
  never changes model or safety gates. Individual evidence journals rotate to
  append-only 4 MiB segments without deleting history.
- Each available device passes harmless capture, movement, click, text and
  window-focus tests, restoring prior focus and pointer. Human permission
  gates remain explicitly separate from completed code.
- Fleet operations report sanitized outcomes to this DGX; recurring failures
  and operator lessons are available to subsequent use without model APIs.
- Extend the existing file memory convention with a local graph projection:
  stable devices, projects, sessions, tasks, decisions, permission observations,
  outcomes and lessons, linked to evidence with verification times. Preserve
  superseded facts; expired/conflicting permission facts never authorize input.
  Verify a real action -> linked memory -> independent later recall, plus stale
  and conflicting observations. Do not ingest raw text, screens or transcripts.
- Parallel sessions use per-device input leases, independent across devices;
  no interleaved input on one physical desktop and no forced takeover.
- Persistent, independent mouse/keyboard on/off switches and a user control
  panel. OFF refuses new fleet input, physical input stays usable, task finish
  restores focus/pointer when allowed and releases ownership.
- Exact evidence, remaining gates, a v1.1 checkpoint and rollback instructions
  are saved here. No push or publication is authorized.
- Raw Caveman remains a byte-pinned, default-OFF Workman test provider only.
  It never changes doctrine, normal sessions, hooks or model routing.

## Organization decision

Keep working installations in place. The attached `workman` is the desktop
source (clean at baseline dfd13d0); `workman-chrome` is a distinct Manifest V3
extension 0.1.0. Neither is a redundant copy. `src/claude-atmos-workman` and
the Claude cache contain the separate macOS app-helper family. Their hooks,
learning databases and runtimes must not be swept into the new plugin.

The fleet plugin source belongs under `codex/workman-fleet` in this repository;
the personal marketplace will reference it through `~/plugins/workman-fleet`.
Documentation lives in `docs/fleet-v1.1`; private evidence and backup
archives are excluded from Git. No folder moves or cleanup
deletions are needed to establish these roles.

## Fresh baseline

- DGX: linux_x11, DISPLAY :0, 1920×1080 capture seen. Python 3.12 virtual
  environment: mcp 1.29.1, Pillow 12.3.0, pytest 9.1.1. `gi` is absent there
  but present in `/usr/bin/python3`; optional GTK operations need a scoped fix.
- Mini: SSH succeeds; helper daemon reachable, live Screen Recording and
  Accessibility true. Source `~/src/claude-atmos-workman` at c36d7ee has
  untracked `.claude/worktrees/`; direct `~/workman` has a retained darwin backup.
- Air: SSH succeeds; cached helper daemon reachable, both grants false.
  `~/claude-atmos-workman` is desktop source dfd13d0 (not the app helper).
  `~/src/claude-atmos-workman` is helper source 4ac77db with dirty hooks.
- machome: SSH succeeds; helper source and .venv exist under `~/src`; app and
  daemon are absent. Installer status returned NOT REACHABLE.
- DGX `~/src/claude-atmos-workman` has three modified and two untracked hook
  files. Their contents are snapshotted, not overwritten or staged.
- GPT-6 Astra / xhigh / default service tier freshly read from Codex config.
- Mini stale-frontmost bug independently reproduced: fresh Workman adapter saw
  GitHub Desktop PID 74723 while the daemon reported dead Claude PID 9853.
  Fleet focus identity now comes from a fresh adapter process; input and window
  titles still use the app daemon. Verify this during a real focus transition.

## Memory inspection

Read `engineering-library/NOTES/memory-tool-and-our-convention.md`: existing
memory uses a short MEMORY.md index, typed frontmatter, one topic per file and
wikilinks. Workman's project memory exists at
`~/.claude/projects/-home-monzurul-workman/memory/`. No graph MCP configuration
or graph database was found in the inspected Workman/autonomy memory locations.
The existing Mac learner has action/profile storage; it does not represent the
requested context relationships. Extend the DGX learning archive with a local
SQLite graph projection and add a typed reference in the existing memory index.
Keep the journals as source evidence and the graph as a queryable projection.

The old `WORKMAN_SETUP.md` beside v1.0 is historical and stale. This directory
is the v1.1 source of truth. v1.0 has a pre-change SHA-256 manifest in evidence.
