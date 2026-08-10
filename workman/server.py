"""claude-workman — an MCP connector for human-mode desktop control on Linux/X11.

Exposes see / click / type / window / accessibility tools over MCP (stdio) so any
MCP client (Claude, etc.) can drive a real desktop the way a person does:
screenshot -> locate (by pixel OR accessibility element) -> act -> screenshot to
verify. The AT-SPI layer makes clicks element-accurate instead of pixel-guessed.

Run:  python -m workman.server        (stdio MCP server)
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP, Image

from . import atspi, x11

mcp = FastMCP("claude-workman")


# ---- SEE -------------------------------------------------------------------
@mcp.tool()
def screenshot(max_dim: int | None = None) -> Image:
    """Capture the whole display as a PNG. max_dim downscales the long side
    (e.g. 1600) for faster round-trips. Take one before AND after any action."""
    return Image(data=x11.screenshot(max_dim=max_dim), format="png")


@mcp.tool()
def screenshot_region(x: int, y: int, w: int, h: int) -> Image:
    """Capture a sub-rectangle of the screen (faster than a full grab)."""
    return Image(data=x11.screenshot(region=(x, y, w, h)), format="png")


# ---- WINDOWS ---------------------------------------------------------------
@mcp.tool()
def list_windows() -> list[dict]:
    """List on-screen windows with id, name, pid and geometry."""
    return x11.list_windows()


@mcp.tool()
def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    """Raise a window by id or name-substring. On focus-stealing WMs (e.g.
    mutter) plain activation can silently fail, so the frontmost blocker is
    minimized first. Always screenshot to verify focus before typing."""
    return x11.focus_window(query, minimize_blockers=minimize_blockers)


# ---- MOUSE -----------------------------------------------------------------
@mcp.tool()
def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    """Click at pixel (x, y). button 1=left 2=middle 3=right. count=2 double."""
    return x11.click(x, y, button=button, count=count)


@mcp.tool()
def move(x: int, y: int) -> dict:
    """Move the pointer to (x, y) without clicking."""
    return x11.move(x, y)


@mcp.tool()
def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    """Press at (from_x, from_y), drag to (to_x, to_y), release."""
    return x11.drag(from_x, from_y, to_x, to_y)


@mcp.tool()
def scroll(direction: str, amount: int = 3) -> dict:
    """Scroll up|down|left|right by `amount` wheel steps."""
    return x11.scroll(direction, amount=amount)


# ---- KEYBOARD --------------------------------------------------------------
@mcp.tool()
def type_text(text: str, delay_ms: int = 40) -> dict:
    """Type literal text into the focused window."""
    return x11.type_text(text, delay_ms=delay_ms)


@mcp.tool()
def press_key(key: str) -> dict:
    """Press a key/combo in xdotool syntax: 'Return', 'Tab', 'ctrl+c',
    'super+l', 'KP_0'."""
    return x11.press_key(key)


# ---- ACCESSIBILITY (the accuracy layer) ------------------------------------
@mcp.tool()
def enable_accessibility(enable_web: bool = True) -> dict:
    """Turn on AT-SPI tree export. enable_web also lets Chromium/Electron export
    their web content. Orca is killed afterwards so nothing is spoken aloud."""
    return atspi.ensure_a11y(enable_web=enable_web, silence=True)


@mcp.tool()
def accessibility_tree(app: str | None = None, actionable_only: bool = True) -> list[dict]:
    """Dump actionable UI elements as {app, role, name, x, y, w, h} with screen
    coordinates. Filter by app name substring. Call enable_accessibility first."""
    return atspi.tree(app=app, actionable_only=actionable_only)


@mcp.tool()
def click_element(name: str, role: str | None = None, app: str | None = None) -> dict:
    """Find an element by role+name in the accessibility tree and click its
    center — element-accurate, no pixel guessing."""
    return atspi.click_element(name, role=role, app=app)


@mcp.tool()
def show_cursor(on: bool = True) -> dict:
    """Start/stop the visual click cursor overlay — a Codex-style ring + click
    ripple that shows on the monitor exactly where Workman is acting (for human
    oversight). Requires GTK (python3-gi). Actions auto-notify it once running."""
    import subprocess
    import sys
    from . import cursor as _cursor
    if on:
        _cursor.fifo_path()
        subprocess.Popen([sys.executable, "-m", "workman.cursor"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "overlay": "starting", "fifo": _cursor.FIFO}
    try:
        with open(_cursor.FIFO, "w") as f:
            f.write("quit\n")
    except OSError:
        pass
    return {"ok": True, "overlay": "stopped"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
