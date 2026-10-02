"""Android TV over the LAN: read the TV's own UI and move focus the way its remote does.

Two transports, both already authorized on the set:

- ADB over TCP (port 5555), the primary: screencap, `uiautomator dump`,
  `input keyevent`, `am start` and dumpsys reads. Key pair in KEYDIR/adbkey(.pub).
- The paired remote protocol (port 6466), the fallback for keys and power when
  ADB is down (standby, adbd restarting). Cert and key in KEYDIR/cert.pem and
  key.pem. It has no screen and no element tree, so it cannot verify focus.

Facts that shape this module, measured on the owner's Android TV 9 set:

- Keys sent in a burst get dropped: 50 back-to-back `input keyevent`s landed 37
  while the set was loaded. Keys therefore go one per shell call, at least
  MIN_SPACING_S apart, and every key tool returns the focus it ended on so the
  caller checks it instead of assuming it.
- TV UIs are focus-driven, not tapped, so there is no tv_click. focus_text walks
  the D-pad and re-reads the element tree after every step.
- Select is the key that commits things. Every route that presses it (select,
  and key() given DPAD_CENTER or ENTER) reads the focused element first and
  refuses when it, or the asked-for target, matches DANGER_TERMS. When focus
  cannot be read the press is refused too: it fails closed.
- The TV's shell stays internal (`_shell`). No tool takes a command string.
- Two kinds of "off". screen() switches the backlight through the
  power_picture_off setting; the set stays on Wi-Fi and fully controllable.
  KEYCODE_POWER is real power-off: the set leaves Wi-Fi and Wake-on-LAN does
  not wake it (network_wol=1 is set and was tested), so only the physical
  remote brings it back. That key is therefore not in KEYCODES; power_off()
  is its only route and wants confirm=True. The set's idle standby (4 hours)
  is on by the owner's choice: leave that setting alone.

Config: WORKMAN_TV_HOST (default 192.168.0.191), WORKMAN_TV_KEYDIR (default
~/.config/workman-tv). Key contents are read for the handshake and never
logged or returned. Public functions never raise; they return dicts with ok.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_HOST = "192.168.0.191"
DEFAULT_KEYDIR = "~/.config/workman-tv"
# The name the remote pairing was made under; the paired cert is what counts.
DEFAULT_CLIENT_NAME = "claude-dgx"
ADB_PORT = 5555
REMOTE_PORT = 6466

CONNECT_TIMEOUT_S = 10.0
AUTH_TIMEOUT_S = 15.0
CONNECT_ATTEMPTS = 2
CONNECT_BACKOFF_S = 1.0
SHELL_TIMEOUT_S = 30.0
DUMP_TIMEOUT_S = 60.0          # uiautomator waits for the UI to idle; slow on a loaded set
DUMP_ATTEMPTS = 2              # a dump misses now and then on the HDMI screen (seen live)
DUMP_BACKOFF_S = 1.0
PULL_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 1.0
REMOTE_CONNECT_TIMEOUT_S = 10.0
LAUNCH_SETTLE_S = 1.0

MIN_SPACING_S = 0.3            # below this the set drops keys
MAX_SPACING_S = 5.0
MAX_KEYS = 40
DEFAULT_STEPS = 15
MAX_STEPS = 40
FOCUS_DEADLINE_S = 240.0
MAX_TARGET_LEN = 200
XML_MAX_CHARS = 2_000_000
PNG_MAX_BYTES = 20_000_000
MAX_ELEMENTS = 300
TEXT_MAX = 120
LABEL_MAX = 4
FIELD_MAX = 200

# Words that must never be confirmed by an agent's select press. Matched at a
# word start in the target, the focused element's text, its children's text and
# its resource id, so "preset" and "information" pass while "Reset to default",
# "FactoryResetActivity" and "factory_reset" are refused.
DANGER_TERMS: tuple[str, ...] = (
    "factory reset", "reset", "erase", "remove account", "oem unlocking",
    "revoke usb debugging", "factory menu", "format", "wipe",
)

# Android KeyEvent codes; the remote protocol uses the same numbers.
KEYCODES: dict[str, int] = {
    "HOME": 3, "BACK": 4, "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21,
    "DPAD_RIGHT": 22, "DPAD_CENTER": 23, "VOLUME_UP": 24, "VOLUME_DOWN": 25,
    "ENTER": 66, "MENU": 82, "MEDIA_PLAY_PAUSE": 85,
    "MEDIA_STOP": 86, "MEDIA_NEXT": 87, "MEDIA_PREVIOUS": 88, "MEDIA_REWIND": 89,
    "MEDIA_FAST_FORWARD": 90, "MEDIA_PLAY": 126, "MEDIA_PAUSE": 127,
    "VOLUME_MUTE": 164, "INFO": 165, "CHANNEL_UP": 166, "CHANNEL_DOWN": 167,
    "SETTINGS": 176, "TV_INPUT": 178, "WAKEUP": 224,
}
# Real power-off, and SLEEP which ends in the same standby, stay out of KEYCODES
# so no key list can switch the set off by accident. power_off() sends POWER.
POWER_KEYCODE = 26
POWER_OFF_WARNING = (
    "Real power-off takes the TV off Wi-Fi and Wake-on-LAN does not wake it: only the "
    "physical remote turns it back on, no tool can. For a dark screen that stays "
    "controllable use tv_screen('off').")
# Backlight switch. The value reads inverted, verified live (the panel driver logs
# EnableBacklightPower fgEnable:0/1): 1 = picture on, 0 = backlight off.
_PICTURE_SETTING = "power_picture_off"
_SCREEN_VALUES = {"on": "1", "off": "0"}
_ALIASES = {"UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT",
            "RIGHT": "DPAD_RIGHT", "CENTER": "DPAD_CENTER", "OK": "DPAD_CENTER",
            "SELECT": "DPAD_CENTER", "MUTE": "VOLUME_MUTE", "PLAY_PAUSE": "MEDIA_PLAY_PAUSE"}
SELECT_KEYS = frozenset({"DPAD_CENTER", "ENTER"})
NAV_KEYS = ("DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT")
_OPPOSITE = {"DPAD_UP": "DPAD_DOWN", "DPAD_DOWN": "DPAD_UP",
             "DPAD_LEFT": "DPAD_RIGHT", "DPAD_RIGHT": "DPAD_LEFT"}

# The live-TV app; launching it resumes the input it last showed (the Mac's HDMI).
TVCENTER = "com.mediatek.wwtv.tvcenter"
_UI_PATH = "/sdcard/workman-ui.xml"
_SHOT_PATH = "/sdcard/workman-tv.png"

# Indirections so tests run without real time passing.
_sleep = time.sleep
_clock = time.monotonic

_LOCK = threading.RLock()
_link: "_AdbLink | None" = None


class TvError(Exception):
    """A TV operation that could not complete; `reply()` is its tool answer."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def reply(self) -> dict:
        return {"ok": False, "error": self.code, "detail": self.detail, "host": _host()}


