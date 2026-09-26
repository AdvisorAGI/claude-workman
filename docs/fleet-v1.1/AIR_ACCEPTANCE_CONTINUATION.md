# Air acceptance continuation, 2026-09-08

Update at 02:11 UTC: the latest adapter, learner and motion profile are installed
on Air with a new verified backup. Their SHA-256 values match the other three
devices and all three files compile under Air's existing helper interpreter.
Fresh status event `221c0074c7b44047b92d78ce8f0201f9` reports the helper
reachable with both grants true and reached the DGX. The pure planner benchmark
performed no screen or input action. The owner-use question below remains the
gate for a live fixture, motion telemetry and the remaining workflow trials.

The Mac mini coordinator released Mini at 01:59:48 UTC. Mini is no longer
reserved, but no Mini UI action was needed for this update.

Current coordination replaces the earlier reservation notes. The original
coordinator released Air at **00:23:46 UTC**, recorded as
`359ae75b913148e08b8757ae2f756d9d`. The DGX journal was checked, rather than
treating the handoff alone as proof. The successor is **Continue DGX email
coordination**, task `01a07e6a-95ea-7c71-89a9-e4c1dc9d3e52`.

The successor previously controlled mini only, had no Air reservation, and had
performed no Air screen actions. Air is available from its coordination boundary,
subject to live lease and owner input checks. Mini is now released. Preserve
Air's running relay and its configuration; this task has not changed either.

Fresh read-only checks at **01:05:36-37 UTC** found Air's lease available,
Screen Recording and Accessibility true, and both input switches enabled.
Both observations recorded locally and reached the DGX. Evidence:
`evidence/air-successor-readiness.json`.

## Completed in this continuation

- Installed the verified source adapter updates on Air through the existing
  per-file backup installer. `device.py`, `learning.py` and `motion_profile.py`
  changed; no helper, service, runtime, app restart or relay configuration
  changed. Live `inspect` reports lease state. Direct move/click omit the
  redundant cursor RPC; live input timing is still unmeasured.
- Hardened the local benchmark fixture: Close test terminates its own process;
  each run has a unique title/state file and uses its exact launched PID. An
  older fixture cannot be mistaken for the new trial. Explicit attempted counts
  avoid counting a failed result twice or counting a preflight as an input trial.
- Cleanup refuses guessed coordinates when no fixture window was located,
  releases without input after a focus change, and records restoration failures.
- Automated suite: **480 passed in 2.85 seconds**, exit 0. Validated and installed
  plugin version **1.1.0+codex.20260908022346**. The installed process exposes all
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

Latest adapter backup:
`/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/backups/20260908T022332Z/device.py`.
Learner and motion backups:
`/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/backups/20260908T021121Z/`.
Current file: `/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/device.py`.
The earlier adapter backup remains at
`/Users/muhammadtariqul/.claude/tools/workman-fleet-v1.1/backups/20260908T011036Z/device.py`.
The installer verified backup and installed bytes. Restore only if the current
hash still matches this update; inspect newer changes first. These files load
per request, so recovery does not require restarting the helper or relay.
Do not change an OFF switch or override another session's input lease.
