"""Persistent task-scoped presentation policy for Workman observations.

The policy changes only returned observation detail. It never changes models,
desktop authorization, input switches, leases, evidence rules or raw storage.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

import learning


IDENT = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")
LEVELS = {
    "off": {"identity_limit": None, "description": "Return the full observation for automatic reads."},
    "low": {"identity_limit": 50, "description": "Compact routine observations and retain up to 50 candidate identities."},
    "medium": {"identity_limit": 25, "description": "Compact routine observations and retain up to 25 candidate identities."},
    "high": {"identity_limit": 10, "description": "Compact routine observations and retain up to 10 candidate identities."},
    "max": {"identity_limit": 5, "description": "Most compact safe view, with the full original recoverable for 30 seconds."},
}
HISTORY_SLOTS = 32


def root():
    return learning.hub_root() / "workman-observation-profiles"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_state(value, task_id):
    if not isinstance(value, dict) or value.get("task_id") != task_id:
        raise ValueError("observation policy identity mismatch")
    if value.get("level") not in LEVELS or value.get("last_enabled_level") not in set(LEVELS) - {"off"}:
        raise ValueError("invalid observation policy level")
    if type(value.get("revision")) is not int or value["revision"] < 0:
        raise ValueError("invalid observation policy revision")
    return value


class TaskPolicy:
    def __init__(self, task_id):
        if not IDENT.fullmatch(str(task_id)):
            raise ValueError("use an explicit nonsecret task ID")
        self.task_id = task_id
        self.directory = root() / task_id
        self.file = self.directory / "SETTING.json"
        self.lock_file = self.directory / ".lock"

    def default(self):
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "level": "off",
            "last_enabled_level": "high",
            "revision": 0,
            "updated_at": None,
            "supersedes_revision": None,
            "history_slots": HISTORY_SLOTS,
            "scope": "task_presentation_only",
            "shared_across_sessions": True,
            "model_behavior": "unchanged",
            "raw_observations_persisted": False,
        }

    def status(self):
        try:
            value = validate_state(json.loads(self.file.read_text()), self.task_id)
        except FileNotFoundError:
            value = self.default()
        return dict(value, profile=LEVELS[value["level"]])

    def _atomic_write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".setting-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def set(self, level):
        with learning.lock(self.lock_file):
            previous = self.status()
            if level == "on":
                level = previous["last_enabled_level"]
            if level not in LEVELS:
                raise ValueError("unknown observation policy level")
            revision = previous["revision"] + 1
            value = {
                **self.default(),
                "level": level,
                "last_enabled_level": level if level != "off" else previous["last_enabled_level"],
                "revision": revision,
                "updated_at": timestamp(),
                "supersedes_revision": previous["revision"],
                "history_slots": HISTORY_SLOTS,
            }
            history = self.directory / "history" / f"slot-{revision % HISTORY_SLOTS:02d}.json"
            self._atomic_write(history, value)
            self._atomic_write(self.file, value)
            return dict(value, profile=LEVELS[level])

    def health(self):
        state = self.status()
        journals = {}
        latest = {}
        for node in learning.NODES:
            path = learning.hub_root() / "by-device" / node / "workman-v1.1.jsonl"
            journals[node] = learning.journal_storage(path)
            rows = learning.read_events(path)
            latest[node] = rows[-1]["ts"] if rows else None
        graph = learning.hub_root() / "workman-v1.1-graph.sqlite3"
        knowledge = learning.hub_root() / "knowledge" / "WORKMAN-FLEET-V1.1.md"
        stale_temporaries = len(list(self.directory.glob(".setting-*"))) if self.directory.exists() else 0
        return {
            "policy": state,
            "evidence_store": {
                "journal_segments": journals,
                "latest_event_by_device": latest,
                "graph": {"exists": graph.exists(), "bytes": graph.stat().st_size if graph.exists() else 0},
                "knowledge": {"exists": knowledge.exists(), "bytes": knowledge.stat().st_size if knowledge.exists() else 0},
            },
            "crash_recovery": {
                "atomic_replace": True,
                "parallel_session_lock": True,
                "bounded_revision_slots": HISTORY_SLOTS,
                "stale_temporary_files": stale_temporaries,
            },
            "remote_probe_performed": False,
            "rule": "This checks local durable state only. Device readiness and permissions still need a fresh live check.",
        }