def _host() -> str:
    return (os.environ.get("WORKMAN_TV_HOST") or DEFAULT_HOST).strip()


def _keydir() -> Path:
    return Path(os.environ.get("WORKMAN_TV_KEYDIR") or DEFAULT_KEYDIR).expanduser()


def _clip(s: str, n: int = TEXT_MAX) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _log(event: str, **fields) -> None:
    """One greppable line per state change. Callers pass no secrets."""
    parts = " ".join(f"{k}={_clip(str(v), FIELD_MAX)!r}" for k, v in fields.items())
    sys.stderr.write(f"workman.tv event={event} host={_host()} {parts}\n")


# ---- transport -------------------------------------------------------------
class _AdbLink:
    """One authorized ADB connection to the TV."""

    def __init__(self, device):
        self._device = device

    def shell(self, cmd: str, timeout_s: float) -> str:
        return self._device.shell(cmd, transport_timeout_s=timeout_s,
                                  read_timeout_s=timeout_s, timeout_s=timeout_s)

    def pull(self, path: str, timeout_s: float) -> bytes:
        buf = io.BytesIO()
        self._device.pull(path, buf, transport_timeout_s=timeout_s, read_timeout_s=timeout_s)
        return buf.getvalue()

    def close(self) -> None:
        try:
            self._device.close()
        except Exception:
            pass


def _connect() -> _AdbLink:
    try:
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner
    except ImportError as exc:
        raise TvError("adb_unavailable",
                      f"ADB client library missing ({exc}); pip install -e '.[tv]'") from None
    key = _keydir() / "adbkey"
    pub = _keydir() / "adbkey.pub"
    if not (key.is_file() and pub.is_file()):
        # Never generate one: a new key needs the owner to accept it on the TV.
        raise TvError("adb_key_missing", f"no authorized ADB key pair at {key}")
    try:
        signer = PythonRSASigner(pub.read_text(), key.read_text())
    except Exception as exc:
        raise TvError("adb_key_unreadable", f"{key}: {type(exc).__name__}") from None
    host = _host()
    last = "no attempt"
    for attempt in range(CONNECT_ATTEMPTS):
        if attempt:
            _sleep(CONNECT_BACKOFF_S * attempt)
        device = None
        try:
            device = AdbDeviceTcp(host, ADB_PORT, default_transport_timeout_s=CONNECT_TIMEOUT_S)
            device.connect(rsa_keys=[signer], transport_timeout_s=CONNECT_TIMEOUT_S,
                           auth_timeout_s=AUTH_TIMEOUT_S)
            return _AdbLink(device)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if device is not None:
                _AdbLink(device).close()
    raise TvError("adb_connect_failed",
                  f"{host}:{ADB_PORT} after {CONNECT_ATTEMPTS} attempts: {_clip(last, FIELD_MAX)}")


