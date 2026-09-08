import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lease
import learning


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path))


def test_other_session_cannot_interleave_input_or_release():
    with lease.hold("one", "click"): pass
    for action in ("type", "release", "reserve"):
        with pytest.raises(lease.Busy):
            with lease.hold("two", action): pytest.fail("must be busy")


def test_owner_can_continue_and_release():
    with lease.hold("one", "reserve"): pass
    with lease.hold("one", "type"):
        assert lease.status("one")["owned_by_this_session"]
    with lease.hold("one", "release"): pass
    with lease.hold("two", "move"): pass


def test_reads_do_not_take_ownership_or_block_other_sessions():
    with lease.hold("one", "reserve"): pass
    with lease.hold("two", "shot") as r:
        assert not r["available"]
    assert lease.status("one")["owned_by_this_session"]


def test_expired_lease_is_recoverable_without_forcing_owner():
    lease.path().write_text(json.dumps({"owner": "gone", "expires": 1}))
    with lease.hold("two", "click"):
        assert lease.status("two")["owned_by_this_session"]


def test_independent_process_honors_input_owner():
    with lease.hold("one", "reserve"): pass
    script = "import lease\ntry:\n with lease.hold('two','click'):pass\nexcept lease.Busy:raise SystemExit(42)"
    r = subprocess.run([sys.executable, "-c", script], env=dict(os.environ, PYTHONPATH=str(Path(lease.__file__).parent)))
    assert r.returncode == 42


def test_separate_device_journals_allow_parallel_owners(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "dgx"))
    with lease.hold("one", "reserve"): pass
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "mini"))
    with lease.hold("two", "reserve"):
        assert lease.status("two")["owned_by_this_session"]
