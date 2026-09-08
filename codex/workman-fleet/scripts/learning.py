"""Deterministic extension of Workman's existing journal and DGX learner stores.

Only explicitly invoked fleet actions enter this path. No process/window scans,
transcript reading, background threads, network listeners or model calls.
"""
from __future__ import annotations

import collections
import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path

NODES = ("dgx", "mini", "air", "machome")
ACTIONS = ("status", "shot", "windows", "active", "focus", "pointer", "move",
           "click", "type", "key", "scroll", "drag", "minimize", "verify", "report",
           "lease", "reserve", "release", "input", "panel", "finish", "correct", "permission_prompt", "permission_settings", "permission_hint", "lesson", "paste", "inspect", "wait_window")
CURATED_LESSONS = {
    "macos-fixture-edit-menu": ("type", "A bare Cocoa test application needs an Edit menu with the Select All responder action before Command-A is a valid typing test. Verify replacement using both exact character count and SHA-256; a successful key call alone proves neither selection nor replacement.", "codex/workman-fleet/tests/gui_fixture.py"),
    "x11-drag-settle": ("drag", "X11 title-bar drag required a brief settle after button-down and after movement. Pair release in finally, and verify the resulting window displacement rather than trusting the command return.", "workman/x11.py"),
}
CODES = ("ok", "permission_required", "daemon_unavailable", "focus_changed",
         "invalid_arguments", "backend_error", "blank_capture", "unsupported",
         "transport_error", "timeout", "recording_failed", "device_busy", "input_disabled")
HEX = re.compile(r"^[a-f0-9]{32}$")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        os.chmod(path, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def clean_event(event):
    """Positive schema, never arbitrary arguments, error messages or result text."""
    if event.get("node") not in NODES or event.get("action") not in ACTIONS:
        raise ValueError("invalid event identity")
    if not HEX.fullmatch(str(event.get("id", ""))):
        raise ValueError("invalid event id")
    if event.get("code") not in CODES:
        raise ValueError("invalid outcome")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(event.get("ts", ""))):
        raise ValueError("invalid timestamp")
    e = {k: event[k] for k in ("id", "node", "action", "code", "ts")}
    e["ok"] = event.get("ok") is True
    for k in ("duration_ms", "typed_chars"):
        if k in event and type(event[k]) is int and 0 <= event[k] <= 1000000:
            e[k] = event[k]
    for k in ("evidence_id", "verifies", "corrects"):
        if HEX.fullmatch(str(event.get(k, ""))):
            e[k] = event[k]
    if event.get("verified") is True:
        e["verified"] = True
    if event.get("lesson_kind") in CURATED_LESSONS:
        e["lesson_kind"] = event["lesson_kind"]
    for k in ("project", "task", "session"):
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", str(event.get(k, ""))):
            e[k] = event[k]
    if isinstance(event.get("permissions"), dict):
        e["permissions"] = {k: v for k, v in event["permissions"].items()
                            if k in ("screen_recording", "accessibility", "daemon_reachable", "mouse_enabled", "keyboard_enabled") and type(v) is bool}
    if re.fullmatch(r"[a-f0-9]{64}", str(event.get("capture_sha256", ""))):
        e["capture_sha256"] = event["capture_sha256"]
    return e


def read_events(path):
    """Read only our schema marker, never return historical unfiltered payloads."""
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("tool") == "workman_fleet_v1.1":
                    rows.append(clean_event(r["payload"]))
            except (ValueError, KeyError, TypeError):
                continue
    return rows


def append(path, events):
    """Idempotent event delivery; the old journal shape stays readable by learner."""
    events = [clean_event(e) for e in events]
    with lock(path.with_suffix(".fleet.lock")):
        known = {e["id"]: e for e in read_events(path)}
        for e in events:
            if e["id"] in known and known[e["id"]] != e:
                raise ValueError("conflicting event identity")
            known[e["id"]] = e
        seen = {e["id"] for e in read_events(path)}
        with path.open("a") as f:
            os.chmod(path, 0o600)
            for e in events:
                if e["id"] not in seen:
                    f.write(json.dumps({"ts": e["ts"], "kind": "action",
                                        "tool": "workman_fleet_v1.1", "payload": e}) + "\n")
                    seen.add(e["id"])
            f.flush()
            os.fsync(f.fileno())
        if path == journal_path():
            publish_status(read_events(path))


def publish_status(rows):
    """Read-only status interface for the existing native taskboard; no task edits."""
    if not rows: return
    last = rows[-1]
    own = [e for e in rows if e["node"] == last["node"]]
    by_id = {e["id"]: e for e in own}
    corrected = {e["corrects"] for e in own if e.get("corrects")}
    verified = next((e for e in reversed(own) if e.get("verified") and e["id"] not in corrected), None)
    action = by_id.get(verified.get("verifies"), {}).get("action") if verified else None
    permission_row = next((e for e in reversed(own) if "accessibility" in e.get("permissions", {}) or "screen_recording" in e.get("permissions", {})), {})
    payload = {"schema_version": 1, "device": last["node"], "task": last.get("task", "Workman"),
        "updated": last["ts"], "current_action": last["action"], "outcome": last["code"],
        "latest_verified": (action + " visually verified") if action else "No current visual verification",
        "verification_event": verified["id"] if verified else None,
        "next_action": "Exact result needs rechecking" if last["action"] == "correct" else LESSONS.get(last["code"], "Continue the authorized task; release input when finished."),
        "permissions": permission_row.get("permissions", {}), "permissions_observed_at": permission_row.get("ts"),
        "source_event": last["id"], "scope": "Task-related Workman outcomes only"}
    p = journal_path().parent / "fleet-status.json"
    tmp = p.with_suffix(".new"); tmp.write_text(json.dumps(payload)); tmp.chmod(0o600); tmp.replace(p)


