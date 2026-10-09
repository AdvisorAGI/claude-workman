"""The desktop picker through a fake AT-SPI: opens by the current label, picks,
closes a wrong menu by its opener (never Escape)."""
from __future__ import annotations


import pytest

from workman.orchestra import picker as opicker

from orchestra_fakes import (FakeA11y, control_world)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return control_world(tmp_path, monkeypatch)


class TestDesktopPicker:
    def test_opens_by_the_current_label_and_picks(self):
        a = FakeA11y()
        pk = opicker.DesktopPicker(a, sleep=lambda s: None)
        out = pk.pick(opicker.model_labels("claude-fable-5-1"), opicker.model_labels("claude-opus-5-5"))
        assert out["ok"] is True and a.clicks == ["Fable 5.1", "Opus 5.5"]

    def test_missing_item_closes_by_its_opener(self):
        a = FakeA11y(items=("Sonnet 5.5",))
        pk = opicker.DesktopPicker(a, sleep=lambda s: None)
        out = pk.pick(opicker.model_labels("claude-fable-5-1"), opicker.model_labels("claude-opus-5-5"))
        assert out["ok"] is False and a.clicks == ["Fable 5.1", "Fable 5.1"]
        assert a.open is False

    def test_missing_opener_clicks_nothing(self):
        a = FakeA11y()
        pk = opicker.DesktopPicker(a, sleep=lambda s: None)
        assert pk.pick(("Haiku 4.5",), ("Opus 5.5",))["ok"] is False and a.clicks == []

    def test_effort_aliases(self):
        a = FakeA11y(items=("Low", "Medium", "Extra high", "Max"))
        pk = opicker.DesktopPicker(a, sleep=lambda s: None)
        assert pk.pick(opicker.effort_labels("high"), opicker.effort_labels("xhigh"))["ok"] is True
        assert a.clicks == ["High", "Extra high"]
