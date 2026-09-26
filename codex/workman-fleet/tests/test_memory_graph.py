import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import learning
import memory_graph


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKMAN_FLEET_LEARN_ROOT", str(tmp_path / "hub"))


def e(id, at="2026-09-07T12:00:00Z", **kwargs):
    return dict(id=id * 32, node="mini", action="status", code="ok", ok=True,
                ts=at, project="workman", task="fleet-v1.1", session="test-session", **kwargs)


def test_graph_has_all_context_links_and_stable_ids():
    event = e("a", permissions={"screen_recording": True})
    memory_graph.ingest([event, event], [])
    r = memory_graph.query()
    kinds = {n["kind"] for n in r["entities"]}
    assert {"device", "project", "session", "task", "decision", "permission_state", "action_outcome"} <= kinds
    assert len([n for n in r["entities"] if n["kind"] == "action_outcome"]) == 1
    assert {x["predicate"] for x in r["relations"]} >= {"performed_on", "for_project", "during_session", "for_task", "evidenced_by", "governed_by"}


def test_new_permission_corrects_old_without_destroying_evidence():
    before = e("a", permissions={"accessibility": False})
    after = e("b", "2026-09-07T12:01:00Z", permissions={"accessibility": True})
    memory_graph.ingest([after, before], [])  # delayed older delivery must not win
    at = datetime(2026, 9, 7, 12, 2, tzinfo=timezone.utc).timestamp()
    r = memory_graph.query("mini", at=at)
    assert r["permissions"][0]["value"] is True
    assert any(x["predicate"] == "supersedes" and x["subject"].startswith("permission:" + "b" * 32) for x in r["relations"])
    assert len([n for n in r["entities"] if n["kind"] == "permission_state"]) == 2


def test_equal_time_conflict_returns_no_usable_permission():
    memory_graph.ingest([e("a", permissions={"accessibility": False}), e("b", permissions={"accessibility": True})], [])
    r = memory_graph.query("mini", at=datetime(2026, 9, 7, 12, 0, 1, tzinfo=timezone.utc).timestamp())
    assert r["permissions"][0]["status"] == "conflict"
    assert r["permissions"][0]["value"] is None
    assert any(x["predicate"] == "conflicts_with" for x in r["relations"])


def test_stale_permission_returns_unknown_and_demands_live_recheck():
    memory_graph.ingest([e("a", permissions={"accessibility": True})], [])
    r = memory_graph.query("mini", at=datetime(2026, 9, 7, 12, 6, tzinfo=timezone.utc).timestamp())
    assert r["permissions"][0]["status"] == "stale"
    assert r["permissions"][0]["value"] is None


def test_independent_process_retrieves_verified_lesson_graph():
    action = e("a"); action["action"] = "click"
    shot = e("b", capture_sha256="f" * 64); shot["action"] = "shot"
    proof = e("c", verifies=action["id"], evidence_id=shot["id"], verified=True); proof["action"] = "verify"
    learning.receive("mini", [action, shot, proof])
    p = subprocess.run([sys.executable, "-c", "import memory_graph,json;print(json.dumps(memory_graph.query('mini','verified_lesson')))"],
                       env=dict(os.environ, PYTHONPATH=str(Path(memory_graph.__file__).parent)), capture_output=True, text=True)
    assert p.returncode == 0
    r = json.loads(p.stdout)
    assert r["entities"][0]["id"] == "lesson:mini:verified_click"
    assert {x["object"] for x in r["relations"] if x["predicate"] == "evidenced_by"} == {"event:" + e["id"] for e in [action, shot, proof]}


@pytest.mark.parametrize("kwargs", [{"node": "../../etc"}, {"kind": "arbitrary_sql"}, {"limit": 0}, {"limit": 201}])
def test_graph_query_bounds(kwargs):
    with pytest.raises(ValueError): memory_graph.query(**kwargs)


def test_graph_cannot_store_unstructured_sensitive_data():
    memory_graph.ingest([e("a", text="PRIVATE-SENTINEL", title="PRIVATE-SENTINEL", error="PRIVATE-SENTINEL")], [])
    assert "PRIVATE-SENTINEL" not in json.dumps(memory_graph.query())
