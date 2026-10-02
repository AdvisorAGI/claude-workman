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

## Workman-mode desk

Park **this session's app** (the terminal or chat running the agent) in a **full-height 1/4 strip**. Put the app you are driving in the other **3/4**.

- Default side: **left**. Pass `side="right"` if asked.
- `layout_agent(agent="Terminal", work="Firefox", side="left")`
- Omit `work` to place only the strip.
- Also `layout_arrange({"layout":"agent","agent":"Terminal","work":"Firefox","side":"left"})`
- Do this before the first screenshot of the work app.

## TV (Android TV on the LAN)

`tv_*` tools drive the owner's Android TV: ADB for sight and keys, the paired remote protocol as the keys/power fallback. Needs `pip install -e ".[tv]"`. Config: `WORKMAN_TV_HOST` (default 192.168.0.191), `WORKMAN_TV_KEYDIR` (default `~/.config/workman-tv`).

1. `tv_status`: window, HDMI session, picture mode, which transports answer.
2. `tv_elements` (compact UI tree, `focused` row) or `tv_screenshot`.
3. Move: `tv_focus_text(text)` walks the D-pad there; `tv_key([...])` for anything else. TV UIs are focus-driven, never tapped.
4. Read `focused` in every reply before the next step, then `tv_select(text)`.

- Keys go one at a time, at least 0.3 s apart: the set drops bursts.
- `tv_select` and select keys in `tv_key` (DPAD_CENTER, ENTER) refuse when the target or the focused element matches the danger list (factory reset, reset, erase, remove account, OEM unlocking, revoke USB debugging, factory menu, format, wipe), or when focus cannot be read.
- `tv_screenshot` fails on the HDMI input (protected video layer); `tv_status` still names the window.
- `tv_input_hdmi` goes back to the Mac's input. There is no TV shell tool, by design.
- Dark screen: `tv_screen("off")` / `tv_screen("on")` (backlight only; the TV stays on Wi-Fi and controllable). `tv_screen("status")` reads it.
- `tv_power_off(confirm=True)` is REAL power-off: the TV drops off Wi-Fi, Wake-on-LAN does not wake it, and only the physical remote turns it back on. POWER is not a `tv_key` name.
- The TV's idle standby (4 hours) is on by the owner's choice. Leave that setting alone.
- Full runbook with every live-verified fact, the hardware fault history, the headless-Air rules and the mistakes made while building this: `TV-REMOTE.md` beside this file.
- Learned live 2026-10-02: space keys >= 0.3 s or the set drops them; `uiautomator` is the only way to read menus (screencap is empty on HDMI); `power_picture_off` is 1 = ON, 0 = OFF; Wake-on-LAN never wakes this set; blind D-pad once reached the account picker beside "Remove account", so read focus before every select.

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
