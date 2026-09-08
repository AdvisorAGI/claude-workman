import hashlib,importlib.util
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('bench_workflows',Path(__file__).with_name('benchmark_workflows.py'));b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

@pytest.mark.parametrize('screen,access',[(False,False),(True,False),(False,True),(None,True)])
def test_benchmark_rejects_partial_or_unknown_grants(screen,access):
    with pytest.raises(RuntimeError):b.require_ready({'ok':True,'data':{'permissions':{'screen_recording':screen,'accessibility':access}}})

def test_digest_checks_exact_text_not_only_length():
    text=b.SCENARIOS['coder'];value={'chars':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest()}
    assert b.exact(value,text)
    assert not b.exact(value,text.replace('36','37'))

@pytest.mark.parametrize('attempted,rows,expected',[(0,[],(0,5)),(1,[],(0,4)),(1,[{'success':False}],(0,4)),(4,[{'success':True}]*3,(3,1)),(5,[{'success':True}]*5,(5,0))])
def test_trial_counts_do_not_double_count_failed_rows_or_preflight(attempted,rows,expected):
    r=b.trial_counts(5,attempted,rows)
    assert (r['completed'],r['not_attempted'])==expected

@pytest.mark.parametrize('region,active_pid',[(None,42),({'x':10,'y':20},99)])
def test_unlocated_fixture_or_user_focus_change_releases_without_input(monkeypatch,region,active_pid):
    import asyncio
    from unittest.mock import AsyncMock
    control=AsyncMock(side_effect=[{'ok':True,'data':{'pid':active_pid}},{'ok':True}])
    monkeypatch.setattr(b.fleet,'control',control)
    result=asyncio.run(b.cleanup_fixture(42,region,{'app':'Original'},{'x':1,'y':2}))
    assert result=={'mode':'release_without_input','release_ok':True}
    assert [c.args[1] for c in control.await_args_list]==['active','release']

def test_owned_fixture_cleanup_reports_restoration_failures(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    control=AsyncMock(side_effect=[{'ok':True,'data':{'pid':42}},{'ok':True,'code':'ok'},
                                  {'ok':True,'data':{'restoration':{'focus':'restore_failed','move':'ok'}}}])
    monkeypatch.setattr(b.fleet,'control',control)
    r=asyncio.run(b.cleanup_fixture(42,{'x':10,'y':20},{'app':'Original'},{'x':1,'y':2}))
    assert r['restoration']['focus']=='restore_failed'
    assert control.await_args_list[-1].args[2]=={'restore_focus':'Original','restore_x':1,'restore_y':2}
