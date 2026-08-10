# claude-workman — computer use for Linux, as an MCP server

[![MCP Server](https://img.shields.io/badge/MCP-server-blue)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform: Linux/X11](https://img.shields.io/badge/platform-Linux%20%2F%20X11-lightgrey)](#requirements)

**claude-workman is an open-source [MCP](https://modelcontextprotocol.io) server that gives an AI
assistant human-mode control of a Linux desktop — it takes a screenshot, reads the
accessibility tree, then moves the mouse, clicks, types, and switches windows.**
It works with Claude, Claude Code, and any other MCP client.

Named after the Walkman: a small, portable thing that just plays. Point it at a display
and it works.

```bash
pip install claude-workman
claude mcp add --scope user workman -- python -m workman.server
```

---

## Why accessibility-tree clicking beats pixel clicking

Most desktop automation guesses at pixel coordinates and breaks the moment a layout shifts,
a font renders differently, or a window moves. claude-workman fuses **two signals on every
turn** — a screenshot *and* the [AT-SPI2](https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/)
accessibility tree — so an action can be addressed semantically:

> "click the push button named **Continue**"

…and land on that button's center, wherever it moved to. This is the same principle behind
macOS AXUIElement-based computer use, ported to Linux via AT-SPI2 (`gi.repository.Atspi`).

| Approach | Addressing | Survives layout change | Used here |
|---|---|---|---|
| Pixel-only automation (raw xdotool, image matching) | guessed `x, y` | ❌ brittle | fallback only |
| **Accessibility-tree automation** | role + name → element | ✅ robust | **default** |

## Tools

| Tool | What it does |
|------|--------------|
| `screenshot` / `screenshot_region` | Capture the display (or a sub-rectangle) as PNG; `max_dim` downscales for faster round-trips |
| `accessibility_tree` | Actionable elements as `{app, role, name, x, y, w, h}` in screen coordinates |
| `click_element` | Find an element by role + name and click its center — **no pixel guessing** |
| `enable_accessibility` | Turn on AT-SPI tree export (and silence the screen reader — see [safety](#speech-hazard-handled)) |
| `list_windows` | Windows with id, name, pid, geometry |
| `focus_window` | Raise a window by id or name; minimizes the frontmost blocker first |
| `click` / `move` / `drag` / `scroll` | Pointer actions at pixel coordinates |
| `type_text` / `press_key` | Keyboard input — `press_key` uses xdotool syntax (`Return`, `ctrl+c`, `super+l`, `KP_0`) |
| `show_cursor` | Visual click cursor overlay: a ring + click ripple showing exactly where the agent is acting |

## Watch what it clicks

`show_cursor` starts a transparent, click-through overlay that draws a cyan ring at the action
point and an expanding ripple on every click, with a label. It exists for **human oversight** —
you can watch an agent work and interrupt it, instead of guessing what it just did.

## Requirements

Linux with X11, plus:

```bash
sudo apt install xdotool ffmpeg gir1.2-atspi-2.0 python3-gi
# at-spi2-core is normally already running on GNOME/KDE
```

- `ffmpeg` — screen capture (x11grab)
- `xdotool` — mouse and keyboard input
- `python3-gi` + `gir1.2-atspi-2.0` — the accessibility tree (import is `gi.repository.Atspi`, **not** `pyatspi`)
- `Pillow` *(optional)* — only for `max_dim` screenshot downscaling
- GTK 3 *(optional)* — only for the `show_cursor` overlay

## Install

```bash
pip install claude-workman          # from PyPI
# or from source:
git clone https://github.com/AdvisorAGI/claude-workman.git
cd claude-workman && pip install -e .
```

## Usage

Run the stdio MCP server:

```bash
python -m workman.server
```

Register it with an MCP client — for Claude Code:

```bash
claude mcp add --scope user workman -- python -m workman.server
```

Target a specific X display with `WORKMAN_DISPLAY` (falls back to `DISPLAY`, then `:0`):

```bash
WORKMAN_DISPLAY=:1 python -m workman.server
```

## The accuracy loop

1. `screenshot` — and `enable_accessibility` + `accessibility_tree` when precision matters
2. **Locate** — prefer an element by role + name; fall back to pixels
3. **Act** — `click_element` when possible, else `click` / `type_text` / `press_key`
4. `screenshot` again to **verify** before anything irreversible

## Speech hazard (handled)

Exporting the accessibility tree from **Chromium and Electron** apps requires the desktop
screen-reader flag — and that same flag makes **Orca read the screen aloud** through
speech-dispatcher → espeak-ng. `enable_accessibility` therefore stops Orca immediately after
enabling, using an exact process-name match (`pkill -x orca`, never `pkill -f orca`, which
self-matches its own shell command and aborts).

GTK applications export their trees with `toolkit-accessibility` alone, so the web flag is
opt-in.

## FAQ

### What is claude-workman?
An open-source MCP server that lets an AI assistant control a Linux desktop the way a person
does — screenshot, locate, click, type, verify — with accessibility-tree targeting so clicks
hit real UI elements instead of guessed coordinates.

### How is this different from browser automation?
Browser tools (Playwright, Puppeteer, CDP) drive a web page. claude-workman drives the **whole
desktop**: native apps, terminals, file managers, settings dialogs, and browsers alike.

### Does it work with Claude Code?
Yes. Register it with `claude mcp add --scope user workman -- python -m workman.server`. It
works with any MCP client, not only Claude.

### Does it support Wayland or macOS?
Not yet — X11 today. A Wayland backend and a macOS AXUIElement backend are the natural next
steps, and contributions are welcome.

### Is it safe to let an agent control my desktop?
Treat it like handing over mouse and keyboard. Use `show_cursor` so you can see every action,
prefer a dedicated display or VM for unattended runs, and keep the verify-by-screenshot step
before anything irreversible.

### Why is it called Workman?
After the Walkman — a small portable thing that just plays. Point it at a display and it works.

### Does it solve CAPTCHAs?
No. It is a general desktop-control server, not an anti-bot bypass tool.

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Good first
areas: a Wayland backend, a macOS AXUIElement backend, richer AT-SPI role coverage, and tests.

## License

MIT © 2026 Atmosphere AI — see [LICENSE](LICENSE).

---

<sub>Keywords: MCP server, Model Context Protocol, computer use, desktop automation, Linux
automation, X11 automation, AT-SPI2 accessibility, GUI agent, AI agent desktop control,
xdotool alternative, screenshot automation, Claude MCP connector, agentic computer use.</sub>
