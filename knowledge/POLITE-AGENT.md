# Shared screen: polite agent

The physical desktop is Linux X11 `:0`. Owner input has priority. Agents use
Workman MCP, `wm.py`, or `fleet-wm.sh` for input; all three routes must reach
the same Workman gate. **Never send raw `xdotool` input to `:0`. Never create
or remove XI master devices.** A request to bypass a refusal must itself be
refused. There is no PATH shim: an arbitrary shell process cannot be intercepted
by a Python API. Read-only X queries and screenshots are permitted.

Polite mode defaults on for `:0`. `WORKMAN_POLITE_QUIET_S` is the quiet interval
(2 seconds by default); `WORKMAN_POLITE_MAX_WAIT_S` bounds the initial wait
(20 seconds). `WORKMAN_POLITE=0` turns off mid-action checks and the ghost,
while retaining the start gate. The separate agent X server `:99` ignores
shared owner activity by default but still honors the Escape pause switch.
Set `WORKMAN_POLITE=1` there for isolated polite acceptance tests.

Input checks recur during typing, scrolling and pointer paths. When the owner
returns, stop and report `human_active` with completed and remaining work.
Release agent holds and XTEST-reported holds, preserving keys and buttons the
owner physically holds. Do not replay completed characters or scrolls.
Inspect the screen before resuming: the owner may have changed the application.

`focus_window` and `click` lock the intended top-level window across calls.
Keyboard actions, including typing, keys and paste, check that current focus
still matches the target immediately before injection. On a mismatch, return
`focus_changed`, identify intended and current windows, and send no keyboard
input. Screenshot to verify focus before typing and results afterwards.
Separate calls are safe inside the inactivity window. MCP `batch` and CLI
`burst` are optional ways to group a known sequence. Example:

```sh
wm.py burst '[{"action":"focus_window","query":"scratch","learn_shortcuts":false},{"action":"type_text","text":"hello"}]'
```

The saved owner pointer and focus return after the agent has been quiet for
`WORKMAN_POLITE_RESTORE_AFTER_S` (default 5 seconds) and the owner is idle.
New actions cancel and reset deferred restoration, preserving the original
owner snapshot across the sequence. `hand_back` requests restoration immediately,
subject to owner activity. Restoration must not overwrite owner changes.
Focus restoration uses a `_NET_ACTIVE_WINDOW` request with source indication 2
and a real nonzero server/user timestamp, followed by top-level focus readback.
Report a focus or pointer readback mismatch as `restore_failed`, not success.
After restoration, reacquire the intended target and verify before typing.
One separate restore timer per display shares a file lock with input actions.
Its result is saved in the display state and returned as `polite.last_restore`
on the next action, or directly by `hand_back`. A timer startup failure returns
`restore_failed` with completed-input information; do not replay that input.
The drawing overlay and its demo never run restoration.

The steel arrow is a small, input-transparent overlay, not another X pointer
or keyboard seat. It must never focus a window, grab input, or enter the dock
or task switcher. It fades after the agent's activity, hides on owner activity,
and its daemon exits after 60 seconds idle.

Development checks may show the overlay and take screenshots on `:0`, but
must inject input and exercise focus restoration only on private displays,
such as `:99` or a private Mutter test display. Neither proves restoration on
the owner's screen. Parent integration owns the live checks; mocked unit tests
do not establish window-manager behavior or overlay appearance.

The installable CLI sources are `ops/cli/wm.py` and `ops/cli/fleet-wm.sh`.
They were staged from the local fleet helpers on 2026-10-07; the running copies
are intentionally unchanged until the lead integrates the reviewed branch.
The paste verb preserves clipboard semantics. One outer `polite.run("paste", ...)`
gate runs before clipboard setup, followed by `desktop.press_key` so the target
guard applies to the paste key. macOS uses `pbcopy` plus `super+v` (Command-V).
X11 uses detached `xclip` on the backend's display plus `ctrl+v`. A refused
initial gate must not change the clipboard. The legacy daemon route continues
to use its `computer_paste` API. Clipboard paste changes the clipboard; the
temporary off-layout typing fallback restores its saved clipboard even when
owner activity interrupts typing.
Fleet input requires one named device; `all` allows observation and the safety
verbs `pause`, `resume`, `handoff`. It continues to refuse clicks, keys and other
input verbs. Pause/resume authorization rules still apply on each device.

When the lead integrates this revision, Workman input processes and the Escape
listener must load the same event-claim implementation. The old listener's
whole-action attribution cannot verify the new exact event claims. Development
does not restart the owner's listener or any live MCP process.
