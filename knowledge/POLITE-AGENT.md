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
while retaining the start gate. The separate agent X server `:99` defaults to
no owner gate; set `WORKMAN_POLITE=1` there for isolated acceptance tests.

Input checks recur during typing, scrolling and pointer paths. When the owner
returns, stop and report `human_active` with completed and remaining work.
Release only agent-owned holds. Do not replay completed characters or scrolls.
Inspect the screen before resuming: the owner may have changed the application.

Put focus/click and dependent typing in one MCP `batch` or CLI `burst`, so the
agent keeps its target for that short burst and restores the owner's focus at
the end. A standalone focus or click gives focus back on completion; a later
standalone type acts on the then-focused window. Example:

```sh
wm.py burst '[{"action":"focus_window","query":"scratch","learn_shortcuts":false},{"action":"type_text","text":"hello"}]'
```

The steel arrow is a small, input-transparent overlay, not another X pointer
or keyboard seat. It must never focus a window, grab input, or enter the dock
or task switcher. It fades after the agent's activity, hides on owner activity,
and its daemon exits after 60 seconds idle.

Development checks may show the overlay and take screenshots on `:0`, but
must inject input and exercise focus restoration only on `:99`. This does not
prove focus restoration under Mutter on the owner's screen. Do not infer that
result from a successful test under a different window manager.

The installable CLI sources are `ops/cli/wm.py` and `ops/cli/fleet-wm.sh`.
They were staged from the local fleet helpers on 2026-10-07; the running copies
are intentionally unchanged until the lead integrates the reviewed branch.
The paste verb uses Workman's bounded text insertion and reports partial
progress. Off-layout browser characters use the existing temporary clipboard
fallback; restoration is skipped if the owner returns, so that fallback can
leave its character in the clipboard. Fleet input requires one named device; `all`
is restricted to observation.
