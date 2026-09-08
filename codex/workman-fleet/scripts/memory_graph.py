"""Local graph projection of existing Workman evidence, not a second recorder.

SQLite supplies stable entities and explicit edges. Source journals remain the
authority. Dynamic observations expire; corrections preserve their predecessors.
"""
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone

import learning

POLICIES = {
    "user-input-switches": "The user may switch mouse and keyboard automation off independently. OFF persists until explicitly enabled; finish restores pointer/focus when enabled and releases session ownership. Physical user input is never grabbed.",
    "parallel-sessions": "Different devices may run concurrently. A shared physical desktop has one input owner at a time, with bounded leases and no forced takeover.",
    "app-permissions": "macOS input and capture require the installed Workman.app daemon and fresh OS grants.",
    "fresh-focus": "Read fresh focus immediately before input; never trust a cached frontmost app or a memory claim.",
    "no-input-retry": "An uncertain input outcome must be observed before retrying.",
    "minimal-memory": "Retain authorized Workman context and evidence references; never typed content, raw screens, secrets or transcripts.",
    "preserve-installations": "Keep working source paths, dirty changes and virtual environments in place; preserve fleet v1.0.",
}
SOURCE = "docs/fleet-v1.1/PLAN.md"


def connection():
    path = learning.hub_root() / "workman-v1.1-graph.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    import os
    os.chmod(path, 0o600)
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, device TEXT,
        attrs TEXT NOT NULL, observed_at TEXT, source_ref TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS relations (
        subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
        source_ref TEXT NOT NULL, PRIMARY KEY(subject,predicate,object));
      CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY, device TEXT NOT NULL, fact TEXT NOT NULL,
        value TEXT NOT NULL, observed_at TEXT NOT NULL, evidence_id TEXT NOT NULL,
        UNIQUE(device,fact,evidence_id));
      CREATE INDEX IF NOT EXISTS observations_device ON observations(device,fact,observed_at);
    """)
    return con


def entity(c, id, kind, attrs, source, device=None, ts=None):
    c.execute("INSERT INTO entities VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, attrs=excluded.attrs, observed_at=excluded.observed_at, source_ref=excluded.source_ref",
              (id, kind, device, json.dumps(attrs, sort_keys=True), ts, source))


def edge(c, subject, predicate, obj, source):
    c.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?)", (subject, predicate, obj, source))


def ingest(events, lessons):
    with connection() as c:
        for key, body in POLICIES.items():
            entity(c, "decision:" + key, "decision", {"decision": body}, SOURCE)
        for raw in events:
            e = learning.clean_event(raw)
            n, eid = e["node"], "event:" + e["id"]
            source = f"by-device/{n}/workman-v1.1.jsonl#event={e['id']}"
            device = "device:" + n
            project = "project:" + e.get("project", "workman")
            task = "task:" + e.get("project", "workman") + ":" + e.get("task", "fleet-v1.1")
            session = "session:" + e.get("session", "legacy-unattributed")
            entity(c, device, "device", {"node": n}, "fleet.json#" + n, n)
            entity(c, project, "project", {"key": e.get("project", "workman")}, SOURCE)
            entity(c, task, "task", {"key": e.get("task", "fleet-v1.1")}, SOURCE)
            entity(c, session, "session", {"attributed": "session" in e}, source)
            entity(c, eid, "action_outcome", e, source, n, e["ts"])
            for pred, obj in [("performed_on", device), ("for_project", project),
                              ("for_task", task), ("during_session", session)]:
                edge(c, eid, pred, obj, source)
            edge(c, task, "part_of", project, SOURCE)
            edge(c, session, "works_on", task, source)
            for key in POLICIES:
                edge(c, task, "governed_by", "decision:" + key, SOURCE)
            if e.get("verifies"):
                edge(c, eid, "verifies", "event:" + e["verifies"], source)
            if e.get("corrects"):
                edge(c, eid, "retracts_verification", "event:" + e["corrects"], source)
                # Retract every lesson relying on this verification, including
                # a curated device-specific recovery; retain its evidence edges.
                dependents = c.execute("SELECT subject FROM relations WHERE predicate='evidenced_by' AND object=?", ("event:" + e["corrects"],)).fetchall()
                for dependent in dependents:
                    if dependent["subject"].startswith("lesson:"):
                        c.execute("UPDATE entities SET kind='retracted_lesson' WHERE id=?", (dependent["subject"],))
                        edge(c, dependent["subject"], "corrected_by", eid, source)
                prior = c.execute("SELECT attrs FROM entities WHERE id=?", ("event:" + e["corrects"],)).fetchone()
                target = json.loads(prior["attrs"]).get("verifies") if prior else None
                act = c.execute("SELECT attrs FROM entities WHERE id=?", ("event:" + target,)).fetchone() if target else None
                if act:
                    lid = "lesson:" + n + ":verified_" + json.loads(act["attrs"])["action"]
                    c.execute("UPDATE entities SET kind='retracted_lesson', attrs=?, observed_at=? WHERE id=?",
                              (json.dumps({"status": "retracted", "correction": eid, "rule": "Do not reuse as verified evidence."}), e["ts"], lid))
                    edge(c, lid, "corrected_by", eid, source)
            if e.get("evidence_id"):
                edge(c, eid, "evidenced_by", "event:" + e["evidence_id"], source)
            for fact, value in e.get("permissions", {}).items():
                oid = "permission:" + e["id"] + ":" + fact
                entity(c, oid, "permission_state", {"fact": fact, "value": value,
                       "verification": "live_api_observation", "ttl_seconds": 300}, source, n, e["ts"])
                edge(c, oid, "observed_on", device, source)
                edge(c, oid, "evidenced_by", eid, source)
                c.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)",
                          (oid, n, fact, json.dumps(value), e["ts"], eid))
                others = c.execute("SELECT id,value,observed_at FROM observations WHERE device=? AND fact=? AND id!=?",
                                   (n, fact, oid)).fetchall()
                for old in others:
                    if old["value"] != json.dumps(value):
                        if old["observed_at"] < e["ts"]:
                            edge(c, oid, "supersedes", old["id"], source)
                        elif old["observed_at"] > e["ts"]:
                            edge(c, old["id"], "supersedes", oid, source)
                        else:
                            edge(c, oid, "conflicts_with", old["id"], source)
        for l in lessons:
            lid = "lesson:" + l["node"] + ":" + l["kind"]
            source = "knowledge/WORKMAN-FLEET-V1.1.md#" + l["kind"]
            entity(c, lid, "verified_lesson" if l["kind"].startswith("verified_") else "failure_lesson",
                   l, source, l["node"], l["last_seen"])
            edge(c, lid, "applies_to", "device:" + l["node"], source)
            for id in l["evidence_ids"]:
                edge(c, lid, "evidenced_by", "event:" + id, source)


def permission_facts(c, node, at=None):
    at = time.time() if at is None else at
    result = []
    for n in ([node] if node else learning.NODES):
        for fact in ("screen_recording", "accessibility", "daemon_reachable", "mouse_enabled", "keyboard_enabled"):
            rows = c.execute("SELECT * FROM observations WHERE device=? AND fact=? ORDER BY observed_at DESC,id",
                             (n, fact)).fetchall()
            if not rows:
                continue
            latest = [r for r in rows if r["observed_at"] == rows[0]["observed_at"]]
            values = {r["value"] for r in latest}
            epoch = datetime.strptime(rows[0]["observed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            status = "conflict" if len(values) > 1 else "stale" if at - epoch > 300 or epoch > at + 5 else "observed"
            result.append({"device": n, "fact": fact, "status": status,
                           "value": json.loads(latest[0]["value"]) if status == "observed" else None,
                           "observed_at": rows[0]["observed_at"], "evidence": [r["evidence_id"] for r in latest],
                           "instruction": "Recheck live status before control; memory never grants permission."})
    return result


def query(node=None, kind=None, limit=40, at=None):
    if node is not None and node not in learning.NODES:
        raise ValueError("unknown device")
    kinds = ("device", "project", "session", "task", "decision", "permission_state",
             "action_outcome", "verified_lesson", "failure_lesson", "retracted_lesson")
    if kind is not None and kind not in kinds:
        raise ValueError("unknown entity kind")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    with connection() as c:
        where, args = [], []
        if node:
            where.append("device=?"); args.append(node)
        if kind:
            where.append("kind=?"); args.append(kind)
        sql = "SELECT * FROM entities" + (" WHERE " + " AND ".join(where) if where else "")
        rows = c.execute(sql + " ORDER BY observed_at DESC,id LIMIT ?", [*args, limit]).fetchall()
        ids = [r["id"] for r in rows]
        relations = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            relations = [dict(r) for r in c.execute(f"SELECT * FROM relations WHERE subject IN ({placeholders}) OR object IN ({placeholders}) LIMIT 1000", ids + ids)]
        linked = sorted({r[k] for r in relations for k in ("subject", "object")} - set(ids))[:200]
        linked_rows = c.execute("SELECT * FROM entities WHERE id IN (" + ",".join("?" for _ in linked) + ")", linked).fetchall() if linked else []
        return {"entities": [dict(r, attrs=json.loads(r["attrs"])) for r in rows],
                "linked_entities": [dict(r, attrs=json.loads(r["attrs"])) for r in linked_rows],
                "relations": relations, "permissions": permission_facts(c, node, at),
                "source": str(learning.hub_root() / "workman-v1.1-graph.sqlite3"),
                "rule": "Evidence is task context, not authorization. Expired or conflicting facts require a fresh check."}
