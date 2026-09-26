import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import device
import fleet
import learning


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "local"))
    monkeypatch.setenv("WORKMAN_FLEET_LEARN_ROOT", str(tmp_path / "hub"))


def event(i="a", node="dgx", action="move", code="ok", **extra):
    return dict(id=i * 32, node=node, action=action, code=code,
                ts="2026-09-07T12:00:00Z", ok=code == "ok", **extra)


@pytest.mark.parametrize("action,args", [
    ("shell", {}), ("move", {"x": True, "y": 1}), ("click", {"x": 1.5, "y": 1}),
    ("click", {"x": 1, "y": 2, "button": "middle"}), ("type", {"text": "x"}),
    ("type", {"text": "x", "expect_focus": "abc", "password": "x"}),
    ("focus", {"query": ""}), ("key", {"key": "a" * 501, "expect_focus": "x"}),
    ("scroll", {"direction": "down", "amount": 0, "expect_focus": "x"}),
    ("drag", {"x1": 1, "y1": 1, "x2": 2, "y2": 3}),
    ("shot", {"display": "unconfigured"}), ("type", {"text": "x" * 10001, "expect_focus": "x"})])
def test_reject_unsafe_or_malformed_actions(action, args):
    with pytest.raises(device.Refusal):
        device.validate(action, args)