def journal_path():
    return Path(os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")).expanduser() / "journal.jsonl"


def hub_root():
    return Path(os.environ.get("WORKMAN_FLEET_LEARN_ROOT", "~/autonomy/learnings")).expanduser()


LESSONS = {
    "input_disabled": "The user switched automation off. Leave physical input to the user, release the session's desktop lease and wait for an explicit request to enable it again.",
    "device_busy": "Another Workman session owns this physical desktop. Continue on a different device or wait for release/expiry; do not interleave input or steal focus.",
    "permission_required": "Use the Workman.app daemon and check its live Screen Recording and Accessibility grants. A human must grant missing permissions; never fall back to SSH-process capture/input.",
    "daemon_unavailable": "Start the installed Workman.app helper before control. A successful SSH connection is not a running GUI helper.",
    "focus_changed": "Capture and read the current focus token immediately before typing or key chords. A stale token is refused; obtain a fresh token after changing focus.",
    "blank_capture": "A uniform capture is not proof of screen access. Inspect Workman.app permissions and the active display before attempting input.",
    "timeout": "An action timed out and may already have executed. Observe the device before trying again; never blindly retry input.",
    "transport_error": "Reconnect the existing authenticated mesh/SSH, then observe the device before retrying an uncertain action.",
}


def lessons(events):
    """Deduplicate by device + failure mechanism or verified operation."""
    by_id = {e["id"]: e for e in events}
    retracted = {e["corrects"] for e in events if e.get("corrects") and e["ok"]}
    found = {}
    failures = {}
    for e in events:
        key = (e["node"], e["action"])
        if e["action"] == "lesson" and e.get("lesson_kind") in CURATED_LESSONS and e.get("evidence_id") in by_id:
            verification = by_id[e["evidence_id"]]
            target = by_id.get(verification.get("verifies"), {})
            kind = e["lesson_kind"]
            operation, text, source = CURATED_LESSONS[kind]
            if e["ok"] and verification.get("verified") and verification["id"] not in retracted and target.get("action") == operation:
                found[(e["node"], "verified_" + kind)] = {"node": e["node"], "kind": "verified_" + kind,
                    "lesson": text, "source_ref": source, "evidence_ids": [e["id"], verification["id"], target["id"], verification["evidence_id"]], "last_seen": e["ts"]}
        if e["code"] in LESSONS:
            failures[key] = e
            found[(e["node"], e["code"])] = {
                "node": e["node"], "kind": e["code"], "lesson": LESSONS[e["code"]],
                "evidence_ids": [e["id"]], "last_seen": e["ts"]}
        if e.get("verified") and e["id"] not in retracted and e.get("verifies") in by_id and e.get("evidence_id") in by_id:
            target, evidence = by_id[e["verifies"]], by_id[e["evidence_id"]]
            if target["node"] != e["node"] or evidence["node"] != e["node"] or not target["ok"]:
                continue
            if evidence["action"] != "shot" or not evidence.get("capture_sha256"):
                continue
            action = target["action"]
            failure = failures.get((e["node"], action))
            ids = [target["id"], evidence["id"], e["id"]]
            text = f"{action} was visually verified through the fleet entry point. Reuse this device's configured Workman backend, capture before acting, and restore prior focus and pointer after a test."
            if failure and failure["ts"] <= target["ts"]:
                ids.insert(0, failure["id"])
                text = f"Recovery verified for {action} after {failure['code']}. " + LESSONS.get(failure["code"], "")
            found[(e["node"], "verified_" + action)] = {
                "node": e["node"], "kind": "verified_" + action, "lesson": text,
                "evidence_ids": ids, "last_seen": e["ts"]}
    return list(found.values())


def hub_events():
    return [e for n in NODES for e in read_events(hub_root() / "by-device" / n / "workman-v1.1.jsonl")]


def receive(node, events):
    if node not in NODES or any(e.get("node") != node for e in events):
        raise ValueError("node mismatch")
    root = hub_root()
    with lock(root / ".workman-v1.1.lock"):
        append(root / "by-device" / node / "workman-v1.1.jsonl", events)
        all_rows = hub_events()
        learned = lessons(all_rows)
        # Incremental graph projection; same journal evidence and dedupe keys.
        import memory_graph
        memory_graph.ingest(events, learned)
        knowledge = root / "knowledge" / "WORKMAN-FLEET-V1.1.md"
        knowledge.parent.mkdir(parents=True, exist_ok=True)
        body = "# Workman fleet v1.1 evidence-backed lessons\n\nLocal deterministic processing; only explicit Workman fleet actions.\n\n"
        for l in learned:
            body += f"- [{l['last_seen']} {l['node']}] {l['lesson']} Evidence: {', '.join(l['evidence_ids'])}.\n"
        tmp = knowledge.with_suffix(".tmp")
        tmp.write_text(body)
        os.replace(tmp, knowledge)
    return {"received": len(events), "unique_events": len(all_rows), "lessons": len(learned)}


def summary(events):
    return {"events": len(events), "outcomes": dict(collections.Counter(e["code"] for e in events)),
            "last_event": events[-1]["ts"] if events else None,
            "lessons": lessons(events)}
