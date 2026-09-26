#!/usr/bin/env python3
"""Let a small model drive this desktop through a deliberately tiny tool surface.

Three things reliably break a 30B-class model on a computer-use loop: too many
tools, coordinate-space arithmetic, and forgetting to look after it acts. This
harness removes all three. The model sees nine tools, it reads coordinates
straight off the picture it was just shown (the harness owns the scale factor),
and every action hands back a fresh screenshot whether the model asked for one
or not.

Actions go through the workman CLI (`~/.claude/tools/wm.py`), run by the
interpreter that has a backend for this machine. On macOS that is the
computer-mcp venv, whose wm.py imports `atmos_computer.server` and forwards
every verb to the Workman.app daemon — the only surface here that can actually
move the pointer. The run prints the resolved `cli_backend` first, so a wrong
`--python` is obvious before anything moves.

    ~/claude-atmos-workman/.venv/bin/python scripts/small_model_drive.py \
        --endpoint http://127.0.0.1:8081/v1 --model muse-glimmer-30b \
        --task "Open example.com and read me the heading."

The model endpoint is anything OpenAI-compatible that takes tool calls and
image parts — llama-server with --jinja, say. `--mock` swaps in a fake desktop
and a scripted model, which is what the tests drive.

Three defaults come from measuring Meta's Muse-Glimmer-30B on llama-server
rather than from taste, and each has a flag to turn it back off:

* `--coord-grid 1000` — Muse answers points on a 0-1000 grid over the picture
  and ignores whatever the prompt says its pixel size is, so the harness, not
  the model, turns a grid point into image pixels. `--coord-grid pixels` is the
  older contract, for a model that really does read pixels.
* `--action-mode json` — an assistant message whose content is a bare `{`, sent
  last, is the only thing that stops Muse reasoning: 2-8 s a step instead of
  20-40 s, and no empty replies. That prefill cannot be combined with native
  tool calls, so the tool definitions ride in the system prompt as a JSON
  schema and the model answers `{"tool": ..., "args": {...}}`. `--action-mode
  tools` is the original OpenAI tool-call path, unchanged.
* `--reasoning low` — the thinking dial is the chat-template kwarg
  `reasoning_strength`, never a boolean; it cannot be switched off at all.

* `--temp 0.1` - at 0.1 the same button came back at the same coordinates six
  times out of six, where the vendor's 1.0 preset is for open generation.
* `--send-width 1568` - at 1024 the model's vertical coordinates land about 145
  grid units high and repeat; at 1568 they are exact.

Messages are assembled so llama-server's prefix cache can hit: a fixed system
prompt, then text-only history, then the one current screenshot as the very
last content item. Each step logs `cached_tokens` so a miss is visible.

Four more behaviours come from watching it drive rather than from taste. The
pointer is parked in a corner before every frame that will be compared, because
a cursor resting on a row lights that row up for more than the no-change
threshold; the pointer is also blanked in both frames, so the glyph itself can
never be the change. A click that changes nothing does not get argued with in
words - measured, nine identical clicks in a row, every one promising to re-aim
- it gets a crop of that part of the screen with a red ring on the point that
missed, and one schema question whose answer is read on the crop's own grid. A
click that brings a different window to the front is a wrong target, not the
resounding success its diff looks like. And three steps in a row that change
nothing end the run rather than burning the budget.

Five more come from the peer's driver on the DGX, which is the one that has been
driving a real X11 screen, rather than from taste:

* `--backend wm|x11` - on the DGX wm.py's own click silently no-ops on some
  pages while `xdotool mousemove; sleep; click 1` lands, so `x11` keeps wm.py
  for the picture and gives the hands to xdotool. The same ten verbs either way;
  the default is `wm`, except on Linux with xdotool installed.
* `--escalate-to claude-seat` - a logged-in `claude -p` seat takes the step after
  two ineffective actions on one target, a give_up, or a stall.
  `--max-escalations` is a hard counter, and each hand-over clears the per-target
  failure counts so one stale count cannot spend them all.
* A key that is silent by nature (ctrl+a, ctrl+c, a bare modifier) and an action
  the backend refused are not evidence that the screen is dead, so neither
  lengthens the no-change streak. The peer gave up on a healthy picker after
  "ctrl+a, refused type, ctrl+a" before this existed.
* The focus question is asked about a 1000x120 strip round the field rather than
  the whole frame, where the same picture came back focused once and unfocused
  twice; if it says no, the field is clicked once and the question asked again.
* The action contract is FLAT - `{"tool": "click", "x": .., "y": .., "why": ..}`
  - and rides as a json_schema on every json-mode request, because that is what
  the muse-workman chat template was trained on. A run of ours against it carried
  a nested schema it had never seen and it answered `{"answer": "PHANTOM_UA"}`
  three times instead of calling done. The nested form still parses, a bare
  answer object is read as done and marked off contract, and a reply with no tool
  call now prints its raw text instead of vanishing.

Every step also writes one JSONL line in the shared trajectory format
(`muse-train/TRAJECTORY.md`) with its screenshot beside it, so a run is training
data for the LoRA and not just a log: the picture the action was decided on, the
action on the 0-1000 grid and in pixels, and what the next screenshot said about
it. `--trajectory-out` chooses where, or `none` turns it off. Two more things
follow from measuring the model rather than from taste: a type checks what has
focus first, because a keystroke with nothing focused hits the page's own
single-key shortcuts, and a step that moved no pixels says so in the text handed
back, because a model told only `Clicked (165,605)` will click there again.
"""
from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DEFAULT_WM = os.path.join(HOME, ".claude", "tools", "wm.py")
#: On macOS this venv's wm.py imports atmos_computer.server and talks to the
#: Workman.app daemon, which holds the Screen Recording + Accessibility grants.
MAC_PYTHON = os.path.join(HOME, "src", "claude-atmos-workman", "computer-mcp",
                          ".venv", "bin", "python")
#: How wide the screenshot the model is sent is. Measured, not chosen: on the
#: same task back to back, at 1024 the 30B model's vertical coordinates landed
#: about 145 grid units high and stayed there - nine identical clicks at
#: (164,510) for a link at (161,655) - while at 1568 the first click hit
#: (162,655) exactly. 1024 is still selectable with --send-width.
IMAGE_WIDTH = 1568
JPEG_QUALITY = 75
#: How many screenshots stay in the conversation as real image parts. Older ones
#: become a one-line placeholder: a 32k context holds about three of these.
KEEP_IMAGES = 2
#: In json mode the budget is one picture, because a second image part above the
#: newest one is exactly what stops llama-server reusing its prefix. A zoom crop
#: is exempt: it rides alongside the full screenshot it was cut from.
KEEP_IMAGES_JSON = 1
PARSE_FAILURE_LIMIT = 3
#: The normalized grid a vision model answers on: 0 is the left or top edge of
#: the picture, GRID the right or bottom one, whatever its pixel size.
GRID = 1000
#: The assistant turn that goes last in json mode. Sending a lone opening brace
#: forces the reply to be the object itself, with no reasoning in front of it.
ACTION_PREFILL = "{"
#: llama-server passes this straight to the chat template. There is no "off".
REASONING_LEVELS = ("low", "medium", "high", "xhigh")
#: A crop narrower than this is scaled up, but never by more than the cap:
#: past 2x a screenshot is inventing detail rather than showing it.
ZOOM_MIN_WIDTH = 320
ZOOM_MAX_UPSCALE = 2.0
#: The address-bar chord open_url sends. xdotool names throughout, as wm.py
#: expects. Note that wm.py maps `super` to Command on macOS but leaves `ctrl`
#: as Control, so on a Mac where ctrl+l does not reach Chrome's omnibox, pass
#: --url-bar-key super+l. The model never sees this either way.
URL_BAR_KEY = "ctrl+l"

#: Where a run's training lines land when --trajectory-out says nothing. The
#: DGX holds the corpus; every other machine writes the same layout locally and
#: rsyncs the run directory over.
TRAJECTORY_ROOT = os.path.join(HOME, "muse-train", "data", "trajectories")
#: The collector rejects a line whose task, history or typed text matches this,
#: so the harness never writes one in the first place.
CREDENTIAL_RE = re.compile(r"pass|token|secret|otp|2fa", re.I)
#: Two screenshots this close are the same picture: the action moved nothing.
NO_CHANGE_DIFF = 0.005
#: Both frames come down to this width before they are compared, so JPEG noise
#: and a moving cursor do not register while a repainted pane does.
DIFF_WIDTH = 256
#: A done call that admits it could not finish is a gave_up step, not an ok one.
GAVE_UP_RE = re.compile(r"could ?not|couldn.t|can ?not|can.t|unable|gave up|"
                        r"giving up|not able|failed to|no answer", re.I)
#: What a forced re-ground can act on. Only a click carries a point to zoom
#: around; a dead key or scroll is left to the streak rule.
REGROUND_TOOLS = ("click",)
#: How much of the screen a re-ground shows: a third of the width and half the
#: height. The peer's 400x200 was too small for us - measured, the miss is about
#: 157 screen points straight up, so a crop half that tall ends above the target
#: and the model is asked to find something that is not in the picture.
REGROUND_FRACTION_W = 3
REGROUND_FRACTION_H = 2
#: And the box sits low: the error is always upward, so the point the click
#: landed on belongs above the middle of the crop, not in it. A quarter of the
#: crop's height of downward bias puts the real target near the centre.
REGROUND_BIAS = 4
#: An answer this close to the point that already missed is the model reading
#: its own previous number back, not looking at the crop.
REGROUND_ECHO = 5
#: The one object a re-ground answer may be.
REGROUND_SCHEMA = {"type": "object",
                   "properties": {"target": {"type": "string"},
                                  "x": {"type": "integer"},
                                  "y": {"type": "integer"}},
                   "required": ["target", "x", "y"], "additionalProperties": False}
#: Blanked in both frames around the pointer before and after, so the cursor
#: glyph itself never counts as a change.
POINTER_BLANK = 48
#: Where the pointer is parked before every frame that will be diffed. Measured
#: on GitHub: the glyph is 0.0002 of the frame, but the row it hovers lights up
#: for 0.008 - over the threshold - and a mask wide enough to hide the highlight
#: also hides the checkmark the step is looking for. Parking takes the hover
#: with it: 0.00798 -> 0.00001.
PARK_MARGIN = (70, 80)
#: Consecutive steps that changed nothing before the run is called stuck. The
#: streak is per acting backend and resets when the backend changes.
NO_CHANGE_LIMIT = 3
#: Keys that are silent by nature. Select-all on an already-selected field, a
#: copy, a bare modifier: none of them repaints anything, so none of them is
#: evidence that the UI is dead. The peer gave up on a healthy screen after
#: "ctrl+a, refused type, ctrl+a" before this existed.
NEUTRAL_KEYS = frozenset({"ctrl+a", "ctrl+c", "shift", "ctrl", "alt", "super"})
#: What a step that changed nothing says when the key was one of those.
NEUTRAL_NOTE = "no visible change, which is normal for this key"
#: The crop the focus question is asked on, in full-resolution pixels around the
#: field's own point. The peer asked it on the whole frame and got True once and
#: False twice for the same picture; a strip round the field shows the caret or
#: the highlighted border and little else to be wrong about.
FOCUS_CROP_W, FOCUS_CROP_H = 1000, 120
#: The one object a focus answer may be.
FOCUS_SCHEMA = {"type": "object",
                "properties": {"text_field_focused": {"type": "boolean"},
                               "evidence": {"type": "string"}},
                "required": ["text_field_focused"], "additionalProperties": False}
#: How many steps the fallback seat may take in one run.
MAX_ESCALATIONS = 3
#: Per-target failures that hand the next step to the seat. Two is reached only
#: after the re-grounded retry also changed nothing.
ESCALATE_AT = 2
#: The seat model, and the numbers that make one `claude -p` call answer in one
#: turn. Measured by the peer: with its default tool set and the user's MCP
#: servers the seat spent every turn on its own session protocol and stopped on
#: tool_use with no result; read-only, no MCP servers, no user settings, it
#: answered in two turns.
SEAT_MODEL = "sonnet"
SEAT_MAX_TURNS = 6
SEAT_TIMEOUT = 180
#: How many small steps the X11 pointer takes to cross the screen. Native UI
#: only reacts to motion that looks like motion; one teleport is ignored by
#: hover controls.
X11_MOVE_STEPS = 4
#: xdotool's inter-keystroke delay, in milliseconds.
X11_TYPE_DELAY = 40
#: A live run outside this cgroup slice is refused on Linux.
GATE_SLICE = "muse-agents.slice"
EXIT_REFUSED = 3
#: What an observation says when the action did not actually happen.
ACTION_FAILED_MARKERS = ("failed", "refused", "not confirmed", "no coordinates")
#: Short fleet names, matched as a whole word in the hostname. Anything else
#: keeps the bare hostname, which is still a machine the corpus can group by.
MACHINE_NAMES = ("dgx", "air", "m1", "m3")


# --------------------------------------------------------------------- images