def _adb() -> _AdbLink:
    global _link
    if _link is None:
        _link = _connect()
        _log("adb_connected", port=ADB_PORT)
    return _link


def _drop() -> None:
    global _link
    if _link is not None:
        _link.close()
        _link = None


def _shell(cmd: str, timeout_s: float = SHELL_TIMEOUT_S, retry: bool = False) -> str:
    """Run one command in the TV's shell. Internal only: no tool takes a command
    string. `retry` reconnects and runs it once more, so pass it only for reads;
    a key press must never be sent twice."""
    last = "no attempt"
    for _attempt in range(2 if retry else 1):
        link = _adb()
        try:
            return link.shell(cmd, timeout_s)[:XML_MAX_CHARS]
        except Exception as exc:
            _drop()
            last = f"{type(exc).__name__}: {_clip(str(exc))}"
    raise TvError("adb_command_failed", f"{cmd.split(' ', 1)[0]}: {last}")


def _pull(path: str) -> bytes:
    try:
        return _adb().pull(path, PULL_TIMEOUT_S)
    except TvError:
        raise
    except Exception as exc:
        _drop()
        raise TvError("adb_pull_failed", f"{path}: {type(exc).__name__}: {_clip(str(exc))}") from None


def _port_open(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _send_keys_remote(codes: list[int], spacing_s: float) -> dict:
    """Send keys over the paired remote protocol. Its client is asyncio, and a
    sync MCP tool already runs inside the server's event loop, so the client
    gets its own loop on a worker thread, bounded by a timeout."""
    try:
        from androidtvremote2 import AndroidTVRemote
    except ImportError as exc:
        raise TvError("remote_unavailable",
                      f"remote client library missing ({exc}); pip install -e '.[tv]'") from None
    cert, key = _keydir() / "cert.pem", _keydir() / "key.pem"
    if not (cert.is_file() and key.is_file()):
        raise TvError("remote_key_missing", f"no paired remote cert/key in {_keydir()}")
    name = os.environ.get("WORKMAN_TV_CLIENT_NAME") or DEFAULT_CLIENT_NAME

    async def run() -> dict:
        remote = AndroidTVRemote(name, str(cert), str(key), _host())
        try:
            await asyncio.wait_for(remote.async_connect(), REMOTE_CONNECT_TIMEOUT_S)
            for i, code in enumerate(codes):
                if i:
                    await asyncio.sleep(spacing_s)
                remote.send_key_command(code)
            await asyncio.sleep(spacing_s)  # sends are buffered; let the last one leave
            return {"is_on": remote.is_on, "current_app": remote.current_app}
        finally:
            remote.disconnect()

    budget = REMOTE_CONNECT_TIMEOUT_S + spacing_s * (len(codes) + 1) + 5.0
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(asyncio.run, run()).result(timeout=budget)
    except Exception as exc:
        raise TvError("remote_failed", f"{type(exc).__name__}: {_clip(str(exc))}") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---- pure helpers ----------------------------------------------------------
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _bounds(raw: str) -> list[int] | None:
    m = _BOUNDS.fullmatch(raw or "")
    return [int(v) for v in m.groups()] if m else None


def extract_hierarchy(out: str) -> str | None:
    """The <hierarchy> document inside raw dump output, or None when the dump failed."""
    start, end = out.find("<hierarchy"), out.rfind("</hierarchy>")
    if start < 0 or end < start:
        return None
    return out[start:end + len("</hierarchy>")]


def parse_ui(xml: str) -> list[dict]:
    """Every node worth reading or acting on, in document order, as compact rows.

    A node is kept when it has text or a content description, or can take focus
    or a click. Rows carry only non-empty fields: text, desc, id (resource id
    without its package), cls, bounds [x1, y1, x2, y2], the true flags among
    focused/focusable/clickable/checked/selected, and for a focusable row with
    no text of its own, `label`: its children's text, which is what TV rows
    actually show. Raises ValueError on oversized input and ET.ParseError on
    malformed XML.
    """
    if len(xml) > XML_MAX_CHARS:
        raise ValueError(f"hierarchy is {len(xml)} chars, over {XML_MAX_CHARS}")
    # The TV is a LAN device, so this is bounded first; ElementTree never
    # resolves external entities and expat caps entity expansion.
    root = ET.fromstring(xml)
    rows = []
    for node in root.iter("node"):
        a = node.attrib
        text, desc = _clip(a.get("text", "")), _clip(a.get("content-desc", ""))
        flags = {k: True for k in ("focused", "focusable", "clickable", "checked", "selected")
                 if a.get(k) == "true"}
        actionable = bool(flags.keys() & {"focused", "focusable", "clickable"})
        if not (text or desc or actionable):
            continue
        row: dict = {}
        if text:
            row["text"] = text
        if desc and desc != text:
            row["desc"] = desc
        rid = a.get("resource-id", "")
        if rid:
            row["id"] = rid.split(":id/", 1)[-1]
        row["cls"] = a.get("class", "").rsplit(".", 1)[-1]
        b = _bounds(a.get("bounds", ""))
        if b:
            row["bounds"] = b
        row.update(flags)
        if actionable and not (text or desc):
            inner = []
            for child in node.iter("node"):
                if child is node:
                    continue
                t = _clip(child.get("text", "")) or _clip(child.get("content-desc", ""))
                if t:
                    inner.append(t)
                    if len(inner) >= LABEL_MAX:
                        break
            if inner:
                row["label"] = inner
        rows.append(row)
    return rows


def focused_row(rows: list[dict]) -> dict | None:
    """The focused row (the last one in document order if several claim it)."""
    hits = [r for r in rows if r.get("focused")]
    return hits[-1] if hits else None


def _fold(s: str) -> str:
    return " ".join((s or "").casefold().split())


def _norm_words(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "")
    return " ".join(re.sub(r"[\W_]+", " ", s).casefold().split())


_DANGER_RES = tuple((term, re.compile(r"\b" + re.escape(_norm_words(term))))
                    for term in DANGER_TERMS)


def danger_hit(*texts: str) -> str | None:
    """The first DANGER_TERMS entry starting a word in any of `texts`, else None."""
    for text in texts:
        words = _norm_words(text)
        for term, rx in _DANGER_RES:
            if rx.search(words):
                return term
    return None


def _danger_texts(row: dict) -> list[str]:
    return [row.get("text", ""), row.get("desc", ""), row.get("id", ""), *row.get("label", [])]


def _pieces(row: dict) -> list[str]:
    return [_fold(p) for p in (row.get("text", ""), row.get("desc", ""), *row.get("label", [])) if p]


def _matches(row: dict | None, want: str, mode: str) -> bool:
    if row is None:
        return False
    pieces = _pieces(row)
    return want in pieces if mode == "exact" else any(want in p for p in pieces)


def find_target(rows: list[dict], text: str) -> tuple[dict | None, str]:
    """The row to steer toward and how it matched ('exact' beats 'loose', a
    substring). Rows that can take focus win over the bare text inside them."""
    want = _fold(text)
    for mode in ("exact", "loose"):
        hits = [r for r in rows if _matches(r, want, mode)]
        if hits:
            hits.sort(key=lambda r: not (r.get("focusable") or r.get("focused") or r.get("clickable")))
            return hits[0], mode
    return None, "exact"


def _center(b: list[int]) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def next_key(focused: dict | None, target: dict | None, explore: str = "DPAD_DOWN",
             tried: frozenset[str] | set[str] = frozenset()) -> str | None:
    """The D-pad key to press next, never a select key: toward the target when
    both it and the focus have bounds, else the explore direction. Keys in
    `tried` already failed to move focus from here; None means none are left."""
    if focused and target and "bounds" in focused and "bounds" in target:
        f, t = focused["bounds"], target["bounds"]
        gaps = [(t[1] - f[3], "DPAD_DOWN"), (f[1] - t[3], "DPAD_UP"),
                (t[0] - f[2], "DPAD_RIGHT"), (f[0] - t[2], "DPAD_LEFT")]
        order = [k for g, k in sorted(gaps, reverse=True) if g >= 0]
        (fx, fy), (tx, ty) = _center(f), _center(t)
        soft = sorted([(abs(ty - fy), "DPAD_DOWN" if ty > fy else "DPAD_UP"),
                       (abs(tx - fx), "DPAD_RIGHT" if tx > fx else "DPAD_LEFT")], reverse=True)
        order += [k for d, k in soft if d and k not in order]
    else:
        order = [explore]
    return next((k for k in order if k not in tried), None)


def _same_focus(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    keys = ("bounds", "text", "desc", "id", "label")
    return all(a.get(k) == b.get(k) for k in keys)


def normalize_keys(keys) -> tuple[list[str], list[str]]:
    """(known key names, unknown entries). Accepts KEYCODE_ prefixes, any case
    and the short aliases (UP, OK, MUTE, ...)."""
    names, unknown = [], []
    for raw in keys if isinstance(keys, (list, tuple)) else [keys]:
        k = str(raw).strip().upper().replace("-", "_").replace(" ", "_").removeprefix("KEYCODE_")
        k = _ALIASES.get(k, k)
        if k in KEYCODES:
            names.append(k)
        else:
            unknown.append(_clip(str(raw), 40))
    return names, unknown


def clamp_spacing(spacing_s) -> float:
    """Key spacing bounded to [MIN_SPACING_S, MAX_SPACING_S]; junk gets the minimum."""
    try:
        value = float(spacing_s)
    except (TypeError, ValueError):
        return MIN_SPACING_S
    if value != value:  # NaN
        return MIN_SPACING_S
    return max(MIN_SPACING_S, min(MAX_SPACING_S, value))


_STATUS_FIELDS = ("model", "android", "boot", "uptime", "picture_mode", "backlight",
                  "screen", "window", "input")
_STATUS_CMD = "; ".join(f"echo @{f}; {c}" for f, c in zip(_STATUS_FIELDS, (
    "getprop ro.product.model", "getprop ro.build.version.release",
    "getprop sys.boot.reason", "cat /proc/uptime",
    "settings get global picture_mode", "settings get global picture_backlight",
    f"settings get global {_PICTURE_SETTING}",
    "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'",
    "dumpsys tv_input | grep -E '^ +inputId: '")))
_FOCUS_WINDOW = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^}\s]+)\}")
_FOCUS_APP = re.compile(r"mFocusedApp=.*?ActivityRecord\{\S+ \S+ ([^}\s]+)")


