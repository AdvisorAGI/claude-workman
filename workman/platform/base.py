"""The desktop contract every backend implements, plus the helpers they share.

CONTRACT is enforced by tests, not by inheritance: the backends are plain
modules (that is what the original X11 backend was, and callers already import
them that way), so parity is checked as "every name in CONTRACT exists and is
callable" rather than through an ABC nobody can instantiate.

Two rules hold across all three backends:

* Coordinates are *screen* pixels of the whole virtual desktop, top-left origin.
  A backend whose native capture is denser than its coordinate space (macOS
  Retina) must normalise the image, not the coordinates, or every click lands at
  half or double the distance.
* A capability the OS cannot offer returns `unsupported(...)` — a normal result
  dict with ok=False and a hint. It never raises, because an MCP tool that
  raises tells the model nothing about what to do instead.
"""
from __future__ import annotations

import struct
import zlib

#: Names a backend module must expose. Kept in dependency order: see, then
#: windows, then input, so a partially-implemented backend fails loudly early.
CONTRACT: tuple[str, ...] = (
    # see
    "screen_size",
    "screenshot",
    "monitors",
    # windows
    "list_windows",
    "focus_window",
    "active_window",
    "kill_window",
    # pointer
    "click",
    "click_with",
    "move",
    "drag",
    "hover",
    "scroll",
    "scroll_at",
    "pointer_position",
    "mouse_down",
    "mouse_up",
    # keyboard
    "type_text",
    "press_key",
    "key_down",
    "key_up",
    # environment
    "clipboard_get",
    "clipboard_set",
    "display_name",
    "platform_info",
)

#: Capabilities a backend MAY add. The facade answers `unsupported(...)` for any
#: it does not find, which is how a desktop without virtual desktops (macOS
#: Spaces are not scriptable) says so without every caller branching on the OS.
OPTIONAL: tuple[str, ...] = (
    "list_windows_rich",
    "window_action",
    "window_geometry",
    "workspaces",
    "set_workspace",
    "move_to_workspace",
)

#: Canonical modifier names. `super` is the OS "command" key: Command on macOS,
#: Win on Windows, Super on Linux — one word so a model does not have to know
#: which desktop it is driving.
MODIFIERS = {"ctrl", "control", "alt", "shift", "super", "meta"}

#: Everything a caller might reasonably type, mapped to the canonical name.
MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "^": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "super": "super",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "windows": "super",
    "meta": "meta",
}


class Unsupported(Exception):
    """Raised only inside a backend; tools convert it with `unsupported()`."""


def unsupported(feature: str, platform: str, hint: str = "") -> dict:
    """The honest no-op result. Always shaped like a normal tool reply."""
    out = {
        "ok": False,
        "error": f"{feature} is not available on {platform}",
        "unsupported": True,
        "platform": platform,
    }
    if hint:
        out["hint"] = hint
    return out


def normalize_modifiers(mods: list[str] | None) -> tuple[list[str], list[str]]:
    """Canonicalise modifier names. Returns (canonical, unknown)."""
    canonical: list[str] = []
    unknown: list[str] = []
    for raw in mods or []:
        name = (raw or "").strip().lower()
        if not name:
            continue
        mapped = MODIFIER_ALIASES.get(name)
        if mapped is None:
            unknown.append(raw)
        elif mapped not in canonical:
            canonical.append(mapped)
    return canonical, unknown


def split_combo(key: str) -> tuple[list[str], str]:
    """Split xdotool-style 'ctrl+shift+t' into (['ctrl','shift'], 't').

    The last segment is the base key even when it is itself a modifier name
    ('ctrl+super' holds ctrl and taps super), and a bare '+' survives because
    only separators between non-empty segments are split on.
    """
    parts = [p for p in str(key).split("+") if p != ""]
    if not parts:
        return [], str(key)
    if len(parts) == 1:
        return [], parts[0]
    mods, unknown = normalize_modifiers(parts[:-1])
    if unknown:
        # A non-modifier prefix means this was never a combo (e.g. a literal
        # 'a+b'); treat the whole string as the base key and let the backend's
        # key table reject it with a useful message.
        return [], str(key)
    return mods, parts[-1]


# ---- image helpers ---------------------------------------------------------
# A backend that captures raw pixels (Windows GDI) still has to hand back PNG,
# and Pillow is an optional extra, so the encoder lives here in pure stdlib.


def png_encode(width: int, height: int, rgb: bytes) -> bytes:
    """Encode RGB8 rows (width*height*3 bytes, top row first) as PNG."""
    if len(rgb) != width * height * 3:
        raise ValueError(f"expected {width * height * 3} bytes of RGB, got {len(rgb)}")
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)  # filter type 0 (None): cheapest, zlib does the work
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def bgra_to_rgb(buf: bytes, width: int, height: int, stride: int | None = None,
                bottom_up: bool = False) -> bytes:
    """Convert a BGRA/BGRX framebuffer (what GDI hands back) to packed RGB."""
    stride = stride or width * 4
    out = bytearray(width * height * 3)
    for y in range(height):
        src_y = (height - 1 - y) if bottom_up else y
        row = buf[src_y * stride: src_y * stride + width * 4]
        o = y * width * 3
        out[o:o + width * 3] = bytes(b for i in range(0, len(row), 4)
                                     for b in (row[i + 2], row[i + 1], row[i]))
    return bytes(out)


def downscale_rgb(rgb: bytes, width: int, height: int,
                  max_dim: int) -> tuple[bytes, int, int]:
    """Nearest-neighbour downscale so no side exceeds max_dim.

    Only reached when Pillow is absent. Nearest-neighbour is the right trade
    here: it is fast enough in pure Python for one frame, and a screenshot is
    read for layout and text position, not for photographic quality.
    """
    longest = max(width, height)
    if max_dim <= 0 or longest <= max_dim:
        return rgb, width, height
    scale = max_dim / longest
    new_w = max(1, int(width * scale))
    new_h = max(1, int(height * scale))
    cols = [min(width - 1, int(x / scale)) * 3 for x in range(new_w)]
    out = bytearray(new_w * new_h * 3)
    for y in range(new_h):
        src_row = min(height - 1, int(y / scale)) * width * 3
        o = y * new_w * 3
        for i, c in enumerate(cols):
            s = src_row + c
            out[o + i * 3: o + i * 3 + 3] = rgb[s:s + 3]
    return bytes(out), new_w, new_h


def resize_png(data: bytes, max_dim: int | None) -> bytes:
    """Downscale PNG bytes so no side exceeds max_dim. No-op without Pillow.

    Returning the original on failure is deliberate: an oversized-but-correct
    image degrades the caller's coordinate math (it still gets the reported
    scale from the server layer), whereas a raised exception loses the frame.
    """
    if not max_dim:
        return data
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(data))
        scale = min(1.0, max_dim / max(im.size))
        if scale >= 1.0:
            return data
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return data
