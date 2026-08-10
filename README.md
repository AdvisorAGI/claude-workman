# claude-workman

**Human-mode desktop control as an MCP connector — for Linux/X11.**

Workman lets any [MCP](https://modelcontextprotocol.io) client (Claude and
others) drive a real desktop the way a person does: **see the screen, move the
mouse, click, type, and switch windows** — and, crucially, **act on the
accessibility tree** so clicks target UI *elements* by role and name instead of
guessed pixel coordinates.

Named after the Walkman: a small, portable thing that just plays. Point it at a
display and it works.

## Why the accessibility layer matters

Blind pixel automation drifts the moment a layout shifts. Workman fuses two
signals every turn — a screenshot **and** the AT-SPI2 accessibility tree — so a
click can be addressed as *"the push button named 'Continue'"* and land on its
center. This is the same principle Apple's AXUIElement-based tools use on macOS,
ported to Linux via AT-SPI2 (`gi.repository.Atspi`).

## Tools

| Tool | What it does |
|------|--------------|
| `screenshot` / `screenshot_region` | Grab the display (or a sub-rect) as PNG; `max_dim` downscales for speed |
| `list_windows` | Windows with id, name, pid, geometry |
| `focus_window` | Raise a window by id/name; minimizes the frontmost blocker first (for focus-stealing WMs like mutter) |
| `click` / `move` / `drag` / `scroll` | Pointer actions at pixel coordinates |
| `type_text` / `press_key` | Keyboard input (`press_key` uses xdotool syntax: `Return`, `ctrl+c`, `super+l`, `KP_0`) |
| `enable_accessibility` | Turn on AT-SPI tree export (and silence the screen reader — see below) |
| `accessibility_tree` | Actionable elements as `{app, role, name, x, y, w, h}` in screen coords |
| `click_element` | Find an element by role+name and click its center — no pixel guessing |
| `show_cursor` | Start/stop a **visual click cursor** overlay — a Codex-style ring + click ripple that shows on the monitor exactly where Workman is acting, for human oversight (needs GTK / python3-gi) |

## Install

System dependencies (Debian/Ubuntu):

```bash
sudo apt install xdotool ffmpeg gir1.2-atspi-2.0 python3-gi
# accessibility bus is usually already running (at-spi2-core)
```

Python:

```bash
pip install claude-workman            # or: pip install -e .
```

## Run

```bash
python -m workman.server              # stdio MCP server
```

Register with an MCP client, e.g. Claude Code:

```bash
claude mcp add --scope user workman -- python -m workman.server
```

Set `WORKMAN_DISPLAY` (or `DISPLAY`) to target a specific X display (defaults to
`:0`).

## The accuracy loop

1. `screenshot` (optionally `enable_accessibility` + `accessibility_tree`)
2. locate — prefer an element by role+name; fall back to pixels
3. act — `click_element` when possible, else `click`/`type_text`/`press_key`
4. `screenshot` again to verify before anything irreversible

## ⚠️ Speech hazard (handled, worth knowing)

Making Chromium/Electron export their web accessibility tree requires the
desktop **screen-reader flag** — and that same flag makes **Orca narrate the
screen aloud** (speech-dispatcher → espeak-ng). `enable_accessibility` therefore
kills Orca immediately after enabling. It does so by **exact process name**
(`pkill -x orca`) — never `pkill -f orca`, which self-matches its own shell
command and aborts. GTK apps export their trees on `toolkit-accessibility` alone,
so the web flag is opt-in.

## Notes / limits

- Linux/X11 today. Wayland and a macOS AXUIElement backend are natural follow-ups.
- `focus_window` copes with mutter's focus-stealing prevention by minimizing the
  blocker; still, **always screenshot to verify focus before typing**.
- `screenshot` needs `ffmpeg`; input needs `xdotool`; the accuracy layer needs
  AT-SPI (`python3-gi` + `gir1.2-atspi-2.0`). `Pillow` is only needed for
  `max_dim` downscaling.

## License

MIT © 2026 Atmosphere AI
