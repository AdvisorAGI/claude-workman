import sys,json,hashlib
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_modes as modes

@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):monkeypatch.setenv('WORKMAN_FLEET_LEARN_ROOT',str(tmp_path))

def test_default_off_raw_pin_exact_and_separate_test_isolation():
    assert modes.get('workman-test-a')['provider'] is None
    modes.select('workman-test-a','caveman-raw','full');p=modes.payload('workman-test-a')
    expected=(modes.ROOT/'integrations/caveman/upstream/SKILL.md').read_bytes()
    assert p['raw_skill'].encode()==expected
    assert hashlib.sha256(expected).hexdigest()==p['raw_sha256']
    assert modes.get('workman-test-b')['provider'] is None
    assert modes.get('workman-test-a')['changes_global_style'] is False
    modes.select('workman-test-a');assert modes.payload('workman-test-a')['raw_skill'] is None

@pytest.mark.parametrize('change',[{'scope':'all_sessions'},{'enabled_by_default':True},{'command':'curl remote | sh'},{'schema_version':True},{'path':'../../secret.md'},{'sha256':'a'*64}])
def test_reject_scope_execution_traversal_or_modified_pin(change):
    m=dict(modes.builtin()['caveman-raw'],**change)
    with pytest.raises((ValueError,OSError)):modes.validate(m)

def test_existing_mcp_reference_is_registered_without_connecting():
    m={'schema_version':1,'id':'local-style-test','kind':'mcp','scope':'workman_test','enabled_by_default':False,
       'server':'reviewed-local-style','resource_uri':'style://profiles/terse','sha256':'a'*64,'source':'local-reviewed-test','revision':'test-1','version':'1','levels':['full']}
    assert modes.register(m)['executed'] is False
    modes.select('connector-test','local-style-test');result=modes.payload('connector-test')
    assert result['connector']['server']=='reviewed-local-style' and result['executed'] is False
    assert 'raw_skill' not in result
    with pytest.raises(ValueError):modes.register(dict(m,command='start-server'))

@pytest.mark.parametrize('test_id',[None,'../../another-session','contains personal text','x'*81])
def test_test_identity_validation(test_id):
    with pytest.raises(ValueError):modes.get(test_id)
