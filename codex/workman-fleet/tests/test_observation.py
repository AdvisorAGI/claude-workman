import asyncio
import copy
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device
import fleet
import learning
import observation


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKMAN_LEARN_ROOT', str(tmp_path / 'local'))
    monkeypatch.setenv('WORKMAN_FLEET_LEARN_ROOT', str(tmp_path / 'hub'))
    observation._originals.clear()


def raw():
    return {'ok': True, 'code': 'ok', 'local_recorded': True,
            'event': {'id': 'f' * 32, 'ts': '2026-09-08T01:00:00Z'},
            'reporting': {'ok': False, 'code': 'report_failed'},
            'data': {'observed_at': '2026-09-08T01:00:00Z',
                     'status': {'permissions': {'screen_recording': True, 'accessibility': False},
                                'daemon_reachable': True, 'input_switches': {'mouse': False, 'keyboard': True}},
                     'lease': {'available': False, 'owned_by_this_session': False, 'remaining_seconds': 80},
                     'active': {'app': 'Fixture', 'pid': 10, 'focus_token': 'fresh',
                                'windows': [{'id': 1, 'pid': 10, 'title': 'Draft one'},
                                            {'id': 2, 'pid': 10, 'title': 'Draft two'}]},
                     'pointer': {'x': 100, 'y': 50}}}


def test_compact_keeps_denied_off_busy_focus_ambiguity_and_reporting_failure(monkeypatch):
    call = AsyncMock(return_value=raw()); monkeypatch.setattr(fleet, 'control', call)
    r = asyncio.run(observation.observe('air', query='Draft'))
    assert r['permissions']['accessibility'] is False and r['input_switches']['mouse'] is False
    assert r['lease']['available'] is False and r['focus']['focus_token'] == 'fresh'
    assert r['candidates']['count'] == 2 and r['candidates']['state'] == 'ambiguous'
    assert not r['reporting']['ok'] and r['field_revision'] is None and r['load_state'] is None
    assert 'global visual evidence' in r['required']
    call.assert_awaited_once_with('air', 'inspect')


def test_full_original_uses_no_second_remote_call_and_cannot_mutate_cache(monkeypatch):
    original = raw(); call = AsyncMock(return_value=original); monkeypatch.setattr(fleet, 'control', call)
    async def run():
        r = await observation.observe('air', query='one')
        assert r['candidates']['state'] == 'unique'
        first = await observation.observe('air', view='full', observation_id=r['observation_id'])
        assert first['original'] == original
        first['original']['data']['active']['app'] = 'changed'
        second = await observation.observe('air', view='full', observation_id=r['observation_id'])
        assert second['original'] == original
    asyncio.run(run()); assert call.await_count == 1


@pytest.mark.parametrize('change', [{'node': 'unknown'}, {'query': ''}, {'query': 'x'*201}, {'view': 'omit_errors'}, {'receipt_id': 'invalid'}])
def test_invalid_request_refused_before_inspection(monkeypatch, change):
    call = AsyncMock(); monkeypatch.setattr(fleet, 'control', call)
    with pytest.raises(ValueError): asyncio.run(observation.observe(**dict({'node':'air'}, **change)))
    call.assert_not_called()


def test_missing_backend_information_stays_unknown(monkeypatch):
    monkeypatch.setattr(fleet, 'control', AsyncMock(return_value={'ok':False,'code':'timeout','uncertain':True}))
    r = asyncio.run(observation.observe('machome'))
    assert r['result']['uncertain'] is True
    assert r['permissions']['accessibility'] is None and r['input_switches'] is None
    assert r['lease'] is None and r['candidates']['count'] is None
    assert r['candidates']['state'] == 'unavailable' and r['receipt'] is None


def test_x11_inspection_preserves_platform_and_pointer_without_full_fallback(monkeypatch):
    original=raw();data=original['data']
    data['status']={'ready':True,'backend':'x11','platform':{'backend':'linux_x11','accessibility':'atspi'},'input_switches':{'mouse':True,'keyboard':False}}
    data['active']={'ok':True,'id':'123','name':'Local Fixture','pid':'456','x':'10','y':'20','w':'600','h':'400','focus_token':'fresh'}
    data['pointer']={'ok':True,'x':100,'y':50,'screen':'0','window':'123'}
    monkeypatch.setattr(fleet,'control',AsyncMock(return_value=original))
    r=asyncio.run(observation.observe('dgx',query='Fixture'))
    assert 'fallback_original' not in r
    assert r['platform']==data['status']['platform'] and r['pointer']==data['pointer']
    assert r['candidates']['count']==1 and r['input_switches']['keyboard'] is False


def test_candidate_limit_retains_full_ambiguity_and_original(monkeypatch):
    original = raw(); original['data']['active']['windows'] *= 10
    monkeypatch.setattr(fleet, 'control', AsyncMock(return_value=original))
    r = asyncio.run(observation.observe('air'))
    assert r['candidates']['count'] == 20 and r['candidates']['truncated'] is True
    assert len(r['candidates']['identities']) == 10 and r['candidates']['state'] == 'ambiguous'


