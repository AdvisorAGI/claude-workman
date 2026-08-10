# Contributing to claude-workman

Thanks for helping build open-source computer use for Linux.

## Good first contributions

- **Wayland backend** — the biggest gap. Portal-based screencast + `libei`/`ydotool` input.
- **macOS backend** — AXUIElement + ScreenCaptureKit, mirroring the AT-SPI layer.
- **Richer AT-SPI coverage** — more roles in `ACTIONABLE`, better name/label resolution.
- **Tests** — the backends are shell-driven, so fakes around `_run()` go a long way.
- **Docs** — real-world recipes for driving specific apps.

## Development setup

```bash
git clone https://github.com/AdvisorAGI/claude-workman.git
cd claude-workman
sudo apt install xdotool ffmpeg gir1.2-atspi-2.0 python3-gi
pip install -e .
python -m workman.server        # stdio MCP server
```

Useful during development:

```bash
python -m workman.cursor        # run the visual cursor overlay on its own
python scripts/atspi-dump.py    # dump the accessibility tree
```

## Design principles

1. **Accessibility first, pixels second.** New actions should be addressable by role + name
   before falling back to coordinates.
2. **See → locate → act → verify.** Anything irreversible should be verifiable by a screenshot.
3. **Never surprise the operator.** Side effects that change the desktop (like turning on the
   screen-reader flag) must be reversible and documented.
4. **Degrade gracefully.** Optional dependencies (Pillow, GTK) must never break the core tools.

## Pull requests

- Keep changes focused; explain the behaviour change in the description.
- Match the surrounding style (standard library first, small helpers, docstrings that state the
  gotcha rather than the obvious).
- Note any new system dependency in `README.md` under Requirements.

## Scope

claude-workman is a general desktop-control server. Contributions specifically aimed at
defeating anti-bot systems or CAPTCHAs will not be accepted.