def parse_window(out: str) -> str | None:
    """'package/activity' of the window holding input focus, from dumpsys window."""
    m = _FOCUS_WINDOW.search(out or "")
    return m.group(1) if m else None


def parse_status(out: str) -> dict:
    """Fields of the @-sectioned status command output; 'null' settings become None."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("@") and line[1:] in _STATUS_FIELDS:
            current = line[1:]
            sections[current] = []
        elif current and line:
            sections[current].append(line)

    def first(field: str) -> str | None:
        lines = sections.get(field) or []
        value = _clip(lines[0], FIELD_MAX) if lines else ""
        return None if value in ("", "null") else value

    uptime_s = None
    raw = first("uptime")
    if raw:
        try:
            uptime_s = round(float(raw.split()[0]), 1)
        except ValueError:
            uptime_s = None
    window_text = "\n".join(sections.get("window") or [])
    app = _FOCUS_APP.search(window_text)
    return {"model": first("model"), "android": first("android"),
            "boot_reason": first("boot"), "uptime_s": uptime_s,
            "picture_mode": first("picture_mode"), "backlight": first("backlight"),
            "screen": screen_state(first("screen")), "window": parse_window(window_text), "focused_app": app.group(1) if app else None,
            "tv_input_sessions": parse_input_sessions("\n".join(sections.get("input") or []))}


def screen_state(raw: str | None) -> str:
    """'on', 'off' or 'unknown' from the power_picture_off value (1 is on)."""
    return {"1": "on", "0": "off"}.get((raw or "").strip(), "unknown")


def parse_input_sessions(out: str) -> list[str]:
    """Input ids of open TV input sessions (the HDMI port on screen), from dumpsys tv_input."""
    ids = [line.split(":", 1)[1].strip() for line in (out or "").splitlines()
           if line.strip().startswith("inputId:")]
    return [_clip(i, FIELD_MAX) for i in ids if i][:8]


_PKG = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+"
_TARGET = re.compile(rf"(?P<pkg>{_PKG})(?:/(?P<act>\.?[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*))?")


def parse_launch_target(target: str) -> tuple[str, str | None] | None:
    """(package, activity or None) for 'pkg' or 'pkg/activity', else None. The
    character set excludes everything a shell would interpret."""
    m = _TARGET.fullmatch((target or "").strip())
    return (m.group("pkg"), m.group("act")) if m else None


# ---- device operations -----------------------------------------------------
def _elements() -> list[dict]:
    # uiautomator's own message stays in the output: on success it is one line
    # before the document (extract_hierarchy skips it), on failure it is the reason.
    out, xml = "", None
    for attempt in range(DUMP_ATTEMPTS):
        if attempt:
            _sleep(DUMP_BACKOFF_S)
        out = _shell(f"rm -f {_UI_PATH}; uiautomator dump {_UI_PATH} 2>&1; "
                     f"cat {_UI_PATH} 2>/dev/null; rm -f {_UI_PATH}", DUMP_TIMEOUT_S, retry=True)
        xml = extract_hierarchy(out)
        if xml is not None:
            break
    if xml is None:
        raise TvError("ui_dump_failed",
                      f"uiautomator returned no hierarchy after {DUMP_ATTEMPTS} tries (it fails "
                      f"while video plays or an animation never idles): {_clip(out, 160)!r}")
    try:
        return parse_ui(xml)
    except (ET.ParseError, ValueError) as exc:
        raise TvError("ui_parse_failed", f"{type(exc).__name__}: {exc}") from None


def _current_window() -> str | None:
    try:
        return parse_window(_shell("dumpsys window windows | grep -E 'mCurrentFocus'", retry=True))
    except TvError:
        return None


def _snapshot() -> dict:
    """Focus and window after an action. Never raises: a failed read is reported."""
    out: dict = {"window": _current_window()}
    try:
        out["focused"] = focused_row(_elements())
    except TvError as exc:
        out.update({"focused": None, "ui_error": exc.detail})
    return out


def _press(name: str) -> None:
    _shell(f"input keyevent {KEYCODES[name]}")


def _select_guard() -> dict | None:
    """The refusal for a select press right now, or None when it may go ahead:
    focus must be readable, present, and not match DANGER_TERMS."""
    try:
        focused = focused_row(_elements())
    except TvError as exc:
        return {"ok": False, "error": "select_unverified", "detail": exc.detail,
                "hint": "focus could not be read, so select is refused; use "
                        "MEDIA_PLAY_PAUSE for playback or tv_select for a named item"}
    if focused is None:
        return {"ok": False, "error": "select_unverified",
                "detail": "nothing on screen reports focus, so select is refused"}
    hit = danger_hit(*_danger_texts(focused))
    if hit:
        _log("select_refused", matched=hit, focused=focused.get("text") or focused.get("label"))
        return {"ok": False, "error": "danger_refused", "matched": hit, "focused": focused}
    return None


def status() -> dict:
    """Model, Android version, boot reason, uptime, focused window, picture mode,
    backlight and screen on/off, live TV input sessions, and which transports answer."""
    with _LOCK:
        host, kd = _host(), _keydir()
        out = {"ok": True, "host": host,
               "reachable": {"adb": _port_open(host, ADB_PORT),
                             "remote": _port_open(host, REMOTE_PORT)},
               "keys_present": {"adb": (kd / "adbkey").is_file() and (kd / "adbkey.pub").is_file(),
                                "remote": (kd / "cert.pem").is_file() and (kd / "key.pem").is_file()}}
        try:
            out.update(parse_status(_shell(_STATUS_CMD, retry=True)))
        except TvError as exc:
            out.update({"ok": False, "error": exc.code, "detail": exc.detail})
        return out


def elements(limit: int = MAX_ELEMENTS) -> dict:
    """The TV's current element tree as compact rows, plus the focused one."""
    with _LOCK:
        try:
            rows = _elements()
        except TvError as exc:
            return exc.reply()
        try:
            cap = max(1, min(int(limit), MAX_ELEMENTS))
        except (TypeError, ValueError):
            cap = MAX_ELEMENTS
        return {"ok": True, "count": len(rows), "truncated": len(rows) > cap,
                "focused": focused_row(rows), "elements": rows[:cap]}


