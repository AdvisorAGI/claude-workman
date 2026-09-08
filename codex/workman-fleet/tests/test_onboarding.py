import asyncio
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import onboarding as o


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch): monkeypatch.setenv("WORKMAN_FLEET_LEARN_ROOT", str(tmp_path))


def status(screen=False, access=False, ok=True):
    return {"ok":ok,"data":{"ready":screen and access,"permissions":{"screen_recording":screen,"accessibility":access}},"event":{"ts":"2026-09-07T23:00:00Z","id":"a"*32}}


def test_only_required_missing_permissions_prompt_once():
    calls=[]
    async def call(n,a,args=None):
        calls.append((a,args));return status(True,False) if a=="status" else {"ok":True}
    for _ in range(2):asyncio.run(o.run("air",intent="begin",control=call))
    assert [a for op,a in calls if op=="permission_settings"]==[{"grant":"accessibility"}]
    r=asyncio.run(o.run("air",features=["view"],intent="begin",control=call))
    assert r["ready"] and len(r["requirements"])==1


def test_denial_or_no_answer_has_bounded_backoff_and_no_false_success():
    ticks=[0];sleeps=[];prompts=[]
    async def sleep(s):sleeps.append(s);ticks[0]+=s
    async def call(n,a,args=None):
        if a=="permission_settings":prompts.append(args);return {"ok":True}
        return status()
    r=asyncio.run(o.run("air",intent="begin",timeout_seconds=15,control=call,sleeper=sleep,clock=lambda:ticks[0]))
    assert not r["ready"] and r["wait_timed_out"]
    assert sleeps==[1,2,4,8] and len(prompts)==1


def test_grant_then_relaunch_then_ready_continues_without_reprompt():
    rows=iter([status(False,False),status(True,False),status(ok=False),status(True,True)])
    prompts=[]
    async def call(n,a,args=None):
        if a=="permission_settings":prompts.append(args["grant"]);return {"ok":True}
        return next(rows)
    async def sleep(_):pass
    r=asyncio.run(o.run("air",intent="begin",timeout_seconds=60,control=call,sleeper=sleep))
    assert r["ready"] and prompts==["screen_recording","accessibility"]


def test_revocation_is_reported_and_inspection_does_not_prompt():
    async def granted(n,a,args=None):assert a=="status";return status(True,True)
    asyncio.run(o.run("air",control=granted))
    async def revoked(n,a,args=None):assert a=="status";return status(True,False)
    r=asyncio.run(o.run("air",control=revoked))
    assert r["state"]=="revoked" and r["revoked"]==["accessibility"]


def test_cancellation_does_not_change_input_switches_or_request_grants():
    r=asyncio.run(o.run("air",intent="cancel"))
    assert r["state"]=="cancelled" and r["input_changed"] is False


def test_transient_helper_unavailable_preserves_last_grant_for_revocation():
    async def granted(n,a,args=None): return status(True,True)
    asyncio.run(o.run("air",control=granted))
    async def unavailable(n,a,args=None): return {"ok":False,"code":"daemon_unavailable"}
    asyncio.run(o.run("air",control=unavailable))
    async def revoked(n,a,args=None): return status(True,False)
    r=asyncio.run(o.run("air",control=revoked))
    assert r["revoked"]==["accessibility"]
