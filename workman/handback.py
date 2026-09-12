"""Leave the desk the way a person leaves it.

When an agent is done, nothing of its work may linger on the owner's
machine: no key or button still down, no window it opened still in the
way, the pointer put back where it found it, the screen awake. This module
keeps the small amount of state that makes that checkable, and `hand_back`
does the work and VERIFIES it with the X server rather than assuming.

What it never does: touch a window the agent did not open, move the
pointer while the owner is using it, or send a key release for a key the
agent never pressed (the owner may be physically holding it).
"""
from __future__ import annotations

import json
import os
import tempfile
import time

from . import desktop, human, owner_pause

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

#: Programs this server started (launch_app), with the window seen at launch.
_LAUNCHED: list[dict] = []
#: Keys and buttons held through key_hold / mouse_button and not yet released.
_HELD: dict = {"keys": [], "buttons": []}
#: Where the pointer was before the agent's first input action this session.
_POINTER_BEFORE: dict = {"at": None}

CLOSE_WAIT_S = 3.0
PARK_MARGIN_PX = 24

#: Persist launches so a later process's hand_back can close them.
STATE_ENV = "WORKMAN_STATE_DIR"
_LAUNCHED_FILE = "launched.json"
_UNREADABLE = object()
_STATE = {"unknown": False}


# ---- persisted launch tracking -------------------------------------------------------
def _state_dir() -> str:
    override = (os.environ.get(STATE_ENV) or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".local", "state", "workman")


def _state_path() -> str:
    return os.path.join(_state_dir(), _LAUNCHED_FILE)