def screenshot() -> dict:
    """PNG bytes of the TV's UI under 'png'. Measured on the owner's set:
    screencap exits 1 with an empty file while the HDMI input is on screen
    (a protected video layer), so that comes back as screencap_failed with
    the window named, never as a blank image."""
    with _LOCK:
        try:
            try:
                out = _shell(f"screencap -p {_SHOT_PATH}; echo rc=$?")
                m = re.search(r"rc=(\d+)", out)
                code = m.group(1) if m else "?"
                data = _pull(_SHOT_PATH) if code == "0" else b""
            finally:
                try:
                    _shell(f"rm -f {_SHOT_PATH}")
                except TvError:
                    pass
        except TvError as exc:
            return exc.reply()
        if code != "0":
            return {"ok": False, "error": "screencap_failed", "window": _current_window(),
                    "detail": f"screencap exited {code}; a protected video layer (the HDMI "
                              "input, live TV) cannot be captured"}
        if not data.startswith(b"\x89PNG"):
            return {"ok": False, "error": "screencap_failed", "detail": f"{len(data)} bytes, not a PNG"}
        if len(data) > PNG_MAX_BYTES:
            return {"ok": False, "error": "screencap_too_large", "detail": f"{len(data)} bytes"}
        return {"ok": True, "png": data, "bytes": len(data)}


