# Changelog

All notable changes to claude-workman.

## [Unreleased]

### Added — workman-mode 1/4 agent strip

- **`layout_agent`** — park the session app (the terminal or chat running the
  agent) in a full-height quarter of the working area; the app being driven
  fills the other three quarters. `side` is left (default) or right. `work` is
  optional. Also `layout_arrange({"layout":"agent",...})` and aliases
  `strip` / `workman`.

## [0.3.0] — 2026-09-02

### Added — macOS and Windows

Workman is no longer Linux-only. The tool surface is unchanged; a platform layer
underneath picks the backend for the host.

- **`workman/platform/`** — a documented backend contract (`base.CONTRACT`) with
  three implementations: `linux_x11` (the original ffmpeg + xdotool code,
  re-exported unchanged), `darwin`, and `win32`. Selection is by `sys.platform`
  and overridable with `WORKMAN_BACKEND=linux|darwin|win32`, which is how the
  macOS and Windows backends are unit-tested from a Linux CI box.
- **`workman/desktop.py`** — the facade every caller now uses instead of
  importing `x11` directly.
- **macOS backend** — `screencapture` for seeing, Quartz `CGEvent` for input
  (falling back to `cliclick`, then System Events), `CGWindowList` for windows,
  System Events for window state and geometry, `pbcopy`/`pbpaste` for the
  clipboard. Captures are normalised from Retina backing pixels to points, so a
  coordinate read off a screenshot is the coordinate to click.
- **Windows backend** — `ctypes` only, no third-party packages: GDI `BitBlt`
  capture with a pure-stdlib PNG encoder, `SendInput` for mouse and keyboard
  (`KEYEVENTF_UNICODE`, so typing is keyboard-layout independent),
  `EnumWindows`/`SetWindowPos` for windows, per-monitor DPI awareness declared at
  import. Coordinates are relative to the virtual desktop's top-left, so they
  match screenshot pixels even with a monitor left of the primary one.
- **`workman/a11y.py`** — accessibility facade over AT-SPI2 (Linux) and a new
  AXUIElement backend (macOS, `workman/platform/ax_darwin.py`). Same vocabulary
  on both: `click_element("Continue", role="push button")`.
- **`workman_platform` tool** — reports the active backend, what `super` maps to,
  which helper binaries are present, the accessibility layer, and the local
  gotcha (Wayland; Screen Recording / Accessibility grants). `screen_info` now
  carries `backend` and `modifier_super` too.
- **Cross-platform `launch_app` / `list_apps` / `terminate_app`** — `.desktop`
  entries on Linux, `.app` bundles on macOS, Start Menu shortcuts on Windows.
- **66 new tests** covering contract parity across all three backends, backend
  selection, the key translation tables, the PNG encoder, BGRA conversion and
  downscaling, and that an unsupported capability returns a readable result
  instead of raising.

### Changed

- `press_key` accepts xdotool syntax on every platform and translates it, with
  `super` normalised to Command on macOS and Win on Windows. Modifier aliases
  (`cmd`, `command`, `option`, `win`) are accepted everywhere.
- A capability the OS cannot provide now returns `{"ok": false,
  "unsupported": true, "hint": ...}` rather than raising — virtual desktops on
  macOS and Windows, fullscreen toggling on Windows, accessibility on Windows.

### Known gaps

- No UI Automation backend on Windows yet: `accessibility_tree`,
  `click_element` and friends refuse there. Web UI is still element-addressable
  through the `chrome_*` and `bridge_*` tools on every OS.
- Native Wayland (portal capture, libei input) is not implemented; XWayland
  clients work today and the backend reports `wayland: true`.
- `show_cursor` (the click overlay) remains Linux/GTK only.
- The macOS and Windows backends are verified by contract and unit tests plus
  API review; they have not yet been run against a physical Mac or Windows
  desktop.

## [0.2.0]

- Human Mode (OS-level human cadence), the Chrome bridge, autoscroll-and-read.
