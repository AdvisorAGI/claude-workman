# Focused acceptance plan, updated 2026-09-08

Current measured status is in RESULTS.md. Air is the only desktop permitted for
further tests and is currently reserved for the coordinator's credential work.
The earlier mini typing failure was retained, corrected and subsequently passed
exact replacement/digest checks. This plan's targets are not completion claims.

## Scope

Keep the existing Workman runtimes, app helpers, SSH, reporter, learner and
project memory. Add a small Codex fleet entry point, graph projection, task
ownership, persistent input switches, bounded onboarding and optional motion
choices. No customer API secrets, model APIs, external submissions or new
remote-access service. Small local fixtures represent coding, marketing and
business/investment planning; they perform no real business actions.

## Measured checks

1. Direct and human-mode automation use the same fixture trials and coordinates.
   Target: every task complete, exact text/digest match, pointer endpoint within
   2 logical points, zero uncontrolled input retries. Report actual denominators.
2. Pointer speed is a per-call multiplier 0.25–4.0; smooth mode is optional.
   Compare latency distributions, click error, retries and movement path costs.
   No claim that motion proves human identity or defeats bot detection.
3. STOP must refuse subsequent inputs across participating sessions. Measure
   in-flight response; target p95 below 500 ms under the local fixture load.
   Always report any atomic action that finished after STOP. Physical user
   input stays usable; finish restores prior focus/pointer and releases ownership.
4. Measure process CPU, peak/resident memory and idle CPU, separately for the
   existing helper, plugin server, optional panel and benchmark fixture. Do not
   report total runtime memory as the incremental cost of the new plugin.
5. A real human baseline requires a person running identical trials. Until then
   report MISSING, with no human-relative task score. Prior Air planner-only
   results are synthetic path checks, not live desktop benchmarks.
6. MacBook Air live benchmarks begin only after fresh Screen Recording AND
   Accessibility checks pass. Capture and harmless input each need separate
   proof. Device ownership must be explicitly released before further UI tests.
   Preserve failure history and require exact content and digest for recovery.

## Onboarding

Inventory only permissions required by enabled features and present reasons,
current state, source and timestamp together. Installation consent, OS grants
and per-task authorization remain separate. Open only a missing permission's
supported Settings pane through the existing Workman helper. A human approves
system dialogs/authentication. Do not write privacy databases.

Use one setup flow per installation/device. Persist prompted state, avoid
repeating granted prompts, and use bounded backoff when checking changes.
Reuse the helper's existing grant-change watcher and launcher restart behavior.
Handle unavailable helpers, denied/not-yet-granted access, cancellation,
revocation, restart and timeout without claiming success. Without capture,
provide plain text. Any numbered target hint needs a fresh screenshot and
expires quickly; default to no animation, respecting reduced-motion settings.

## Existing taskboard consolidation

Mini has two processes of the same taskboard: PID 50402 from its existing
LaunchAgent and PID 50406 started manually from the same board directory.
Both read .claude/board/sessions/*.json. Preserve all session/task data and
verified source/binary backups. Prepare source changes while UI is reserved.

Acceptance: one running readable panel, on-screen bounds, adjustable text,
current task/device/freshness, latest verified result and next action/permission.
Collapse completed items and duplicate display rows without deleting data.
Follow latest only when enabled; user scroll-away pauses following and exposes
Resume live. No endless animation or changes to unrelated app internals.
Verify a clean screenshot and harmless controls after the UI is released.

## Isolated raw Caveman comparison

Raw Caveman is test data, default off, one provider per explicit Workman test.
It must not change doctrine or normal session behavior. Check GitHub provenance
and source hash, then prove installed MCP payload/resource access, test isolation
and OFF. A live comparison requires matched fixtures/model/effort/tools and real
measurement; it cannot use customer secrets or metered model APIs. The provider
registry may extend reviewed packaged skills/plugins or already configured MCP
resource references without installing executors or new services. See CAVEMAN_TEST.md.
