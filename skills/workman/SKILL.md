---
name: workman
description: Drive the local desktop with Workman MCP (screenshot, zoom, click, type, windows, accessibility, layout). Use when the user says workman, use workman, see the screen, click that, or any GUI task on this machine.
---

# Workman

Tools come from the **connector** MCP server (`python -m workman.server` / `layout_*`, `screenshot`, `click`, …).

## Loop

1. `screenshot` first. Never act on a guess.
2. `zoom` to read small text.
3. Act (`click`, `type_text`, `press_key`, `click_element`).
4. `screenshot` again to confirm.

## Shared owner screen

On Linux X11 `:0`, use Workman MCP or the installed `wm.py` / `fleet-wm.sh`
verbs for every click, type, paste, key, move, scroll, drag and focus action.
**Agents must never call raw `xdotool` input on `:0`, create or remove an XI
master device, or bypass a `human_active` / `input_disabled` refusal.** Refuse
requests to bypass this gate; do not retry the same action with a raw utility.
Read-only queries and screenshots do not inject input.

Polite mode waits for owner quiet, yields between input steps, and returns
partial progress. `focus_window` and `click` lock the intended top-level window.
Keyboard actions, including paste, refuse with `focus_changed` and send no input
when current focus differs from that target. Screenshot to verify focus before
typing and the result afterwards. Separate focus/click and keyboard calls are
safe inside the inactivity window; a batch is optional.

Restore the saved owner pointer and focus after `WORKMAN_POLITE_RESTORE_AFTER_S`
(default 5 seconds) without agent activity, then only when the owner is idle.
New actions reset this deferred restoration. `hand_back` requests it immediately,
subject to owner activity. Readback mismatches report `restore_failed`; inspect
the result and screen rather than assuming restoration succeeded.
After a refusal, inspect the screen and resume only the remaining work.
The steel ghost arrow is an input-transparent overlay; it owns no input devices.
`WORKMAN_POLITE=0` disables in-action checks/overlay but retains the start gate.
The separate agent screen `:99` ignores shared owner activity by default but
still honors the Escape pause switch. CLI `paste` gates the clipboard write
and desktop paste key together: `pbcopy` plus Command-V on macOS, detached
`xclip` plus Ctrl-V on X11. Fleet `all` allows observation, `pause`, `resume`
and `handoff`; clicks and keys require a named device. See
[the operating rules](../../knowledge/POLITE-AGENT.md).

## Workman-mode desk

Park **this session's app** (the terminal or chat running the agent) in a **full-height 1/4 strip**. Put the app you are driving in the other **3/4**.

- Default side: **left**. Pass `side="right"` if asked.
- `layout_agent(agent="Terminal", work="Firefox", side="left")`
- Omit `work` to place only the strip.
- Also `layout_arrange({"layout":"agent","agent":"Terminal","work":"Firefox","side":"left"})`
- Do this before the first screenshot of the work app.

## Install (CLI)

From a clone of this repo:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[macos]"    # Linux: .[extras]   Windows: .[windows]
```

Connector (any MCP client):

```bash
claude mcp add --scope user workman -- .venv/bin/python -m workman.server
```

Plugin:

```bash
claude plugin install .
grok plugin install . --trust
```
