# Workman first-party fast preset

This is an input preset, separate from the coding model's Fast mode. Coding
remains Astra / Extra High / model Fast OFF.

Use it only for explicitly authorized first-party development: an owned editor,
our GitHub work, or a harmless local fixture. It does not authorize submissions,
merge, settings changes or third-party automation. No secrets, authentication
codes or customer API credentials may be passed to these tools.

1. `inspect` combines live status, active-window identity/focus token and pointer
   in one authenticated call. It is read-only and captures no screen. A partial
   permission state remains partial, with input fields omitted.
2. Observe the actual target with a fresh screenshot or region. Reuse that
   observation only while the target/focus stays valid; capture again after
   changes, errors or unexpected state. Never blindly batch dependent inputs.
3. `paste {text, expect_focus, preset:"first_party_fast", surface:"owned_editor"}`
   uses the existing clipboard/paste backend. Other allowed surfaces are
   `owned_github` and `local_fixture`. This declaration is a caller assertion,
   not automatic website ownership detection. Confirm the field and task scope.
4. Paste requires the keyboard switch ON, current focus and the device lease.
   It replaces the clipboard with the supplied ordinary text; it does not read,
   copy or restore the previous clipboard because it could contain a secret.
   An already posted paste is atomic and can complete after STOP. It cannot be
   interrupted midway like chunked typing.
5. `exact_readback_required:true` means the tool has NOT established text
   correctness. Read back only the intended nonsecret field, compare exact text
   and digest, and use screenshot confirmation when useful. Do not call
   `fleet_verify` from a successful backend return alone.
6. `wait_window` uses bounded backoff for window presence, up to ten seconds.
   This is not proof of a page finishing loading or a task completing.

`move`/`click` default to direct pointer movement. Optional `motion:"human"`
uses the existing smooth planner with `speed` 0.25–4. It is an ergonomics choice,
not a detector-evasion feature. Generic stable DOM targeting was not added;
use existing supported first-party UI tools when appropriate, after verifying
capabilities and scope. No claim about bypassing bot detection is made.

Regression sources: `tests/multiline_fixture.py` and `benchmark_workflows.py`
inside the plugin. The native editor has a normal Edit/Paste/Select All menu.
It retains only length/digest of its own field. The Air workflow runner checks
both grants before installing/launching the local fixture; no real ad, funding,
payment or investment operation occurs. A true human baseline remains missing.

Measured mini results are in `evidence/fast-inspect-benchmark.json` and
`evidence/mini-fast-paste-benchmark.json`. These are scoped engineering tests,
not an Air benchmark or a human comparison.