def _pid_starttime(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat, or None when /proc is missing."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            stat = fh.read().decode(errors="replace")
        rest = stat.rsplit(")", 1)[1].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _entry_live(entry: dict) -> bool:
    """Drop entries whose pid is gone or whose starttime no longer matches."""
    if not isinstance(entry, dict):
        return False
    try:
        pid = int(entry.get("pid"))
    except (TypeError, ValueError):
        return False
    stored = entry.get("starttime")
    if stored is None:
        # Recorded without /proc (macOS, tests with fake pids): keep.
        return True
    now = _pid_starttime(pid)
    if now is None:
        return False
    try:
        return int(now) == int(stored)
    except (TypeError, ValueError):
        return False


def _lock_fd():
    if fcntl is None:
        return None
    path = _state_path() + ".lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock_fd(fd) -> None:
    if fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_unlocked():
    """List of entries, [] if missing, _UNREADABLE if corrupt."""
    path = _state_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _UNREADABLE
    if isinstance(data, dict):
        entries = data.get("entries")
    else:
        entries = data
    if not isinstance(entries, list):
        return _UNREADABLE
    return [e for e in entries if isinstance(e, dict)]


def _write_unlocked(entries: list[dict]) -> None:
    directory = _state_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".launched.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "entries": entries}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _state_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_entries():
    """Live entries from disk (and memory if the file is missing).

    Returns _UNREADABLE when the file exists but cannot be parsed.
    """
    fd = None
    try:
        fd = _lock_fd()
        loaded = _read_unlocked()
    except OSError:
        _STATE["unknown"] = True
        return _UNREADABLE
    finally:
        _unlock_fd(fd)
    if loaded is _UNREADABLE:
        _STATE["unknown"] = True
        return _UNREADABLE
    _STATE["unknown"] = False
    live = [e for e in loaded if _entry_live(e)]
    _LAUNCHED[:] = live
    return live


def _persist_entries(entries: list[dict]) -> None:
    fd = None
    try:
        fd = _lock_fd()
        _write_unlocked(entries)
        _LAUNCHED[:] = list(entries)
        _STATE["unknown"] = False
    except OSError:
        _LAUNCHED[:] = list(entries)
    finally:
        _unlock_fd(fd)


# ---- bookkeeping (cheap, called from the MCP tools) -----------------------------------
def window_ids() -> set[str]:
    """Ids of every window open right now; the `before` half of a launch diff."""
    return {str(w.get("id")) for w in _list_windows()}


def _list_windows() -> list[dict]:
    try:
        return list(desktop.list_windows())
    except Exception:
        return []


def _pid_of(win: dict) -> int | None:
    try:
        pid = win.get("pid")
        return int(pid) if pid not in (None, "") else None
    except (TypeError, ValueError):
        return None


def note_launch(result: dict, before: set[str] | None = None) -> None:
    """Record a launch and the windows it opened: open now, absent from
    `before`, and owned by the launched process tree. hand_back closes those
    ids and nothing else."""
    # Fixed 2026-09-12: hand_back used to close every window whose pid was in
    # the launch pid tree, re-walked at hand_back time. A Chrome this server
    # started then owned every Chrome window the owner opened in it later, and
    # a reused pid could adopt an unrelated app. The set is now fixed here, at
    # launch time, from a before/after diff; a pre-existing window is never in it.
    if not (isinstance(result, dict) and result.get("ok") and result.get("pid")):
        return
    pid = int(result["pid"])
    reported = (result.get("window") or {}).get("id")
    tree = _descendants(pid)
    new: list[str] = []
    for win in _list_windows():
        wid = str(win.get("id"))
        # Without a before snapshot only the window launch() itself saw appear is new.
        is_new = wid not in before if before is not None else wid == str(reported)
        if is_new and _pid_of(win) in tree and wid not in new:
            new.append(wid)
    entry = {"pid": pid, "starttime": _pid_starttime(pid),
             "argv": result.get("argv"), "window": reported,
             "windows": new, "at": time.time()}
    loaded = _load_entries()
    if loaded is _UNREADABLE:
        # Do not clobber a corrupt file; this process still tracks in memory.
        _LAUNCHED.append(entry)
        return
    loaded.append(entry)
    _persist_entries(loaded)


def note_key(key: str, down: bool) -> None:
    key = (key or "").strip()
    if not key:
        return
    if down:
        if key not in _HELD["keys"]:
            _HELD["keys"].append(key)
    elif key in _HELD["keys"]:
        _HELD["keys"].remove(key)


def note_button(button: int, down: bool) -> None:
    b = int(button)
    if down:
        if b not in _HELD["buttons"]:
            _HELD["buttons"].append(b)
    elif b in _HELD["buttons"]:
        _HELD["buttons"].remove(b)


def note_pointer_before() -> None:
    """Remember where the owner left the pointer, once, before the first
    agent input. One XQueryPointer; nothing afterwards."""
    if _POINTER_BEFORE["at"] is not None:
        return
    try:
        p = desktop.pointer_position()
        if isinstance(p, dict) and p.get("ok"):
            _POINTER_BEFORE["at"] = (int(p["x"]), int(p["y"]))
    except Exception:
        pass


def launched() -> list[dict]:
    loaded = _load_entries()
    if loaded is _UNREADABLE:
        return list(_LAUNCHED)
    return list(loaded)


def held() -> dict:
    return {"keys": list(_HELD["keys"]), "buttons": list(_HELD["buttons"])}


def reset() -> None:
    _LAUNCHED.clear()
    _HELD["keys"].clear()
    _HELD["buttons"].clear()
    _POINTER_BEFORE["at"] = None
    _STATE["unknown"] = False
    try:
        _persist_entries([])
    except OSError:
        pass


# ---- the channel, when there is one --------------------------------------------------
def _channel():
    try:
        from . import xtest
        return xtest.channel()
    except Exception:
        return None


def _backend_attr(name: str):
    """An optional capability of the active backend, or None."""
    try:
        return getattr(desktop.backend(), name, None)
    except Exception:
        return None


# ---- 1. release --------------------------------------------------------------------
def release_all() -> dict:
    """Let go of everything the agent holds, then ask the X server what the
    XTEST devices still hold. `verified` is that answer, not a guess."""
    out: dict = {"tracked": held(), "released": {"keys": [], "buttons": []}, "errors": []}
    for key in list(_HELD["keys"]):
        r = desktop.key_up(key)
        if isinstance(r, dict) and r.get("ok") is False:
            out["errors"].append({"key": key, "error": r.get("error")})
        else:
            out["released"]["keys"].append(key)
        note_key(key, False)
    for button in list(_HELD["buttons"]):
        r = desktop.mouse_up(button=button)
        if isinstance(r, dict) and r.get("ok") is False:
            out["errors"].append({"button": button, "error": r.get("error")})
        else:
            out["released"]["buttons"].append(button)
        note_button(button, False)
    ch = _channel()
    if ch is not None:
        try:
            xt = ch.release_xtest_held()
            out["xtest"] = xt
            still = xt.get("still_held") or {}
            out["verified"] = not still.get("keys") and not still.get("buttons")
            try:
                down = ch.core_keys_down()
                out["core_keys_down"] = ch.keycode_names(down) if down else []
            except Exception as exc:
                out["core_keys_down_error"] = str(exc)
        except Exception as exc:
            out["errors"].append({"xtest": str(exc)})
            out["verified"] = False
    elif (hook := _backend_attr("release_all")) is not None:
        # The backend answers for what is safe beyond the tracked holds
        # (macOS: nothing, unverifiable). No blind release on such a backend.
        try:
            res = hook()
            out["platform_release"] = res
            out["verified"] = res.get("verified")
            out["note"] = res.get("note", "")
        except Exception as exc:
            out["errors"].append({"platform_release": str(exc)})
            out["verified"] = False
    else:
        # No persistent channel: release the usual suspects blindly. A key
        # release for an unheld key is harmless to the server.
        for key in ("ctrl", "alt", "shift", "super"):
            try:
                desktop.key_up(key)
            except Exception:
                pass
        for button in (1, 2, 3):
            try:
                desktop.mouse_up(button=button)
            except Exception:
                pass
        out["verified"] = None
        out["note"] = "no XTest channel: released blind, could not verify with the server"
    out["ok"] = not out["errors"]
    return out


# ---- 2. close what we opened ------------------------------------------------------
def _descendants(pid: int) -> set[int]:
    """pid and every process under it (Linux /proc; elsewhere just pid)."""
    found = {pid}
    if not os.path.isdir("/proc"):
        return found
    parents: dict[int, int] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    stat = fh.read().decode(errors="replace")
                # "pid (comm) state ppid ..." and comm may contain spaces.
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
                parents[int(entry)] = ppid
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return found
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in found and child not in found:
                found.add(child)
                changed = True
    return found


def _our_windows() -> list[dict]:
    """The still-open windows among those recorded at launch time. No pid
    walk here: a pid tree read now would include the owner's later windows."""
    loaded = _load_entries()
    if loaded is _UNREADABLE:
        return []
    ids = {wid for entry in loaded for wid in entry.get("windows") or ()}
    if not ids:
        return []
    return [win for win in desktop.list_windows() if str(win.get("id")) in ids]


def close_launched(wait_s: float = CLOSE_WAIT_S) -> dict:
    """Ask every window the agent opened to close (WM_DELETE_WINDOW, so an
    app with unsaved work gets to prompt), then wait for them to go."""
    loaded = _load_entries()
    if loaded is _UNREADABLE:
        return {"ok": False, "asked": [], "closed": [], "still_open": [],
                "launched": "unknown"}
    targets = _our_windows()
    out: dict = {"ok": True, "asked": [], "closed": [], "still_open": [],
                 "launched": list(loaded)}
    if not targets:
        _persist_entries([])
        return out
    ch = _channel()
    for win in targets:
        wid = str(win["id"])
        try:
            if ch is not None:
                ch.close_window(_window_id(wid))
            else:
                desktop.window_action(wid, "close")
            out["asked"].append({"id": wid, "name": win.get("name")})
        except Exception as exc:
            out["still_open"].append({"id": wid, "name": win.get("name"), "error": str(exc)})
    deadline = time.monotonic() + max(0.0, wait_s)
    remaining = {str(w["id"]) for w in targets}
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        present = {str(w.get("id")) for w in desktop.list_windows()}
        gone = remaining - present
        for wid in gone:
            out["closed"].append(wid)
        remaining &= present
    for win in targets:
        if str(win["id"]) in remaining:
            out["still_open"].append({"id": str(win["id"]), "name": win.get("name"),
                                      "note": "did not close; it may be asking about unsaved work"})
    if not remaining and not out["still_open"]:
        _persist_entries([])
    else:
        still = {str(wid) for wid in remaining}
        kept = []
        for entry in loaded:
            wins = [wid for wid in (entry.get("windows") or ()) if str(wid) in still]
            if wins:
                kept.append({**entry, "windows": wins})
        _persist_entries(kept)
    out["ok"] = not remaining and not out["still_open"]
    return out


def _window_id(wid: str) -> int:
    """xdotool prints decimal ids; xwininfo and people write 0x hex."""
    s = str(wid).strip().lower()
    return int(s, 16) if s.startswith("0x") else int(s)


# ---- 3. park the pointer ----------------------------------------------------------
def park_pointer() -> dict:
    """Put the pointer back where the owner left it (or out of the way),
    unless he is using it right now."""
    if owner_pause.human_active():
        return {"ok": True, "parked": False, "reason": "owner_active"}
    if desktop.remote_viewer_focused():
        # A focused viewer forwards pointer motion to the machine it shows.
        return {"ok": True, "parked": False, "reason": "remote_viewer_focused"}
    at = _POINTER_BEFORE["at"]
    if at is None:
        try:
            w, h = desktop.screen_size()
        except Exception:
            return {"ok": True, "parked": False, "reason": "no_screen_size"}
        at = (max(0, w - PARK_MARGIN_PX), h // 2)
        reason = "out_of_the_way"
    else:
        reason = "where_the_owner_left_it"
    mover = human.human_move if human.enabled() else desktop.move
    res = mover(int(at[0]), int(at[1]))
    ok = not (isinstance(res, dict) and res.get("ok") is False)
    out = {"ok": ok, "parked": ok, "to": [int(at[0]), int(at[1])], "reason": reason}
    if not ok:
        out["error"] = res.get("error") if isinstance(res, dict) else str(res)
    else:
        _POINTER_BEFORE["at"] = None
    return out


# ---- 4. screen ------------------------------------------------------------------
def wake_screen() -> dict:
    ch = _channel()
    if ch is None:
        return {"ok": True, "checked": False, "note": "no XTest channel"}
    try:
        res = ch.wake_screen()
        return {"ok": True, "checked": True, "was_on": res["was"].get("on", True),
                "on": res["now"].get("on", True)}
    except Exception as exc:
        return {"ok": False, "checked": True, "error": str(exc)}


# ---- 5. verify ------------------------------------------------------------------
def verify() -> dict:
    out: dict = {"held_tracked": held()}
    ch = _channel()
    if ch is not None:
        try:
            out["xtest_held"] = ch.xtest_held()
        except Exception as exc:
            out["xtest_held_error"] = str(exc)
    try:
        win = desktop.active_window()
        out["active_window"] = {k: win.get(k) for k in ("id", "name", "class")} \
            if isinstance(win, dict) and win.get("ok") else None
    except Exception:
        out["active_window"] = None
    out["our_windows_open"] = [{"id": w.get("id"), "name": w.get("name")} for w in _our_windows()]
    out["input_switches"] = owner_pause.status().get("input_switches")
    return out


def hand_back(close_launched_windows: bool = True, park: bool = True) -> dict:
    """Everything above, in order, then the checklist a person would run
    before walking away. `clean` is True only when every check passed."""
    out: dict = {"released": release_all()}
    tracked = _load_entries()
    if tracked is _UNREADABLE:
        out["launched"] = "unknown"
        if close_launched_windows:
            out["closed"] = {"ok": False, "asked": [], "closed": [],
                             "still_open": [], "launched": "unknown"}
    elif close_launched_windows:
        out["closed"] = close_launched()
        if out["closed"].get("launched") == "unknown":
            out["launched"] = "unknown"
    if park:
        out["pointer"] = park_pointer()
    out["screen"] = wake_screen()
    out["verify"] = verify()
    xt = out["verify"].get("xtest_held") or {}
    checks = {
        "nothing_held_by_agent": not xt.get("keys") and not xt.get("buttons")
        and not out["verify"]["held_tracked"]["keys"]
        and not out["verify"]["held_tracked"]["buttons"],
        "our_windows_closed": (
            False if out.get("launched") == "unknown"
            else (not out["verify"]["our_windows_open"] if close_launched_windows else True)
        ),
        "pointer_parked": (out["pointer"].get("parked") or out["pointer"].get("reason")
                           in ("owner_active", "remote_viewer_focused")) if park else True,
        "screen_on": out["screen"].get("on", True) is not False,
        "no_grab": True,  # this server never grabs; see owner_pause and esc_pause
    }
    out["checks"] = checks
    out["clean"] = all(checks.values()) and out.get("launched") != "unknown"
    out["ok"] = out["clean"]
    return out
