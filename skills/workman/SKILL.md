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