def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return int(w), int(h)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segments to the frame header. Reading the real dimensions
    matters because `sips` rounds the resampled height its own way."""
    if data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return int(w), int(h)
        i += 2 + length
    return None


def image_size(path: str) -> tuple[int, int] | None:
    """Real pixel size of a PNG or JPEG on disk, read from its header."""
    with open(path, "rb") as fh:
        head = fh.read(4096)
    return _png_size(head) or _jpeg_size(head)


def _have_pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def downscale(src: str, dst: str, width: int = IMAGE_WIDTH) -> tuple[int, int]:
    """Write `src` to `dst` as a JPEG no wider than `width`; return its real size.

    Pillow when the running interpreter has it, `sips` on macOS when it does
    not, and a plain copy when neither is available — the scale factor is
    computed from the dimensions actually written, so every path stays correct.
    """
    if _have_pillow():
        from PIL import Image

        with Image.open(src) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w > width:
                im = im.resize((width, max(1, round(h * width / w))), Image.LANCZOS)
            im.save(dst, "JPEG", quality=JPEG_QUALITY)
    elif sys.platform == "darwin" and shutil.which("sips"):
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY)]
        size = image_size(src)
        if size and size[0] > width:
            cmd += ["--resampleWidth", str(width)]
        cmd += [src, "--out", dst]
        subprocess.run(cmd, capture_output=True, timeout=60)
    else:
        shutil.copyfile(src, dst)
    size = image_size(dst)
    if size is None:
        raise RuntimeError(f"cannot read the dimensions of {dst}")
    return size


def zoom_factor(width: int, min_width: int = ZOOM_MIN_WIDTH,
                cap: float = ZOOM_MAX_UPSCALE) -> float:
    """How much to enlarge a crop `width` pixels across. Never past `cap`:
    beyond about 2x the picture stops carrying detail and starts inventing it."""
    if width <= 0:
        return 1.0
    return max(1.0, min(cap, min_width / width))


def crop(src: str, dst: str, box: tuple[int, int, int, int],
         min_width: int = ZOOM_MIN_WIDTH, cap: float = ZOOM_MAX_UPSCALE) -> tuple[int, int]:
    """Cut `box` (left, top, right, bottom) out of `src` at its own resolution.

    Native pixels are the whole point of zoom: an address bar cropped out of the
    full-resolution frame is transcribed exactly, where the same strip inside a
    1024-wide screenshot is a smear. Returns the size actually written.
    """
    left, top, right, bottom = box
    width, height = max(1, right - left), max(1, bottom - top)
    factor = zoom_factor(width, min_width, cap)
    target = (max(1, round(width * factor)), max(1, round(height * factor)))
    if _have_pillow():
        from PIL import Image

        with Image.open(src) as im:
            cut = im.convert("RGB").crop((left, top, left + width, top + height))
            if factor > 1.0:
                cut = cut.resize(target, Image.LANCZOS)
            cut.save(dst, "JPEG", quality=JPEG_QUALITY)
    elif sys.platform == "darwin" and shutil.which("sips"):
        # sips takes the crop as height-then-width, and the offset as top-left.
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions",
                        str(JPEG_QUALITY), "-c", str(height), str(width),
                        "--cropOffset", str(top), str(left), src, "--out", dst],
                       capture_output=True, timeout=60)
    else:
        raise RuntimeError("zoom needs Pillow or sips to cut a crop")
    size = image_size(dst)
    if size is None:
        raise RuntimeError(f"cannot read the dimensions of {dst}")
    return size


def crop_with_marker(src: str, cx: int, cy: int, w: int, h: int, out_path: str,
                     centre: tuple[int, int] | None = None):
    """Crop a w x h box around `centre` and ring (cx, cy) in red.

    The peer's marker: a hollow ring with four ticks that stop short of it, so
    the thing being pointed at is never painted over. `centre` defaults to the
    marked point; the driver passes a point below it, because the miss this
    exists for is always upward and the target has to be inside the picture.
    Returns the box, or None when there is nothing to draw with.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    ccx, ccy = centre if centre else (cx, cy)
    try:
        with Image.open(src) as im:
            x1 = max(0, min(im.width - w, ccx - w // 2))
            y1 = max(0, min(im.height - h, ccy - h // 2))
            box = (x1, y1, min(im.width, x1 + w), min(im.height, y1 + h))
            crop = im.crop(box).convert("RGB")
    except (OSError, ValueError):
        return None
    mx, my = cx - box[0], cy - box[1]
    draw = ImageDraw.Draw(crop)
    r = 13
    draw.ellipse([mx - r, my - r, mx + r, my + r], outline=(255, 0, 0), width=3)
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        draw.line([mx + dx * (r + 4), my + dy * (r + 4),
                   mx + dx * (r + 12), my + dy * (r + 12)], fill=(255, 0, 0), width=3)
    crop.save(out_path, "PNG")
    return box


def synthetic_png(width: int = 1920, height: int = 1080, shift: int = 0) -> bytes:
    """A gradient PNG written with the standard library, for --mock.

    `shift` moves the green channel, which is most of what grayscale sees, so a
    mock desktop can hand back a screen that really did change - and, by
    repeating a shift, one that really did not.
    """
    raw = bytearray()
    green = (90 + 40 * shift) % 256
    for y in range(height):
        v = 20 + (y * 200) // max(1, height - 1)
        raw += b"\x00" + bytes((v, green, 220 - v)) * width

    def chunk(tag: bytes, body: bytes) -> bytes:
        payload = tag + body
        return (struct.pack(">I", len(body)) + payload
                + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


@dataclass
class Shot:
    """One screenshot, and the arithmetic the model is never shown."""

    step: int
    path: str
    image_w: int
    image_h: int
    screen_w: int
    screen_h: int
    origin_x: int = 0
    origin_y: int = 0
    #: The full-resolution frame the shown image was shrunk from, kept on disk
    #: so zoom can cut a crop that is sharp instead of one that is enlarged.
    raw_path: str | None = None
    raw_w: int = 0
    raw_h: int = 0
    #: Where the pointer was in screen points when this frame was taken, and
    #: which window was in front. Both are properties of the picture, not of the
    #: step, so they travel with it.
    pointer: tuple[int, int] | None = None
    window: str = ""

    @property
    def scale_x(self) -> float:
        return self.screen_w / self.image_w if self.image_w else 1.0

    @property
    def scale_y(self) -> float:
        return self.screen_h / self.image_h if self.image_h else 1.0

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Image pixels -> screen points. Truncating, not rounding: a half-pixel
        of bias is invisible on a click target and the result is deterministic."""
        return (int(x * self.scale_x) + self.origin_x,
                int(y * self.scale_y) + self.origin_y)

    def from_grid(self, x: float, y: float) -> tuple[int, int]:
        """0-GRID grid -> image pixels, truncating for the same reason
        `to_screen` does: two truncations, no rounding mode to remember."""
        return (int(x * self.image_w / GRID), int(y * self.image_h / GRID))

    def to_raw(self, x: float, y: float) -> tuple[int, int]:
        """Image pixels -> pixels of the full-resolution frame behind them."""
        raw_w = self.raw_w or self.image_w
        raw_h = self.raw_h or self.image_h
        return (int(x * raw_w / max(1, self.image_w)),
                int(y * raw_h / max(1, self.image_h)))

    def from_screen(self, x: float, y: float) -> tuple[int, int]:
        """Screen points -> pixels of this picture, the inverse of to_screen."""
        return (int((x - self.origin_x) * self.image_w / max(1, self.screen_w)),
                int((y - self.origin_y) * self.image_h / max(1, self.screen_h)))

    def pointer_px(self) -> tuple[int, int] | None:
        return self.from_screen(*self.pointer) if self.pointer else None

    def inside(self, x: float, y: float) -> bool:
        return 0 <= x < self.image_w and 0 <= y < self.image_h

    def data_url(self) -> str:
        with open(self.path, "rb") as fh:
            blob = fh.read()
        mime = "image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")


# ----------------------------------------------------------------- trajectory

def machine_name(node: str | None = None) -> str:
    """Which box this is, as the corpus names it: dgx, air, m1 or m3.

    The hostname is the source of truth and the fallback both: an unrecognised
    machine keeps its own short name rather than being filed under a guess.
    """
    host = (platform.node() if node is None else node).split(".")[0]
    parts = re.split(r"[^a-z0-9]+", host.lower())
    for name in MACHINE_NAMES:
        if name in parts:
            return name
    return host or "unknown"


def utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(machine: str | None = None, now: datetime | None = None,
               nonce: str | None = None) -> str:
    """`20260915T221000Z-air-abc123`: when, where, and enough entropy that two
    runs started in the same second on the same box never collide."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    tail = os.urandom(3).hex() if nonce is None else nonce
    return f"{stamp}-{machine or machine_name()}-{tail}"


def driver_id(model) -> str:
    """Who drove this step, as `<family>:<model-id>`.

    The local model is the muse family; a model that says otherwise - the
    fallback Claude seat - names its own. The id is whatever model name went out
    in the request, and the scripted stand-in says so plainly rather than
    borrowing a real model's name.
    """
    family = getattr(model, "family", "muse")
    name = getattr(model, "model", None)
    if isinstance(name, str) and name.strip():
        return f"{family}:{name.strip()}"
    return f"{family}:mock"


def window_label(active: dict | None) -> str:
    """One string for whatever `active` reported: app, title, or both."""
    if not isinstance(active, dict) or active.get("ok") is False:
        return ""
    app = str(active.get("app") or "").strip()
    title = str(active.get("title") or "").strip()
    if app and title and title != app:
        return f"{app} - {title}"
    return app or title


def switched_window(before: str, after: str) -> bool:
    """True only when both frames named a window and they are not the same one.
    Silence is not a switch: a backend that cannot report the front window must
    not turn every action into a wrong target."""
    return bool(before and after and before != after)


def switch_allowed(name: str, args: dict) -> bool:
    """Which actions are supposed to bring another window forward. open_url is
    the obvious one; a chord is the other, because cmd+tab and ctrl+w change the
    front window on purpose. A plain click never does."""
    if name == "open_url":
        return True
    return name == "key" and "+" in str(args.get("combo") or "")


def _short(value, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def canonical_echo(name: str, args: dict) -> str:
    """The action as it goes back into the conversation, in the flat contract.

    Byte-stable whatever the model wrote around it, because the history above
    the next screenshot has to be identical to last step's for the prefix cache
    to hit - and flat, because echoing a shape the schema forbids teaches the
    model the wrong one.
    """
    return json.dumps({"tool": name, **args})


def compact_action(name: str, args: dict) -> str:
    """`click(165,605)` - one short form for the history lines and the
    no-change warning, so a step reads the same wherever it is quoted."""
    return f"{name}({','.join(_short(v) for v in args.values())})"


def _gray_thumb(path: str, width: int = DIFF_WIDTH, blank_points=None,
                blank_box: int = POINTER_BLANK):
    """One picture as (size, grayscale bytes) `width` across, or None.

    `blank_points` are filled black first, in this picture's own pixels, and the
    caller passes the same points for both frames: a box that is black in both
    cannot contribute to the difference, which is how the pointer is kept out of
    the measurement.
    """
    if not (path and os.path.exists(path)):
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            gray = im.convert("L")
    except (OSError, ValueError):
        return None
    if blank_points:
        draw = ImageDraw.Draw(gray)
        half = max(1, blank_box // 2)
        for point in blank_points:
            if not point:
                continue
            px, py = int(point[0]), int(point[1])
            draw.rectangle([px - half, py - half, px + half, py + half], fill=0)
    w, h = gray.size
    size = (width, max(1, round(h * width / max(1, w))))
    return size, gray.resize(size, Image.LANCZOS).tobytes()


def screenshot_diff(before: str, after: str, width: int = DIFF_WIDTH,
                    blank_points=None, blank_box: int = POINTER_BLANK) -> float | None:
    """How much of the screen moved between two frames, 0.0 to 1.0.

    Both are taken down to `width` across and to grayscale, then averaged
    absolute difference over 255. None means a picture could not be read, which
    is not the same as nothing having changed - the caller has to keep them
    apart, because one trains and the other does not. Two frames of different
    shape are 1.0: the screen did not merely change, it is a different screen.
    """
    first = _gray_thumb(before, width, blank_points, blank_box)
    second = _gray_thumb(after, width, blank_points, blank_box)
    if first is None or second is None or not first[1]:
        return None
    if first[0] != second[0]:
        return 1.0
    a, b = first[1], second[1]
    return sum(abs(x - y) for x, y in zip(a, b)) / (len(a) * 255)


class Trajectory:
    """One JSONL line per driven step, in the shape the Muse LoRA trains on.

    A line cannot be finished when its action runs: `outcome` and `diff` are
    what the NEXT screenshot says about it. So exactly one line is buffered, and
    the arrival of the next pre-action screenshot settles the one before it;
    `close` settles the last one with no screenshot to judge it by.

    The schema lives in `muse-train/TRAJECTORY.md`, and the key order here is
    that document's order, because these files are read by hand as often as by
    a loader.
    """

    #: `screen_coords` and `reground` are this harness's two additions to the
    #: schema's list, both restoring something a reader needs and 107357f
    #: dropped. A line that carries `action_px` but no screen point cannot be
    #: replayed against a real desktop, and a re-ground line whose picture is a
    #: crop is unreadable without the box that crop came out of.
    FIELDS = ("run_id", "step", "driver", "machine", "task", "screenshot",
              "image_size", "history", "zoom", "action", "action_px",
              "screen_coords", "reground", "off_contract",
              "outcome", "diff", "verified_by", "quality", "ts")
    #: Two departures from the schema's own list, both so a line says what is
    #: actually known. `unknown` is new: a step with no screenshot after it has
    #: not been verified, and calling it `ok` would train on a step nobody
    #: checked. And `wrong_target` is set automatically in exactly one case - a
    #: click that brought a different window to the front - because that case is
    #: machine-decidable and scores as a huge successful diff otherwise. Every
    #: other wrong target is still a reviewer's call.

    def __init__(self, directory: str, run_id: str, driver: str, machine: str,
                 task: str, log=print):
        self.dir, self.run_id, self.driver = directory, run_id, driver
        self.machine, self.task, self.log = machine, task, log
        self.path = os.path.join(directory, "steps.jsonl")
        self.written = 0
        self.skipped = 0
        self._pending: dict | None = None

    def add(self, row: dict, shot: Shot, *, changes: bool = False,
            outcome: str | None = None, zoom_src: str | None = None,
            before: str | None = None, after: str | None = None) -> None:
        """Settle the line before this one against this step's screenshot, then
        hold this one until the step after it says how it went.

        `before` and `after` are the two full screens this step sits between,
        which is not always the picture the step was shown: after a forced
        re-ground the model is looking at a crop, and a crop measured against a
        whole screen is a number with no meaning.
        """
        self.settle(before or shot.path)
        self._pending = {"row": row, "shot": shot, "changes": changes,
                         "outcome": outcome, "zoom_src": zoom_src,
                         "before": before or shot.path, "after": after}

    def settle(self, next_shot: str | None) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        row, shot = pending["row"], pending["shot"]
        after = pending["after"] or next_shot
        diff = screenshot_diff(pending["before"], after) if after else None
        outcome = pending["outcome"]
        if outcome is None:
            if diff is None:
                # Nothing to check it against. done is its own evidence - the
                # answer is the result - and everything else is simply not
                # verified, which is a 0, not a 1.
                outcome = "ok" if row["action"]["tool"] == "done" else "unknown"
            else:
                outcome = ("no_change" if pending["changes"]
                           and diff < NO_CHANGE_DIFF else "ok")
        row["outcome"] = outcome
        row["diff"] = None if diff is None else round(diff, 6)
        row["verified_by"] = None if diff is None else "screenshot-diff"
        # -1 is never trained on: a refusal did not happen, and a click that
        # landed in another window happened somewhere real and wrong.
        row["quality"] = (1 if outcome == "ok"
                          else -1 if outcome in ("refused", "wrong_target") else 0)
        self._write(row, shot, pending["zoom_src"])

    def close(self) -> None:
        self.settle(None)

    @staticmethod
    def has_credential(line: dict) -> bool:
        """The collector's rule, applied before anything is written: a task, a
        history line or a typed string that looks like a credential."""
        blobs = [str(line.get("task") or "")]
        blobs += [str(part) for part in (line.get("history") or [])]
        blobs.append(str((line.get("action") or {}).get("text") or ""))
        return any(CREDENTIAL_RE.search(blob) for blob in blobs)

    @staticmethod
    def _copy(src: str, dst: str) -> None:
        """The frame the action was decided on, next to the line that cites it.

        Re-encoded so the .png the schema names really is a PNG; without Pillow
        the bytes are copied as they are, which keeps the pixels honest even
        though the extension then lies about the container.
        """
        try:
            from PIL import Image
        except ImportError:
            shutil.copyfile(src, dst)
            return
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "PNG")

    def _write(self, row: dict, shot: Shot, zoom_src: str | None) -> None:
        line = {"run_id": self.run_id, "driver": self.driver,
                "machine": self.machine, "task": self.task, **row}
        line = {key: line[key] for key in self.FIELDS if key in line}
        if self.has_credential(line):
            self.skipped += 1
            self.log(f"trajectory: step {line.get('step')} not written, it matches the "
                     f"credential guard ({CREDENTIAL_RE.pattern})")
            return
        os.makedirs(self.dir, exist_ok=True)
        self._copy(shot.path, os.path.join(self.dir, line["screenshot"]))
        if zoom_src and isinstance(line.get("zoom"), dict):
            self._copy(zoom_src, os.path.join(self.dir, line["zoom"]["file"]))
        with open(self.path, "a") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
        self.written += 1


# -------------------------------------------------------------------- backend

class Backend:
    """One method per wm.py verb, so the execution surface is trivially swapped."""

    def __init__(self, python: str, wm: str, timeout: int = 90):
        self.python, self.wm, self.timeout = python, wm, timeout

    def _run(self, *args: str) -> dict:
        cmd = [self.python, self.wm, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "cmd": cmd[1:]}
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {"ok": proc.returncode == 0,
                   "stdout": proc.stdout.strip()[:800],
                   "stderr": proc.stderr.strip()[:800]}
        if proc.returncode != 0 and isinstance(out, dict):
            out.setdefault("ok", False)
        return out if isinstance(out, dict) else {"ok": True, "result": out}

    def platform(self) -> dict:
        return self._run("platform")

    def shot(self, path: str) -> dict:
        return self._run("shot", path)

    def windows(self) -> dict:
        return self._run("windows")

    def active(self) -> dict:
        return self._run("active")

    def focus(self, query: str) -> dict:
        return self._run("focus", query)

    def pointer(self) -> dict:
        """Where the cursor is, in screen points. wm.py has no verb for it on
        every backend, so a failure here is ordinary: the diff simply loses its
        pointer blanking, which parking has mostly made redundant anyway."""
        return self._run("pointer")

    def park(self, x: int, y: int) -> dict:
        """Move the pointer without clicking. `move` on every backend that has
        it; this is the one call that must not be swallowed by a dry run,
        because an unparked cursor makes a still screen look like it moved."""
        return self._run("move", str(x), str(y))

    def click(self, x: int, y: int) -> dict:
        return self._run("click", str(x), str(y))

    def type(self, text: str) -> dict:
        return self._run("type", text)

    def key(self, combo: str) -> dict:
        return self._run("key", combo)

    def human(self, value: str) -> dict:
        return self._run("human", value)


class X11Backend(Backend):
    """The same ten verbs on X11, where wm.py takes the picture and xdotool
    moves the hands.

    Folded from the peer's driver on the DGX, which is the surface that actually
    works there: wm.py's own click silently no-ops on some pages, while
    `xdotool mousemove; sleep; click 1` lands. Capture stays with wm.py, at
    native resolution (`shot <path> full`), so one grab per step is the single
    source of truth the model image, the zoom and the re-ground crop are all cut
    from.

    Every method still returns a dict and never raises, so the driver above it
    cannot tell which backend it is holding.
    """

    name = "x11"

    def __init__(self, python: str | None = None, wm: str = DEFAULT_WM,
                 timeout: int = 90, display: str | None = None,
                 type_delay: int = X11_TYPE_DELAY, sleep=time.sleep):
        super().__init__(python or sys.executable, wm, timeout)
        self.display = display or os.environ.get("DISPLAY") or ":0"
        self.type_delay = type_delay
        self.sleep = sleep
        self.env = dict(os.environ)
        self.env["DISPLAY"] = self.display
        self.env.setdefault("XAUTHORITY", os.path.join(HOME, ".Xauthority"))
        #: Filled in by shot(), so platform() can report a screen size.
        self.screen: tuple[int, int] = (0, 0)

    # -- process helpers ----------------------------------------------------
    def _exec(self, argv: list[str], timeout: int | None = None):
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout or self.timeout, env=self.env)

    def _xdotool(self, args: list[str]) -> dict:
        try:
            proc = self._exec(["xdotool", *args])
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"xdotool {' '.join(args)}: {exc}"}
        if proc.returncode != 0:
            return {"ok": False,
                    "error": f"xdotool {' '.join(args)} failed: "
                             f"{(proc.stderr or proc.stdout).strip()}"}
        return {"ok": True, "out": proc.stdout.strip(),
                "cmd": "xdotool " + " ".join(args)}

    # -- verbs --------------------------------------------------------------
    def platform(self) -> dict:
        return {"ok": True, "cli_backend": "x11", "display": self.display,
                "xdotool": shutil.which("xdotool"), "wmctrl": shutil.which("wmctrl"),
                "wm_py": self.wm,
                "screen": list(self.screen) if self.screen != (0, 0) else None,
                "human_mode": "not applicable on x11"}

    def shot(self, path: str) -> dict:
        """Native-resolution capture through wm.py, which owns the display."""
        try:
            proc = self._exec([self.python, self.wm, "shot", path, "full"])
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"wm.py shot: {exc}"}
        if proc.returncode != 0:
            return {"ok": False,
                    "error": f"wm.py shot failed: {(proc.stderr or proc.stdout).strip()}"}
        meta = json_object(proc.stdout) or {}
        if (meta.get("blank_check") or {}).get("blank"):
            return {"ok": False,
                    "error": "screen capture came back blank - display unreachable?"}
        size = image_size(path)
        if size is None:
            return {"ok": False, "error": f"screenshot unreadable: {path}"}
        width, height = size
        screen = meta.get("screen") or [width, height]
        self.screen = (int(screen[0]), int(screen[1]))
        return {"ok": True, "saved": path, "format": "png",
                "points_w": self.screen[0], "points_h": self.screen[1],
                "origin_x": 0, "origin_y": 0,
                "image_w": width, "image_h": height, "wm": meta}

    def windows(self) -> dict:
        if shutil.which("wmctrl"):
            try:
                proc = self._exec(["wmctrl", "-lx"])
            except (OSError, subprocess.SubprocessError):
                proc = None
            if proc is not None and proc.returncode == 0:
                rows = []
                for line in proc.stdout.splitlines():
                    parts = line.split(None, 4)
                    if len(parts) >= 5:
                        rows.append({"id": parts[0], "desktop": parts[1],
                                     "app": parts[2], "host": parts[3],
                                     "title": parts[4]})
                return {"ok": True, "windows": rows, "via": "wmctrl"}
        found = self._xdotool(["search", "--onlyvisible", "--name", ".*"])
        if not found.get("ok"):
            return found
        rows = []
        for wid in (found.get("out") or "").split():
            named = self._xdotool(["getwindowname", wid])
            rows.append({"id": wid, "app": "",
                         "title": named.get("out", "") if named.get("ok") else ""})
        return {"ok": True, "windows": rows, "via": "xdotool"}

    def active(self) -> dict:
        wid = self._xdotool(["getactivewindow"])
        if not wid.get("ok"):
            return wid
        named = self._xdotool(["getwindowname", wid["out"]])
        title = named.get("out", "") if named.get("ok") else ""
        cls = self._xdotool(["getwindowclassname", wid["out"]])
        return {"ok": True, "id": wid["out"], "title": title,
                "app": cls.get("out", "") if cls.get("ok") else ""}

    def focus(self, query: str) -> dict:
        """Match a window by title or class and raise it. Never launches anything."""
        listing = self.windows()
        if not listing.get("ok"):
            return listing
        want = (query or "").lower()
        for win in listing.get("windows") or []:
            hay = f"{win.get('title', '')} {win.get('app', '')}".lower()
            if want and want in hay:
                res = self._xdotool(["windowactivate", "--sync", win["id"]])
                if not res.get("ok"):
                    res = self._xdotool(["windowactivate", win["id"]])
                if res.get("ok"):
                    return {"ok": True, "focused": {"app": win.get("app", ""),
                                                    "title": win.get("title", "")},
                            "matched": query}
                return res
        return {"ok": False, "error": f"no window or app matches {query!r}"}

    def click(self, x: int, y: int, button: int = 1) -> dict:
        """Click at screen pixels. The caller has already done grid -> screen."""
        moved = self._move_real(x, y)
        if not moved.get("ok"):
            return moved
        self.sleep(0.15)
        res = self._xdotool(["click", str(button)])
        if not res.get("ok"):
            return res
        return {"ok": True, "x": x, "y": y, "button": button,
                "cmds": moved.get("cmds", []) + [res["cmd"]]}

    def type(self, text: str) -> dict:
        res = self._xdotool(["type", "--delay", str(self.type_delay), "--", text])
        if not res.get("ok"):
            return res
        return {"ok": True, "typed": len(text),
                "cmds": [f"xdotool type --delay {self.type_delay} -- "
                         f"<{len(text)} chars>"]}

    def key(self, combo: str) -> dict:
        if not combo:
            return {"ok": False, "error": "no key combination given"}
        res = self._xdotool(["key", "--", combo])
        if not res.get("ok"):
            return res
        return {"ok": True, "key": combo, "cmds": [res["cmd"]]}

    def human(self, value: str) -> dict:
        """There is no human-mode toggle on X11 - the pointer path is already
        incremental, which is what the toggle buys on the daemon."""
        return {"ok": True, "human": value, "backend": self.name,
                "note": "x11 has no human-mode toggle; pointer moves are always "
                        "incremental"}

    def pointer(self) -> dict:
        res = self._xdotool(["getmouselocation", "--shell"])
        if not res.get("ok"):
            return res
        vals = dict(line.split("=", 1) for line in (res["out"] or "").splitlines()
                    if "=" in line)
        try:
            return {"ok": True, "x": int(vals.get("X", 0)), "y": int(vals.get("Y", 0))}
        except ValueError:
            return {"ok": False, "error": f"unreadable pointer: {res['out']!r}"}

    def park(self, x: int, y: int) -> dict:
        """Capture-side, never an action, and never skipped: an unparked cursor
        makes a still screen look like it moved."""
        return self._move_real(x, y)

    def _move_real(self, x: int, y: int) -> dict:
        """The pointer walks there in X11_MOVE_STEPS hops, because native UI
        only reacts to motion that looks like motion."""
        here = self.pointer()
        cx, cy = ((here.get("x", x), here.get("y", y)) if here.get("ok") else (x, y))
        cmds = []
        for i in range(1, X11_MOVE_STEPS + 1):
            ix = round(cx + (x - cx) * i / X11_MOVE_STEPS)
            iy = round(cy + (y - cy) * i / X11_MOVE_STEPS)
            res = self._xdotool(["mousemove", str(ix), str(iy)])
            if not res.get("ok"):
                return res
            cmds.append(res["cmd"])
            self.sleep(0.05)
        return {"ok": True, "x": x, "y": y, "cmds": cmds}


class MockBackend:
    """A 1920x1080 desktop that records what it was asked to do.

    Every screenshot differs from the last, the way a real screen does, except
    at `stuck_shot` - one index or several - which hands back the picture before
    it. That repeat is what lets a scripted run exercise a step that changed
    nothing, and two in a row a model that is stuck.
    """

    SCREEN = (1920, 1080)
    #: Cached by shift: drawing a 1920x1080 gradient in pure python is slow
    #: enough to notice across a test suite, and there are only a few of them.
    _frames: dict[int, bytes] = {}

    #: Where the mock says the cursor is. Fixed: the frames are synthetic, so
    #: the blanked box only has to be the same in both of them.
    POINTER = (100, 100)

    def __init__(self, chrome: bool = True, active_app: str = "Google Chrome",
                 stuck_shot=0, switch_after_click: int = 0,
                 switched_app: str = "Google Chrome - Antibot probe"):
        self.calls: list[tuple] = []
        self.chrome = chrome
        self.active_app = active_app
        self.stuck = ({stuck_shot} if isinstance(stuck_shot, int)
                      else set(stuck_shot)) - {0}
        self.shots = 0
        self.clicks = 0
        self.switch_after_click = switch_after_click
        self.switched_app = switched_app
        self._shift = 0

    def _frame(self, shift: int) -> bytes:
        if shift not in self._frames:
            self._frames[shift] = synthetic_png(*self.SCREEN, shift=shift)
        return self._frames[shift]

    def _rec(self, verb: str, *args) -> None:
        self.calls.append((verb, *args))

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]

    def platform(self) -> dict:
        self._rec("platform")
        return {"cli_backend": "mock", "os": sys.platform, "daemon_reachable": True}

    def shot(self, path: str) -> dict:
        self._rec("shot", path)
        self.shots += 1
        if self.shots not in self.stuck:
            self._shift = self.shots
        with open(path, "wb") as fh:
            fh.write(self._frame(self._shift))
        w, h = self.SCREEN
        return {"ok": True, "saved": path, "format": "png", "points_w": w, "points_h": h,
                "origin_x": 0, "origin_y": 0, "image_w": w, "image_h": h}

    def windows(self) -> dict:
        self._rec("windows")
        return {"ok": True, "windows": [{"app": "Google Chrome", "title": "New Tab"}]}

    def active(self) -> dict:
        self._rec("active")
        return {"ok": True, "app": self.active_app}

    def focus(self, query: str) -> dict:
        self._rec("focus", query)
        if not self.chrome:
            return {"ok": False, "error": f"no window or app matches {query!r}"}
        return {"ok": True, "focused": {"app": "Google Chrome", "title": "New Tab"}}

    def pointer(self) -> dict:
        self._rec("pointer")
        return {"ok": True, "x": self.POINTER[0], "y": self.POINTER[1]}

    def park(self, x: int, y: int) -> dict:
        self._rec("park", x, y)
        return {"ok": True, "x": x, "y": y}

    def click(self, x: int, y: int) -> dict:
        self._rec("click", x, y)
        self.clicks += 1
        if self.switch_after_click and self.clicks >= self.switch_after_click:
            self.active_app = self.switched_app
        return {"ok": True, "x": x, "y": y}

    def type(self, text: str) -> dict:
        self._rec("type", text)
        return {"ok": True, "typed": len(text)}

    def key(self, combo: str) -> dict:
        self._rec("key", combo)
        return {"ok": True, "key": combo}

    def human(self, value: str) -> dict:
        self._rec("human", value)
        return {"ok": True, "human": value == "on", "backend": "mock"}


# ---------------------------------------------------------------- tool surface

AUTO_LOOK = "Each of these returns a fresh screenshot of the screen after it ran."

#: One plain sentence per grid, and the model is told nothing beyond it. Turning
#: a point into image pixels and then into screen points stays the harness's
#: job either way — the model never sees a resolution or a factor.
CLICK_GRID = {
    "pixels": "Click at x,y in pixels of the last screenshot.",
    "1000": ("Click at x,y on a 0 to 1000 grid over the last screenshot, where 0 is its "
             "left or top edge and 1000 is its right or bottom edge."),
}
COORD_SENTENCE = {
    "pixels": ("Coordinates are pixels of the screenshot you were just shown, measured "
               "from its top-left corner. Read a position off the image and pass those "
               "numbers straight in — no scaling, no screen resolution, no arithmetic."),
    "1000": ("Coordinates are a 0 to 1000 grid laid over the screenshot you were just "
             "shown: 0,0 is its top-left corner and 1000,1000 its bottom-right one, "
             "whatever size the picture is. Read a position off the image as a fraction "
             "of its width and height and pass those numbers straight in."),
}
ZOOM_HELP = {
    "pixels": ("Show a rectangle of the last screenshot close up, its corners in pixels "
               "of that screenshot, when text is too small to read."),
    "1000": ("Show a rectangle of the last screenshot close up, its corners on the same "
             "0 to 1000 grid as click, when text is too small to read."),
}


def base_tools(coord_grid: str = "pixels") -> list[dict]:
    """The seven tools this loop shipped with, worded for one coordinate grid."""
    axis = ("pixels from the left of the screenshot", "pixels from the top of the screenshot")
    if coord_grid == "1000":
        axis = ("0 at the left edge of the screenshot, 1000 at the right edge",
                "0 at the top edge of the screenshot, 1000 at the bottom edge")
    return [
        {"type": "function", "function": {
            "name": "look",
            "description": "Take a screenshot of the screen as it is right now.",
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "click",
            "description": CLICK_GRID[coord_grid] + " " + AUTO_LOOK,
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer", "description": axis[0]},
                "y": {"type": "integer", "description": axis[1]}},
                "required": ["x", "y"]}}},
        {"type": "function", "function": {
            "name": "type",
            "description": ("Type text into whatever has keyboard focus. Give x,y "
                            "for the field itself and it is checked, and focused if "
                            "it is not. " + AUTO_LOOK),
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "the literal text to type"},
                "x": {"type": "integer", "description": "the field, " + axis[0]},
                "y": {"type": "integer", "description": "the field, " + axis[1]}},
                "required": ["text"]}}},
        {"type": "function", "function": {
            "name": "key",
            "description": ("Press one key combo, xdotool names: ctrl+l, Return, Tab, Escape, "
                            "Page_Down. " + AUTO_LOOK),
            "parameters": {"type": "object", "properties": {
                "combo": {"type": "string", "description": "for example ctrl+l or Return"}},
                "required": ["combo"]}}},
        {"type": "function", "function": {
            "name": "scroll",
            "description": "Scroll the focused window a page at a time. " + AUTO_LOOK,
            "parameters": {"type": "object", "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer", "description": "how many pages, 1 to 10"}},
                "required": ["direction"]}}},
        {"type": "function", "function": {
            "name": "open_url",
            "description": "Focus Chrome and load a URL in the current tab. " + AUTO_LOOK,
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "the full URL, including https://"}},
                "required": ["url"]}}},
        {"type": "function", "function": {
            "name": "done",
            "description": "Finish the run and report the answer to the task.",
            "parameters": {"type": "object", "properties": {
                "answer": {"type": "string", "description": "the answer, in plain words"}},
                "required": ["answer"]}}},
    ]