def key(keys, spacing_s: float = MIN_SPACING_S) -> dict:
    """Send keys one at a time, spaced, and return the focus they ended on.
    DPAD_CENTER/ENTER pass the select guard first. Without ADB the keys go over
    the remote protocol, where select keys are refused (focus is unreadable).
    POWER is not a key here: power_off() is its only route."""
    if isinstance(keys, (list, tuple)) and len(keys) > MAX_KEYS:
        return {"ok": False, "error": "too_many_keys", "detail": f"{len(keys)} > {MAX_KEYS}"}
    names, unknown = normalize_keys(keys)
    if unknown:
        return {"ok": False, "error": "unknown_keys", "unknown": unknown[:10], "known": sorted(KEYCODES)}
    if not names:
        return {"ok": False, "error": "no_keys"}
    spacing = clamp_spacing(spacing_s)
    with _LOCK:
        try:
            # A cached connection can be dead (TV rebooted); a no-op read with
            # retry reconnects now, so a key is never the thing that finds out.
            _shell("echo ok", PROBE_TIMEOUT_S * 5, retry=True)
        except TvError as exc:
            return _key_via_remote(names, spacing, exc)
        sent: list[str] = []
        try:
            for i, name in enumerate(names):
                if i:
                    _sleep(spacing)
                if name in SELECT_KEYS:
                    refused = _select_guard()
                    if refused:
                        return {**refused, "transport": "adb", "sent": sent}
                _press(name)
                sent.append(name)
        except TvError as exc:
            return {**exc.reply(), "transport": "adb", "sent": sent}
        _log("keys", transport="adb", keys=",".join(sent))
        _sleep(spacing)
        return {"ok": True, "transport": "adb", "sent": sent, "spacing_s": spacing, **_snapshot()}


