"""Bot safety: guards on sensitive sites, pacing, wait jitter, scoring."""
import pytest

from workman import botsafety, human

STRIPE = "Invoices – Advisorppc – Stripe - Google Chrome"
NEUTRAL = "Terminal"


@pytest.fixture
def bs(monkeypatch):
    clock = {"t": 1000.0, "slept": []}
    title = {"v": STRIPE}
    monkeypatch.setattr(botsafety, "_now", lambda: clock["t"])

    def fake_sleep(ms):
        clock["slept"].append(ms)
        clock["t"] += ms / 1000.0
    monkeypatch.setattr(botsafety, "_sleep", fake_sleep)
    monkeypatch.setattr(botsafety, "_active_title", lambda: title["v"])
    botsafety.reset("enforce")
    human.set_mode(True, seed=7)
    yield clock, title
    human.reset()
    botsafety.reset()


def act(name, args=None, result=None):
    refused = botsafety.before(name, args or {})
    if refused is None:
        botsafety.after(name, result or {"ok": True})
    return refused


def test_address_bar_refused_on_sensitive_site(bs):
    r = act("press_key", {"key": "ctrl+l"})
    assert r["error"] == "bot_safety" and r["tell"] == "address_bar_nav"
    assert "Click" in r["instruction"]


def test_address_bar_only_counted_elsewhere(bs):
    _, title = bs
    title["v"] = NEUTRAL
    assert act("press_key", {"key": "ctrl+l"}) is None
    assert botsafety.report()["tells"]["address_bar_nav"] == 1


def test_short_paste_refused_long_paste_allowed(bs):
    botsafety.note_clipboard("290.00")
    assert act("press_key", {"key": "ctrl+v"})["tell"] == "short_paste"
    botsafety.note_clipboard("x" * 200)
    assert act("press_key", {"key": "ctrl+v"}) is None


def test_human_mode_off_refused_on_sensitive(bs):
    human.set_mode(False)
    assert act("click", {"x": 1, "y": 1})["tell"] == "human_mode_off"


def test_synthetic_dom_click_refused(bs):
    assert act("bridge_dom_click", {"tabId": 1})["tell"] == "synthetic_input"
    assert act("chrome_type", {"text": "a", "human": False})["tell"] == "synthetic_input"


def test_warn_mode_never_refuses_or_sleeps(bs):
    clock, _ = bs
    botsafety.set_mode("warn")
    assert act("press_key", {"key": "ctrl+l"}) is None
    act("click")
    act("click")
    assert clock["slept"] == []


def test_off_mode_does_nothing(bs):
    botsafety.set_mode("off")
    assert act("press_key", {"key": "ctrl+l"}) is None
    assert botsafety.report()["tells"] == {}


def test_back_to_back_inputs_are_paced(bs):
    clock, _ = bs
    act("click")
    act("click")  # 0 ms after the first
    assert clock["slept"] and clock["slept"][0] >= botsafety.MIN_GAP_MS
    assert botsafety.report()["tells"]["too_fast"] >= 1


def test_metronome_is_broken_up(bs):
    clock, title = bs
    title["v"] = NEUTRAL
    for _ in range(botsafety.RHYTHM_WINDOW + 2):
        act("click")
        clock["t"] += 2.0  # exactly 2 s apart, every time
    assert botsafety.report()["tells"].get("metronome", 0) >= 1


def test_rate_budget_on_sensitive(bs):
    clock, _ = bs
    for _ in range(botsafety.RATE_PER_MIN["sensitive"] + 1):
        act("click")
        clock["t"] += 0.9
    assert botsafety.report()["tells"].get("rate_burst", 0) >= 1


def test_wait_is_jittered_and_round_waits_counted(bs):
    got = {botsafety.jitter_wait(2.0) for _ in range(20)}
    assert len(got) > 1
    assert all(1.4 <= g <= 2.8 for g in got)
    assert botsafety.report()["tells"]["round_wait"] == 20


def test_challenge_seen_counts(bs):
    botsafety.after("click", {"ok": False, "error": "bot_challenge"})
    assert botsafety.report()["tells"]["challenge_seen"] == 1


def test_report_score_and_verdict(bs):
    rep = botsafety.report()
    assert rep["score"] == 100 and rep["verdict"] == "human"
    assert rep["sensitive_site"] is True
    for _ in range(5):
        act("press_key", {"key": "ctrl+l"})
    rep = botsafety.report()
    assert rep["score"] == 70 and rep["verdict"] == "watch"
    assert rep["refused"]["address_bar_nav"] == 5
    assert rep["advice"]


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        botsafety.set_mode("stealth")


def test_sensitive_markers():
    assert botsafety.is_sensitive(STRIPE)
    assert botsafety.is_sensitive("Google Ads - Overview")
    assert not botsafety.is_sensitive("YouTube")
