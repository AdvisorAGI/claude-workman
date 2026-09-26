"""Opt-in compact view of one real inspection, never an authorization shortcut.

Original access expires in 30 seconds; eviction happens on requests or process
exit. Storage is bounded process memory. No screen pixels,
field values, transcript scan, model call or second persistent memory store.
"""
import copy
import json
import re
import time
import uuid
from collections import OrderedDict

import fleet
import learning

TTL = 30
MAX_BYTES = 262144
MAX_ITEMS = 8
_originals = OrderedDict()


def receipt(node, event_id):
    if event_id is None:
        return None
    if not learning.HEX.fullmatch(str(event_id)):
        raise ValueError("invalid action receipt ID")
    rows = [e for e in learning.hub_events() if e['node'] == node]
    by_id = {e['id']: e for e in rows}
    target = by_id.get(event_id)
    if target is None:
        raise ValueError("action receipt is not present for this device")
    corrections = {e['corrects'] for e in rows if e.get('corrects')}
    verified = None
    for e in rows:
        shot = by_id.get(e.get('evidence_id'), {})
        if (e.get('ok') and e.get('verified') and e.get('verifies') == event_id and e['id'] not in corrections
                and event_id not in corrections and shot.get('id') not in corrections
                and target['ok'] and shot.get('ok') and shot.get('action') == 'shot' and shot.get('capture_sha256')
                and rows.index(target) < rows.index(shot) < rows.index(e)):
            verified = {'id': e['id'], 'capture': shot['id'], 'sha256': shot['capture_sha256'], 'at': e['ts']}
    return {'id': event_id, 'action': target['action'], 'ok': target['ok'], 'code': target['code'],
            'at': target['ts'], 'visual_verification': verified,
            'exact_readback': None}  # A screenshot confirmation is not an exact-text digest receipt.


def put(raw, node, query, event_id):
    now = time.monotonic()
    for key, item in list(_originals.items()):
        if now - item['created'] > TTL:
            del _originals[key]
    if len(json.dumps(raw).encode()) > MAX_BYTES:
        raise ValueError("inspection too large for bounded compact cache; use fleet_control inspect")
    key = uuid.uuid4().hex
    _originals[key] = {'created': now, 'node': node, 'query': query,
                       'receipt_id': event_id, 'raw': copy.deepcopy(raw)}
    while len(_originals) > MAX_ITEMS:
        _originals.popitem(last=False)
    return key


def mapping(value):
    return value if isinstance(value, dict) else {}


def needs_original(raw):
    """Only omit fields from the reviewed inspect schema; preserve new diagnostics."""
    data = mapping(raw.get('data')); status = mapping(data.get('status'))
    active = mapping(data.get('active')); perms = mapping(status.get('permissions'))
    checks = [
        (raw, {'ok','code','node','action','data','event','local_recorded','reporting','recalled_lessons','recalled_memory','uncertain','recording_warning','recovery'}),
        (raw.get('event'), {'id','node','action','code','ts','ok','duration_ms','project','task','session','permissions'}),
        (raw.get('reporting'), {'ok','code','received','pending','state'}),
        (raw.get('data'), {'status','permissions','observed_at','reads_only','active','pointer','lease'}),
        (data.get('status'), {'ready','daemon_reachable','backend','permissions','input_switches','platform'}),
        (data.get('lease'), {'available','owned_by_this_session','remaining_seconds','scope'}),
        (data.get('active'), {'ok','id','pid','name','app','bundle','windows','focus_token','identity_source','x','y','w','h'}),
        (data.get('pointer'), {'ok','x','y','screen','window'}),
        (status.get('input_switches'), {'mouse','keyboard'}),
        (status.get('permissions'), {'screen_recording','accessibility','all_granted','responsible_process','fix'}),
        (data.get('permissions'), {'screen_recording','accessibility','all_granted','responsible_process','fix'}),
        (perms.get('responsible_process'), {'bundle_id','name','pid','self_pid','executable'}),
        (perms.get('fix'), {'screen_recording','accessibility','grant_to'}),
    ]
    if raw.get('ok') is not True or raw.get('code') != 'ok' or status.get('ready') is False:
        return True
    windows = active.get('windows')
    if windows is not None:
        if not isinstance(windows,list): return True
        checks.extend((w, {'id','pid','title','name','app','x','y','w','h'}) for w in windows)
    return any(v is not None and (not isinstance(v,dict) or set(v)-keys) for v,keys in checks)


