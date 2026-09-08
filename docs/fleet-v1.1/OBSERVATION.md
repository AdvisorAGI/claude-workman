# Compact observations, 2026-09-08

Coordination correction: the old Air lease ended at 00:23:46 UTC. The assumption
below that it remained reserved was stale. The successor has no Air reservation;
the adapter update is now installed. See AIR_ACCEPTANCE_CONTINUATION.md for the
fresh checks and current owner-use question. No new input timing is claimed.

Status: opt-in interface implemented, tested and installed. The owner targets
70-80% fewer total tokens while retaining full reasoning and accurate control.
That target is not met or promised by these payload measurements. The model
remains Astra xhigh with coding Fast off. Raw Caveman remains isolated test data,
not doctrine. No model calls were made by this implementation or benchmark.

## Interface version 1

`fleet_observe(node, query=None, receipt_id=None, view="compact", observation_id=None)`

- A fresh call performs one existing `inspect`, including its ordinary recall,
  local recording and DGX delivery. It does not capture, click, type, focus an
  app, acquire a lease or poll in the background.
- `query` is an optional nonsecret title substring. Candidates are WINDOWS in
  the current application only. Their full count, identities, ambiguity and
  truncation flag are retained. This is not an element, browser-tab or global
  window selector. At most ten candidate identities are included; the full
  original remains available. No candidate is chosen automatically.
- The capsule retains focus identity, pointer, permission/OFF state, lease,
  platform diagnostics, inspection event, recording and delivery receipts.
  `observed_at` is the device timestamp; `cache_age_ms` only measures residence
  in the local cache. Neither is an authorization to use old state.
- `field_revision`, `load_state` and receipt `exact_readback` are null because
  the current backend does not measure them. Missing Mac lease information from
  an older installed adapter is null too. No synthetic revisions or inferred
  successful readback are supplied.
- `receipt_id` must exist in that device's DGX journal. Visual verification
  requires a successful action, a later successful hashed capture, and a later
  verification, in journal order. Corrections to the action, capture or
  verification invalidate it. Exact-text evidence remains separate and absent
  until an actual digest/readback interface supplies it.
- Failures, unavailable readiness and unrecognized inspected diagnostic fields
  include `fallback_original` in full. That may increase payload size. Unknown
  details must not disappear merely to meet a token target.
- `view="full", observation_id=...` returns the original observation without a
  second device call. Access is limited to the same process/device and expires
  after 30 seconds. At most eight observations of 256 KiB encoded size are held
  in process memory; eviction occurs on requests or process exit. Raw observations
  are not written to disk or graph memory. No field values or screen pixels are
  collected by this interface.
- The MCP response contains one JSON text representation; it does not repeat
  the same object as automatic `structuredContent`.

Independent global visual verification, fresh input guards and exact readback
remain necessary. A window token does not identify the focused field. A historical
visual receipt does not prove present task completion. Callers should retain the
ordinary Workman entry point when projection would hide needed context.

Sources: `codex/workman-fleet/scripts/observation.py`, `server.py`,
`tests/test_observation.py`. The Silent Mode experiment owns its separate pure
inventory selector and Low-Max profiles; this interface adds no global profile
or parallel memory/scheduler.

## Measured results

Fresh installed MCP check, DGX 00:51:42 UTC, version
`1.1.0+codex.20260908005141`: eight tools exposed; one real read-only inspection,
local recording and DGX receipt succeeded. On-demand original retrieval preserved
the same action/visual receipt and guards. No screen capture or desktop input.

- Equivalent full response with the same historical receipt: **2354 bytes**.
- Compact response: **2081 bytes**, **11.597% smaller**.
- Ordinary inspect without the added receipt: **1804 bytes**. Do not compare
  this smaller, less informative baseline as if it carried the same evidence.
- One live inspection round trip: **370.965 ms**; cached full retrieval:
  **3.941 ms**. These are single samples, not a latency benchmark or input speedup.
- Initial conservative DGX runs included full fallback and grew in size because
  normal X11 geometry/pointer keys were outside the reviewed schema. Those
  standard fields were checked in the existing backend and added. The earlier
  results remain in evidence; unfamiliar fields still cause full fallback.

Synthetic active-app window inventories, no model/device calls:

| Windows | Matching candidates | Ordinary JSON bytes | Compact bytes | Byte reduction |
|---:|---:|---:|---:|---:|
| 1 | 1 | 1125 | 1034 | 8.089% |
| 5 | 2, ambiguous | 1512 | 1055 | 30.225% |
| 20 | 2, ambiguous | 3003 | 1055 | 64.868% |
| 100 | 2, ambiguous | 11004 | 1055 | 90.413% |

These synthetic comparisons omit a historical action receipt in both arms.
Median projection CPU time over 100 iterations was 0.005-0.036 ms in this run.
Bytes are not tokens. A local tokenizer was not available in the Workman runtime;
no new tokenizer or model dependency was installed. Total provider tokens and
cost were not measured here. Full originals, retries, fixed tool/prompt context
and subsequent visual verification can change overall savings substantially.

Evidence: `evidence/observation-installed-check.json`,
`observation-payload-benchmark-final.json`, `observation-live-readonly.json`,
`observation-installed-before-x11-pointer-shape.json`.

## Direct input optimization and remaining gates

Mac direct move/click now omit an adapter-level pointer RPC used only to compute
smooth-motion duration. The existing helper still performs its own movement,
settle, click and grant handling. Smooth mode keeps the duration calculation;
permission, input switches, lease and focus rules were not relaxed. Regression
tests verify the direct and smooth call paths and STOP behavior.

The source change is included in the installed DGX plugin package. **It has not
been deployed to the Mac adapter files during the coordinator's credential
workflow.** No new Mac click timing or typing correctness is claimed. Deployment
must use the existing verified-backup installer once that device is released,
then recheck actual capture/input and measure matched trials.

Air's read-only grant check at 00:40:18 UTC showed Screen Recording and
Accessibility true; event `08995b00491b4187864b7fdc6546586b` recorded locally and
received by DGX. The coordinator's reservation remains. No Air screenshot or
input was taken during this addition. Machome's earlier privacy gates and all
other pending acceptance items in RESULTS.md remain open.

Final automated run: **456 passed in 2.03 seconds**, exit 0. The first new test
collection found a missing bracket; a later test edit misplaced three assertions
and failed five cases. Both were corrected before installation; the failed logs
are retained with the final passing log. Plugin validation, installation and
installed MCP verification exited 0. No push or publication was performed.