def test_unknown_node_never_spawns(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(fleet, "transport", mock)
    r = asyncio.run(fleet.control("mini; touch /tmp/nope", "shot"))
    assert r["code"] == "invalid_arguments"
    mock.assert_not_called()


def test_ssh_no_forwarding_and_arguments_are_not_shell_text():
    cmd, env = fleet.command("mini")
    assert "StrictHostKeyChecking=yes" in cmd and "ForwardAgent=no" in cmd
    assert "ClearAllForwardings=yes" in cmd and "BatchMode=yes" in cmd
    assert env is None and not any(x in cmd for x in ("-L", "-R", "-D", "-tt"))
    assert "device.py" in cmd[-1] and "workman.server" not in cmd[-1]


def test_local_endpoint_does_not_start_browser_bridge():
    cmd, env = fleet.command("dgx")
    assert cmd[-1].endswith("device.py")
    assert env["WORKMAN_SHOT_ARCHIVE"] == "0"
    assert env["WORKMAN_GTK_PYTHON"] == "/usr/bin/python3"


@pytest.mark.parametrize("action,perms", [("shot", {"screen_recording": False, "accessibility": True}),
                                       ("click", {"screen_recording": True, "accessibility": False}),
                                       ("type", {})])
def test_mac_permission_gate_before_action(action, perms):
    b = object.__new__(device.Backend)
    b.mac = True
    b.status = lambda: {"daemon_reachable": True, "permissions": perms}
    with pytest.raises(device.Refusal) as ex:
        b.gate(action)
    assert ex.value.code == "permission_required"


def test_missing_daemon_never_falls_back():
    b = object.__new__(device.Backend)
    b.mac = True
    b.status = lambda: {"daemon_reachable": False}
    with pytest.raises(device.Refusal) as ex:
        b.gate("shot")
    assert ex.value.code == "daemon_unavailable"


def test_changed_focus_refuses_before_typing():
    b = object.__new__(device.Backend)
    b.mac = False
    b.gate = lambda a: None
    b.active = lambda: {"focus_token": "fresh"}
    b.call = lambda *a, **k: pytest.fail("input must not run")
    with pytest.raises(device.Refusal) as ex:
        b.execute("type", {"text": "never", "expect_focus": "old"})
    assert ex.value.code == "focus_changed"


def test_literal_text_never_reaches_local_or_hub_log():
    secret = "test-only-sensitive-sentinel-$(`literal`)"
    class Fake:
        def __init__(self, kind): pass
        def execute(self, action, args):
            assert args["text"] == secret
            return {"typed_chars": len(secret)}
    r = device.run({"node": "dgx", "backend": "x11", "action": "type", "args": {"text": secret, "expect_focus": "x"}}, Fake)
    assert r["ok"] and r["local_recorded"]
    learning.receive("dgx", learning.read_events(learning.journal_path()))
    for path in [learning.journal_path(), *learning.hub_root().rglob("*.jsonl")]:
        assert secret not in path.read_text()
    assert r["event"]["typed_chars"] == len(secret)


def test_exception_message_cannot_leak_text():
    class Fake:
        def __init__(self, kind): raise RuntimeError("SENSITIVE_SENTINEL")
    r = device.run({"node": "dgx", "backend": "x11", "action": "shot"}, Fake)
    assert r["code"] == "backend_error" and "SENSITIVE_SENTINEL" not in json.dumps(r)


def test_event_whitelist_drops_every_unstructured_value():
    e = learning.clean_event(event(text="SECRET", title="SECRET", key="SECRET", error="SECRET",
                                   arbitrary={"value": "SECRET"}, typed_chars=6))
    assert "SECRET" not in json.dumps(e) and e["typed_chars"] == 6


def test_motion_outcome_is_sanitized_for_journal_and_graph():
    class Fake:
        def __init__(self, kind): pass
        def execute(self, action, args):
            return {"humanized": True, "steps": 31, "duration_s": 0.42,
                    "title": "PRIVATE-SENTINEL", "at": [args["x"], args["y"]]}
    result = device.run({"node": "dgx", "backend": "x11", "action": "move",
                         "args": {"x": 10, "y": 20, "motion": "human", "speed": 1.5}}, Fake)
    recorded = result["event"]
    assert {k: recorded[k] for k in ("motion", "speed_milli", "humanized", "motion_steps", "motion_duration_ms")} == {
        "motion": "human", "speed_milli": 1500, "humanized": True,
        "motion_steps": 31, "motion_duration_ms": 420}
    assert "PRIVATE-SENTINEL" not in learning.journal_path().read_text()
    learning.receive("dgx", [recorded])
    import memory_graph
    attrs = memory_graph.query("dgx", "action_outcome")["entities"][0]["attrs"]
    assert attrs["motion_steps"] == 31 and "PRIVATE-SENTINEL" not in json.dumps(attrs)


def test_motion_fields_are_bounded_and_bad_values_are_dropped():
    row = learning.clean_event(event(motion="human", speed_milli=1500, humanized=True,
                                     motion_steps=25, motion_duration_ms=500))
    assert row["motion"] == "human" and row["speed_milli"] == 1500 and row["humanized"] is True
    bad = learning.clean_event(event(motion="stealth", speed_milli=99999, humanized="yes",
                                     motion_steps=-1, motion_duration_ms=-1))
    assert not set(bad) & {"motion", "speed_milli", "humanized", "motion_steps", "motion_duration_ms"}


def test_existing_unrelated_journal_rows_never_export():
    p = learning.journal_path()
    p.parent.mkdir()
    p.write_text(json.dumps({"tool": "computer_type", "payload": {"text": "PRIVATE"}}) + "\ninvalid json\n")
    learning.append(p, [event()])
    assert learning.read_events(p) == [event()]
    assert "PRIVATE" in p.read_text()  # Historical data is preserved, not copied.


def test_large_journal_rotates_to_append_only_segments_without_losing_events(monkeypatch):
    monkeypatch.setattr(learning, "MAX_JOURNAL_BYTES", 1)
    path = learning.journal_path()
    learning.append(path, [event("a")])
    learning.append(path, [event("b")])
    assert [row["id"] for row in learning.read_events(path)] == ["a" * 32, "b" * 32]
    storage = learning.journal_storage(path)
    assert storage["files"] == 2 and storage["append_only_segments"] is True
    assert len(list(learning.segment_directory(path).glob("*.jsonl"))) == 2


def test_receipt_idempotence_and_node_identity():
    learning.receive("dgx", [event()])
    learning.receive("dgx", [event(), event()])
    assert len(learning.hub_events()) == 1
    with pytest.raises(ValueError):
        learning.receive("mini", [event()])


def test_same_event_id_cannot_rewrite_graph_evidence():
    learning.receive("dgx", [event()])
    with pytest.raises(ValueError):
        learning.receive("dgx", [event(code="backend_error")])
    import memory_graph
    graph = memory_graph.query("dgx", "action_outcome")
    assert graph["entities"][0]["attrs"]["code"] == "ok"


def test_verified_recovery_deduplicates_and_reuses_evidence():
    fail = event("a", action="type", code="focus_changed")
    success = event("b", action="type")
    shot = event("c", action="shot", capture_sha256="d" * 64)
    verify = event("d", action="verify", verifies=success["id"], evidence_id=shot["id"], verified=True)
    learning.append(learning.journal_path(), [fail, success, shot, verify])
    learning.receive("dgx", learning.read_events(learning.journal_path()))
    learning.receive("dgx", [verify])
    rows = fleet.recall("dgx")
    recovered = [r for r in rows if r["kind"] == "verified_type"]
    assert len(recovered) == 1 and recovered[0]["lesson"].startswith("Recovery verified")
    assert recovered[0]["evidence_ids"] == [r["id"] for r in [fail, success, shot, verify]]


@pytest.mark.parametrize("old_shot,wrong_node", [(True, False), (False, True)])
def test_verification_requires_later_same_device_capture(old_shot, wrong_node):
    action = event("a")
    shot = event("b", node="mini" if wrong_node else "dgx", action="shot", capture_sha256="c" * 64)
    learning.append(learning.journal_path(), [shot, action] if old_shot else [action, shot])
    r = device.run({"node": "dgx", "action": "verify", "args": {"event_id": action["id"], "evidence_id": shot["id"]}})
    assert not r["ok"] and not fleet.recall()


def test_backend_success_without_visual_proof_is_not_a_success_lesson():
    learning.receive("dgx", [event()])
    assert fleet.recall() == []


def test_verified_motion_lesson_retains_only_bounded_profile_evidence():
    action = event("a", action="move", motion="human", speed_milli=1500,
                   humanized=True, motion_steps=24, motion_duration_ms=390)
    shot = event("b", action="shot", capture_sha256="d" * 64)
    proof = event("c", action="verify", verifies=action["id"], evidence_id=shot["id"], verified=True)
    learning.receive("dgx", [action, shot, proof])
    lesson = next(item for item in fleet.recall("dgx") if item["kind"] == "verified_move")
    assert lesson["motion_profile"] == {"motion": "human", "speed_milli": 1500,
                                         "humanized": True, "motion_steps": 24,
                                         "motion_duration_ms": 390}


def test_retract_mistaken_verification_preserves_correction_evidence():
    action = event("a", action="type")
    shot = event("b", action="shot", capture_sha256="d" * 64)
    verify = event("c", action="verify", verifies=action["id"], evidence_id=shot["id"], verified=True)
    learning.append(learning.journal_path(), [action, shot, verify])
    learning.receive("dgx", [action, shot, verify])
    r = device.run({"node": "dgx", "action": "correct", "args": {"verification_id": verify["id"]}})
    assert r["ok"]
    learning.receive("dgx", [r["event"]])
    assert fleet.recall() == []
    import memory_graph
    graph = memory_graph.query("dgx", "retracted_lesson")
    assert graph["entities"][0]["attrs"]["status"] == "retracted"
    assert any(x["predicate"] == "corrected_by" for x in graph["relations"])
    assert len(learning.hub_events()) == 4


def test_failed_reporting_never_retries_input(monkeypatch):
    control = AsyncMock(return_value={"ok": True, "code": "ok", "local_recorded": True})
    monkeypatch.setattr(fleet, "transport", control)
    monkeypatch.setattr(fleet, "collect", AsyncMock(side_effect=OSError()))
    r = asyncio.run(fleet.control("dgx", "type", {"text": "x", "expect_focus": "x"}))
    assert r["ok"] and not r["reporting"]["ok"]
    assert control.await_count == 1


def test_timeout_is_uncertain_and_never_retried(monkeypatch):
    class Proc:
        returncode = None
        killed = False
        async def communicate(self, data):
            await asyncio.sleep(1)
        def kill(self): self.killed = True
        async def wait(self): self.returncode = -9
    proc = Proc()
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    r = asyncio.run(fleet.transport("dgx", "type", {"text": "test"}, timeout=0.01))
    assert r == {"ok": False, "code": "timeout", "uncertain": True}
    assert proc.killed and spawn.await_count == 1


def test_report_uses_existing_reporter_workman_only_mode():
    cmd, _ = fleet.command("mini", report=True)
    assert "fleet-report.py --workman-json" in cmd[-1]


def test_device_report_handles_deduplication_cursor():
    rows = [event("a"), event("b")]
    learning.append(learning.journal_path(), rows)
    r = device.run({"node": "dgx", "action": "report", "args": {"after": rows[0]["id"]}})
    assert r["events"] == [rows[1]]


def test_x11_coordinates_are_not_scaled_twice():
    b = object.__new__(device.Backend)
    b.mac = False
    b.gate = lambda _: None
    b.call = lambda name, **args: {"name": name, **args}
    assert b.execute("move", {"x": 192, "y": 108}) == {"name": "move", "x": 192, "y": 108}


def test_mac_drag_translation_preserves_negative_origins():
    b = object.__new__(device.Backend)
    b.mac = True
    b.gate = lambda _: None
    b.active = lambda: {"focus_token": "x"}
    b.call = lambda name, **args: args
    args = dict(x1=-200, y1=5, x2=-100, y2=5, expect_focus="x")
    assert b.execute("drag", args) == {k: v for k, v in args.items() if k != "expect_focus"}


def test_first_party_fast_paste_requires_explicit_scope_and_retains_no_text():
    args={'text':'# Local fixture\n\n- First line\n- Second line','expect_focus':'current','preset':'first_party_fast','surface':'local_fixture'}
    with pytest.raises(device.Refusal):device.validate('paste',dict(args,surface='any_website'))
    b=object.__new__(device.Backend);b.mac=True;b.gate=lambda a:None;b.active=lambda:{'focus_token':'current'}
    calls=[];b.call=lambda *a,**k:calls.append((a,k)) or {}
    r=b.execute('paste',args)
    assert calls==[(('paste_text',),{'text':args['text']})]
    assert r['exact_readback_required'] and not r['verified']
    assert args['text'] not in json.dumps(r)
    import input_switch
    input_switch.update(keyboard=False)
    with pytest.raises(input_switch.Disabled):b.execute('paste',args)
    assert len(calls)==1


def test_curated_lesson_requires_verified_evidence_and_retracts_with_it():
    rows=[event('a',action='type'),event('b',action='shot',capture_sha256='e'*64),event('c',action='verify',verified=True,verifies='a'*32,evidence_id='b'*32)]
    learning.append(learning.journal_path(),rows);learning.receive('dgx',rows)
    r=device.run({'node':'dgx','action':'lesson','args':{'kind':'macos-fixture-edit-menu','verification_id':'c'*32}})
    assert r['ok'];learning.receive('dgx',[r['event']])
    assert any('Edit menu' in x['lesson'] for x in fleet.recall('dgx'))
    correction=device.run({'node':'dgx','action':'correct','args':{'verification_id':'c'*32}})
    learning.receive('dgx',[correction['event']])
    assert fleet.recall('dgx')==[]
    import memory_graph
    assert not memory_graph.query('dgx','verified_lesson')['entities']


def test_batched_inspection_is_read_only_and_does_not_capture_by_default():
    b=object.__new__(device.Backend);b.mac=True;b.status=lambda:{'permissions':{'accessibility':True,'screen_recording':True}}
    b.active=lambda:{'focus_token':'fresh'};calls=[]
    b.call=lambda name,**args:calls.append(name) or {'x':1,'y':2}
    result=b.execute('inspect',{})
    assert calls==['cursor_pos'] and result['active']['focus_token']=='fresh' and 'capture' not in result


def test_wait_window_timeout_does_not_mean_page_loaded():
    b=object.__new__(device.Backend);b.mac=True;b.gate=lambda a:None;b.call=lambda *a,**k:[]
    result=b.execute('wait_window',{'query':'test fixture','timeout_seconds':0})
    assert result=={'matched':False,'polls':1,'timed_out':True}


def test_stop_does_not_wait_for_memory_or_reporting(monkeypatch):
    monkeypatch.setattr(fleet,'recall',lambda *a:pytest.fail('STOP must not wait for recall'))
    monkeypatch.setattr(fleet,'collect',AsyncMock(side_effect=AssertionError('STOP must not wait for reporting')))
    monkeypatch.setattr(fleet,'transport',AsyncMock(return_value={'ok':True,'code':'ok','local_recorded':True}))
    result=asyncio.run(fleet.control('mini','input',{'keyboard':False}))
    assert result['ok'] and result['reporting']=={'ok':None,'pending':True,'state':'deferred_for_stop'}