def zoom_tool(coord_grid: str = "pixels") -> dict:
    """The eighth tool: a crop of the last screenshot at full resolution.

    Enlarging the whole screen costs a fortune in visual tokens and reads no
    better. One measured crop of an address bar was 91 prompt tokens and 1.1 s,
    and it was transcribed exactly.
    """
    corner = "in pixels of the last screenshot"
    if coord_grid == "1000":
        corner = "on the 0 to 1000 grid of the last screenshot"
    return {"type": "function", "function": {
        "name": "zoom",
        "description": ZOOM_HELP[coord_grid]
                       + " It returns the crop only; the screen does not change.",
        "parameters": {"type": "object", "properties": {
            "x1": {"type": "integer", "description": f"left edge, {corner}"},
            "y1": {"type": "integer", "description": f"top edge, {corner}"},
            "x2": {"type": "integer", "description": f"right edge, {corner}"},
            "y2": {"type": "integer", "description": f"bottom edge, {corner}"}},
            "required": ["x1", "y1", "x2", "y2"]}}}


def give_up_tool() -> dict:
    """The ninth: the model saying it is stuck, which is worth more than another
    dead click. On the peer's loop this is what hands the step to the fallback
    seat rather than ending the run."""
    return {"type": "function", "function": {
        "name": "give_up",
        "description": ("Stop: you are stuck, or the screen does not match the task. "
                        "Say why. Prefer this to repeating an action that is not "
                        "working."),
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "why you cannot continue"}},
            "required": ["reason"]}}}


def tools_for(coord_grid: str = "pixels") -> list[dict]:
    """The whole surface the model is offered: the seven, zoom, and give_up.

    Zoom goes next to look, because it is the other way of seeing, and because
    a model reads the list it is given in order. give_up goes last, next to
    done, because they are the two ways a run ends.
    """
    seven = base_tools(coord_grid)
    return [seven[0], zoom_tool(coord_grid)] + seven[1:] + [give_up_tool()]


#: The seven tools in their original pixel wording, kept under the name callers
#: already use. `tools_for()` is what the driver sends.
TOOLS = base_tools()
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
ALL_TOOL_NAMES = TOOL_NAMES | {"zoom", "give_up"}
ACTION_TOOLS = {"click", "type", "key", "scroll", "open_url"}

#: The peer's flat contract names three arguments differently from ours. Their
#: model is the one we are folding towards, so their spelling is accepted and
#: translated here rather than being argued with at the far end.
PEER_ARG_ALIASES = {"key": "combo", "dir": "direction", "n": "amount"}
#: Per-tool: the peer's name for the one argument that tool carries, so a reply
#: in their dialect still reaches ours.
PEER_TOOL_ALIASES = {"done": ("summary", "answer"), "give_up": ("reason", "reason")}
#: The peer's own system prompt names three of OUR tools differently: their
#: `type_text` is our `type`, their `press_key` is our `key`, and their `wait` is
#: our `look` with a number in front of it. A run carrying their prompt text
#: would otherwise be steered at names our schema rejects, so their name is
#: mapped to ours at parse, before the arguments are translated. Their
#: `double_click`, `right_click` and `move` are deliberately NOT here: this loop
#: has no such action, and accepting a name we cannot perform is worse than
#: refusing it.
PEER_TOOL_NAMES = {"type_text": "type", "press_key": "key", "wait": "look"}
#: What a peer-named branch carries that ours does not. `wait` is `look` with
#: the seconds to sleep first, and that number is required: a wait with no
#: number is a look, and the model should have said look.
PEER_TOOL_EXTRAS = {"wait": ({"seconds": {"type": "number",
                                          "description": "how long to wait"}},
                             ["seconds"])}
#: The longest one `wait` may hold the loop. A minute spent on nothing costs
#: more of a step budget than looking again does.
WAIT_CAP = 10
#: An off-contract object with only these keys is a finished answer...
ANSWER_ONLY_KEYS = frozenset({"answer", "summary"})
#: ...and with only these it is a model that has stopped.
GIVE_UP_ONLY_KEYS = frozenset({"reason", "give_up", "error"})
#: ...but an object carrying any of these is an action the model failed to name,
#: never a terminal one. Mapping `{"x":.., "y":..}` to done would end a run on a
#: click.
NEVER_TERMINAL_KEYS = frozenset({"x", "y", "text", "key", "combo"})


def _peer_spellings(name: str, own: dict) -> list[tuple[str, str]]:
    """Every (their name, our name) pair that applies to one tool's own arguments.

    Only the arguments that tool actually carries: a per-tool branch that also
    accepted `url` on `click` would be the flat schema again under another name.
    """
    pairs = [(theirs, ours) for theirs, ours in PEER_ARG_ALIASES.items()
             if ours in own and theirs != ours]
    theirs, ours = PEER_TOOL_ALIASES.get(name, (None, None))
    if theirs and ours in own and theirs != ours:
        pairs.append((theirs, ours))
    return pairs


