# Changelog

All notable changes to claude-workman.

## [Unreleased]

### Added — orchestra tools (one place to see and steer every session)

- **`sessions_list`**, **`session_needs`**, **`session_usage`**, **`session_events`**
  (`workman/orchestra_tools.py` over the `workman/orchestra/` package,
  `workman orchestra-list|needs|usage|events`) —
  every Claude Code session on the machine from its transcript, the CLI's live
  registry (`<config>/sessions/<pid>.json`), the desktop app's session store and
  /proc: account (email only), model and effort, context % of 1M, state
  (working / idle / needs_input / blocked / silent / limit_hit), front end
  (desktop, Remote Control, headless, tmux) and pid. Usage is deduped per
  message id and exposes repeated first prompts. Zero model calls.
- **`workman orchestra-collect`** — a zero-model collector for a 30-60 s timer:
  appends question, permission, blocked, limit_hit, silent, compact_ready and
  finished events to `~/orchestra/session-events.jsonl`, deduped by key, with a
  monotonic cursor. Consolidates the orchestra watchers and the hook feed.
- **`session_compact`** (`workman orchestra-compact`) — compacts ANOTHER session
  from its front end only at a clean turn and inside a token band: desktop via
  the `front_compact` runner, Remote Control via claude.ai/code in Chrome,
  headless refused. Dry run by default; refuses the caller's own session.
- **`session_set_model_effort`** (`workman orchestra-set-model`) — the same
  guards, through the composer-footer picker; Fable only with `allow_fable=True`;
  a model switch is refused above 400k context; headless seats are told the
  `run-claude-seat --resume` line instead.
- Every control action refuses when the calling session cannot be identified
  (env, Claude ancestor in the live registry, or `self_id`), blocks on a
  registry `waiting` as well as `busy`, clamps the caller's band into the
  300k-400k policy, and rechecks the full turn guards (main chain only) right
  before the first click.
- `session_front_compact` now runs through `session_compact`'s guards, and so
  do the runner CLIs (`python -m workman.front_compact`, `rc-compact`):
  band included, `--force` only with a logged `--reason`. The resume line
  after a compaction is typed only after the same turn check passes again.
  An explicit self id must be a session id (>= 8 hex chars).

### Added — front-end compaction (`session_front_compact`)

- **`session_front_compact`** / `python -m workman.front_compact` — compacts a
  Claude desktop session from its front end on :0: waits until the transcript
  shows the turn ended, defers while vlc or mpv runs, fronts the session by its
  deep link, refuses unless the header hash on screen matches, types /compact,
  confirms a new compact_boundary, then sends a one-line resume. The tool starts
  a detached runner and returns at once (the caller may be the target). Header
  hashes are learned with `--learn` into `~/.workman/front-compact/headers.json`;
  every outcome is one JSON line in `~/orchestra/compact-log.jsonl`.

### Added — skill finder (load skills on demand)

- **`skill_search`**, **`skill_show`**, **`skill_add`**, **`skill_reindex`** — a
  session no longer has to load every SKILL.md up front. `skill_search` ranks
  every skill under `~/.claude/skills` and `$WORKMAN_SKILL_ROOTS` by name,
  headings and description and returns names and short descriptions only.
  `skill_show` loads one skill, or one `## section` of it, and lists the skill's
  other files. `skill_add` copies a validated skill directory into a skill root
  and never overwrites. The index is cached at
  `~/.cache/workman/skills-index.json` and rebuilds itself when any SKILL.md is
  added, removed or modified.

### Added — Workman Desk (composer + CLI seats)

- **`workman-desk`** / `python -m workman.desk` — a localhost app (macOS, Linux,
  Windows) with chats and a Monday-style board. Agent picker: Workman, Grok,
  Claude, Codex, Cursor. Send runs the **logged-in CLI**, not an API key.
  Loopback only. The server detaches so the Chrome window does not go dead when
  the launching terminal exits. Chats keep a transcript (Grok also uses
  `--session-id` / `--resume`). The board has Backlog / Working / Blocked / Done;
  each item can be assigned to an agent and **Run** starts that agent on the
  brief.

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
