"""One Workman request over stdin; fixed operations, explicit backend, local log.

This file is installed beside learning.py on each device. Desktop actions use
Workman itself. macOS always calls the existing app daemon, never local fallback.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import time
import uuid

import learning
import lease
import input_switch
import motion_profile


class Refusal(Exception):
    def __init__(self, code, detail=None):
        self.code, self.detail = code, detail


def token(data):
    identity = {k: data.get(k) for k in ("id", "pid", "name", "app", "bundle")}
    if "windows" in data:
        identity["window"] = (data["windows"] or [{}])[0].get("id")
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def integer(v, minimum=-100000, maximum=100000):
    if type(v) is not int or not minimum <= v <= maximum:
        raise Refusal("invalid_arguments")
    return v


FIELDS = {
    "status": set(), "shot": {"region"}, "windows": set(), "active": set(), "pointer": set(),
    "focus": {"query"}, "move": {"x", "y", "motion", "speed"}, "click": {"x", "y", "button", "motion", "speed"},
    "type": {"text", "expect_focus"}, "key": {"key", "expect_focus"},
    "scroll": {"direction", "amount", "expect_focus"},
    "drag": {"x1", "y1", "x2", "y2", "expect_focus"},
    "minimize": {"expect_focus"}, "verify": {"event_id", "evidence_id"}, "report": {"after"},
    "reserve": {"seconds"}, "release": set(), "lease": set(),
    "input": {"mouse", "keyboard"}, "panel": set(),
    "finish": {"restore_focus", "restore_x", "restore_y"},
    "correct": {"verification_id"}, "permission_prompt": set(),
    "permission_settings": {"grant"},
    "permission_hint": {"grant", "evidence_id", "x", "y", "w", "h"}}
FIELDS["lesson"] = {"kind", "verification_id"}
FIELDS["paste"] = {"text", "expect_focus", "preset", "surface"}
FIELDS["inspect"] = set()
FIELDS["wait_window"] = {"query", "timeout_seconds"}


def validate(action, args):
    if action not in FIELDS or not isinstance(args, dict) or set(args) - FIELDS[action]:
        raise Refusal("invalid_arguments")
    if action == "lesson" and (args.get("kind") not in learning.CURATED_LESSONS or not learning.HEX.fullmatch(str(args.get("verification_id", "")))):
        raise Refusal("invalid_arguments")
    if action == "paste" and (args.get("preset") != "first_party_fast" or args.get("surface") not in ("owned_editor", "owned_github", "local_fixture")):
        raise Refusal("invalid_arguments")
    if action == "wait_window":
        if not isinstance(args.get("query"), str) or not 1 <= len(args["query"]) <= 200: raise Refusal("invalid_arguments")
        integer(args.get("timeout_seconds", 5), 0, 10)
    if action == "shot" and "region" in args:
        r = args["region"]
        if not isinstance(r, dict) or set(r) != {"x", "y", "w", "h"}:
            raise Refusal("invalid_arguments")
        integer(r["x"]); integer(r["y"]); integer(r["w"], 1, 10000); integer(r["h"], 1, 10000)
    for k in ("x", "y", "x1", "x2", "y1", "y2"):
        if k in FIELDS[action]:
            integer(args.get(k))
    if action in ("type", "paste") and (not isinstance(args.get("text"), str) or len(args["text"]) > 10000):
        raise Refusal("invalid_arguments")
    if action in ("focus", "key"):
        v = args.get("query" if action == "focus" else "key")
        if not isinstance(v, str) or not v.strip() or len(v) > 500:
            raise Refusal("invalid_arguments")
    if action == "click" and args.get("button", "left") not in ("left", "right", "double"):
        raise Refusal("invalid_arguments")
    if action == "scroll":
        if args.get("direction") not in ("up", "down"):
            raise Refusal("invalid_arguments")
        integer(args.get("amount", 3), 1, 100)
    if "expect_focus" in FIELDS[action] and not isinstance(args.get("expect_focus"), str):
        raise Refusal("invalid_arguments")
    if action == "reserve":
        integer(args.get("seconds", 120), 15, 300)
    if action == "input" and any(type(v) is not bool for v in args.values()):
        raise Refusal("invalid_arguments")
    if action in ("permission_settings", "permission_hint") and args.get("grant") not in ("screen_recording", "accessibility"):
        raise Refusal("invalid_arguments")
    if action == "permission_hint":
        integer(args.get("w"), 1, 10000); integer(args.get("h"), 1, 10000)
        if not learning.HEX.fullmatch(str(args.get("evidence_id", ""))):
            raise Refusal("invalid_arguments")
    if action in ("move", "click"):
        if args.get("motion", "direct") not in ("direct", "human"):
            raise Refusal("invalid_arguments")
        speed = args.get("speed", 1.0)
        if type(speed) not in (int, float) or not .25 <= speed <= 4:
            raise Refusal("invalid_arguments")
    if action == "finish":
        if set(args) != FIELDS[action] or not isinstance(args.get("restore_focus"), str) or not 1 <= len(args["restore_focus"]) <= 500:
            raise Refusal("invalid_arguments")
        integer(args["restore_x"]); integer(args["restore_y"])


class Backend:
    def __init__(self, kind):
        self.mac = kind == "app"
        if self.mac:
            from atmos_computer import client
            self.client = client
        elif kind == "x11" and sys.platform.startswith("linux"):
            # No workman.server.main(): that starts the optional browser listener.
            from workman import server
            self.server = server
        else:
            raise Refusal("unsupported")

    def call(self, name, **args):
        try:
            r = self.client.call_daemon(name, timeout=35, **args) if self.mac else getattr(self.server, name)(**args)
        except Exception:
            if not all(input_switch.state().values()): raise Refusal("input_disabled") from None
            raise
        if isinstance(r, dict) and (r.get("ok") is False or "error" in r):
            if not all(input_switch.state().values()):
                raise Refusal("input_disabled")
            raise Refusal("backend_error")
        return r

    def status(self):
        if self.mac:
            if not self.client.available():
                return {"ready": False, "daemon_reachable": False, "backend": "app"}
            perms = self.client.call_daemon("permissions", timeout=5)
            return {"ready": perms.get("all_granted") is True,
                    "daemon_reachable": True, "backend": "app", "permissions": perms}
        return {"ready": True, "backend": "x11", "platform": self.call("workman_platform")}

    def active(self):
        if self.mac:
            # NSWorkspace caches notifications in the long-lived helper, whose
            # socket loop does not pump the Cocoa run loop. This short-lived
            # Workman adapter reads fresh identity; window titles still come
            # through the permission-holding daemon. No local input fallback.
            from AppKit import NSWorkspace
            from Foundation import NSRunLoop, NSDate
            # Deliver activation notifications even within a multi-chunk request.
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            pid = int(app.processIdentifier())
            os.kill(pid, 0)  # Refuse a dead/stale application identity.
            r = {"app": str(app.localizedName()), "pid": pid,
                 "bundle": str(app.bundleIdentifier() or ""),
                 "windows": [w for w in self.call("windows") if w["pid"] == pid and w.get("w", 0) > 50 and w.get("h", 0) > 50],
                 "identity_source": "fresh_workman_adapter"}
        else:
            r = self.call("active_window")
        r["focus_token"] = token(r)
        return r

    def gate(self, action):
        if not self.mac:
            return
        st = self.status()
        if not st["daemon_reachable"]:
            raise Refusal("daemon_unavailable", st)
        perms = st["permissions"]
        required = "screen_recording" if action == "shot" else "accessibility"
        if not perms.get(required):
            raise Refusal("permission_required", st)

    def execute(self, action, a):
        if action == "status":
            return dict(self.status(), input_switches=input_switch.state())
        if action == "inspect":
            st = dict(self.status(), input_switches=input_switch.state())
            result = {"status": st, "permissions": st.get("permissions", {}), "observed_at": learning.now(), "reads_only": True}
            if not self.mac or st.get("permissions", {}).get("accessibility"):
                result["active"] = self.active()
                result["pointer"] = self.call("cursor_pos" if self.mac else "pointer_position")
            return result
        if action == "wait_window":
            self.gate("windows")
            deadline = time.monotonic() + a.get("timeout_seconds", 5); delay = .2; polls = 0
            while True:
                rows = self.call("windows" if self.mac else "list_windows"); polls += 1
                hits = [w for w in rows if a["query"].casefold() in str(w.get("title", w.get("name", ""))).casefold()]
                if hits: return {"matched": True, "windows": hits, "polls": polls, "proof": "Window presence only; not page loading or task completion."}
                if time.monotonic() >= deadline: return {"matched": False, "polls": polls, "timed_out": True}
                time.sleep(min(delay, max(0, deadline-time.monotonic()))); delay = min(1, delay*2)
        if action in ("permission_prompt", "permission_settings"):
            if not self.mac:
                raise Refusal("unsupported")
            if not self.client.available():
                raise Refusal("daemon_unavailable")
            if action == "permission_settings":
                return self.call("open_permission_settings", grant=a["grant"])
            return {"permissions": self.call("request_permissions"), "human_action_required": True}
        input_switch.check(action)
        self.gate(action)
        if "expect_focus" in FIELDS[action] and self.active()["focus_token"] != a["expect_focus"]:
            raise Refusal("focus_changed")
        if action == "active":
            return self.active()
        if action == "windows":
            return self.call("windows" if self.mac else "list_windows")
        if action == "pointer":
            return self.call("cursor_pos" if self.mac else "pointer_position")
        if action == "focus":
            result = self.call("focus_window", query=a["query"], **({} if self.mac else {"minimize_blockers": False}))
            time.sleep(0.15)
            result["active"] = self.active()
            current = result["active"]
            labels = [str(current.get(k, "")) for k in ("id", "name", "app", "bundle")]
            labels += [str(w.get("title", "")) for w in current.get("windows", [])]
            if not any(a["query"].casefold() in label.casefold() for label in labels):
                raise Refusal("focus_changed")
            return result
        if action == "move":
            return self.move(a)
        if action == "click":
            button = a.get("button", "left")
            kwargs = {"button": button} if self.mac else {"button": 3 if button == "right" else 1, "count": 2 if button == "double" else 1}
            if self.mac:
                start = self.call("cursor_pos")
                kwargs.update(humanize=a.get("motion", "direct") == "human",
                              duration_ms=motion_profile.duration_ms((start["x"], start["y"]), (a["x"], a["y"]), a.get("speed", 1)))
            elif a.get("motion", "direct") == "human":
                self.move(a)
                input_switch.check("click")
            return self.call("click", x=a["x"], y=a["y"], **kwargs)
        if action == "paste":
            # Explicit first-party preset. Never inspect/copy the prior clipboard
            # (it may contain a secret); ordinary supplied text replaces it.
            input_switch.check(action)
            if self.active()["focus_token"] != a["expect_focus"]: raise Refusal("focus_changed")
            if self.mac:
                self.call("paste_text", text=a["text"])
            else:
                self.call("clipboard_set", text=a["text"])
                input_switch.check(action)
                if self.active()["focus_token"] != a["expect_focus"]: raise Refusal("focus_changed")
                self.call("press_key", key="ctrl+v")
            same = self.active()["focus_token"] == a["expect_focus"]
            return {"pasted_chars": len(a["text"]), "focus_unchanged": same,
                    "exact_readback_required": True, "verified": False,
                    "clipboard": "Replaced with supplied nonsecret text; prior clipboard was not read."}
        if action == "type":
            count = 0
            # Small Workman calls bound STOP latency and recheck focus. No text
            # is placed in the journal, graph, process argv or clipboard.
            for start in range(0, len(a["text"]), 4):
                input_switch.check(action)
                if self.active()["focus_token"] != a["expect_focus"]:
                    raise Refusal("focus_changed", {"typed_chars": count, "partial": count > 0})
                self.call("type_text", text=a["text"][start:start + 4])
                count += len(a["text"][start:start + 4])
            return {"typed_chars": count}
        if action == "key":
            return self.call("press_key", **({"combo": a["key"]} if self.mac else {"key": a["key"]}))
        if action == "scroll":
            return self.call("scroll", direction=a["direction"], amount=a.get("amount", 3))
        if action == "drag":
            coords = {k: a[k] for k in ("x1", "y1", "x2", "y2")}
            if not self.mac:
                coords = dict(zip(("from_x", "from_y", "to_x", "to_y"), coords.values()))
            return self.call("drag", **coords)
        if action == "minimize":
            if self.mac:
                return self.call("press_key", combo="cmd+m")
            return self.call("window", action="minimize", query=str(self.active()["id"]))
        if action == "shot":
            if self.mac:
                result = self.call("region", **a["region"]) if "region" in a else self.call("screenshot", full=False)
                raw = result.pop("image")
                fmt = result.get("format", "jpeg")
                contract = {"image_to_points": result.get("image_to_points"),
                            "origin_x": result.get("origin_x", 0), "origin_y": result.get("origin_y", 0)}
                if "region" in a:
                    r = a["region"]
                    contract = {"image_to_points": r["w"] / result["pixels"][0],
                                "origin_x": r["x"], "origin_y": r["y"]}
            else:
                # Workman capture/vision primitives without retaining screen files.
                s = self.server
                r = a.get("region")
                raw = s.desktop.screenshot(region=tuple(r[k] for k in ("x", "y", "w", "h"))) if r else s.desktop.screenshot()
                raw, meta = s.vision.to_view(raw)
                result, fmt = meta, "png"
                contract = {"image_to_points": 1 / meta["scale"], "origin_x": r["x"] if r else 0, "origin_y": r["y"] if r else 0}
            from PIL import Image
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            if all(lo == hi for lo, hi in im.getextrema()):
                raise Refusal("blank_capture")
            return {"image": base64.b64encode(raw).decode(), "mime": "image/" + fmt,
                    "width": im.width, "height": im.height, "coordinates": contract,
                    "capture_sha256": hashlib.sha256(raw).hexdigest()}
        raise Refusal("unsupported")

    def move(self, a):
        smooth = a.get("motion", "direct") == "human"
        speed = a.get("speed", 1)
        if self.mac:
            p = self.call("cursor_pos")
            return self.call("move", x=a["x"], y=a["y"], humanize=smooth,
                             duration_ms=motion_profile.duration_ms((p["x"], p["y"]), (a["x"], a["y"]), speed))
        if smooth:
            p = self.call("pointer_position")
            return motion_profile.linux_move((p["x"], p["y"]), (a["x"], a["y"]), speed,
                                            lambda x, y: self.call("move", x=x, y=y))
        return self.call("move", x=a["x"], y=a["y"])


def run(request, backend_factory=Backend):
    node, action, args = request.get("node"), request.get("action"), request.get("args", {})
    if node not in learning.NODES:
        return {"ok": False, "code": "invalid_arguments"}
    started = time.monotonic()
    event = {"id": uuid.uuid4().hex, "node": node, "action": action,
             "ts": learning.now(), "ok": False, "code": "backend_error"}
    event.update({k: v for k, v in request.get("context", {}).items() if k in ("project", "task", "session")})
    result = None
    try:
        validate(action, args)
        if action == "report":
            events = learning.read_events(learning.journal_path())
            own = [e for e in events if e["node"] == node]
            after = args.get("after")
            start = next((i + 1 for i, e in enumerate(own) if e["id"] == after), 0)
            return {"ok": True, "code": "ok", "events": own[start:start + 500],
                    "more": len(own) > start + 500}
        if action == "input":
            result = input_switch.update(**args) if args else input_switch.state()
            event["permissions"] = {k + "_enabled": v for k, v in result.items()}
        elif action == "permission_hint":
            from datetime import datetime, timezone
            import subprocess
            from pathlib import Path
            backend = backend_factory(request.get("backend"))
            if not backend.mac: raise Refusal("unsupported")
            backend.gate("shot")
            rows = [e for e in learning.read_events(learning.journal_path()) if e["node"] == node]
            shots = [e for e in rows if e["action"] == "shot" and e.get("capture_sha256")]
            shot = shots[-1] if shots else {}
            if shot.get("id") != args["evidence_id"]: raise Refusal("invalid_arguments")
            observed = datetime.strptime(shot["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            if not 0 <= time.time() - observed <= 30: raise Refusal("invalid_arguments")
            # Changed screens invalidate the target hint, including our own inputs.
            if any(e["action"] not in ("status", "active", "pointer", "windows", "report") for e in rows[rows.index(shot)+1:]):
                raise Refusal("invalid_arguments")
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("permission_hint.py"))],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            proc.stdin.write(json.dumps({"target": [args[k] for k in ("x", "y", "w", "h")], "grant": args["grant"], "observed": observed}).encode())
            proc.stdin.close()
            result = {"launched": True, "expires_in_seconds": min(20, int(30-(time.time()-observed))), "motion": "none", "human_approval_required": True}
            event["evidence_id"] = shot["id"]
        elif action == "panel":
            import subprocess
            from pathlib import Path
            python = os.environ.get("WORKMAN_GTK_PYTHON", sys.executable) if request.get("backend") == "x11" else sys.executable
            subprocess.Popen([python, str(Path(__file__).with_name("input_panel.py"))],
                             env=dict(os.environ, WORKMAN_FLEET_NODE=node),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            result = {"launched": True, "instruction": "Use STOP BOTH or the individual toggles. Closing the panel preserves switch settings."}
        elif action == "finish":
            with lease.hold(event.get("session", "unattributed"), "finish"):
                backend = backend_factory(request.get("backend"))
                result = {"released": True, "restoration": {}}
                for op, params in [("focus", {"query": args["restore_focus"]}),
                                   ("move", {"x": args["restore_x"], "y": args["restore_y"]})]:
                    try:
                        backend.execute(op, params)
                        result["restoration"][op] = "ok"
                    except input_switch.Disabled:
                        result["restoration"][op] = "skipped_input_disabled"
                    except Exception:
                        result["restoration"][op] = "restore_failed"
        elif action in ("reserve", "release", "lease"):
            with lease.hold(event.get("session", "unattributed"), action, args.get("seconds", 120)) as st:
                result = st
        elif action == "lesson":
            rows = learning.read_events(learning.journal_path()); by_id = {e["id"]: e for e in rows}
            verification = by_id.get(args["verification_id"], {})
            target = by_id.get(verification.get("verifies"), {})
            if verification.get("node") != node or not verification.get("verified") or any(e.get("corrects") == args["verification_id"] for e in rows):
                raise Refusal("invalid_arguments")
            if target.get("action") != learning.CURATED_LESSONS[args["kind"]][0]: raise Refusal("invalid_arguments")
            event.update(lesson_kind=args["kind"], evidence_id=args["verification_id"])
            result = {"lesson_kind": args["kind"], "evidence_id": args["verification_id"]}
        elif action == "correct":
            rows = learning.read_events(learning.journal_path())
            target = next((e for e in rows if e["node"] == node and e["id"] == args.get("verification_id") and e.get("verified")), None)
            if not target:
                raise Refusal("invalid_arguments")
            event["corrects"] = target["id"]
            result = {"retracted_verification": target["id"], "reason": "verification_did_not_establish_expected_result"}
        elif action == "verify":
            rows = [e for e in learning.read_events(learning.journal_path()) if e["node"] == node]
            by_id = {e["id"]: e for e in rows}
            target, evidence = by_id.get(args.get("event_id")), by_id.get(args.get("evidence_id"))
            if not target or not evidence or not target["ok"] or evidence["action"] != "shot" or not evidence.get("capture_sha256") or rows.index(evidence) <= rows.index(target):
                raise Refusal("invalid_arguments")
            event.update(verified=True, verifies=target["id"], evidence_id=evidence["id"])
            result = {"verified": target["id"], "evidence": evidence["id"], "method": "agent_visual_confirmation"}
        else:
            with lease.hold(event.get("session", "unattributed"), action):
                result = backend_factory(request.get("backend")).execute(action, args)
        event.update(ok=True, code="ok")
        if action in ("type", "paste"):
            event["typed_chars"] = len(args["text"])
        if action == "shot":
            event["capture_sha256"] = result["capture_sha256"]
    except Refusal as e:
        event["code"], result = e.code, e.detail
    except lease.Busy:
        event["code"] = "device_busy"
        result = lease.status(event.get("session", "unattributed"))
    except input_switch.Disabled:
        event["code"] = "input_disabled"
        result = {"input_switches": input_switch.state(), "instruction": "User input is OFF. Do not enable it without the user's request; a preceding short input may have completed."}
    except Exception:
        # Never echo exception messages that may include typed text or commands.
        event["code"] = "backend_error"
    event["duration_ms"] = round((time.monotonic() - started) * 1000)
    if isinstance(result, dict):
        perms = result.get("permissions") or {}
        event.setdefault("permissions", {}).update({k: perms[k] for k in ("screen_recording", "accessibility") if k in perms})
        if "daemon_reachable" in result:
            event["permissions"]["daemon_reachable"] = result["daemon_reachable"]
    try:
        event = learning.clean_event(event)
        learning.append(learning.journal_path(), [event])
        recorded = True
    except Exception:
        recorded = False
    return {"ok": event["ok"], "code": event["code"], "data": result,
            "event": event, "local_recorded": recorded}


if __name__ == "__main__":
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.buffer.readline(100000))
        if not isinstance(request, dict):
            raise ValueError()
        print(json.dumps(run(request)))
    except Exception:
        print(json.dumps({"ok": False, "code": "invalid_arguments"}))