def _tool_branches(const: str, own: dict, required: list[str],
                   alias_name: str) -> list[dict]:
    """The anyOf branches for one tool, under one name for the `tool` const.

    `const` is the name the reply must carry and `alias_name` is OUR name for
    the same tool, which is what the argument spellings are looked up under: a
    `press_key` branch takes `key` or `combo` exactly as our `key` branch does.
    """
    properties: dict[str, dict] = {"tool": {"const": const}}
    spellings: dict[str, list[str]] = {}
    for arg, spec in own.items():
        properties[arg] = {k: v for k, v in spec.items() if k in ("type", "enum")}
        spellings[arg] = [arg]
    for theirs, ours in _peer_spellings(alias_name, own):
        properties.setdefault(theirs, dict(properties[ours]))
        spellings[ours].append(theirs)
    properties["why"] = {"type": "string", "description": "one sentence"}
    # One branch per spelling of the required arguments, so either dialect
    # validates and neither can drop the argument.
    return [{"type": "object", "properties": dict(properties),
             "required": ["tool", *chosen, "why"],
             "additionalProperties": False}
            for chosen in itertools.product(*(spellings[arg] for arg in required))]


def action_schema(tools: list[dict]) -> dict:
    """The one object a json-mode reply may be, as a JSON Schema: an `anyOf` of
    one branch per tool, so each tool's own arguments are required with it.

    Still flat - no nested `args` - because the peer's contract is what the
    muse-workman chat template was trained on: `{"tool": "click", "x": .., "y":
    .., "why": ".."}`. A run of ours against that model on 8082 answered
    `{"answer": "PHANTOM_UA"}` three times because our request carried a nested
    schema it had never seen.

    What changed is where `required` lives. One FLAT object with
    `required: ["tool", "why"]` cannot say "url is required, but only when tool
    is open_url", so llama-server's grammar let the model answer `open_url` with
    no url at all: run 20260915T001056Z-air-bb3274 step 1, and 1 of 5 probe
    replies on each Muse model. Measured on the Air against both Muse endpoints,
    5 POSTs per cell with the `{` prefill at temp 0.1, this anyOf gave 5/5
    replies carrying `url` on both models at the same latency (1.2-1.3 s), the
    same cached_tokens (774), and no 400 or 500. anyOf rather than if/then
    because llama.cpp's json-schema-to-grammar turns anyOf into a grammar
    alternation and does not implement if/then; the DGX peer who owns the
    contract chose it for that reason.

    An argument the peer spells differently rides as a sibling property, and
    when it is a REQUIRED one it earns its own branch rather than an if/then:
    `key` takes `combo` or `key`, `scroll` takes `direction` or `dir`, and
    `done` takes `answer` or `summary` - the peer's terminal form is
    `{"tool": "done", "summary": ..}`.

    llama-server only enforces a schema when the reply cannot wander off into
    reasoning first, so this is sent together with the `{` prefill or not at all.
    """
    branches: list[dict] = []
    for tool in tools:
        fn = tool["function"]
        name = fn["name"]
        params = fn.get("parameters") or {}
        own = params.get("properties") or {}
        required = list(params.get("required") or [])
        branches += _tool_branches(name, own, required, name)
        # ...and, right beside it, the same tool under the peer's name for it,
        # so a run carrying their prompt text validates instead of being
        # steered off the only names our grammar allows.
        for theirs, ours in PEER_TOOL_NAMES.items():
            if ours != name:
                continue
            extra_props, extra_required = PEER_TOOL_EXTRAS.get(theirs, ({}, []))
            branches += _tool_branches(theirs, {**own, **extra_props},
                                       [*required, *extra_required], name)
    return {"name": "desktop_action", "schema": {"anyOf": branches}}


def action_catalog(tools: list[dict]) -> str:
    """The same tools written out as flat objects, which is the shape the answer
    has to take, so the prompt and the schema agree."""
    rows = []
    for tool in tools:
        fn = tool["function"]
        props = (fn.get("parameters") or {}).get("properties") or {}
        shape = {"tool": fn["name"],
                 **{name: spec.get("type", "string") for name, spec in props.items()}}
        rows.append(f'{json.dumps(shape)}  - {fn["description"]}')
    return "\n".join(rows)


def system_prompt(coord_grid: str = "pixels", action_mode: str = "tools",
                  tools: list[dict] | None = None, notes: str = "") -> str:
    """The fixed string every request starts with.

    Fixed is the operative word: llama-server reuses its KV cache only while
    everything above the current screenshot is byte-identical, so nothing here
    may carry a step number, a timestamp, or the task.
    """
    tools = tools if tools is not None else tools_for(coord_grid)
    names = [t["function"]["name"] for t in tools]
    listed = ", ".join(names[:-1]) + " and " + names[-1]
    text = f"""You are driving a real desktop to finish one task.

Your tools are {listed}.
{COORD_SENTENCE[coord_grid]}
Every action hands you a fresh screenshot, so act, then read the new screen
before deciding anything.
Key combos use xdotool names: ctrl+l, Return, Tab, Page_Down.
Take one small step at a time and check it landed.
If the screen does not change the way you expected, look again and try a
different spot rather than repeating the same click.
Call done with the answer in plain words as soon as you can answer the task."""
    if notes:
        text += f"""

These are operating notes for this machine, written down from earlier runs on
it. They were written for a different tool surface, so where a note names a
tool you do not have, take the lesson and not the tool name. Where a note
contradicts a habit of yours, follow the note.
{notes}"""
    if action_mode != "json":
        return text
    return text + f"""

Answer with one JSON object and nothing else, shaped like
{{"tool": "<name>", "why": "<one sentence>"}} with the keys that tool needs
beside them. The keys are flat: there is no "args" object, and "tool" and "why"
are always required.
These are the tools and the keys each one takes:
{action_catalog(tools)}"""


SYSTEM_PROMPT = system_prompt()


# ------------------------------------------------------------- tool-call parse

_TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _json_blobs(text: str) -> list[str]:
    """Every balanced {...} run in the text, outermost first."""
    blobs, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blobs.append(text[start:i + 1])
    return blobs


def peer_args(args: dict) -> dict:
    """One already-separated argument object, with the peer's spellings turned
    into ours. Only their three: nothing else is renamed."""
    return {PEER_ARG_ALIASES.get(key, key): value for key, value in args.items()}


def flat_args(obj: dict, name: str) -> dict:
    """A flat reply's arguments: every key but `tool` and `why`, in our names.

    The peer's contract spells three of them differently (`key`, `dir`, `n`) and
    calls done's answer a `summary`. Translating here is cheaper than asking a
    model that was trained on their template to learn ours.
    """
    args = {}
    for key, value in obj.items():
        if key in ("tool", "name", "why") or value is None:
            continue
        args[PEER_ARG_ALIASES.get(key, key)] = value
    theirs, ours = PEER_TOOL_ALIASES.get(name, (None, None))
    if theirs and theirs in args and ours not in args:
        args[ours] = args.pop(theirs)
    return args


def off_contract_action(obj: dict) -> tuple[str, dict, str] | None:
    """The narrow rescue for a reply that named no tool at all.

    Measured, run 20260914T233905Z-air-da49a8: asked with no schema and no
    system prompt, the muse-workman model finished the task and answered
    `{"answer": "PHANTOM_UA"}` three times, which the harness read as three
    replies with no tool call and stopped on. The right answer was in the first
    one. So an object whose ONLY content is an answer is a done, and one whose
    only content is a reason is a give_up - and nothing else is rescued. An
    object carrying x, y, text or a key is an action the model failed to name,
    and ending a run on it would be worse than asking again.
    """
    keys = {k for k, v in obj.items() if v is not None} - {"why"}
    if not keys or keys & NEVER_TERMINAL_KEYS:
        return None
    if keys <= ANSWER_ONLY_KEYS:
        answer = obj.get("answer") or obj.get("summary") or ""
        return "done", {"answer": str(answer)}, "bare answer object read as done"
    if keys <= GIVE_UP_ONLY_KEYS:
        reason = obj.get("reason") or obj.get("give_up") or obj.get("error") or ""
        return "give_up", {"reason": str(reason)}, "bare reason object read as give_up"
    return None


def action_from_object(obj) -> tuple[str, dict, str | None] | None:
    """One parsed object -> (tool, args, off-contract note), or None.

    Flat is the contract and is tried first; the older nested
    `{"tool": ..., "args": {...}}` stays a fallback so a muse-glimmer run and
    the scripted mock keep working unchanged.
    """
    if not isinstance(obj, dict):
        return None
    said = obj.get("tool") or obj.get("name")
    if isinstance(said, str) and (said in ALL_TOOL_NAMES or said in PEER_TOOL_NAMES):
        # Their name for the tool becomes ours FIRST, so everything downstream -
        # the argument aliasing, the history line, the echo, the dispatch - sees
        # only the name this loop actually performs.
        name = PEER_TOOL_NAMES.get(said, said)
        nested = obj.get("arguments", obj.get("parameters", obj.get("args")))
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except json.JSONDecodeError:
                nested = None
        if isinstance(nested, dict):
            return name, peer_args(nested) if name != said else nested, None
        return name, flat_args(obj, name), None
    return off_contract_action(obj)


def _from_text(content) -> tuple[str, str, dict, str | None] | None:
    """Small models often narrate a tool call instead of emitting one. Accept the
    shapes that actually show up: a <tool_call> block, or a bare JSON object."""
    text = content if isinstance(content, str) else ""
    if not text:
        return None
    for blob in _TOOL_TAG.findall(text) or _json_blobs(text):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        parsed = action_from_object(obj)
        if parsed is not None:
            name, args, off = parsed
            return f"text_{name}", name, args, off
    return None


def parse_action(message: dict) -> tuple[str, str, dict, str | None] | None:
    """-> (call id, tool name, arguments, off-contract note), or None."""
    calls = message.get("tool_calls") or []
    if calls:
        call = calls[0]
        fn = call.get("function") or {}
        name, args = fn.get("name") or "", fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = None
        if name in PEER_TOOL_NAMES and isinstance(args, dict):
            name, args = PEER_TOOL_NAMES[name], peer_args(args)
        if name in ALL_TOOL_NAMES and isinstance(args, dict):
            return call.get("id") or f"call_{name}", name, args, None
        return None
    return _from_text(message.get("content"))


def parse_tool_call(message: dict) -> tuple[str, str, dict] | None:
    """-> (call id, tool name, arguments), or None when nothing is parseable."""
    parsed = parse_action(message)
    return None if parsed is None else parsed[:3]


def json_object(text: str, keys=()) -> dict | None:
    """The first balanced object in the text that carries every key in `keys`."""
    if not isinstance(text, str) or not text:
        return None
    for blob in _TOOL_TAG.findall(text) or _json_blobs(text):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and all(key in obj for key in keys):
            return obj
    return None


def action_why(message: dict) -> str:
    """The one sentence the model gave for its action, for the training line.

    json mode carries it in the object itself; a tools-mode reply has only the
    prose beside the call, which is the same thing said less formally.
    """
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    if not text:
        return ""
    for blob in _TOOL_TAG.findall(text) or _json_blobs(text):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("why"), str):
            return obj["why"].strip()
    stripped = text.strip()
    if stripped.startswith(("{", "<")):
        return ""
    return _short(stripped.splitlines()[0], 200)


def restore_prefill(message: dict, prefill: str = ACTION_PREFILL) -> dict:
    """Put the prefilled brace back on the front of a json-mode reply.

    Which build you are talking to decides whether there is anything to repair:
    one echoes the assistant turn it was handed, so the content already opens
    with `{`, and another does not, so it comes back mid-object as
    `"tool": "click", ...}`. `raw if raw.lstrip().startswith("{") else "{" + raw`
    covers both, and nothing downstream has to know which server it got.
    """
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return message
    if content.lstrip().startswith(prefill):
        return message
    return {**message, "content": prefill + content}


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


# -------------------------------------------------------------- model transport

def cached_tokens(usage: dict) -> int | None:
    """How much of this prompt the server answered from its KV cache.

    llama-server reports it under `usage.prompt_tokens_details.cached_tokens`;
    other OpenAI-compatible servers flatten it or name it after their own cache.
    None means the server said nothing, which is not the same as a miss.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for key in ("cached_tokens", "cache_read_input_tokens"):
            if isinstance(details.get(key), int):
                return details[key]
    for key in ("cached_tokens", "prompt_cache_hit_tokens", "cache_read_input_tokens"):
        if isinstance(usage.get(key), int):
            return usage[key]
    return None


def ensure_image_last(messages: list[dict]) -> list[dict]:
    """Move the newest screenshot to the end of the message that carries it.

    The prefix cache only reuses a run of bytes that is identical from the start,
    so a line of text sitting after the picture re-encodes the picture too. The
    driver already builds the turn this way; this is the guard that says so.
    """
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        spots = [i for i, part in enumerate(content)
                 if isinstance(part, dict) and part.get("type") == "image_url"]
        if not spots:
            continue
        if spots[-1] != len(content) - 1:
            content.append(content.pop(spots[-1]))
        break
    return messages


class HttpModel:
    """One POST to <endpoint>/chat/completions, shaped like an OpenAI client's."""

    def __init__(self, endpoint: str, model: str, temp: float = 0.7,
                 max_tokens: int = 1024, timeout: int = 600, api_key: str | None = None,
                 action_mode: str = "tools", reasoning: str | None = None,
                 schema: dict | None = None, prefill: str = ACTION_PREFILL):
        self.url = endpoint.rstrip("/") + "/chat/completions"
        self.model, self.temp, self.max_tokens = model, temp, max_tokens
        self.timeout, self.api_key = timeout, api_key
        self.action_mode, self.reasoning = action_mode, reasoning
        self.schema, self.prefill = schema, prefill

    def build_body(self, messages: list[dict], tools: list[dict],
                   schema: dict | None = None) -> dict:
        """The request as a dict, so a test can read it without a server.

        json mode and native tool calls are mutually exclusive: the prefill is
        what buys the speed, and a prefilled assistant turn forbids a tool call.
        `schema` overrides the standing one for a single ask, which is what the
        re-ground question needs.
        """
        body = {"model": self.model, "messages": list(messages),
                "temperature": self.temp, "max_tokens": self.max_tokens}
        if self.reasoning:
            # Not a boolean and not a budget: the chat template takes a strength,
            # and there is no setting that turns the thinking off.
            body["chat_template_kwargs"] = {"reasoning_strength": self.reasoning}
        if self.action_mode == "json":
            body["messages"].append({"role": "assistant", "content": self.prefill})
            wanted = schema or self.schema
            if wanted:
                name = wanted.get("name") or "desktop_action"
                payload = wanted if "schema" in wanted else {"name": name, "schema": wanted}
                body["response_format"] = {"type": "json_schema", "json_schema": payload}
        else:
            # Never optional: without tools[] this llama-server drops into
            # content-only mode and the tool-call form comes back HTTP 500.
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def __call__(self, messages: list[dict], tools: list[dict],
                 schema: dict | None = None) -> dict:
        body = json.dumps(self.build_body(messages, tools, schema)).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


#: A data: URL's payload, so an image part can be put back on disk for a seat
#: that reads files rather than bytes on a wire.
DATA_URL_RE = re.compile(r"^data:image/(?P<kind>[a-z]+);base64,(?P<body>.*)$", re.S)


