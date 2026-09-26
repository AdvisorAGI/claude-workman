"""Consolidated, bounded onboarding over the existing helper and grant watcher."""
import asyncio
import hashlib
import json
import time

import fleet
import learning

REQUIREMENTS = {
    "view": ("screen_recording", "View and verify the authorized desktop.", "Screen & System Audio Recording"),
    "input": ("accessibility", "Move the pointer, click, type and manage windows for authorized tasks.", "Accessibility"),
}


def state_path(node):
    return learning.hub_root() / (".workman-onboarding-" + node + ".json")


def load(node):
    try: return json.loads(state_path(node).read_text())
    except (OSError, ValueError): return {}


def save(node, state):
    p = state_path(node)
    with learning.lock(p.with_suffix(".lock")):
        temp = p.with_suffix(".new")
        temp.write_text(json.dumps(state)); temp.chmod(0o600); temp.replace(p)


def describe(node, status, features, previous=None):
    data = status.get("data") or {}
    perms = data.get("permissions") or {}
    requirements = []
    if node != "dgx":
        for feature in features:
            key, reason, pane = REQUIREMENTS[feature]
            value = perms.get(key)
            requirements.append({"number": len(requirements) + 1, "id": key, "reason": reason,
                "state": "granted" if value is True else "missing" if value is False else "unknown",
                "settings_pane": "System Settings > Privacy & Security > " + pane,
                "human_approval": True})
    revoked = [r["id"] for r in requirements if r["state"] == "missing" and (previous or {}).get(r["id"]) is True]
    ready = status.get("ok") is True and data.get("ready") is True
    if requirements:
        ready = status.get("ok") is True and all(r["state"] == "granted" for r in requirements)
    return {"node": node, "ready": ready, "requirements": requirements, "revoked": revoked,
            "observed_at": status.get("event", {}).get("ts"), "evidence_id": status.get("event", {}).get("id"),
            "transport_verified": status.get("ok") is True,
            "capture_authorized": perms.get("screen_recording") is True if node != "dgx" else True,
            "browser_permissions": "None requested: desktop control does not enable the optional browser companion.",
            "consent": {"installation": "Separate owner approval for this installation.",
                        "os_grants": "Each OS prompt is approved by the human.",
                        "task": "Permission grants do not authorize unrelated tasks."},
            "input_switches": data.get("input_switches"),
            "proof": "Live grant state only; verify capture and harmless input separately."}


async def _run(node, features=None, intent="inspect", timeout_seconds=0, control=None, sleeper=asyncio.sleep, clock=time.monotonic):
    control = control or fleet.control
    node = fleet.ALIASES.get(node, node)
    features = ["view", "input"] if features is None else features
    if node not in learning.NODES or not isinstance(features, list) or not features or len(set(features)) != len(features) or any(f not in REQUIREMENTS for f in features):
        raise ValueError("choose an authorized device and enabled view/input features")
    if intent not in ("inspect", "begin", "wait", "retry", "cancel") or type(timeout_seconds) is not int or not 0 <= timeout_seconds <= 60:
        raise ValueError("invalid onboarding request")
    installation = hashlib.sha256(json.dumps(fleet.registry()[node], sort_keys=True).encode()).hexdigest()[:16]
    state = load(node)
    if state.get("installation") != installation:
        state = {"installation": installation, "prompted": [], "last_grants": {}}
    if intent == "cancel":
        state["cancelled"] = True; save(node, state)
        return {"node": node, "state": "cancelled", "ready": False, "input_changed": False}
    if intent == "retry":
        state["prompted"] = []; state["cancelled"] = False
    if intent == "begin": state["cancelled"] = False
    deadline = clock() + timeout_seconds
    delay = 1
    prompted_now = []
    while True:
        status = await control(node, "status")
        result = describe(node, status, features, state.get("last_grants"))
        perms = (status.get("data") or {}).get("permissions") or {}
        # A helper relaunch is an unknown observation, not a revocation or a
        # reason to forget the last live grant. Preserve it until re-observed.
        state.setdefault("last_grants", {}).update({k: v for k, v in perms.items()
            if k in ("screen_recording", "accessibility") and type(v) is bool})
        result["prompted_now"] = prompted_now
        result["state"] = "ready" if result["ready"] else "awaiting_human" if status.get("ok") else "helper_or_transport_unavailable"
        if result["revoked"]: result["state"] = "revoked"
        if state.get("cancelled"): result["state"] = "cancelled"
        missing = [r["id"] for r in result["requirements"] if r["state"] == "missing"]
        # One grant at a time, and only an explicit setup flow requests prompts.
        if not state.get("cancelled") and intent in ("begin", "retry") and missing:
            grant = missing[0]
            if grant not in state["prompted"]:
                opening = await control(node, "permission_settings", {"grant": grant})
                if opening.get("ok"):
                    state["prompted"].append(grant); prompted_now.append(grant)
                else:
                    result["settings_open_error"] = opening.get("code", "backend_error")
        save(node, state)
        if result["ready"] or state.get("cancelled") or intent == "inspect" or clock() >= deadline:
            result["wait_timed_out"] = bool(timeout_seconds and not result["ready"] and not state.get("cancelled"))
            result["next"] = None if result["ready"] else "Human: approve only the missing grant in the named pane; then run wait. A false grant cannot distinguish denial from an unanswered prompt."
            return result
        await sleeper(min(delay, max(0, deadline - clock())))
        delay = min(delay * 2, 8)


async def run(node, *args, **kwargs):
    import fcntl
    node = fleet.ALIASES.get(node, node)
    if node not in learning.NODES: raise ValueError("unknown device")
    p = state_path(node).with_suffix(".active-lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        p.chmod(0o600)
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"node": node, "state": "setup_in_progress_in_another_session", "ready": False}
        return await _run(node, *args, **kwargs)
