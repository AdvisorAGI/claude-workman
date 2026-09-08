# Air acceptance continuation, 2026-09-08

Current coordination replaces the earlier reservation notes. The original
coordinator released Air at **00:23:46 UTC**, recorded as
`359ae75b913148e08b8757ae2f756d9d`. The DGX journal was checked, rather than
treating the handoff alone as proof. The successor is **Continue DGX email
coordination**, task `01a07e6a-95ea-7c71-89a9-e4c1dc9d3e52`.

The successor confirms it controls mini only, has no Air reservation, and has
performed no Air screen actions. Air is available from its coordination boundary,
subject to live lease and owner input checks. Mini remains reserved. Preserve
Air's running relay and its configuration; this task has not changed either.

Fresh read-only checks at **01:05:36-37 UTC** found Air's lease available,
Screen Recording and Accessibility true, and both input switches enabled.
Both observations recorded locally and reached the DGX. Evidence:
`evidence/air-successor-readiness.json`.

## Completed in this continuation

- Installed the verified source adapter update on Air through the existing
  per-file backup installer. Only `device.py` changed; no helper, service,
  runtime, app restart or relay configuration changed. Its SHA-256 is
  `e98c994f8ea17fe02a34a47eaadef33f6c0be541f8bd2a726791da975e7248ef`.
  Live `inspect` now reports lease state from that adapter. Direct move/click
  omit the redundant cursor RPC; live input timing is still unmeasured.
- Hardened the local benchmark fixture: Close test terminates its own process;
  each run has a unique title/state file and uses its exact launched PID. An
  older fixture cannot be mistaken for the new trial. Explicit attempted counts
  avoid counting a failed result twice or counting a preflight as an input trial.
- Cleanup refuses guessed coordinates when no fixture window was located,
  releases without input after a focus change, and records restoration failures.
- Automated suite: **464 passed in 2.06 seconds**, exit 0. Validated and installed
  plugin version **1.1.0+codex.20260908012019**. The installed process exposes all
  eight tools, passes actual read-only Air status/lease calls and confirms the
  raw Caveman test remains OFF. This is not new live-input proof.

## Current owner-use check

A short lease was acquired to inspect the current Air desktop. Active-window
metadata showed Chrome verification pages. No click, typing, focus change or
other input occurred. A new capture had been made before that metadata was
reviewed; it was discarded **without viewing or extracting possible codes**.
The task-created 146,093-byte image was removed. The capture event remains as an
unverified historical event, not as a successful control lesson.

The lease was released again, event `6ced86be1551410db326d347e859a714`.
No agent reservation is being inferred from the open pages. The pending owner
question is whether sign-in is still active or a temporary local test window
may take focus and restore Chrome afterward. This is the current gate, not the
old coordinator hold. Evidence: `evidence/air-credential-surface-pause.json`.
No additional GUI fixture was launched while this owner-use question was pending.

Once that is clarified, recheck lease and grants, launch the uniquely identified
local fixture, capture only its region, run the five remaining workflow trials
in a new output directory, and record exact digest/click/error/STOP/overhead
results. Preserve the initial 13 successes, one interruption and four unstarted
trials in their original evidence. Report any explicit recovery attempt separately.
Restore the observed original window/pointer and release input. A human baseline
and representative total-token reduction remain unmeasured.

## Adapter rollback

Backup: `/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/backups/20260908T011036Z/device.py`.
Current file: `/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/device.py`.
The installer verified both the backup and installed bytes. Exact before/after
hashes are in `ROLLBACK_AIR_ADAPTER_20260908.json`. Restore only if the current
hash still matches this update; inspect newer changes first. This adapter is
loaded per request, so recovery does not require restarting the helper or relay.
Do not change an OFF switch or override another session's input lease.
