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