def _key_via_remote(names: list[str], spacing: float, why: TvError) -> dict:
    if SELECT_KEYS & set(names):
        return {"ok": False, "error": "select_unverified", "sent": [],
                "detail": f"ADB is unavailable ({why.detail}); the remote fallback cannot read "
                          "focus, so select keys are refused"}
    try:
        state = _send_keys_remote([KEYCODES[n] for n in names], spacing)
    except TvError as exc:
        return {**exc.reply(), "adb_error": why.detail, "sent": []}
    _log("keys", transport="remote", keys=",".join(names))
    return {"ok": True, "transport": "remote", "sent": names, "spacing_s": spacing,
            "focused": None, "adb_error": why.detail, **state,
            "note": "the remote fallback has no element tree; check with tv_screenshot once ADB answers"}


def focus_text(text: str, max_steps: int = DEFAULT_STEPS,
               spacing_s: float = MIN_SPACING_S) -> dict:
    """Move D-pad focus until the element showing `text` is focused, re-reading
    the tree after every step. Presses only D-pad directions, never select."""
    want = _fold(text)
    if not want or len(want) > MAX_TARGET_LEN:
        return {"ok": False, "error": "bad_text", "detail": f"text must be 1-{MAX_TARGET_LEN} chars"}
    try:
        cap = max(0, min(int(max_steps), MAX_STEPS))
    except (TypeError, ValueError):
        cap = DEFAULT_STEPS
    spacing = clamp_spacing(spacing_s)
    with _LOCK:
        try:
            return _walk(want, cap, spacing)
        except TvError as exc:
            return exc.reply()


def _walk(want: str, cap: int, spacing: float) -> dict:
    deadline = _clock() + FOCUS_DEADLINE_S
    rows = _elements()
    explore, flipped = "DPAD_DOWN", False
    tried: set[str] = set()
    steps: list[str] = []
    while True:
        focused = focused_row(rows)
        target, mode = find_target(rows, want)
        if _matches(focused, want, mode):
            return {"ok": True, "focused": focused, "match": mode, "steps": steps}
        why = "max_steps" if len(steps) >= cap else "deadline" if _clock() > deadline else None
        if why:
            return {"ok": False, "error": "not_focused", "reason": why, "steps": steps,
                    "focused": focused, "target_visible": target is not None}
        k = next_key(focused, target, explore, tried)
        if k is None:
            if target is None and not flipped:
                # Ran off the end of a list without seeing it: search the other way.
                explore, flipped = _OPPOSITE[explore], True
                tried.clear()
                continue
            return {"ok": False, "error": "stuck" if target else "not_found", "steps": steps,
                    "focused": focused, "target_visible": target is not None}
        if k not in NAV_KEYS:  # next_key's contract; a select press here would commit blind
            raise TvError("internal", f"navigation produced {k}")
        _press(k)
        steps.append(k)
        _sleep(spacing)
        new_rows = _elements()
        if _same_focus(focused_row(new_rows), focused):
            tried.add(k)
        else:
            tried.clear()
        rows = new_rows


def select(text: str, max_steps: int = DEFAULT_STEPS, spacing_s: float = MIN_SPACING_S) -> dict:
    """focus_text, then one DPAD_CENTER, unless the target or the focused
    element matches DANGER_TERMS, in which case nothing is pressed."""
    hit = danger_hit(text or "")
    if hit:
        _log("select_refused", matched=hit, target=text)
        return {"ok": False, "error": "danger_refused", "matched": hit, "target": _clip(text),
                "pressed": False}
    with _LOCK:
        walked = focus_text(text, max_steps=max_steps, spacing_s=spacing_s)
        if not walked.get("ok"):
            return {**walked, "pressed": False}
        # The tree read that proved focus is the last one focus_text took, after
        # its final key, so no agent key sits between this check and the press.
        # The screen changing on its own in that gap is the remaining risk.
        focused = walked["focused"]
        hit = danger_hit(*_danger_texts(focused))
        if hit:
            _log("select_refused", matched=hit, focused=focused.get("text") or focused.get("label"))
            return {"ok": False, "error": "danger_refused", "matched": hit, "focused": focused,
                    "steps": walked["steps"], "pressed": False}
        try:
            _press("DPAD_CENTER")
        except TvError as exc:
            return {**exc.reply(), "steps": walked["steps"], "pressed": False}
        _log("select", target=text)
        _sleep(clamp_spacing(spacing_s))
        return {"ok": True, "pressed": "DPAD_CENTER", "selected": focused,
                "steps": walked["steps"], **_snapshot()}


