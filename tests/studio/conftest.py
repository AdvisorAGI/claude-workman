"""Studio tests never touch the owner's job root, render lock, network or ElevenLabs key."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # studio_synth


@pytest.fixture(scope="session")
def _font_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("studio-cache")


@pytest.fixture(autouse=True)
def _studio_sandbox(monkeypatch, tmp_path, _font_cache):
    monkeypatch.setenv("WORKMAN_STUDIO_CACHE", str(_font_cache))
    monkeypatch.setenv("WORKMAN_STUDIO_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setenv("WORKMAN_STUDIO_LOCK", str(tmp_path / "render.lock"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")

    def no_network(*_a, **_k):
        raise AssertionError("studio tests must not open the network")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
