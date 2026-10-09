"""session_set_model_effort: change ANOTHER session's model and/or effort from
its front end at a clean turn, verify the new value, log it."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from .. import front_compact as fc
from . import registry as rg
from . import state as st
from .common import EFFORTS
from .drivers import ChromeDriver, page_shows
from .guards import (MODEL_POLICY, _guard, _public, band_guard, expected_titles, find_session,
                     headless_refusal, log_line, model_log_path, norm, rc_url, turn_guards)
from .picker import ChromePicker, DesktopPicker, effort_labels, model_labels, normalize_model


FRONTEND_LOCK_WAIT_S = 60

VERIFY_POLLS, VERIFY_POLL_S = 20, 0.5

def _desk_now(row: dict) -> dict:
    """The desktop store's model/effort for this session right now."""
    if not row.get("_scan"):
        return {}
    d = rg.desktop_sessions(row["_scan"].home).get(row["session_id"]) or {}
    return {"model": d.get("model") or "", "effort": d.get("effort") or ""}

def _request_error(model: str, effort: str, allow_fable: bool) -> tuple[str, str]:
    """(model_id, error) for the asked change, before any session is read."""
    model_id, err = normalize_model(model, allow_fable)
    if err:
        return "", err
    if effort and effort not in EFFORTS:
        return "", f"effort must be one of {list(EFFORTS)}"
    if not model_id and not effort:
        return "", "give a model, an effort, or both"
    return model_id, ""


def _current(row: dict) -> tuple[str, str]:
    """What the picker shows now: the desktop store for a desktop session; for
    any other front end the last turn's own record beats the launch flags,
    which a /model or /effort since then has made stale."""
    if row.get("front_end") == "desktop":
        return (row.get("front_model") or row.get("model") or "",
                row.get("front_effort") or row.get("effort") or "")
    return (row.get("model") or row.get("front_model") or "",
            row.get("effort") or row.get("front_effort") or "")


def _changes(cur: tuple[str, str], model_id: str, effort: str) -> list[tuple]:
    changes = []
    if model_id and model_id.split("[")[0] != cur[0].split("[")[0]:
        changes.append(("model", model_labels(cur[0]), model_labels(model_id)))
    if effort and effort != cur[1]:
        changes.append(("effort", effort_labels(cur[1]), effort_labels(effort)))
    return changes


def _picker_guards(row: dict, changes: list[tuple], self_id: str, procs: Any,
                   max_tokens: int, force: bool) -> list[dict]:
    guards = turn_guards(row, self_id, procs)
    if any(c[0] == "model" for c in changes):
        band, _ = band_guard(int(row.get("ctx_tokens") or 0), MODEL_POLICY[0], max_tokens,
                             force, MODEL_POLICY)
        guards.append(band)
    unknown = [c[0] for c in changes if not c[1][0]]
    guards.append(_guard("current_known", not unknown,
                         f"current {', '.join(unknown)} unknown: cannot find the picker by its "
                         "label" if unknown else ""))
    return guards


def session_set_model_effort(session: str, model: str = "", effort: str = "",
                             mode: str = "dry_run", self_id: str = "", home: Any = None,
                             now: float | None = None, proc_root: Path = Path("/proc"),
                             procs: Callable[[], list[str]] | None = None,
                             picker: Any = None, driver: Any = None,
                             sleep: Callable[[float], None] = time.sleep,
                             allow_fable: bool = False, max_tokens: int = MODEL_POLICY[1],
                             force: bool = False) -> dict[str, Any]:
    """Change ANOTHER session's model and/or effort from its front end, at a
    clean turn. Dry run by default. Fable only with allow_fable=True. A model
    switch re-reads the whole context uncached, so it is refused above the
    policy ceiling (400k; a caller may lower it) unless forced."""
    if mode not in ("dry_run", "run"):
        return {"ok": False, "error": "mode must be 'dry_run' or 'run'"}
    effort = (effort or "").strip().lower()
    model_id, err = _request_error(model, effort, allow_fable)
    row = None if err else find_session(session, home, now, proc_root)
    if err or not row[0]:
        err = err or row[1]
        log_line(model_log_path(), target=session, tool="session_set_model_effort", mode=mode,
                 result=err if err.startswith("refused") else f"refused: {err}")
        return {"ok": False, "mode": mode, "error": err}
    row = row[0]
    cur = _current(row)
    out: dict[str, Any] = {"mode": mode, "session": _public(row),
                           "current": {"model": cur[0], "effort": cur[1]},
                           "target": {"model": model_id or cur[0], "effort": effort or cur[1]}}

    def logr(result: str) -> None:
        log_line(model_log_path(), target=row.get("local_id") or row["session_id"],
                 tool="session_set_model_effort", mode=mode, front_end=row.get("front_end"),
                 current=out["current"], wanted=out["target"], result=result)

    if row.get("front_end") == "headless":
        why = headless_refusal(row, "model", model_id, effort)
        logr(f"refused: {why}")
        return {**out, "ok": False, "refused": why}
    changes = _changes(cur, model_id, effort)
    if not changes:
        logr("no change: already at the target")
        return {**out, "ok": True, "result": "no change: already at the target"}
    out["guards"] = _picker_guards(row, changes, self_id, procs, max_tokens, force)
    out["would"] = picker_steps(row, changes)
    failed = [g for g in out["guards"] if not g["ok"]]
    if failed:
        why = "; ".join(f"{g['name']}: {g['detail']}" for g in failed)
        logr(f"refused: {why}")
        return {**out, "ok": False, "refused": why}
    if mode == "dry_run":
        logr("dry_run: would change " + ", ".join(c[0] for c in changes))
        return {**out, "ok": True, "result": "dry_run: every guard passed; nothing was touched"}
    result = _run_picker(row, changes, procs, picker, driver, sleep, self_id)
    logr(result["result"])
    return {**out, **result}


