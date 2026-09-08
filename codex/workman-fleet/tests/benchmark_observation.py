"""Local payload projection benchmark. Synthetic inventory, no desktop/model calls.

Bytes are not provider tokens. No target percentage is assumed or enforced.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import observation


def fixture(count):
    return {'ok': True, 'code': 'ok', 'node': 'air', 'action': 'inspect',
        'local_recorded': True, 'reporting': {'ok': True, 'received': 1},
        'event': {'id': 'a'*32, 'ts': '2026-09-08T01:00:00Z', 'node': 'air',
                  'action': 'inspect', 'ok': True, 'code': 'ok', 'duration_ms': 500},
        'recalled_lessons': ['verified_click', 'verified_type', 'verified_focus'],
        'recalled_memory': ['lesson:air:verified_'+k for k in ('click','type','focus')],
        'data': {'observed_at': '2026-09-08T01:00:00Z', 'reads_only': True,
            'permissions': {'screen_recording': True, 'accessibility': True},
            'status': {'ready': True, 'daemon_reachable': True, 'backend': 'app',
                'permissions': {'screen_recording': True, 'accessibility': True, 'all_granted': True},
                'input_switches': {'mouse': True, 'keyboard': True}},
            'lease': {'available': True, 'owned_by_this_session': True, 'remaining_seconds': 90},
            'active': {'app': 'Local Fixture', 'pid': 10, 'bundle': 'local.workman.fixture', 'focus_token': 'b'*24,
                'identity_source': 'fresh_workman_adapter',
                'windows': [{'id': i+1, 'pid': 10, 'title': ('Goal draft ' if i<2 else 'Unrelated draft ')+str(i),
                             'app': 'Local Fixture', 'x': 20, 'y': 20, 'w': 600, 'h': 400} for i in range(count)]},
            'pointer': {'x': 100, 'y': 100}}}


def size(value): return len(json.dumps(value, separators=(',', ':')).encode())


def run():
    rows=[]
    for count in (1,5,20,100):
        original=fixture(count);key=observation.put(original,'air','Goal',None)
        item=observation._originals[key];samples=[]
        for _ in range(100):
            start=time.perf_counter_ns();compact=observation.project(item,key)
            samples.append((time.perf_counter_ns()-start)/1e6)
        a,b=size(original),size(compact)
        assert compact['candidates']['count']==min(count,2)
        assert compact['field_revision'] is None and compact['load_state'] is None
        assert compact['input_switches']==original['data']['status']['input_switches']
        rows.append({'synthetic_windows':count,'candidate_count':compact['candidates']['count'],
            'ambiguity':compact['candidates']['state'],'full_json_bytes':a,'compact_json_bytes':b,
            'byte_reduction_percent':(1-b/a)*100,'projection_median_ms':statistics.median(samples)})
    return {'scope':'Synthetic active-app window inventories; JSON payload only; no desktop or provider calls.',
            'trials':rows,'provider_total_tokens':None,'cost':None,
            'limitation':'Total-token target is unproven. Small observations may grow. Exact readback and global visual checks remain mandatory.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=run();a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
