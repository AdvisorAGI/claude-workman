"""Vision budget and zoom — reading fine detail without lying about coordinates.

Two facts drive this module.

**1. Silent downscaling breaks clicks.** Every vision model shrinks an oversized
image before it sees it. A 3840x2160 grab handed over raw comes back with
coordinates in a space nobody recorded, and clicks land *near* the target instead
of on it. So Workman does the resize itself and reports the scale it used; the
caller can always map a view coordinate back to a screen pixel.

**2. Detail lost to that downscale is unrecoverable.** Upscaling the shrunken
copy invents pixels. `zoom` therefore re-grabs the region from the live screen at
full resolution and magnifies *that*, which is the whole point of the action.

Budgets are deliberately not pinned to one vendor. `WORKMAN_IMAGE_MAX_EDGE` sets
the long-edge limit for whichever model is driving; the 1568px default is the
conservative tier that every current vision model accepts, and high-resolution
tiers can raise it (2576 at the time of writing).
"""
from __future__ import annotations

import io
import os
import time

# Long-edge budgets, in pixels. Conservative default so any model can consume the
# result; raise via env for a high-resolution tier.
DEFAULT_MAX_EDGE = 1568
_TIERS = {"standard": 1568, "high": 2576}

# JPEG for magnified crops: zoom results accumulate in a conversation and full
# budget PNGs blow past request size limits. subsampling=0 keeps chroma at full
# resolution, which is what makes thin chart lines and small text survive.
_JPEG_QUALITY = 92
_JPEG_SUBSAMPLING = 0


class VisionUnavailable(RuntimeError):
    """Pillow is missing — screenshots still work, resizing and zoom do not."""


def _pillow():
    try:
        from PIL import Image
    except ImportError as exc:  # degrade gracefully, per design principle 4
        raise VisionUnavailable(
            "Pillow is required for resizing and zoom — pip install Pillow"
        ) from exc
    return Image


def max_edge(override: int | None = None) -> int:
    """Resolve the long-edge pixel budget: explicit > env > default."""
    if override:
        return int(override)
    raw = (os.environ.get("WORKMAN_IMAGE_MAX_EDGE") or "").strip()
    if raw:
        if raw.lower() in _TIERS:
            return _TIERS[raw.lower()]
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_MAX_EDGE


def fit(width: int, height: int, edge: int, allow_upscale: bool = False) -> tuple[int, int]:
    """Largest aspect-preserving size whose long edge is within `edge`."""
    if width <= 0 or height <= 0:
        return (max(width, 1), max(height, 1))
    scale = edge / max(width, height)
    if not allow_upscale:
        scale = min(1.0, scale)
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def view_size(width: int, height: int, edge: int | None = None) -> tuple[int, int]:
    """The size a model will actually see — downscale only, never enlarge."""
    return fit(width, height, max_edge(edge), allow_upscale=False)


def zoom_size(width: int, height: int, edge: int | None = None) -> tuple[int, int]:
    """The largest in-budget size for a crop, upscaling small regions so the
    magnified detail actually occupies the visual token budget."""
    return fit(width, height, max_edge(edge), allow_upscale=True)


def to_view(data: bytes, edge: int | None = None) -> tuple[bytes, dict]:
    """Downscale a PNG grab to the model's budget.

    Returns (png_bytes, meta) where meta carries the scale factor needed to turn
    a view coordinate back into a screen pixel. `scale` is view/screen, so
    screen_x = view_x / scale.
    """
    Image = _pillow()
    im = Image.open(io.BytesIO(data))
    src_w, src_h = im.size
    dst_w, dst_h = view_size(src_w, src_h, edge)
    meta = {
        "screen": [src_w, src_h],
        "view": [dst_w, dst_h],
        "scale": round(dst_w / src_w, 6) if src_w else 1.0,
        "resized": (dst_w, dst_h) != (src_w, src_h),
    }
    if not meta["resized"]:
        return data, meta
    im = im.convert("RGB") if im.mode not in ("RGB", "RGBA", "L") else im
    im = im.resize((dst_w, dst_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), meta


def magnify(data: bytes, edge: int | None = None) -> tuple[bytes, dict]:
    """Scale a full-resolution crop up to the budget and encode it as JPEG."""
    Image = _pillow()
    im = Image.open(io.BytesIO(data))
    src_w, src_h = im.size
    dst_w, dst_h = zoom_size(src_w, src_h, edge)
    if (dst_w, dst_h) != (src_w, src_h):
        im = im.resize((dst_w, dst_h), Image.Resampling.LANCZOS)
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=_JPEG_QUALITY, subsampling=_JPEG_SUBSAMPLING)
    return buf.getvalue(), {
        "crop": [src_w, src_h],
        "magnified": [dst_w, dst_h],
        "magnification": round(dst_w / src_w, 2) if src_w else 1.0,
    }


def normalize_region(x1: int, y1: int, x2: int, y2: int,
                     bounds: tuple[int, int]) -> tuple[int, int, int, int]:
    """Clamp a top-left/bottom-right rect into `bounds`, tolerating swapped
    corners. Raises ValueError if nothing is left of it."""
    max_w, max_h = bounds
    left, right = sorted((int(x1), int(x2)))
    top, bottom = sorted((int(y1), int(y2)))
    left, right = max(0, min(left, max_w)), max(0, min(right, max_w))
    top, bottom = max(0, min(top, max_h)), max(0, min(bottom, max_h))
    if right - left < 1 or bottom - top < 1:
        raise ValueError(
            f"region [{x1},{y1},{x2},{y2}] is empty after clamping to {max_w}x{max_h}"
        )
    return left, top, right - left, bottom - top


def scale_region(region: tuple[int, int, int, int], scale: float) -> tuple[int, int, int, int]:
    """Convert a view-space rect to screen space (scale is view/screen)."""
    if not scale or scale == 1.0:
        return region
    return tuple(round(v / scale) for v in region)  # type: ignore[return-value]


def save(data: bytes, suffix: str = "png", directory: str | None = None) -> str:
    """Write an image where a human can open it, and return the path.

    Mirrors the CLI's `save_to_disk`: only worth doing when the image is meant to
    be shared, since every saved frame is a file someone has to clean up.
    """
    directory = directory or os.environ.get(
        "WORKMAN_IMAGE_DIR", os.path.expanduser("~/.cache/workman/shots")
    )
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"workman-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.{suffix}")
    with open(path, "wb") as handle:
        handle.write(data)
    return path