def picker_steps(row: dict, changes: list[tuple]) -> list[str]:
    steps = [f"hold {fc.FRONTEND_LOCK}"]
    if row.get("front_end") == "desktop":
        steps += [f"xdg-open {fc.deep_link(row.get('local_id', ''))}",
                  "hash the header strip on :0, abort unless it equals the learned hash"]
    else:
        steps += ["open the session's claude.ai/code page in Chrome on :0",
                  "abort unless the page shows the session title"]
    steps.append("re-check the turn is idle")
    for what, cur, tgt in changes:
        steps.append(f"{what}: press the footer control showing {cur[0]!r}, pick {tgt[0]!r} "
                     "(a missing item closes the menu by its opener, never Escape)")
    steps.append("verify the footer shows the new value" +
                 (" and the desktop store records it" if row.get("front_end") == "desktop" else ""))
    steps.append("log to " + str(model_log_path()))
    return steps

def _run_picker(row: dict, changes: list[tuple], procs: Any, picker: Any, driver: Any,
                sleep: Callable[[float], None], self_id: str = "") -> dict[str, Any]:
    lock = fc.try_lock(fc.FRONTEND_LOCK, FRONTEND_LOCK_WAIT_S, sleep)
    if lock is None:
        return {"ok": False, "result": "abort: frontend lock busy"}
    try:
        if row["front_end"] == "desktop":
            fc.open_session(row["local_id"])
            sleep(fc.OPEN_SETTLE_S)
            seen, want = fc.header_hash(), fc.stored_header(row["local_id"])
            if not fc.header_matches(seen, want):
                return {"ok": False, "result": f"abort: header {seen} != {want}"}
            pk = picker or DesktopPicker(sleep=sleep)
        else:
            drv = driver or ChromeDriver()
            reg = row["_scan"].reg.get(row["session_id"]) or {}
            if not drv.open_url(rc_url(reg.get("_bridge") or "")).get("ok"):
                return {"ok": False, "result": "abort: could not open the session page"}
            drv.wait_load(20)
            if not page_shows(drv.read_page(), expected_titles(row)):
                return {"ok": False, "result": "abort: page does not show the session title"}
            pk = picker or ChromePicker(drv, sleep)
        ok, busy = st.turn_ready(row["path"], self_id=self_id, home=row["_scan"].home,
                                 proc_root=row["_scan"].proc_root)
        if not ok:
            return {"ok": False, "result": f"abort: target busy again: {busy}"}
        done = []
        for what, cur, tgt in changes:
            got = pk.pick(cur, tgt)
            if not got.get("ok"):
                return {"ok": False, "result": f"abort: {what}: {got.get('error')}",
                        "changed": done}
            done.append(what)
        verified = {}
        for _ in range(VERIFY_POLLS):
            desk = _desk_now(row) if row["front_end"] == "desktop" else {}
            for what, _cur, tgt in changes:
                stored = desk.get(what, "")
                verified[what] = (pk.shows(tgt) or
                                  (bool(stored) and norm(stored) in {norm(x) for x in tgt}))
            if all(verified.values()):
                break
            sleep(VERIFY_POLL_S)
        if not all(verified.values()):
            return {"ok": False, "result": "fail: picked, but the new value was not confirmed: "
                    + ", ".join(k for k, v in verified.items() if not v), "changed": done}
        return {"ok": True, "result": "changed " + ", ".join(done), "changed": done,
                "verified": verified}
    finally:
        os.close(lock)
