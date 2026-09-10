# claude-workman — computer use for Linux, macOS and Windows, as an MCP server

[![MCP Server](https://img.shields.io/badge/MCP-server-blue)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform: Linux | macOS | Windows](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](#platforms)

**claude-workman is an open-source [MCP](https://modelcontextprotocol.io) server that gives an AI
assistant human-mode control of a real desktop — it takes a screenshot, reads the
accessibility tree, then moves the mouse, clicks, types, and switches windows.**
One tool surface, three backends: X11 on Linux, Quartz on macOS, Win32 on Windows.
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

**Seeing**

| Tool | What it does |
|------|--------------|
| `screenshot` | Capture the display. Downscaled to the model's image budget, and **reports the scale** so coordinates map back to screen pixels |
| `zoom` | Magnify a region — re-grabbed from the live screen at full resolution, so it recovers detail the downscale threw away |
| `screenshot_region` | A sub-rectangle at native resolution, no magnification |
| `screen_info` | Screen size, monitor layout, image budget, last capture's scale |

**Accessibility — the accuracy layer**

| Tool | What it does |
|------|--------------|
| `accessibility_tree` | Actionable elements as `{app, role, name, x, y, w, h}` in screen coordinates |
| `click_element` | Find an element by role + name and click its center — **no pixel guessing** |
| `perform_element_action` | Invoke the element's *own* action (AXPress-style) — works where a synthetic click cannot reach |
| `set_element_value` | Set a field's contents directly; no keystroke timing, no autocomplete corruption |
| `element_actions` / `focused_element` / `wait_for_element` | What an element offers, what has focus, and blocking until something appears |
| `enable_accessibility` | Turn on AT-SPI tree export (and silence the screen reader — see [safety](#speech-hazard-handled)) |

**Input**

| Tool | What it does |
|------|--------------|
| `click` | Click at a coordinate, with optional `modifiers` (shift-click) and `count` (double/triple) |
| `move` / `hover` / `drag` / `scroll` | Pointer actions; `hover` waits for tooltips, `scroll` takes a position |
| `mouse_button` / `key_hold` | Hold and release separately — rubber-band selection, ctrl-click runs |
| `type_text` / `press_key` | Keyboard input — `press_key` uses xdotool syntax (`Return`, `ctrl+c`, `super+l`, `KP_0`) |
| `pointer_position` / `wait` | Where the pointer is; pause for the UI to settle |

**Windows, workspaces, apps, clipboard**

| Tool | What it does |
|------|--------------|
| `list_windows` | Windows with id, name, pid, geometry **and state** (minimized/maximized/fullscreen/active/workspace) |
| `window` | State changes: activate, minimize, maximize, fullscreen, above, pin, close |
| `window_geometry` | Move and/or resize (unmaximizes first, since a maximized window ignores geometry) |
| `focus_window` / `active_window` / `kill_window` | Raise by name, read focus, force-kill a stuck client |
| `workspace` | List, switch, or move a window to another virtual desktop |
| `launch_app` / `list_apps` / `terminate_app` | Start a program detached and wait for its window; list running or installable apps |
| `clipboard_get` / `clipboard_set` | Read/write the clipboard — the reliable way to enter long or exact strings |
| `batch` | Run several actions in one round-trip |
| `show_cursor` | Visual click cursor overlay: a ring + click ripple showing exactly where the agent is acting |

**Memory (token-light)**

| Tool | What it does |
|------|--------------|
| `cu_memory` | One tool, always `{ok, op, n, lines}` (~1600 chars). Working checklist, valid facts, history. Same shape for local Qwen and frontier models |
| `cu_skill_recall` | Short taught skills (`s:title \| app \| steps`). Call before inventing clicks |
| `cu_skill_teach` | Save one verified skill. No secrets |

### Coordinates, and why `zoom` exists

Every vision model shrinks an oversized image before it sees it. Hand over a 4K grab and let
that happen silently and the model reports coordinates in a space nobody recorded — the classic
"clicks land near the target, not on it".

So Workman resizes screenshots itself, to a budget you control
(`WORKMAN_IMAGE_MAX_EDGE`, default `1568`; high-resolution model tiers can take `2576`), and
every capture reports its `scale`. Read a coordinate off the image and pass it straight back
with `space="view"`, or multiply it yourself — but never guess.

Detail lost to that resize is gone, and enlarging the shrunken copy only invents pixels.
`zoom` therefore re-grabs the region from the live screen at **full resolution** and magnifies
that, which is what makes small text, icon labels and status bars legible.

## Watch what it clicks

`show_cursor` starts a transparent, click-through overlay that draws a cyan ring at the action
point and an expanding ripple on every click, with a label. It exists for **human oversight** —
you can watch an agent work and interrupt it, instead of guessing what it just did.

## Platforms

The MCP tools are identical everywhere; what changes underneath is the backend. Call
`workman_platform` once at the start of a session and the server tells you which one is
loaded, what `super` maps to, and the local gotcha to expect.

| | Linux (X11) | macOS | Windows |
|---|---|---|---|
| see (`screenshot`, `zoom`) | ffmpeg x11grab | `screencapture`, normalised from Retina pixels to points | GDI `BitBlt`, DPI-aware |
| input (`click`, `type_text`) | xdotool (XTEST) | Quartz `CGEvent`, else `cliclick`/System Events | `SendInput` |
| windows | xdotool + Wnck/EWMH | `CGWindowList` + System Events | `EnumWindows` + `SetWindowPos` |
| accessibility tree | AT-SPI2 | AXUIElement | not yet — use `chrome_*`/`bridge_*` for web UI |
| virtual desktops | yes | no public API (says so, with the shortcut to use) | no public API (same) |
| extra install | see below | nothing required; `pyobjc` strongly recommended | nothing required |

`super` is the one key that differs by hardware, so it is normalised: write `super+l` and it is
Super on Linux, **Command** on macOS, **Win** on Windows. `WORKMAN_BACKEND=linux|darwin|win32`
forces a backend, which is how the other two are unit-tested from one machine.

Two honest limits. There is no UI Automation backend on Windows yet, so `accessibility_tree`
and `click_element` refuse there instead of returning an empty tree that would send a model back
to guessing pixels. And on Wayland, XTEST input and x11grab only reach XWayland clients — the
backend reports `wayland: true` so you know before you act, rather than after.

### Linux requirements

X11, plus:

```bash
sudo apt install xdotool ffmpeg gir1.2-atspi-2.0 gir1.2-wnck-3.0 python3-gi
# at-spi2-core is normally already running on GNOME/KDE
```

- `ffmpeg` — screen capture (x11grab)
- `xdotool` — mouse and keyboard input
- `python3-gi` + `gir1.2-atspi-2.0` — the accessibility tree (import is `gi.repository.Atspi`, **not** `pyatspi`)
- `gir1.2-wnck-3.0` — window state and workspaces over EWMH: maximize, fullscreen, always-on-top,
  pin, graceful close. `xdotool` can move and raise a window but cannot set these, and `wmctrl`
  is not installed everywhere
- `Pillow` — screenshot resizing and `zoom`. Without it the raw tools still work, but captures
  are handed over full-size and the model's coordinates stop matching the screen
- GTK 3 — the clipboard tools and the `show_cursor` overlay

### macOS requirements

Nothing is strictly required: `screencapture`, `osascript` and `sips` all ship with macOS. Two
optional installs make it substantially better:

```bash
pip install "claude-workman[macos]"     # pyobjc: real CGEvent input + the AX tree
brew install cliclick                   # optional fallback for input without pyobjc
```

Then grant the app that *launches* Workman (Terminal, iTerm, or your MCP client — not Workman
itself, it has no bundle) two permissions in **System Settings > Privacy & Security**:

- **Screen Recording** — without it `screenshot` returns a black frame rather than an error
- **Accessibility** — without it synthetic clicks and keys are dropped silently

Restart that app after granting; the grant is read at process start.

### Windows requirements

None. The backend is `ctypes` against `user32`/`gdi32`, so a bare `pip install claude-workman`
is enough. `pip install "claude-workman[windows]"` adds Pillow, which only speeds up encoding
large screenshots. Run the MCP client as the same user that owns the desktop session; a service
account gets its own invisible window station and will capture a black screen.

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

### Does it support macOS and Windows?
Yes, since 0.3.0 — macOS through Quartz and AXUIElement, Windows through ctypes to user32/gdi32
with no third-party packages. See [Platforms](#platforms) for what each backend does and the two
gaps that remain (Windows accessibility, and native Wayland).

### Does it support Wayland?
Only through XWayland. A native Wayland backend (portal-based capture and libei input) is the
next backend on the list; the platform layer added in 0.3.0 is what makes it a drop-in.

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