def launch(target: str) -> dict:
    """Start an app by package (its TV launcher activity) or 'package/activity'."""
    parsed = parse_launch_target(target)
    if parsed is None:
        return {"ok": False, "error": "bad_target",
                "detail": "give a package (com.example.app) or package/activity"}
    hit = danger_hit(target)
    if hit:
        _log("launch_refused", matched=hit, target=target)
        return {"ok": False, "error": "danger_refused", "matched": hit, "target": target}
    pkg, act = parsed
    with _LOCK:
        try:
            component = f"{pkg}/{act}" if act else _resolve_launcher(pkg)
            if component is None:
                return {"ok": False, "error": "no_launcher", "detail": f"{pkg} has no launcher activity"}
            hit = danger_hit(component)
            if hit:
                return {"ok": False, "error": "danger_refused", "matched": hit, "target": component}
            out = _shell(f"am start -n {component}")
        except TvError as exc:
            return exc.reply()
        if "Error" in out:
            return {"ok": False, "error": "launch_failed", "component": component, "detail": _clip(out, FIELD_MAX)}
        _log("launch", component=component)
        _sleep(LAUNCH_SETTLE_S)
        return {"ok": True, "component": component, "window": _current_window()}


def _resolve_launcher(pkg: str) -> str | None:
    for category in ("LEANBACK_LAUNCHER", "LAUNCHER"):
        out = _shell("cmd package resolve-activity --brief -a android.intent.action.MAIN "
                     f"-c android.intent.category.{category} {pkg}", retry=True)
        for line in reversed(out.splitlines()):
            parsed = parse_launch_target(line.strip())
            if parsed and parsed[1] and parsed[0] == pkg:
                return line.strip()
    return None


def input_hdmi() -> dict:
    """Bring the live-TV app forward, which resumes the input it last showed:
    the Mac's HDMI. Reports the TV input sessions afterwards to confirm."""
    result = launch(TVCENTER)
    if result.get("ok"):
        with _LOCK:
            try:
                result["tv_input_sessions"] = parse_input_sessions(
                    _shell("dumpsys tv_input | grep -E '^ +inputId: '", retry=True))
            except TvError as exc:
                result["tv_input_error"] = exc.detail
    return result


def screen(state: str = "status") -> dict:
    """Switch the picture (backlight) on or off, or read it, and report what
    the setting reads afterwards. Off is not power: the set stays reachable."""
    want = (state or "").strip().lower()
    if want not in ("on", "off", "status"):
        return {"ok": False, "error": "bad_state", "detail": "state is on, off or status"}
    with _LOCK:
        try:
            if want != "status":
                # A settings write is idempotent, so a reconnect-and-retry is safe.
                _shell(f"settings put global {_PICTURE_SETTING} {_SCREEN_VALUES[want]}", retry=True)
                _log("screen", state=want)
            raw = _shell(f"settings get global {_PICTURE_SETTING}", retry=True).strip()
        except TvError as exc:
            return exc.reply()
    now = screen_state(raw)
    if want != "status" and now != want:
        return {"ok": False, "error": "screen_not_applied", "screen": now,
                "detail": f"asked {want}, {_PICTURE_SETTING} reads {_clip(raw, 20)!r}"}
    return {"ok": True, "screen": now}


def power_off(confirm: bool = False) -> dict:
    """Real power-off (KEYCODE_POWER), over ADB or else the remote protocol.
    Without confirm=True nothing is sent and the reply is the warning."""
    if confirm is not True:
        return {"ok": False, "error": "confirm_required", "warning": POWER_OFF_WARNING}
    with _LOCK:
        try:
            _shell("echo ok", PROBE_TIMEOUT_S * 5, retry=True)
        except TvError as why:
            try:
                _send_keys_remote([POWER_KEYCODE], MIN_SPACING_S)
            except TvError as exc:
                return {**exc.reply(), "adb_error": why.detail, "pressed": False}
            transport = "remote"
        else:
            try:
                _shell(f"input keyevent {POWER_KEYCODE}")
            except TvError as exc:
                return {**exc.reply(), "pressed": False}
            transport = "adb"
    _log("power_off", transport=transport)
    return {"ok": True, "pressed": "POWER", "transport": transport, "warning": POWER_OFF_WARNING}