def project(item, observation_id, identity_limit=10):
    raw = item['raw']; data = mapping(raw.get('data')); status = mapping(data.get('status'))
    active = mapping(data.get('active')); perms = mapping(status.get('permissions'))
    windows = active.get('windows')
    if windows is None and 'id' in active:
        windows = [active]
    hits = None
    if isinstance(windows, list) and all(isinstance(w,dict) for w in windows):
        query = (item['query'] or '').casefold()
        hits = [w for w in windows if query in str(w.get('title', w.get('name', ''))).casefold()]
    candidates = {'kind': 'window', 'scope': 'active_application_only',
                  'count': len(hits) if hits is not None else None,
                  'state': 'unavailable' if hits is None else 'none' if not hits else 'unique' if len(hits) == 1 else 'ambiguous',
                  'identities': [{k: w[k] for k in ('id', 'pid') if k in w} for w in (hits or [])[:identity_limit]],
                  'truncated': hits is not None and len(hits) > identity_limit}
    compact = {'v': 1, 'observation_id': observation_id, 'device': item['node'],
        'observed_at': data.get('observed_at', mapping(raw.get('event')).get('ts')),
        'cache_age_ms': round((time.monotonic() - item['created']) * 1000),
        'result': {k: raw[k] for k in ('ok', 'code', 'uncertain', 'recording_warning', 'recovery') if k in raw},
        'permissions': {k: perms.get(k) for k in ('screen_recording', 'accessibility')},
        'daemon_reachable': status.get('daemon_reachable'),
        'platform': status.get('platform'),  # Preserve platform diagnostics without interpreting them.
        'input_switches': status.get('input_switches'), 'lease': data.get('lease'),
        'focus': {k: active[k] for k in ('focus_token', 'id', 'pid', 'app', 'bundle') if k in active},
        'pointer': data.get('pointer'), 'candidates': candidates,
        'field_revision': None, 'load_state': None,
        'receipt': receipt(item['node'], item['receipt_id']),
        'inspection_event': mapping(raw.get('event')).get('id'),
        'local_recorded': raw.get('local_recorded'), 'reporting': raw.get('reporting'),
        'required': 'Recheck live guards before input. Use independent global visual evidence; exact text needs readback. Null means unmeasured. Full original is available by observation_id for 30s.'}
    if needs_original(raw):
        compact['fallback_reason'] = 'Failure, unavailable readiness or unrecognized diagnostic schema; original retained in full.'
        compact['fallback_original'] = copy.deepcopy(raw)
    return compact


async def observe(node, query=None, receipt_id=None, view='compact', observation_id=None, identity_limit=10):
    node = fleet.ALIASES.get(node, node)
    if node not in learning.NODES or view not in ('compact', 'full') or type(identity_limit) is not int or not 1 <= identity_limit <= 50:
        raise ValueError('unknown device or observation view')
    if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 200):
        raise ValueError('use a short nonsecret window query')
    if observation_id is None:
        # Validate requested evidence before any remote call, including a read.
        receipt(node, receipt_id)
        raw = await fleet.control(node, 'inspect')
        observation_id = put(raw, node, query, receipt_id)
    elif query is not None or receipt_id is not None:
        raise ValueError('cached reads cannot change the observation query or receipt')
    if not re.fullmatch(r'[a-f0-9]{32}', str(observation_id)):
        raise ValueError('invalid observation ID')
    item = _originals.get(observation_id)
    if item is None or item['node'] != node or time.monotonic() - item['created'] > TTL:
        raise ValueError('observation expired, unavailable or belongs to another device; inspect again')
    if view == 'full':
        return {'v': 1, 'observation_id': observation_id, 'device': node,
                'cache_age_ms': round((time.monotonic() - item['created']) * 1000),
                'original': copy.deepcopy(item['raw']), 'receipt': receipt(node, item['receipt_id']),
                'rule': 'Historical inspection, not current authorization; recheck live guards before input.'}
    return project(item, observation_id, identity_limit)