class ClaudeSeatModel:
    """The fallback rung: a logged-in Claude seat, called as a subprocess.

    Same call signature as HttpModel, so the driver cannot tell them apart. The
    screenshots are written into the run directory, which is also the seat's
    working directory, and named in the prompt as relative paths: the seat opens
    them with its own Read tool and this harness uploads nothing.

    The flag list is not decoration. Measured by the peer on the DGX: with its
    default tool set and the user's MCP servers the seat spent every one of its
    turns on its own session protocol and stopped on tool_use with no result
    (72K tokens, ~50 s). Read only, no MCP servers, project settings only: two
    turns, end_turn, 6 s, a parseable action. And a stop reason that is not
    end_turn is an ERROR, not an envelope to go fishing in - a half-finished
    seat turn that happens to contain a JSON object is the worst of both.

    Confirmed on a live run: the seat answers on the same 0-1000 grid as the
    local model, so a seat reply needs no separate coordinate handling.
    """

    name = "claude-seat"
    family = "claude"

    def __init__(self, model: str = SEAT_MODEL, cwd: str | None = None,
                 timeout: int = SEAT_TIMEOUT, max_turns: int = SEAT_MAX_TURNS,
                 log=print, run=None):
        self.model = model
        self.cwd = cwd or os.getcwd()
        self.timeout, self.max_turns = timeout, max_turns
        self.log = log
        #: Injected so a test can drive the whole seat with no `claude` binary.
        self._run_process = run or subprocess.run
        self.calls = 0

    def argv(self, prompt: str) -> list[str]:
        return ["claude", "-p", "--model", self.model, "--output-format", "json",
                "--max-turns", str(self.max_turns), "--tools", "Read",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "project", prompt]

    # -- the conversation, as a prompt and some files ------------------------
    def _write_images(self, messages: list[dict]) -> list[str]:
        """Every image part in the conversation, put back on disk beside the run."""
        names = []
        os.makedirs(self.cwd, exist_ok=True)
        for message in messages:
            for part in (message.get("content") if isinstance(message.get("content"), list)
                         else []):
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = str((part.get("image_url") or {}).get("url") or "")
                match = DATA_URL_RE.match(url)
                if not match:
                    continue
                kind = "jpg" if match.group("kind") == "jpeg" else match.group("kind")
                name = f"seat-{self.calls:02d}-{len(names) + 1:02d}.{kind}"
                try:
                    body = base64.b64decode(match.group("body"))
                except (ValueError, TypeError):
                    continue
                with open(os.path.join(self.cwd, name), "wb") as fh:
                    fh.write(body)
                names.append(name)
        return names

    @staticmethod
    def transcript(messages: list[dict]) -> tuple[str, str]:
        """(the system turn, everything else as plain text)."""
        system, lines = "", []
        for message in messages:
            text = message_text(message).strip()
            if message.get("role") == "system":
                system = system or text
                continue
            if text:
                lines.append(f"{message.get('role', 'user')}: {text}")
        return system, "\n".join(lines)

    def prompt_for(self, messages: list[dict], schema: dict, images: list[str]) -> str:
        system, history = self.transcript(messages)
        names = ", ".join("./" + name for name in images) or "(none)"
        return (f"{system}\n\n---\nRead these images with the Read tool, in order, and "
                f"decide from the LAST one: {names}\n\nThe run so far:\n{history}\n\n"
                f"Reply with ONLY a JSON object matching this schema, no prose, no "
                f"code fence:\n{json.dumps(schema)}")

    def __call__(self, messages: list[dict], tools: list[dict],
                 schema: dict | None = None) -> dict:
        self.calls += 1
        wanted = schema or action_schema(tools)
        payload = wanted.get("schema", wanted) if isinstance(wanted, dict) else {}
        images = self._write_images(messages)
        prompt = self.prompt_for(messages, payload, images)
        started = time.time()
        try:
            proc = self._run_process(self.argv(prompt), capture_output=True, text=True,
                                     timeout=self.timeout, cwd=self.cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failed(f"seat failed: {type(exc).__name__}: {exc}", started)
        if proc.returncode != 0:
            return self._failed(f"seat exited {proc.returncode}: "
                                f"{(proc.stderr or proc.stdout or '')[:400]}", started)
        envelope = json_object(proc.stdout) or {}
        result = envelope.get("result") or ""
        stop = envelope.get("stop_reason")
        if not result or stop not in (None, "end_turn"):
            # Deliberately not parsed: a seat that stopped early may still have
            # written a JSON object, and acting on it is acting on a half-turn.
            return self._failed(
                f"seat stopped on {stop} with {'no' if not result else 'a'} result "
                f"after {envelope.get('num_turns')} turns", started)
        usage = envelope.get("usage") or {}
        return {"choices": [{"message": {"role": "assistant", "content": result},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": usage.get("input_tokens"),
                          "completion_tokens": usage.get("output_tokens"),
                          "prompt_tokens_details": {
                              "cached_tokens": usage.get("cache_read_input_tokens")}},
                "seat": {"backend": self.name, "model": self.model,
                         "seconds": round(time.time() - started, 2)}}

    def _failed(self, error: str, started: float) -> dict:
        self.log(f"{self.name}: {error}")
        return {"choices": [{"message": {"role": "assistant", "content": ""},
                             "finish_reason": "error"}],
                "usage": {},
                "seat": {"backend": self.name, "model": self.model, "error": error,
                         "seconds": round(time.time() - started, 2)}}


def tool_call(name: str, args: dict, index: int = 0) -> dict:
    return {"id": f"call_{index}_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def json_action(name: str, args: dict, prefilled: bool = True,
                why: str | None = None, flat: bool = False) -> dict:
    """One json-mode assistant reply. `prefilled` drops the opening brace, which
    is what a server that was handed the `{` prefill actually sends back;
    `flat` picks the contract shape over the older nested one."""
    action = {"tool": name, **args} if flat else {"tool": name, "args": args}
    if why:
        action["why"] = why
    blob = json.dumps(action)
    return {"role": "assistant", "content": blob[1:] if prefilled else blob}


MOCK_SCRIPT = [
    {"role": "assistant", "content": "Opening the page.",
     "tool_calls": [tool_call("open_url", {"url": "https://example.com/"}, 0)]},
    {"role": "assistant", "content": "Let me look at it.",
     "tool_calls": [tool_call("look", {}, 1)]},
    {"role": "assistant", "content": "Clicking the row.",
     "tool_calls": [tool_call("click", {"x": 512, "y": 300}, 2)]},
    {"role": "assistant", "content": "I can read the answer.",
     "tool_calls": [tool_call("done", {"answer": "passed"}, 3)]},
]

#: The click --mock exercises on the 1000 grid. On a 1920x1080 screen shown at
#: 1024 wide this is 512 x 195 image pixels and lands at 960,365 on the screen —
#: the whole conversion, end to end, in one scripted step.
#: The answer --mock gives to the re-ground question the dead click provokes.
#: Not an echo of the click that missed, which is the failure the echo check
#: exists for, and not the centre either: a real re-aim points somewhere else.
MOCK_REGROUND = {"target": "the first result row", "x": 500, "y": 620}
MOCK_GRID_CLICK = {"x": 500, "y": 340}
MOCK_PIXEL_CLICK = {"x": 512, "y": 300}
#: Which mock screenshot repeats the one before it. The third is the one the
#: scripted click produces, so the click is the step that changed nothing.
MOCK_STUCK_SHOT = 3


def json_reply(obj: dict, prefilled: bool = True) -> dict:
    """One raw JSON object reply, which is the shape ask_json reads back."""
    blob = json.dumps(obj)
    return {"role": "assistant", "content": blob[1:] if prefilled else blob}


def mock_script(action_mode: str = "json", coord_grid: str = "1000") -> list[dict]:
    """The scripted run behind `--mock`, in whichever dialect the flags ask for.

    It walks the whole surface that matters: open a page, look, zoom into a
    strip of it, click a point that turns out to be dead, answer the re-ground
    question that provokes, click again on the crop's own grid, type, answer.
    The first click proves the coordinate chain, the second proves the crop's,
    and the typed string proves the trajectory's credential guard.
    """
    steps = [
        ("open_url", {"url": "https://example.com/"}, "the task names a page"),
        ("look", {}, "check what loaded"),
        ("zoom", {"x1": 300, "y1": 60, "x2": 700, "y2": 110}, "the heading is small"),
        ("click", MOCK_GRID_CLICK if coord_grid == "1000" else MOCK_PIXEL_CLICK,
         "the row is halfway down"),
        # Nothing here is a real credential; the point is that the guard sees
        # one, drops the line, and the run carries on driving.
        ("type", {"text": "opensesame-token"}, "the field wants a value"),
        ("done", {"answer": "passed"}, "the answer is on the screen"),
    ]
    if action_mode == "json":
        # Flat, because that is the contract the schema now enforces; the nested
        # form stays parseable and is pinned by its own tests.
        script = [json_action(name, args, why=why, flat=True) for name, args, why in steps]
    else:
        script = [{"role": "assistant", "content": f"Calling {name}.",
                   "tool_calls": [tool_call(name, args, i)]}
                  for i, (name, args, _why) in enumerate(steps)]
    # The click above lands on a screen that does not move, which queues a
    # re-ground; this is the answer to the question that asks.
    script.insert(4, json_reply(MOCK_REGROUND, prefilled=action_mode == "json"))
    return script


class ScriptedModel:
    """Replays a fixed list of assistant messages; records what it was sent."""

    def __init__(self, script: list[dict] | None = None):
        self.script = [dict(m) for m in (script if script is not None else MOCK_SCRIPT)]
        self.requests: list[list[dict]] = []
        self.schemas: list[dict | None] = []
        self.calls = 0

    def __call__(self, messages: list[dict], tools: list[dict],
                 schema: dict | None = None) -> dict:
        self.requests.append(json.loads(json.dumps(messages)))
        self.schemas.append(schema)
        if self.calls < len(self.script):
            message = dict(self.script[self.calls])
        else:
            message = {"role": "assistant", "content": "I am not sure what to do next."}
        self.calls += 1
        # The cache can only hold what a previous step already sent, so the
        # first call hits nothing and every later one hits the step before it.
        cached = 0 if self.calls == 1 else 1000 + 300 * (self.calls - 1)
        return {"choices": [{"message": message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 1000 + 300 * self.calls,
                          "completion_tokens": 24,
                          "total_tokens": 1024 + 300 * self.calls,
                          "prompt_tokens_details": {"cached_tokens": cached}}}


# ---------------------------------------------------------------------- driver

@dataclass
class Result:
    """What one tool call produced: a line of text, and maybe a new screen."""

    text: str
    shot: Shot | None = None
    coords: tuple[int, int] | None = None
    detail: dict = field(default_factory=dict)
    #: How much the screen moved between the frame the action was decided on
    #: and the one it produced. None when there was nothing to compare.
    diff: float | None = None
    #: The full screenshot this step produced, even when the picture handed back
    #: is a crop of it. It is what the step is judged against, because a crop
    #: and a whole screen are not comparable pictures.
    screen_path: str | None = None
    #: A zoom crop is a picture, but it is not the screen: clicks still measure
    #: against the last full screenshot, so a crop never becomes `last_shot`.
    is_crop: bool = False
    #: Set when the action brought a different window to the front and had no
    #: business doing so. The screen changed, so the diff reads as a success -
    #: which is exactly the failure this catches.
    wrong_target: bool = False


def action_ok(text: str, outcome: str | None) -> bool:
    """Did the step actually do what it said? Not whether it helped - whether it
    happened. Only an action that happened is worth telling the model not to
    repeat."""
    low = (text or "").lower()
    return outcome != "refused" and not any(m in low for m in ACTION_FAILED_MARKERS)


def is_refusal(detail) -> bool:
    """Did the backend say no? A refusal did not happen, so it is not evidence
    that the screen is dead - and it must not read as one."""
    blob = detail if isinstance(detail, dict) else {}
    last = blob.get("last") if isinstance(blob.get("last"), dict) else {}
    for part in (blob, last):
        if part.get("ok") is False:
            return True
        if "unsupported" in str(part.get("error") or "").lower():
            return True
    return False


def is_neutral(name: str, args: dict) -> bool:
    """A key that is silent by nature: select-all on an already-selected field,
    a copy, a bare modifier. None of them repaints anything, so a still screen
    after one says nothing at all about whether the UI is alive."""
    return (name == "key"
            and str(args.get("combo") or "").strip().lower() in NEUTRAL_KEYS)


def step_outcome(name: str, args: dict, result: Result) -> str | None:
    """The part of a step's outcome that is known the moment it ran.

    A backend that said no is `refused`; a click that brought a different window
    to the front is `wrong_target`, the one machine-decidable case of it; a done
    call that admits it could not finish is `gave_up`. Everything else waits for
    the next screenshot, which is the only thing that can tell `ok` from
    `no_change`.
    """
    if is_refusal(result.detail):
        return "refused"
    if result.wrong_target:
        return "wrong_target"
    if name == "give_up":
        return "gave_up"
    if name == "done" and GAVE_UP_RE.search(str(args.get("answer") or "")):
        return "gave_up"
    return None


class Driver:
    """The loop. Two defaults here are deliberately the older ones: a Driver
    built in code keeps the pixel grid and native tool calls it always had,
    while the command line hands it the measured Muse recipe instead."""

    def __init__(self, backend, model, task: str, run_dir: str, *,
                 image_channel: str = "user", max_steps: int = 25,
                 image_width: int = IMAGE_WIDTH, screen: tuple[int, int] | None = None,
                 url_bar_key: str = URL_BAR_KEY, coord_grid: str = "pixels",
                 action_mode: str = "tools", keep_images: int | None = None,
                 trajectory_dir: str | None = None, run_id: str | None = None,
                 machine: str | None = None, driver: str | None = None,
                 park: bool = True, seat=None, max_escalations: int = MAX_ESCALATIONS,
                 notes: str = "", sleep=time.sleep, log=print):
        self.backend, self.model, self.task = backend, model, task
        self.run_dir, self.image_channel = run_dir, image_channel
        self.max_steps, self.image_width, self.screen = max_steps, image_width, screen
        self.url_bar_key = url_bar_key
        self.coord_grid, self.action_mode = coord_grid, action_mode
        self.keep_images = (keep_images if keep_images is not None
                            else KEEP_IMAGES_JSON if action_mode == "json" else KEEP_IMAGES)
        self.sleep, self.log = sleep, log
        self.tools = tools_for(coord_grid)
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt(coord_grid, action_mode,
                                                        self.tools, notes)},
            {"role": "user", "content": f"Task: {task}"},
        ]
        self._image_slots: list[dict] = []
        self._raw_dir: str | None = None
        self._last_raw: str | None = None
        self.step = 0
        self.last_shot: Shot | None = None
        #: The newest FULL screenshot. Usually last_shot's own file; after a
        #: forced re-ground last_shot is a crop and this is the screen it came
        #: from, which is what the next step is judged against.
        self.last_screen: str | None = None
        #: The step before this one, in the short form, so a repeat is one
        #: string comparison.
        self.prev_action: str | None = None
        #: Whether that step actually did what it said. A repeat of an action
        #: that worked is worth one nudge; a repeat of one that failed is not.
        self.last_action_ok = False
        self.park = park
        self._screen_points: tuple[int, int] | None = None
        #: The re-ground queued by the last dead click, consumed by the next step.
        self.reground: dict | None = None
        self.reground_done: set[str] = set()
        self.fail_counts: dict[str, int] = {}
        #: The target a re-grounded click is aiming at, so the retry's failure
        #: lands on the counter the first miss opened rather than a fresh one.
        self.active_target_key: str | None = None
        #: Consecutive steps that changed nothing, per acting backend.
        self.no_change_streak = 0
        self.last_acting: str | None = None
        #: The fallback seat, and how many steps it may take in one run. The
        #: counter is the whole safety rail: a seat that cannot unstick the run
        #: either must not be allowed to spend it.
        self.seat = seat
        self.max_escalations = max_escalations
        self.escalations = 0
        self.force_escalate = False
        self.escalate_reason = ""
        #: Whichever seat is answering this step. The streak rule is written
        #: per backend, so handing over resets it.
        self.active_model = model
        self.answer: str | None = None
        self.done = False
        #: Set when the model called give_up and there was no seat left to take
        #: the step: the run ends, unfinished, rather than burning the budget.
        self.gave_up = False
        self.steps_path = os.path.join(run_dir, "steps.jsonl")
        self.machine = machine or machine_name()
        self.run_id = run_id or new_run_id(self.machine)
        self.driver_id = driver or driver_id(model)
        #: The plain-text account of the run so far, one line per step. It is
        #: what a training line carries as `history`, and it is text only.
        self.history: list[str] = []
        #: Which app the last open_url put in front. Fact (d): a keystroke with
        #: nothing focused hits the page's own single-key shortcuts, so a type
        #: checks this first.
        self.focus_target: str | None = None
        self.trajectory = (Trajectory(trajectory_dir, self.run_id, self.driver_id,
                                      self.machine, task, log=self.log)
                           if trajectory_dir else None)

    # -- screenshots --------------------------------------------------------
    def capture(self) -> Shot:
        """Shoot the screen, shrink it for the model, keep the full frame for zoom.

        The full-resolution PNG lives in a scratch directory, not the run
        directory: it is working material for one step, where the JPEG the model
        actually saw is the artifact worth keeping next to the log.
        """
        os.makedirs(self.run_dir, exist_ok=True)
        self.park_pointer()
        raw = os.path.join(self._scratch(), f"step-{self.step:02d}.png")
        jpg = os.path.join(self.run_dir, f"step-{self.step:02d}.jpg")
        meta = self.backend.shot(raw) or {}
        if not os.path.exists(raw):
            raise RuntimeError(f"wm.py shot wrote no file: {json.dumps(meta, default=str)[:400]}")
        raw_w, raw_h = image_size(raw) or (0, 0)
        image_w, image_h = downscale(raw, jpg, self.image_width)
        if self._last_raw and self._last_raw != raw and os.path.exists(self._last_raw):
            os.unlink(self._last_raw)
        self._last_raw = raw
        self.last_screen = jpg
        screen_w, screen_h = self._screen_size(meta, image_w, image_h)
        self._screen_points = (screen_w, screen_h)
        return Shot(step=self.step, path=jpg, image_w=image_w, image_h=image_h,
                    screen_w=screen_w, screen_h=screen_h,
                    origin_x=int(meta.get("origin_x") or 0),
                    origin_y=int(meta.get("origin_y") or 0),
                    raw_path=raw, raw_w=raw_w, raw_h=raw_h,
                    pointer=self.pointer(), window=window_label(self.backend.active()))

    def pointer(self) -> tuple[int, int] | None:
        """Where the cursor is, in screen points, or None if nothing said."""
        out = self.backend.pointer() or {}
        if out.get("ok") and out.get("x") is not None and out.get("y") is not None:
            try:
                return int(out["x"]), int(out["y"])
            except (TypeError, ValueError):
                return None
        return None

    def park_pointer(self) -> None:
        """Move the cursor to the corner before every frame that will be diffed.

        Parking, not masking. Measured on GitHub: the cursor glyph is 0.0002 of
        the frame, but the row it hovers lights up for 0.008 - over the 0.005
        threshold - and a mask wide enough to cover the highlight also covers
        the checkmark the step is trying to see. Parking takes the hover with
        it: 0.00798 -> 0.00001. Nothing else in the run may skip this, which is
        why it lives in capture rather than in the caller.
        """
        if not self.park:
            return
        size = self.screen or self._screen_points
        if not size:
            return          # first frame: nothing has reported a screen size yet
        w, h = size
        self.backend.park(max(0, w - PARK_MARGIN[0]), max(0, h - PARK_MARGIN[1]))
        self.sleep(0.25)

    def _scratch(self) -> str:
        if self._raw_dir is None:
            self._raw_dir = tempfile.mkdtemp(prefix="small-model-drive-")
        return self._raw_dir

    def cleanup(self) -> None:
        """Drop the scratch frames. The run directory is untouched."""
        if self._raw_dir and os.path.isdir(self._raw_dir):
            shutil.rmtree(self._raw_dir, ignore_errors=True)
        self._raw_dir = self._last_raw = None

    def _screen_size(self, meta: dict, image_w: int, image_h: int) -> tuple[int, int]:
        """Screen points, from whichever key this backend reports them under."""
        if self.screen:
            return self.screen
        if meta.get("points_w") and meta.get("points_h"):
            return int(meta["points_w"]), int(meta["points_h"])
        pair = meta.get("screen")
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            return int(pair[0]), int(pair[1])
        if meta.get("native_w") and meta.get("native_h"):
            return int(meta["native_w"]), int(meta["native_h"])
        return image_w, image_h

    # -- conversation -------------------------------------------------------
    def _image_part(self, shot: Shot) -> dict:
        return {"type": "image_url", "image_url": {"url": shot.data_url()}}

    def _attach(self, call_id: str, name: str, result: Result) -> None:
        """Feed one tool result back. Images inside a `tool` message are rejected
        by some chat templates, so `--image-channel user` puts the picture in a
        following user turn instead; the tool message stays plain text.

        json mode has no tool role at all: the result is a user turn, and the
        picture is the last thing in it, which is the shape the prefix cache
        wants.
        """
        if self.action_mode == "json":
            if result.shot is None:
                self.messages.append({"role": "user", "content": result.text})
                return
            content = [{"type": "text", "text": result.text}, self._image_part(result.shot)]
            self.messages.append({"role": "user", "content": content})
            self._track(content, 1, result.shot.step, result.is_crop)
            return
        if result.shot is None:
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "name": name, "content": result.text})
            return
        if self.image_channel == "tool":
            content = [{"type": "text", "text": result.text}, self._image_part(result.shot)]
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "name": name, "content": content})
            self._track(content, 1, result.shot.step, result.is_crop)
            return
        self.messages.append({"role": "tool", "tool_call_id": call_id,
                              "name": name, "content": result.text})
        content = [{"type": "text", "text": "Screen after your last action:"},
                   self._image_part(result.shot)]
        self.messages.append({"role": "user", "content": content})
        self._track(content, 1, result.shot.step, result.is_crop)

    def _track(self, content: list, index: int, step: int, crop: bool = False) -> None:
        self._image_slots.append({"content": content, "index": index,
                                  "step": step, "crop": crop})

    @staticmethod
    def _blank(slot: dict) -> None:
        """Swap one image part for the line that stands in for it."""
        content, index = slot["content"], slot["index"]
        part = content[index] if index < len(content) else None
        if not (isinstance(part, dict) and part.get("type") == "image_url"):
            index = next((i for i, p in enumerate(content)
                          if isinstance(p, dict) and p.get("type") == "image_url"), None)
            if index is None:
                return
        content[index] = {"type": "text",
                          "text": f"[screenshot from step {slot['step']} removed]"}

    def prune_images(self, keep: int | None = None) -> int:
        """Replace every image part but the newest `keep` with a placeholder line.

        One exception: when the survivors are all zoom crops, the newest full
        screenshot is kept as well. A crop is a detail of the screen, not the
        screen, and a model asked to click with only a detail in front of it has
        nothing left to aim at.
        """
        keep = self.keep_images if keep is None else keep
        survivors = self._image_slots[-keep:] if keep else []
        kept = {id(slot) for slot in survivors}
        if survivors and all(slot["crop"] for slot in survivors):
            full = next((s for s in reversed(self._image_slots) if not s["crop"]), None)
            if full is not None and id(full) not in kept:
                survivors = [full] + survivors
                kept.add(id(full))
        for slot in self._image_slots:
            if id(slot) not in kept:
                self._blank(slot)
        self._image_slots = survivors
        return len(self._image_slots)

    def outgoing(self) -> list[dict]:
        """The exact message list to send this step: pruned to the image budget,
        with the newest picture as the very last content item."""
        self.prune_images()
        return ensure_image_last(self.messages)

    def count_images(self) -> int:
        n = 0
        for message in self.messages:
            content = message.get("content")
            if isinstance(content, list):
                n += sum(1 for p in content
                         if isinstance(p, dict) and p.get("type") == "image_url")
        return n

    # -- tools --------------------------------------------------------------
    def _look(self, args: dict | None = None) -> Result:
        """Take the frame - after a pause, when the peer's `wait` asked for one.

        `wait` is not a tool of ours, it is `look` with a number in front of it,
        so it waits and then does exactly what look does: the same frame, the
        same byte-identical sentence, because the prefix cache reuses it. The
        pause is capped, and it goes through the loop's injected sleep so a
        mock run still costs nothing.
        """
        self._wait_out((args or {}).get("seconds"))
        shot = self.capture()
        return Result(f"Screenshot taken. The image is {shot.image_w}x{shot.image_h} pixels.",
                      shot=shot, screen_path=shot.path)

    def _wait_out(self, seconds) -> None:
        """Sleep for what the model asked, up to WAIT_CAP. A number that is not
        one, or is not positive, waits not at all rather than raising."""
        if isinstance(seconds, str):
            try:
                seconds = float(seconds)
            except ValueError:
                return
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return
        if seconds > 0:
            self.sleep(min(seconds, WAIT_CAP))

    def _after(self, text: str, coords=None, detail=None, action=None,
               tool: str | None = None, args: dict | None = None) -> Result:
        """Act, wait, look again - and say plainly what the new frame means.

        Two things this has to catch, both measured. A model told `Clicked
        (165,605) on the screenshot.` and handed back a picture identical to the
        one before it concludes the click worked and clicks the same spot again.
        And a click that lands 12 points inside another window brings that
        window forward, which moves a great many pixels, so the diff reads as a
        resounding success while the model answers from memory. The ok wording
        is left byte-identical, because the prefix cache reuses it.
        """
        self.sleep(0.6)
        before = self.last_shot
        shot = self.capture()
        diff = None
        if action and before:
            # The pointer is blanked in both frames at both positions, so the
            # glyph cannot be the change even when parking did not happen.
            diff = screenshot_diff(before.path, shot.path,
                                   blank_points=[before.pointer_px(), shot.pointer_px()])
        wrong = bool(action and before and switched_window(before.window, shot.window)
                     and not switch_allowed(tool or "", args or {}))
        if wrong:
            text += (f" That click brought a different window to the front: "
                     f"{shot.window}. The window you were working in was "
                     f"{before.window}.")
        elif is_refusal(detail):
            # A refusal did not run, so the still screen is the refusal's doing
            # and says nothing about the target.
            pass
        elif diff is not None and diff < NO_CHANGE_DIFF:
            if is_neutral(tool or "", args or {}):
                text += f"; {NEUTRAL_NOTE}."
            else:
                text += (f" Nothing on the screen changed after {action}. The target "
                         "was probably missed: zoom on it and re-aim, or choose a "
                         "different action.")
        return Result(text, shot=shot, coords=coords, detail=detail or {}, diff=diff,
                      screen_path=shot.path, wrong_target=wrong)

    def execute(self, name: str, args: dict) -> Result:
        if name == "look":
            return self._look(args)
        if name == "done":
            self.answer = str(args.get("answer", "")).strip()
            self.done = True
            return Result(f"Run finished. Answer: {self.answer}")
        if name == "give_up":
            reason = str(args.get("reason") or "no reason given").strip()
            if self.seat is not None and self.escalations < self.max_escalations:
                # The ladder before the exit: the seat has not had its turn yet.
                self.force_escalate = True
                self.escalate_reason = f"model gave up: {reason}"
                return Result(f"Gave up ({reason}); the next step goes to the "
                              f"fallback seat.")
            self.gave_up = True
            return Result(f"Run stopped. The model gave up: {reason}")
        if name == "click":
            return self._click(args)
        if name == "zoom":
            return self._zoom(args)
        if name == "type":
            text = args.get("text")
            if not isinstance(text, str) or not text:
                return self._after("type needs a non-empty 'text' string.")
            note = self._check_focus(args)
            out = self.backend.type(text)
            return self._after(note + f"Typed {len(text)} characters.", detail=out,
                               action=compact_action("type", {"text": text}),
                               tool="type", args=args)
        if name == "key":
            combo = str(args.get("combo") or "").strip()
            if not combo:
                return self._after("key needs a 'combo' such as ctrl+l or Return.")
            out = self.backend.key(combo)
            return self._after(f"Pressed {combo}.", detail=out,
                               action=compact_action("key", {"combo": combo}),
                               tool="key", args=args)
        if name == "scroll":
            return self._scroll(args)
        if name == "open_url":
            return self._open_url(args)
        return Result(f"There is no tool called {name!r}.")

    def _check_focus(self, args: dict | None = None) -> str:
        """Everything that has to be true before a keystroke is worth sending.

        Two checks, in order. The window one is fact (d): typing with nothing
        focused hits the page's own single-key shortcuts, and one measured run
        navigated itself to /copilot that way. One `active` call is the whole
        fix, and it only runs once an open_url has said which app belongs in
        front.

        The field one is the peer's, moved off the full frame: asked about a
        whole screenshot the same picture came back focused once and unfocused
        twice, so the question now goes to a 1000x120 native strip around the
        field's own point, where a caret or a highlighted border is the only
        thing in shot. It needs that point, so it only runs when the type
        carried x,y; without them this is exactly the check it always was.
        """
        note = self._check_window()
        point = self._field_point(args or {})
        if point is None:
            return note
        focused = self._field_focused(point)
        if focused is not False:
            return note
        # One click on the field, and one only: a first click can be swallowed
        # by the window coming forward, but a second is just guessing.
        result = self._click(dict(args or {}))
        # That click took its own frame, and the frame the first question was
        # asked on has already been swept: the second question has to be asked
        # on the picture that exists now.
        if result.shot is not None and not result.is_crop:
            self.last_shot = result.shot
        clicked = (f"The field at ({args.get('x')},{args.get('y')}) did not look "
                   f"focused, so it was clicked once before typing. ")
        again = self._field_point(args or {})
        if again is not None and self._field_focused(again, suffix="-2") is False:
            clicked += "It still does not look focused. "
        where = {"x": (args or {}).get("x"), "y": (args or {}).get("y")}
        self.history.append(f"step {self.step}: {compact_action('focus_field', where)} "
                            f"-> {clicked.strip()}")
        return note + clicked

    def _check_window(self) -> str:
        """Bring the app the last open_url named back to the front, if it left."""
        target = self.focus_target
        if not target:
            return ""
        active = self.backend.active() or {}
        app = str(active.get("app") or active.get("title") or "")
        if app and target.lower() in app.lower():
            return ""
        self.backend.focus(target)
        note = (f"Focus was on {app or 'no window'}, so {target} was brought forward "
                f"before typing. ")
        self.history.append(f"step {self.step}: {compact_action('focus', {'app': target})} "
                            f"-> {note.strip()}")
        return note

    def _field_point(self, args: dict) -> tuple[int, int] | None:
        """The field's point in full-resolution pixels, or None when the type
        gave no point and the old whole-window check is all there is."""
        shot = self.last_shot
        if shot is None or shot.raw_path is None or not os.path.exists(shot.raw_path):
            return None
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not self._in_range(shot, x, y):
            return None
        px, py = self.to_image(shot, x, y)
        return shot.to_raw(px, py)

    def _field_focused(self, point: tuple[int, int],
                       suffix: str = "") -> bool | None:
        """Ask on a 1000x120 native strip round the field. None means the model
        did not answer, which is not the same as `no` and never clicks."""
        shot = self.last_shot
        if shot is None or shot.raw_path is None:
            return None
        # .jpg, because crop() writes a JPEG whatever the name says, and a file
        # whose extension lies is the kind of thing that costs an hour later.
        dst = os.path.join(self.run_dir, f"step-{self.step:02d}-focus{suffix}.jpg")
        half_w, half_h = FOCUS_CROP_W // 2, FOCUS_CROP_H // 2
        box = (max(0, point[0] - half_w), max(0, point[1] - half_h),
               point[0] + half_w, point[1] + half_h)
        try:
            crop(shot.raw_path, dst, box)
        except (OSError, RuntimeError, ValueError) as exc:
            self.log(f"step {self.step}: focus crop failed: {exc}")
            return None
        question = (f"This is a {FOCUS_CROP_W}x{FOCUS_CROP_H} screen-pixel crop round a "
                    f"text field. Is that field focused and ready to receive typed "
                    f"characters (a highlighted or coloured border, or a visible "
                    f'caret)? Reply with only {{"text_field_focused": true}} or '
                    f'{{"text_field_focused": false}}.')
        answer = self.ask_json(question, dst, schema=FOCUS_SCHEMA,
                               keys=("text_field_focused",))
        if not answer:
            return None
        return bool(answer.get("text_field_focused"))

    # -- coordinates --------------------------------------------------------
    def _in_range(self, shot: Shot, x: float, y: float) -> bool:
        if self.coord_grid == "1000":
            return 0 <= x <= GRID and 0 <= y <= GRID
        return shot.inside(x, y)

    def _out_of_range(self, shot: Shot, x, y) -> str:
        """The refusal, naming the grid the numbers were read against."""
        if self.coord_grid == "1000":
            return (f"({x},{y}) is outside the screenshot: you address it on a 0 to {GRID} "
                    f"grid across its width and its height, so both numbers have to be "
                    f"between 0 and {GRID}.")
        return (f"({x},{y}) is outside the screenshot, which is "
                f"{shot.image_w}x{shot.image_h} pixels. Pick a point inside it.")

    def to_image(self, shot: Shot, x: float, y: float) -> tuple[int, int]:
        """A point as the model gave it -> pixels of the picture it was shown.

        On the grid that is `x * image_w / 1000`, truncated, and the far edge
        clamps inside the picture: 1000 means the last column, not one past it.
        """
        px, py = shot.from_grid(x, y) if self.coord_grid == "1000" else (int(x), int(y))
        return (min(px, max(0, shot.image_w - 1)), min(py, max(0, shot.image_h - 1)))

    def to_grid(self, shot: Shot, x: float, y: float) -> tuple[int, int]:
        """A point as the model gave it -> the 0-GRID grid the schema records.

        On the grid that is the number itself; in pixel mode it is the fraction
        of the picture that pixel sits at, which is what makes a run recorded in
        either dialect trainable as one corpus.
        """
        if self.coord_grid == "1000":
            return int(x), int(y)
        return (int(round(x * GRID / max(1, shot.image_w))),
                int(round(y * GRID / max(1, shot.image_h))))

    #: Which args of a tool are points, so one rule converts them all.
    POINT_ARGS = {"click": (("x", "y"),), "zoom": (("x1", "y1"), ("x2", "y2"))}

    def action_view(self, shot: Shot, name: str, args: dict,
                    why: str = "") -> tuple[dict, list[int] | None]:
        """The action twice: on the grid, which is how the schema stores it, and
        in pixels of the screenshot it was read off, for a reader whose
        coordinate convention is neither of ours."""
        fields, pixels = dict(args), None
        pairs = self.POINT_ARGS.get(name)
        if pairs:
            try:
                points = [(int(args[a]), int(args[b])) for a, b in pairs]
            except (KeyError, TypeError, ValueError):
                points = []
            if points:
                pixels = []
                for (a, b), (x, y) in zip(pairs, points):
                    fields[a], fields[b] = self.to_grid(shot, x, y)
                    pixels += list(self.to_image(shot, x, y))
        return {"tool": name, **fields, "why": why}, pixels

    def _click(self, args: dict) -> Result:
        shot = self.last_shot or self.capture()
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return self._after("click needs integer 'x' and 'y'. "
                               + CLICK_GRID[self.coord_grid])
        if not self._in_range(shot, x, y):
            return self._after(self._out_of_range(shot, x, y))
        px, py = self.to_image(shot, x, y)
        sx, sy = shot.to_screen(px, py)
        out = self.backend.click(sx, sy)
        return self._after(f"Clicked ({x},{y}) on the screenshot.", coords=(sx, sy),
                           detail=out, action=compact_action("click", {"x": x, "y": y}),
                           tool="click", args=args)

    def _zoom(self, args: dict) -> Result:
        """Hand back a crop of the last full screenshot at its native resolution.

        No action runs, so no fresh screenshot comes back: the crop is the whole
        answer, and the screen is exactly where it was.
        """
        shot = self.last_shot
        if shot is None:
            shot = self.last_shot = self.capture()
        try:
            x1, y1, x2, y2 = (int(args[k]) for k in ("x1", "y1", "x2", "y2"))
        except (KeyError, TypeError, ValueError):
            return Result("zoom needs integer 'x1', 'y1', 'x2' and 'y2'. "
                          + ZOOM_HELP[self.coord_grid])
        for corner in ((x1, y1), (x2, y2)):
            if not self._in_range(shot, *corner):
                return Result(self._out_of_range(shot, *corner))
        left, top = self.to_image(shot, x1, y1)
        right, bottom = self.to_image(shot, x2, y2)
        if right <= left or bottom <= top:
            return Result("zoom needs x2 bigger than x1 and y2 bigger than y1.")
        if not (shot.raw_path and os.path.exists(shot.raw_path)):
            return Result("The full-resolution frame behind that screenshot is gone, so "
                          "there is nothing to crop. Call look, then zoom again.")
        box = shot.to_raw(left, top) + shot.to_raw(right, bottom)
        dst = os.path.join(self.run_dir, f"step-{self.step:02d}-zoom.jpg")
        try:
            crop_w, crop_h = crop(shot.raw_path, dst, box)
        except (OSError, RuntimeError, ValueError) as exc:
            return Result(f"zoom could not cut that crop: {exc}")
        sx1, sy1 = shot.to_screen(left, top)
        sx2, sy2 = shot.to_screen(right, bottom)
        crop_shot = Shot(step=self.step, path=dst, image_w=crop_w, image_h=crop_h,
                         screen_w=max(1, sx2 - sx1), screen_h=max(1, sy2 - sy1),
                         origin_x=sx1, origin_y=sy1)
        return Result(
            f"Zoom of ({x1},{y1}) to ({x2},{y2}), {crop_w}x{crop_h} pixels at full "
            f"resolution. Coordinates for click still come from the full screenshot, "
            f"not from this crop.",
            shot=crop_shot, is_crop=True, detail={"crop_box": list(box)})

    def _scroll(self, args: dict) -> Result:
        direction = str(args.get("direction") or "down").lower()
        if direction not in ("up", "down"):
            return self._after("scroll needs direction 'up' or 'down'.")
        try:
            amount = int(args.get("amount") or 1)
        except (TypeError, ValueError):
            amount = 1
        amount = max(1, min(10, amount))
        # wm.py has no scroll verb; a page key is the portable equivalent.
        combo = "Page_Down" if direction == "down" else "Page_Up"
        outs = []
        for _ in range(amount):
            outs.append(self.backend.key(combo))
            self.sleep(0.2)
        return self._after(f"Scrolled {direction} {amount} page(s).",
                           detail={"key": combo, "times": amount, "last": outs[-1]},
                           action=compact_action("scroll", {"direction": direction,
                                                            "amount": amount}),
                           tool="scroll", args=args)

    def _open_url(self, args: dict) -> Result:
        url = str(args.get("url") or "").strip()
        if not url:
            return self._after("open_url needs a 'url'.")
        focused = self.backend.focus("Chrome")
        if isinstance(focused, dict) and (focused.get("ok") is False or "error" in focused):
            # Never launch anything: say so and let the model choose again.
            return self._after(
                "No Chrome window to focus, so nothing was opened: "
                f"{focused.get('error', 'focus failed')}", detail=focused)
        self.focus_target = "Chrome"
        self.sleep(0.5)
        self.backend.key(self.url_bar_key)
        self.sleep(0.3)
        self.backend.type(url)
        # Chrome inline-autocompletes the omnibox from history and leaves the
        # appended part SELECTED, so Return commits the suggestion instead of
        # what was typed. Measured: typing search.google.com/search-console
        # became .../search-console?resource_id=sc-domain%3A...&authuser=2 --
        # a different page, while this tool still reported the typed url as
        # opened. Delete drops that selection; with nothing selected the
        # caret is at the end of the line and it is a no-op.
        self.backend.key("Delete")
        self.backend.key("Return")
        self.sleep(3.0)
        return self._after(f"Opened {url} in Chrome.", detail=focused,
                           action=compact_action("open_url", {"url": url}),
                           tool="open_url", args=args)

    # -- forced re-ground ----------------------------------------------------
    def _target_key(self, why: str, args: dict) -> str:
        """What the model said it was aiming at, normalised, so two attempts at
        the same control share a counter. The rounded grid point is the fallback
        for a model that gave no reason."""
        text = re.sub(r"[^a-z0-9]+", " ", (why or "").lower()).strip()
        if text:
            return _short(text, 60)
        try:
            return f"grid:{round(int(args['x']) / 10) * 10},{round(int(args['y']) / 10) * 10}"
        except (KeyError, TypeError, ValueError):
            return "grid:?"

    def bump_target(self, key: str) -> int:
        """One more ineffective or refused action on this target. Two is what
        hands the next step to the fallback seat."""
        self.fail_counts[key] = self.fail_counts.get(key, 0) + 1
        return self.fail_counts[key]

    def queue_reground(self, args: dict, why: str, result: Result) -> None:
        """A click that moved nothing: line up one zoomed re-aim at that target.

        Once per target, and only once: if the re-aimed click misses too, the
        counter reaches two and the streak rule takes it from there. Telling the
        model in words does not survive temperature 0.1 - measured, nine clicks
        in a row at the same wrong point, every step promising to re-aim, zoom
        never called.
        """
        frame = self.last_shot
        if not (result.coords and frame and frame.raw_path
                and os.path.exists(frame.raw_path)):
            return
        # A re-grounded retry aims at the target the first miss opened, not at
        # a fresh one named after its own `why`: two attempts at one control
        # have to share a counter or the counter never reaches two.
        key = self.active_target_key or self._target_key(why, args)
        self.bump_target(key)
        if key in self.reground_done:
            return
        self.reground_done.add(key)
        # The pre-action frame's scratch copy is already swept, and since
        # nothing changed the post frame is the same picture.
        self.reground = {"screen": list(result.coords), "key": key, "frame": frame,
                         "grid": [args.get("x"), args.get("y")],
                         "target": why or self.task}
        self.history.append(
            f"step {self.step}: {compact_action('reground', args)} -> that click "
            f"changed nothing; the next step re-aims on a zoomed view of the point")

    def reground_step(self, pending: dict) -> dict | None:
        """Cut the crop, ask where the target really is, and turn the answer
        into a click. Returns a plan for this step, or None to fall back to an
        ordinary model turn."""
        frame = pending["frame"]
        if not (frame.raw_path and os.path.exists(frame.raw_path)):
            return None
        raw_w = frame.raw_w or frame.image_w
        raw_h = frame.raw_h or frame.image_h
        cx, cy = self._screen_to_raw(frame, *pending["screen"])
        box_w = max(64, raw_w // REGROUND_FRACTION_W)
        box_h = max(64, raw_h // REGROUND_FRACTION_H)
        # Biased down, because every measured miss was upward: the point that
        # was clicked belongs above the middle, so the target is inside.
        centre = (cx, cy + box_h // REGROUND_BIAS)
        crop_path = os.path.join(self.run_dir, f"step-{self.step:02d}-reground.png")
        box = crop_with_marker(frame.raw_path, cx, cy, box_w, box_h, crop_path,
                               centre=centre)
        if box is None:
            return None
        crop_w, crop_h = box[2] - box[0], box[3] - box[1]
        sent_w, sent_h = self._raw_to_screen(frame, crop_w, crop_h)
        sent_w -= frame.origin_x
        sent_h -= frame.origin_y
        question = (
            f"Task: {self.task}\n\nThe click at the point marked with the red circle "
            f"changed nothing on screen, so it missed. The second image is that area "
            f"of the screen magnified ({sent_w} x {sent_h} screen pixels). Look at "
            f"where the red circle sits relative to the control you were aiming for, "
            f'and reply {{"target": "<the name or label of what you meant to hit>", '
            f'"x": <x>, "y": <y>}} where x and y are the centre of that control in '
            f"THIS CROP's own 0-1000 grid.")
        answer = self.ask_json(question, crop_path)
        if not answer:
            return None
        echoed = self._echoed(answer, pending)
        if echoed:
            # Two of the peer's runs against our one: a re-ground answer that
            # repeats the point that already missed is not always the model
            # reading its own number back. A first click is regularly swallowed
            # by the window coming forward, so the same point can be right the
            # second time. Click it once; if THAT changes nothing the target has
            # failed twice and the escalation ladder takes it from here.
            self.log(f"step {self.step}: the re-ground answer repeated the point that "
                     "already missed; clicking it once anyway, because a first click "
                     "can be swallowed by the window coming forward")
        gx, gy = int(answer["x"]), int(answer["y"])
        rx = round(box[0] + gx * crop_w / GRID)
        ry = round(box[1] + gy * crop_h / GRID)
        screen = self._raw_to_screen(frame, rx, ry)
        target = str(answer.get("target") or pending["target"])
        why = f"re-grounded click on {target} after the previous click missed"
        crop_shot = Shot(step=self.step, path=crop_path, image_w=crop_w, image_h=crop_h,
                         screen_w=max(1, sent_w), screen_h=max(1, sent_h),
                         origin_x=self._raw_to_screen(frame, box[0], box[1])[0],
                         origin_y=self._raw_to_screen(frame, box[0], box[1])[1],
                         window=frame.window)
        args = {"x": gx, "y": gy}
        self.messages.append({"role": "assistant", "content": canonical_echo("click", args)})
        # Everything a reader needs to put this line's crop back on the screen
        # it came from: the box in the frame's own full-resolution pixels, the
        # file, that box's top-left in screen points, and how many screen points
        # one crop pixel is worth. Without them a line whose picture is a crop
        # and whose grid is that crop's cannot be replayed at all.
        origin = self._raw_to_screen(frame, box[0], box[1])
        reground = {"box": list(box), "crop": os.path.basename(crop_path),
                    "origin": list(origin), "scale": [round(sent_w / max(1, crop_w), 6),
                                                      round(sent_h / max(1, crop_h), 6)],
                    "target": target, "echoed": echoed,
                    "previous_screen_px": list(pending["screen"])}
        if echoed:
            self.history.append(
                f"step {self.step}: reground({pending['grid'][0]},{pending['grid'][1]}) "
                f"-> the model answered the same point again; clicking it once anyway")
        return {"tool": "click", "args": args, "why": why, "screen": screen,
                "call_id": f"reground_{self.step}", "shot": crop_shot,
                "action": {"tool": "click", "x": gx, "y": gy, "why": why},
                "action_px": [int(gx * crop_w / GRID), int(gy * crop_h / GRID)],
                "reground": reground,
                "row": {"text": json.dumps(answer)[:2000], "usage": {},
                        "cached_tokens": None, "reasoning_chars": 0,
                        "reground": reground}}

    def _echoed(self, answer: dict | None, pending: dict) -> bool:
        """An answer that repeats the grid point that already missed is the model
        reading its own last number back, not looking at the crop."""
        if not answer:
            return False
        try:
            gx, gy = int(answer["x"]), int(answer["y"])
            px, py = int(pending["grid"][0]), int(pending["grid"][1])
        except (KeyError, TypeError, ValueError):
            return False
        return abs(gx - px) <= REGROUND_ECHO and abs(gy - py) <= REGROUND_ECHO

    def ask_json(self, question: str, crop_path: str, schema: dict | None = None,
                 keys: tuple = ("x", "y")) -> dict | None:
        """The peer's ask_json: an ordinary request with the question and the
        crop appended AFTER the picture the prefix already holds, so asking
        costs the cache nothing. The turn is not kept - the conversation the
        next step sees is the one it would have seen anyway."""
        crop_shot = Shot(step=self.step, path=crop_path, image_w=0, image_h=0,
                         screen_w=0, screen_h=0)
        messages = list(self.outgoing()) + [{"role": "user", "content": [
            {"type": "text", "text": question}, self._image_part(crop_shot)]}]
        response = self.active_model(messages, self.tools,
                                     schema=schema or REGROUND_SCHEMA) or {}
        message = dict((response.get("choices") or [{}])[0].get("message") or {})
        if self.action_mode == "json":
            message = restore_prefill(message)
        return json_object(message_text(message), keys)

    def _click_screen(self, screen: tuple[int, int], args: dict) -> Result:
        """Click a point already worked out in screen points. The re-ground owns
        its own arithmetic, because its grid is the crop's, not the screen's."""
        sx, sy = screen
        out = self.backend.click(sx, sy)
        return self._after(f"Clicked ({args['x']},{args['y']}) on the zoomed view.",
                           coords=(sx, sy), detail=out, tool="click", args=args,
                           action=compact_action("click", args))

    @staticmethod
    def _raw_to_screen(shot: Shot, x: float, y: float) -> tuple[int, int]:
        """Full-resolution pixels -> screen points, rounded rather than
        truncated: this is a rectangle's corner, not a click, and an edge that
        is one point short of the real one moves every coordinate read off it."""
        raw_w, raw_h = shot.raw_w or shot.image_w, shot.raw_h or shot.image_h
        return (round(x * shot.screen_w / max(1, raw_w)) + shot.origin_x,
                round(y * shot.screen_h / max(1, raw_h)) + shot.origin_y)

    @staticmethod
    def _screen_to_raw(shot: Shot, x: float, y: float) -> tuple[int, int]:
        raw_w, raw_h = shot.raw_w or shot.image_w, shot.raw_h or shot.image_h
        return (round((x - shot.origin_x) * raw_w / max(1, shot.screen_w)),
                round((y - shot.origin_y) * raw_h / max(1, shot.screen_h)))

    # -- trajectory ---------------------------------------------------------
    def _trajectory_step(self, name: str, args: dict, why: str, pre: Shot | None,
                         pre_screen: str | None, result: Result,
                         action: dict | None = None, action_px: list | None = None,
                         reground: dict | None = None,
                         off_contract: str | None = None) -> None:
        """Buffer this step as one training line.

        The picture that belongs to a step is the one the model was looking at
        when it chose, which is the last full screenshot - never the one the
        action just produced. A step with nothing behind it is not a training
        example at all, so it is noted and dropped rather than invented.
        """
        if self.trajectory is None:
            return
        if pre is None:
            self.log(f"step {self.step}: no screenshot behind this action, "
                     "so no trajectory line")
            return
        if action is None:
            action, action_px = self.action_view(pre, name, args, why)
        pixels = action_px
        row = {"step": self.step, "screenshot": f"step-{self.step:02d}.png",
               # Whoever actually answered this step, which is not always the
               # run's primary: an escalated step was driven by the seat, and a
               # corpus that says otherwise trains one model on another's work.
               "driver": self.acting_backend(),
               "image_size": [pre.image_w, pre.image_h],
               "history": list(self.history), "action": action,
               "action_px": pixels,
               # Where that action actually landed on the desktop. A line with
               # only the grid and the image pixels cannot be replayed against a
               # real screen, and both of ours are on pictures that have been
               # resized at least once.
               "screen_coords": list(result.coords) if result.coords else None,
               "ts": utc_stamp()}
        if reground:
            row["reground"] = reground
        if off_contract:
            row["off_contract"] = off_contract
        zoom_src = None
        if name == "zoom" and result.is_crop and result.shot is not None:
            row["zoom"] = {"box_grid": [action.get(k) for k in ("x1", "y1", "x2", "y2")],
                           "file": f"step-{self.step:02d}-zoom.png"}
            zoom_src = result.shot.path
        self.trajectory.add(row, pre, changes=name in ACTION_TOOLS,
                            outcome=step_outcome(name, args, result), zoom_src=zoom_src,
                            before=pre_screen or pre.path, after=result.screen_path)

    def note_step(self, name: str, args: dict, result: Result) -> None:
        """One text line per step, the way the next training line will quote it."""
        first = result.text.strip().splitlines()[0] if result.text.strip() else ""
        action = compact_action(name, args)
        self.history.append(f"step {self.step}: {action} -> {_short(first, 160)}")
        self.prev_action = action

    # -- loop ---------------------------------------------------------------
    def record(self, row: dict) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        with open(self.steps_path, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def acting_backend(self) -> str:
        """Which seat acted this step. The streak rule is written per backend, so
        handing over to the fallback seat gives it a clean slate - and the step
        where a fresh backend has just acted can never be the one that gives up,
        because its result is only visible from the next step."""
        return (self.driver_id if self.active_model is self.model
                else driver_id(self.active_model))

    def should_escalate(self) -> bool:
        """Is this step the fallback seat's? The peer's rule, verbatim.

        Either the model asked to stop, or one target has failed twice - which
        is reached only after its re-grounded retry also changed nothing. Each
        hand-over spends one of --max-escalations and clears the per-target
        counts, so a stale count cannot re-trigger it on the very next step.
        """
        if self.seat is None:
            return False
        if self.force_escalate:
            self.force_escalate = False
            self.escalations += 1
            self.fail_counts.clear()
            return True
        if self.escalations >= self.max_escalations:
            return False
        hot = next((k for k, n in self.fail_counts.items() if n >= ESCALATE_AT), None)
        if hot is None:
            return False
        self.escalations += 1
        self.fail_counts.clear()
        self.escalate_reason = f"{ESCALATE_AT} ineffective or refused actions on {hot}"
        return True

    def reask(self, name: str, args: dict) -> tuple | None:
        """One nudge when the model re-sends an action that already worked.

        Out of band: the note is not kept in the conversation, so the prefix the
        next step sends is the one it would have sent anyway.
        """
        note = ("You already performed exactly this action on the previous step and it "
                f"succeeded ({self.history[-1] if self.history else 'see the step history'}). "
                "Do not repeat it. If the task is now complete, reply with done. If it is "
                "not, choose a different action.")
        messages = list(self.outgoing()) + [{"role": "user", "content": note}]
        response = self.active_model(messages, self.tools) or {}
        message = dict((response.get("choices") or [{}])[0].get("message") or {})
        if self.action_mode == "json":
            message = restore_prefill(message)
        parsed = parse_action(message)
        if parsed is None:
            return None
        return parsed[:3], action_why(message), response.get("usage") or {}

    def model_step(self) -> dict:
        """One ordinary turn: ask, log what the cache held, parse, echo back."""
        response = self.active_model(self.outgoing(), self.tools)
        choice = (response.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {})
        if self.action_mode == "json":
            message = restore_prefill(message)
        usage = response.get("usage") or {}
        cached = cached_tokens(usage)
        # cached_tokens is the cache-miss alarm: it should sit near the size
        # of the text history every step, and a zero means something above
        # the screenshot moved.
        self.log(f"step {self.step}: prompt_tokens={usage.get('prompt_tokens')} "
                 f"cached_tokens={cached} "
                 f"completion_tokens={usage.get('completion_tokens')}")
        text = message_text(message)
        # reasoning_content is deepseek-style scratch space: log it, never
        # feed it back, or the next turn inherits the last turn's doubts.
        reasoning = message.get("reasoning_content")
        row = {"text": text[:2000], "usage": usage, "cached_tokens": cached,
               "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0}
        if isinstance(response.get("seat"), dict):
            row["seat"] = response["seat"]
        parsed = parse_action(message)
        if parsed is None:
            # The raw reply, not a summary of it: a run that stops on three
            # unparseable replies is unreadable afterwards unless the text that
            # could not be parsed is on the record and in the live log.
            print(f"step {self.step}: no tool call in the reply: {text[:800]!r}",
                  file=sys.stderr, flush=True)
            self.messages.append({"role": "assistant", "content": text or ""})
            self.messages.append({"role": "user", "content": (
                "That was not a tool call. Reply with one flat JSON object: "
                '{"tool": "<look|zoom|click|type|key|scroll|open_url|done|give_up>", '
                '"why": "<one sentence>"} plus the keys that tool needs.')})
            row.update({"tool": None, "args": None, "result": "no parseable tool call"})
            return {"tool": None, "row": row}
        call_id, name, args, off_contract = parsed
        if off_contract:
            print(f"step {self.step}: off-contract reply, {off_contract}: {text[:400]!r}",
                  file=sys.stderr, flush=True)
            self.log(f"step {self.step}: off-contract reply, {off_contract}")
            row["off_contract"] = off_contract
        why = action_why(message)
        if self.prev_action == compact_action(name, args) and self.last_action_ok:
            again = self.reask(name, args)
            if again is not None:
                (call_id, name, args), why, usage = again
                row["reasked"] = True
                row["usage"] = usage or row["usage"]
        if self.action_mode == "json":
            # Echo the action back as the canonical object, not as whatever
            # the model wrote around it.
            self.messages.append({"role": "assistant",
                                  "content": canonical_echo(name, args)})
        else:
            # Echo back exactly one call, under the id the tool message answers.
            call = {**tool_call(name, args, self.step), "id": call_id}
            self.messages.append({"role": "assistant", "content": text or None,
                                  "tool_calls": [call]})
        return {"tool": name, "args": args, "why": why, "call_id": call_id,
                "off_contract": off_contract, "row": row}

    def run(self) -> int:
        """Drive until done, out of steps, three unparseable replies, or a stall."""
        os.makedirs(self.run_dir, exist_ok=True)
        misses = 0
        while self.step < self.max_steps and not self.done and not self.gave_up:
            self.step += 1
            escalated = self.should_escalate()
            self.active_model = self.seat if escalated else self.model
            if escalated:
                self.log(f"step {self.step}: handing this step to "
                         f"{getattr(self.seat, 'name', 'the fallback seat')} "
                         f"({self.escalate_reason or 'escalated'})")
            plan = None
            if self.reground and not escalated:
                pending, self.reground = self.reground, None
                plan = self.reground_step(pending)
                if plan is not None:
                    self.active_target_key = pending["key"]
            if plan is None:
                self.active_target_key = None
                plan = self.model_step()
            row = {"step": self.step, **(plan.get("row") or {})}
            if escalated:
                row["escalated"] = True
                row["escalate_reason"] = self.escalate_reason

            if plan.get("tool") is None:
                misses += 1
                row["parse_failures"] = misses
                self.record(row)
                if misses >= PARSE_FAILURE_LIMIT:
                    self.log(f"stopping: {misses} replies in a row with no tool call")
                    break
                continue

            misses = 0
            call_id, name, args = plan["call_id"], plan["tool"], plan["args"]
            why = plan.get("why") or ""
            pre = plan.get("shot") or self.last_shot
            pre_screen = self.last_screen
            if plan.get("screen"):
                result = self._click_screen(plan["screen"], args)
            else:
                result = self.execute(name, args)
            if result.shot is not None and not result.is_crop:
                self.last_shot = result.shot
            self._attach(call_id, name, result)

            outcome = step_outcome(name, args, result)
            self.last_action_ok = action_ok(result.text, outcome)
            # A zoom changes no pixels by design, so it is never diffed and can
            # never be the step that ends the run.
            no_change = (name != "zoom" and result.diff is not None
                         and result.diff < NO_CHANGE_DIFF)
            acting = self.acting_backend()
            fresh = acting != self.last_acting
            if fresh:
                self.no_change_streak = 0
            # A refusal never ran and a neutral key is silent by nature, so
            # neither is evidence that the screen is dead: they leave the streak
            # exactly where it was rather than lengthening it or clearing it.
            refused, neutral = outcome == "refused", is_neutral(name, args)
            if refused or neutral:
                pass
            elif no_change:
                self.no_change_streak += 1
            else:
                self.no_change_streak = 0
            self.last_acting = acting
            if no_change and not refused and name in REGROUND_TOOLS:
                self.queue_reground(args, why, result)
            elif refused and name in REGROUND_TOOLS:
                # It did not happen, so there is nothing to re-aim at - but a
                # target the backend keeps refusing still counts towards the
                # hand-over.
                self.bump_target(self.active_target_key or self._target_key(why, args))

            self._trajectory_step(name, args, why, pre, pre_screen, result,
                                  action=plan.get("action"), action_px=plan.get("action_px"),
                                  reground=plan.get("reground"),
                                  off_contract=plan.get("off_contract"))
            self.note_step(name, args, result)
            row.update({"tool": name, "args": args, "screen_coords": list(result.coords)
                        if result.coords else None, "result": result.text[:500],
                        "diff": result.diff, "outcome": outcome,
                        "no_change_streak": self.no_change_streak,
                        "shot": os.path.basename(result.shot.path) if result.shot else None,
                        "image_size": [result.shot.image_w, result.shot.image_h]
                        if result.shot else None,
                        "screen_size": [result.shot.screen_w, result.shot.screen_h]
                        if result.shot else None,
                        "backend": result.detail or None})
            self.record(row)
            self.log(f"step {self.step}: {name}({json.dumps(args)[:120]}) -> {result.text[:120]}")

            if not fresh and self.no_change_streak >= NO_CHANGE_LIMIT:
                if self.seat is not None and self.escalations < self.max_escalations:
                    # The ladder comes before the exit. On the peer's run the
                    # streak limit fired one step before the per-target counter
                    # would have, so the seat never got its turn at all.
                    self.log(f"step {self.step}: {self.no_change_streak} consecutive "
                             f"steps changed nothing - handing the next step to the "
                             f"fallback seat rather than giving up")
                    self.escalate_reason = (f"{self.no_change_streak} consecutive steps "
                                            f"changed nothing")
                    self.force_escalate = True
                    self.no_change_streak = 0
                    continue
                self.log(f"stopping: no visible change for {self.no_change_streak} "
                         "consecutive steps")
                break

        # The last line has no screenshot after it to judge it by, so it settles
        # with a null diff and whatever its own payload already said.
        if self.trajectory is not None:
            self.trajectory.close()
        if not self.done:
            self.log("stopping: the model gave up" if self.gave_up
                     else "stopping: no done call" if self.step >= self.max_steps
                     else "stopping: run ended without a done call")
        return 0 if self.done else 1


# ------------------------------------------------------------------------ cli

def default_python() -> str:
    if sys.platform == "darwin" and os.path.exists(MAC_PYTHON):
        return MAC_PYTHON
    return sys.executable


def default_backend() -> str:
    """`wm` everywhere, except a Linux box that has xdotool, where `x11` is the
    surface that actually works. Computed rather than hard-coded so the help
    text tells you which one you are about to get on this machine."""
    if sys.platform.startswith("linux") and shutil.which("xdotool"):
        return "x11"
    return "wm"


def read_notes(paths: list[str]) -> str:
    """Concatenate note files into one block for the system prompt.

    Each file gets a header naming it, so a model can tell two sources apart
    and a log reader can tell what was loaded. Nothing derived from the
    filesystem beyond the basename goes in: an mtime or a size here would
    change the system prompt between runs and cost the prefix cache.
    """
    chunks = []
    for path in paths:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            body = fh.read().strip()
        if body:
            chunks.append(f"\n--- {os.path.basename(path)} ---\n{body}")
    return "\n".join(chunks)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive this desktop with a small model over nine tools.")
    p.add_argument("--task", required=True, help="what the model should accomplish")
    p.add_argument("--notes", action="append", default=[], metavar="PATH",
                   help="file of operating notes to append to the fixed system "
                        "prompt; repeatable. The text is sent verbatim and must "
                        "not vary between steps, or the prefix cache misses.")
    p.add_argument("--endpoint", default="http://127.0.0.1:8081/v1",
                   help="OpenAI-compatible base URL (default: %(default)s)")
    p.add_argument("--model", default="local", help="model name to send in the request")
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"),
                   help="bearer token, if the endpoint wants one")
    p.add_argument("--wm", default=DEFAULT_WM, help="path to wm.py (default: %(default)s)")
    p.add_argument("--python", default=default_python(),
                   help="interpreter that runs wm.py (default: %(default)s)")
    p.add_argument("--backend", choices=("wm", "x11"), default=default_backend(),
                   help="which hands drive the desktop: 'wm' runs every verb "
                        "through wm.py, which on macOS reaches the Workman.app "
                        "daemon that holds the grants; 'x11' takes the picture "
                        "with wm.py but moves the pointer and types with xdotool, "
                        "which is the surface that works on the DGX - wm.py's own "
                        "click silently no-ops on some pages there. The default is "
                        "wm, except on Linux with xdotool installed, where it is "
                        "x11 (here: %(default)s)")
    p.add_argument("--display", default=None, metavar="DISPLAY",
                   help="X display the x11 backend drives (default: $DISPLAY or :0)")
    p.add_argument("--escalate-to", choices=("none", "claude-seat"), default="none",
                   help="who takes a step the local model cannot: 'claude-seat' "
                        "calls a logged-in `claude -p` seat, read-only and with no "
                        "MCP servers, after two ineffective actions on one target, "
                        "a give_up, or a stall. 'none' means the run gives up "
                        "instead (default: %(default)s)")
    p.add_argument("--max-escalations", type=int, default=MAX_ESCALATIONS, metavar="N",
                   help="how many steps the fallback seat may take in one run; each "
                        "hand-over clears the per-target failure counts "
                        "(default: %(default)s)")
    p.add_argument("--seat-model", default=SEAT_MODEL,
                   help="model the fallback seat runs (default: %(default)s)")
    p.add_argument("--image-channel", choices=("tool", "user"), default="user",
                   help="where the screenshot rides back in tools mode "
                        "(default: %(default)s; json mode always uses a user turn)")
    p.add_argument("--coord-grid", choices=("pixels", "1000"), default="1000",
                   help="what the model's x,y mean: a 0-1000 grid over the picture, "
                        "which is what a vision model answers on, or its real pixels "
                        "(default: %(default)s)")
    p.add_argument("--action-mode", choices=("json", "tools"), default="json",
                   help="how the model is asked for an action: one JSON object behind "
                        "an assistant prefill, which is the fast path, or native tool "
                        "calls (default: %(default)s)")
    p.add_argument("--reasoning", choices=REASONING_LEVELS, default="low",
                   help="chat-template reasoning_strength; there is no off "
                        "(default: %(default)s)")
    p.add_argument("--temp", type=float, default=0.1,
                   help="sampling temperature for the action step; 0.1 is what "
                        "returned identical coordinates six times out of six, where "
                        "the vendor's 1.0 preset is for open generation "
                        "(default: %(default)s)")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--max-steps", type=int, default=25)
    p.add_argument("--send-width", "--image-width", dest="send_width", type=int,
                   default=IMAGE_WIDTH, metavar="N",
                   help="how wide the screenshot the model is sent is, in pixels; a "
                        "narrower picture is left alone. At 1024 this model's "
                        "vertical coordinates land about 145 grid units high and "
                        "repeat; at 1568 they are exact (default: %(default)s)")
    p.add_argument("--no-park", dest="park", action="store_false", default=True,
                   help="stop parking the pointer in the corner before every frame "
                        "that will be compared. Parking is on by default because a "
                        "cursor resting on a row lights that row up for more than the "
                        "no-change threshold, so a still screen reads as a moving one")
    p.add_argument("--trajectory-out", default=None, metavar="DIR",
                   help="where this run's training lines and their screenshots go "
                        "(default: ~/muse-train/data/trajectories/<run_id>/, or a "
                        "trajectory/ inside the run directory under --mock; pass "
                        "'none' to write none)")
    p.add_argument("--runs", default="./runs", help="where run directories go")
    p.add_argument("--url-bar-key", default=URL_BAR_KEY,
                   help="chord open_url uses to reach the address bar "
                        "(default: %(default)s; try super+l on macOS)")
    p.add_argument("--screen", default=None, metavar="WxH",
                   help="override the screen size in points, when the backend omits it")
    p.add_argument("--human", choices=("on", "off"), default=None,
                   help="set persistent Human Mode before the run")
    p.add_argument("--mock", action="store_true",
                   help="fake desktop and scripted model, for tests")
    return p.parse_args(argv)


def inside_gate() -> bool:
    """Is this process inside the cgroup slice a live desktop run belongs in?

    The peer learned this the hard way: a run they believed was a dry run drove
    the real desktop. On Linux the wrapper (`muse-desktop-gate`) puts the job in
    its own slice and this is what proves it. macOS has no such slice and no
    such wrapper - the owner watches the Air directly - so the check is skipped
    there rather than faked.
    """
    if not sys.platform.startswith("linux"):
        return True
    try:
        with open("/proc/self/cgroup") as fh:
            return GATE_SLICE in fh.read()
    except OSError:
        return False


def trajectory_dir(flag: str | None, run_id: str, run_dir: str,
                   mock: bool = False) -> str | None:
    """Where this run's training lines go, or None when it writes none.

    A mock run keeps them inside its own run directory: a test, or somebody
    trying the flags out, must not land scripted steps in the real corpus.
    """
    if flag is None:
        return (os.path.join(run_dir, "trajectory") if mock
                else os.path.join(TRAJECTORY_ROOT, run_id))
    if flag.strip().lower() == "none":
        return None
    return os.path.expanduser(flag)


def main(argv=None) -> int:
    args = parse_args(argv)
    screen = None
    if args.screen:
        w, _, h = args.screen.lower().partition("x")
        screen = (int(w), int(h))

    if not args.mock and not inside_gate():
        print(f"REFUSED: live run outside the gate ({GATE_SLICE}); wrap it in "
              "muse-desktop-gate")
        return EXIT_REFUSED

    if args.mock:
        backend = MockBackend(stuck_shot=MOCK_STUCK_SHOT)
        model = ScriptedModel(mock_script(args.action_mode, args.coord_grid))
        sleep = lambda _s: None  # noqa: E731
    else:
        backend = (X11Backend(args.python, args.wm, display=args.display)
                   if args.backend == "x11" else Backend(args.python, args.wm))
        model = HttpModel(args.endpoint, args.model, temp=args.temp,
                          max_tokens=args.max_tokens, api_key=args.api_key,
                          action_mode=args.action_mode, reasoning=args.reasoning,
                          schema=action_schema(tools_for(args.coord_grid)))
        sleep = time.sleep

    plat = backend.platform()
    print(f"wm.py platform: cli_backend = {plat.get('cli_backend', '(none reported)')}")
    if plat.get("cli_backend") is None:
        print(f"  wm.py said: {json.dumps(plat, default=str)[:400]}")
    if args.human:
        print(f"human {args.human}: {json.dumps(backend.human(args.human), default=str)[:400]}")

    notes = read_notes(args.notes)
    if notes:
        print(f"notes: {len(args.notes)} file(s), {len(notes)} chars -> "
              f"{', '.join(os.path.basename(p) for p in args.notes)}")

    run_id = new_run_id()
    run_dir = os.path.join(args.runs, run_id)
    seat = None
    if args.escalate_to == "claude-seat" and not args.mock:
        # The seat's working directory is the run directory, so the screenshots
        # it is asked to read are already there and nothing is uploaded.
        seat = ClaudeSeatModel(model=args.seat_model, cwd=run_dir)
        print(f"escalates to {seat.name} ({seat.model}), at most "
              f"{args.max_escalations} step(s)")
    driver = Driver(backend, model, args.task, run_dir, seat=seat,
                    max_escalations=args.max_escalations,
                    image_channel=args.image_channel, max_steps=args.max_steps,
                    image_width=args.send_width, screen=screen,
                    url_bar_key=args.url_bar_key, coord_grid=args.coord_grid,
                    action_mode=args.action_mode, run_id=run_id, park=args.park,
                    notes=notes,
                    trajectory_dir=trajectory_dir(args.trajectory_out, run_id,
                                                  run_dir, args.mock),
                    sleep=sleep)
    try:
        code = driver.run()
    except KeyboardInterrupt:
        print("interrupted")
        code = 1
    finally:
        driver.cleanup()
    print(f"run dir: {run_dir}")
    if driver.trajectory is not None:
        print(f"trajectory: {driver.trajectory.written} line(s), "
              f"{driver.trajectory.skipped} withheld -> {driver.trajectory.dir}")
    if driver.answer is not None:
        print(f"ANSWER: {driver.answer}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
