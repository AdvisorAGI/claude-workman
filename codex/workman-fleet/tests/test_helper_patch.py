import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import patch_helper as p
import patch_ops as ops


def test_only_known_loop_is_patched_and_repeat_is_idempotent():
    original = p.BEFORE + "                pass\n" + p.BEFORE_REPLY
    updated = p.patched(original)
    assert updated.count(p.MARKER) == 1
    assert "reply = _handle(line)" in updated and "conn.sendall(reply)" in updated
    assert p.patched(updated) == updated


def test_unknown_helper_version_requires_inspection():
    with pytest.raises(ValueError): p.patched("a different daemon")


def test_ops_patch_is_idempotent_and_requires_known_source():
    text = ops.FOCUS + ops.DRAG + ops.KEY_FLAGS + ops.UNICODE + ops.KEY_PAIR + '# --------------------------------------------------------------------------- dispatch\n    "request_permissions": request_permissions,'
    new = ops.patched(text)
    assert '"AXFrontmost"' in new and "finally:" in new
    assert ops.patched(new) == new
    with pytest.raises(ValueError): ops.patched("unknown")


def test_key_patch_clears_flags_and_releases_on_dwell_failure():
    from types import SimpleNamespace
    calls = []
    def fail(_): raise RuntimeError("dwell failure")
    body = "def press():\n    keycode, flags = 0, 1048576\n" + ops.KEY_PAIR_NEW
    ns = {"_post_key": lambda *a: calls.append(a), "time": SimpleNamespace(sleep=fail), "_human": lambda: False}
    exec(body, ns)
    with pytest.raises(RuntimeError): ns["press"]()
    assert calls == [(0, True, 1048576), (0, False, 0)]
