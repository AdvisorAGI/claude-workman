"""Seat ladder for the daily computer-use tuner."""
from __future__ import annotations

from pathlib import Path
import sys

LAB = Path(__file__).resolve().parents[1] / "ops" / "computer-use-lab"
sys.path.insert(0, str(LAB))

import seats  # noqa: E402


def _info(**usable):
    seats_map = {}
    for name in ("fable", "astra", "grok", "cursor", "qwen", "sonnet"):
        seats_map[name] = {
            "id": name,
            "model": name,
            "usable": bool(usable.get(name)),
        }
    return {"host": "test", "seats": seats_map}


def test_learn_prefers_fable():
    info = seats.choose_rungs(_info(fable=True, grok=True, qwen=True, sonnet=True), "learn")
    assert info["leader"]["id"] == "fable"
    assert info["verifier"]["id"] == "grok"


def test_recheck_skips_fable():
    info = seats.choose_rungs(_info(fable=True, grok=True, qwen=True, sonnet=True), "recheck")
    assert info["leader"]["id"] == "grok"
    assert info["verifier"]["id"] != "grok"


def test_skip_has_no_leader():
    info = seats.choose_rungs(_info(fable=True), "skip")
    assert info["leader"] is None


def test_learn_falls_to_qwen():
    info = seats.choose_rungs(_info(qwen=True), "learn")
    assert info["leader"]["id"] == "qwen"
    assert info["verifier"] is None
