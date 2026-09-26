"""Test-only style providers. Declarative registry; no executor or network client.

One provider per explicit test ID. No global/session prompt hooks, subprocesses,
model calls, token savings claims or changes to desktop authorization.
"""
import hashlib,json,re
from pathlib import Path
import learning

ROOT=Path(__file__).resolve().parents[1]
IDENT=re.compile(r'^[a-z0-9][a-z0-9.-]{0,79}$')
SHA=re.compile(r'^[a-f0-9]{64}$')
GUARDS='Test advice only. Higher-priority instructions, required visible progress, OS grants, task consent, STOP, leases, focus, exact text, errors and evidence remain authoritative. No model/effort/service-tier change. No file, input, network or provider execution is authorized by this mode.'


def root():return learning.hub_root()/'workman-test-modes'


def builtin():
    source=json.loads((ROOT/'integrations/caveman/PROVENANCE.json').read_text())
    return {'caveman-raw':{'schema_version':1,'id':'caveman-raw','kind':'skill','scope':'workman_test','enabled_by_default':False,
        'path':'integrations/caveman/upstream/SKILL.md','sha256':source['files']['SKILL.md']['sha256'],
        'revision':source['revision'],'source':source['repository'],'version':source['version'],
        'levels':['lite','full','ultra','wenyan-lite','wenyan-full','wenyan-ultra']}}


def validate(manifest):
    fields={'schema_version','id','kind','scope','enabled_by_default','path','sha256','revision','source','version','levels','server','resource_uri'}
    m=dict(manifest)
    if set(m)-fields or type(m.get('schema_version')) is not int or m['schema_version']!=1 or not IDENT.fullmatch(str(m.get('id',''))):raise ValueError('invalid provider schema')
    if m.get('kind') not in ('skill','plugin','mcp') or m.get('scope')!='workman_test' or m.get('enabled_by_default') is not False:raise ValueError('provider must be disabled, declarative and test-only')
    if not isinstance(m.get('levels'),list) or not 1<=len(m['levels'])<=12 or not all(IDENT.fullmatch(str(v)) for v in m['levels']):raise ValueError('invalid levels')
    if not SHA.fullmatch(str(m.get('sha256',''))):raise ValueError('pin the reviewed payload SHA-256')
    for k in ('source','revision','version'):
        if not isinstance(m.get(k),str) or not 1<=len(m[k])<=300 or '\n' in m[k]:raise ValueError('missing source provenance')
    if m['kind']=='mcp':
        if not IDENT.fullmatch(str(m.get('server',''))) or not re.fullmatch(r'[a-z][a-z0-9+.-]*://[A-Za-z0-9/._-]{1,200}',str(m.get('resource_uri',''))):raise ValueError('use a named, already configured MCP server and nonsecret resource URI')
        if 'path' in m:raise ValueError('MCP reference cannot contain a file path')
    else:
        path=m.get('path','')
        if not isinstance(path,str) or Path(path).is_absolute() or '..' in Path(path).parts:raise ValueError('provider file must be inside the reviewed package')
        resolved=(ROOT/path).resolve()
        if not resolved.is_relative_to(ROOT.resolve()) or resolved.suffix!='.md':raise ValueError('only package-contained Markdown providers are supported')
        raw=resolved.read_bytes()
        if len(raw)>65536 or hashlib.sha256(raw).hexdigest()!=m['sha256']:raise ValueError('provider source differs from reviewed pin')
    return m


def providers():
    out=builtin()
    directory=root()/'providers'
    for p in sorted(directory.glob('*.json')):
        m=json.loads(p.read_text())
        if m.get('id') in out:raise ValueError('built-in provider collision')
        out[m['id']]=m
    return {k:validate(v) for k,v in out.items()}


def register(manifest):
    m=validate(manifest)
    if m['id'] in builtin():raise ValueError('built-in providers are immutable; review a new version explicitly')
    p=root()/'providers'/(m['id']+'.json')
    with learning.lock(root()/'.registry.lock'):
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists() and json.loads(p.read_text())!=m:raise ValueError('provider ID already pinned; use a new versioned ID')
        p.write_text(json.dumps(m,indent=2));p.chmod(0o600)
    return {'registered':m['id'],'enabled':False,'scope':'workman_test','executed':False}


def state_path(test_id):
    if not IDENT.fullmatch(str(test_id)):raise ValueError('use an explicit nonsecret Workman test ID')
    return root()/'selections'/(test_id+'.json')


def select(test_id,provider=None,level='full'):
    p=state_path(test_id)
    if provider is None:state={'test_id':test_id,'provider':None,'scope':'workman_test'}
    else:
        m=providers().get(provider)
        if m is None or level not in m['levels']:raise ValueError('unknown provider or level')
        state={'test_id':test_id,'provider':provider,'level':level,'scope':'workman_test','sha256':m['sha256'],'revision':m['revision']}
    with learning.lock(p.with_suffix('.lock')):
        p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.new');tmp.write_text(json.dumps(state));tmp.chmod(0o600);tmp.replace(p)
    return dict(state,guard=GUARDS,changes_global_style=False)


def get(test_id):
    p=state_path(test_id)
    try:state=json.loads(p.read_text())
    except FileNotFoundError:state={'test_id':test_id,'provider':None,'scope':'workman_test'}
    return dict(state,guard=GUARDS,changes_global_style=False)


def payload(test_id):
    state=get(test_id)
    if state.get('provider') is None:return dict(state,raw_skill=None)
    m=providers()[state['provider']]
    if m['sha256']!=state.get('sha256'):raise ValueError('selected pin changed; re-review before use')
    if m['kind']=='mcp':
        return dict(state,connector={'server':m['server'],'resource_uri':m['resource_uri'],'sha256':m['sha256']},
                    executed=False,instruction='The host may read the already configured resource for this test only; verify its SHA-256 before use. Registration does not launch/connect a server.')
    raw=(ROOT/m['path']).read_bytes().decode('utf-8')
    return dict(state,raw_skill=raw,source=m['source'],raw_sha256=m['sha256'],bytes=len(raw.encode()),
                instruction='Use this unmodified source only in the isolated Workman test prompt, at the selected level. Do not install hooks or apply it to doctrine/current conversation.')
