"""models_available: content and the 10-minute cache."""
from __future__ import annotations



from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman import model_tools as mt
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401


class TestModels:
    def test_models_available_reports_and_caches(self, fleet):
        r = mt.models_available("dgx")
        assert r["ok"] is True
        m = r["machines"]["dgx"]
        assert m["cached"] is False
        assert m["codex"]["version"] == "codex-cli 9.9.9" and "ChatGPT" in m["codex"]["login"]
        assert m["grok"]["logged_in"] is True and m["grok"]["models"] == ["grok-4.7", "grok-4.6"]
        assert m["cursor"]["models"] == ["auto", "composer-2.5", "grok-4.7-high"]
        assert m["run_model"]["direct_clis"] == ["cursor", "grok", "codex", "astra", "luna", "terra"]
        assert set(m["run_model"]["roles"]) == {"cheap", "review-grok"}
        calls = fake_log(fleet, "fake-cli.log").count("\n")
        again = mt.models_available("dgx")
        assert again["machines"]["dgx"]["cached"] is True
        assert fake_log(fleet, "fake-cli.log").count("\n") == calls
        mcat._MODELS_CACHE.clear()          # a new process reads the disk cache
        assert mt.models_available("dgx")["machines"]["dgx"]["cached"] is True
        mt.models_available("dgx", refresh=True)
        assert fake_log(fleet, "fake-cli.log").count("\n") > calls

    def test_all_machines_with_one_down(self, fleet, monkeypatch):
        monkeypatch.setenv("FAKE_SSH_DOWN", "mac-air")
        r = mt.models_available("all")
        assert set(r["machines"]) == {"dgx", "mac-one", "mac-air"}
        assert r["machines"]["mac-air"]["error"] == "unreachable"
        assert r["machines"]["mac-one"]["ok"] is True