def test_cache_expiry_device_boundary_and_bounded_eviction(monkeypatch):
    key = observation.put(raw(), 'air', None, None)
    with pytest.raises(ValueError): asyncio.run(observation.observe('mini', observation_id=key))
    observation._originals[key]['created'] -= 31
    with pytest.raises(ValueError): asyncio.run(observation.observe('air', observation_id=key))
    for _ in range(12): observation.put(raw(), 'air', None, None)
    assert len(observation._originals) == 8 and key not in observation._originals
    oversized = raw(); oversized['data']['ignored'] = 'x' * observation.MAX_BYTES
    with pytest.raises(ValueError): observation.put(oversized, 'air', None, None)


def event(letter, action, **kwargs):
    return dict(id=letter*32,node='air',action=action,ok=True,code='ok',ts='2026-09-08T01:00:00Z',**kwargs)


def test_verified_receipt_is_separate_from_exact_readback_and_rechecks_correction(monkeypatch):
    rows=[event('a','type'),event('b','shot',capture_sha256='c'*64),event('d','verify',verified=True,verifies='a'*32,evidence_id='b'*32)]
    monkeypatch.setattr(learning, 'hub_events', lambda: rows)
    monkeypatch.setattr(fleet, 'control', AsyncMock(return_value=raw()))
    r=asyncio.run(observation.observe('air',receipt_id='a'*32))
    assert r['receipt']['visual_verification']['id']=='d'*32
    assert r['receipt']['exact_readback'] is None
    rows.append(event('e','correct',corrects='d'*32))
    later=asyncio.run(observation.observe('air',view='full',observation_id=r['observation_id']))
    assert later['receipt']['visual_verification'] is None
    with pytest.raises(ValueError): observation.receipt('mini','a'*32)


def test_bad_order_or_failed_capture_never_counts_as_visual_verification(monkeypatch):
    target=event('a','click');shot=event('b','shot',capture_sha256='c'*64);verify=event('d','verify',verified=True,verifies='a'*32,evidence_id='b'*32)
    monkeypatch.setattr(learning,'hub_events',lambda:[shot,target,verify])
    assert observation.receipt('air','a'*32)['visual_verification'] is None
    shot['ok']=False
    monkeypatch.setattr(learning,'hub_events',lambda:[target,shot,verify])
    assert observation.receipt('air','a'*32)['visual_verification'] is None


@pytest.mark.parametrize('corrected',['a','b','d'])
def test_correction_of_action_capture_or_verification_invalidates_receipt(monkeypatch,corrected):
    rows=[event('a','click'),event('b','shot',capture_sha256='c'*64),event('d','verify',verified=True,verifies='a'*32,evidence_id='b'*32),event('e','correct',corrects=corrected*32)]
    monkeypatch.setattr(learning,'hub_events',lambda:rows)
    assert observation.receipt('air','a'*32)['visual_verification'] is None


@pytest.mark.parametrize('location',['root','data','status','window','failure'])
def test_unknown_fields_and_failure_diagnostics_are_preserved(monkeypatch,location):
    original=raw()
    target={'root':original,'data':original['data'],'status':original['data']['status'],'window':original['data']['active']['windows'][0]}.get(location,original)
    target['new_diagnostic']='Review ambiguity before clicking'
    if location=='failure':original.update(ok=False,code='backend_error',message='Known test diagnostic')
    monkeypatch.setattr(fleet,'control',AsyncMock(return_value=original))
    result=asyncio.run(observation.observe('air'))
    assert result['fallback_original']==original and result['fallback_reason']


@pytest.mark.parametrize('action',['move','click'])
def test_mac_direct_removes_only_redundant_cursor_rpc(action):
    b=object.__new__(device.Backend);b.mac=True;b.gate=lambda _:None;calls=[]
    b.call=lambda name,**a:calls.append((name,a)) or {'x':0,'y':0}
    b.execute(action,{'x':10,'y':20,'motion':'direct','speed':1})
    assert calls == [(action,dict(x=10,y=20,humanize=False,**({'button':'left'} if action=='click' else {})))]
    calls.clear();b.execute(action,{'x':10,'y':20,'motion':'human','speed':1})
    assert calls[0][0]=='cursor_pos' and calls[-1][1]['humanize'] is True


def test_inspect_reports_lease_without_acquiring_it():
    class Fake:
        def __init__(self, kind): pass
        def execute(self, action, args): return raw()['data']
    r=device.run({'node':'dgx','backend':'x11','action':'inspect','context':{'session':'test-observer'}},Fake)
    assert r['ok'] and r['data']['lease']['available'] is True
    assert r['data']['lease']['owned_by_this_session'] is False
    assert 'Draft' not in learning.journal_path().read_text()
