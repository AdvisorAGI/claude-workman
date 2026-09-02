"""Chrome tools — cadence, pointer path, CDP parsing. No live display."""
from __future__ import annotations

import json
import random
import urllib.error

from workman import chrome


class FakeHTTP:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode() if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CDP_PAGES = [
    {"id": "t1", "title": "GitHub", "url": "https://github.com", "type": "page"},
    {"id": "t2", "title": "Gmail", "url": "https://mail.google.com/mail", "type": "page"},
    {"id": "t3", "title": "worker", "url": "chrome-extension://abc", "type": "service_worker"},
    {"id": "t4", "title": "Mail - Inbox", "url": "https://mail.google.com/mail/u/0/#inbox",
     "type": "page"},
]


class TestHumanReexports:
    def test_cadence_and_path_are_the_shared_module(self):
        from workman import human
        assert chrome.typing_cadence is human.typing_cadence
        assert chrome.eased_path is human.eased_path
        assert chrome.human_click is human.human_click
        assert chrome.human_move is human.human_move
        assert chrome.PATH_STEPS == human.PATH_STEPS
        assert chrome.PATH_WAYPOINTS == human.PATH_WAYPOINTS


class TestCdpParsing:
    def test_parse_tabs_skips_workers_and_marks_active(self):
        tabs = chrome.parse_tabs(CDP_PAGES, window_title="Gmail - Google Chrome")
        assert [t["id"] for t in tabs] == ["t1", "t2", "t4"]
        active = [t for t in tabs if t["active"]]
        # "Gmail" and "Mail - Inbox" both contain-match poorly; window starts with Gmail.
        assert len(active) == 1
        assert active[0]["id"] == "t2"
        assert active[0]["url"] == "https://mail.google.com/mail"

    def test_longest_title_wins_when_several_match(self):
        payload = [
            {"id": "short", "title": "Mail", "url": "https://mail.example/", "type": "page"},
            {"id": "long", "title": "Mail - Inbox", "url": "https://mail.example/inbox",
             "type": "page"},
        ]
        tabs = chrome.parse_tabs(payload, window_title="Mail - Inbox - Google Chrome")
        active = next(t for t in tabs if t["active"])
        assert active["id"] == "long"
        assert not tabs[0]["active"]

    def test_non_list_payload_is_empty(self):
        assert chrome.parse_tabs({"Browser": "Chrome/120"}) == []
        assert chrome.parse_tabs(None) == []

    def test_select_tab_by_title_or_url(self):
        tabs = chrome.parse_tabs(CDP_PAGES, "GitHub - Google Chrome")
        assert chrome.select_tab(tabs, "gmail")["id"] == "t2"
        assert chrome.select_tab(tabs, "github.com")["id"] == "t1"
        assert chrome.select_tab(tabs, "inbox")["id"] == "t4"
        assert chrome.select_tab(tabs, "nope") is None
        assert chrome.select_tab(tabs, "") is None

    def test_cdp_request_parses_json_via_urlopen(self, monkeypatch):
        body = json.dumps(CDP_PAGES)
        monkeypatch.setattr(chrome.urllib.request, "urlopen",
                            lambda *a, **k: FakeHTTP(body))
        payload, err = chrome.cdp_request("/json")
        assert err is None
        tabs = chrome.parse_tabs(payload, "GitHub - Google Chrome")
        assert tabs[0]["active"] is True
        assert tabs[0]["id"] == "t1"

    def test_cdp_request_accepts_plain_text_activate(self, monkeypatch):
        monkeypatch.setattr(chrome.urllib.request, "urlopen",
                            lambda *a, **k: FakeHTTP("Target is activating"))
        payload, err = chrome.cdp_request("/json/activate/t1")
        assert err is None
        assert payload["ok"] is True

    def test_cdp_unreachable_is_not_an_exception(self, monkeypatch):
        def boom(*a, **k):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(chrome.urllib.request, "urlopen", boom)
        payload, err = chrome.cdp_request("/json")
        assert payload is None
        assert "refused" in err.lower() or "Connection" in err

    def test_is_loading_uses_cdp_flag_and_blank_title(self):
        assert chrome.is_loading({"loading": True, "title": "Hi", "url": "https://x"})
        assert chrome.is_loading({"title": "Loading...", "url": "https://x"})
        assert chrome.is_loading({"title": "", "url": "about:blank"})
        assert not chrome.is_loading({"title": "Example Domain", "url": "https://example.com"})
        assert not chrome.is_loading(None)


class TestListAndActivate:
    def test_list_tabs_no_cdp_returns_window_title(self, monkeypatch):
        monkeypatch.setattr(chrome, "cdp_request", lambda path="/json": (None, "Connection refused"))
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "9", "name": "GitHub - Google Chrome"})
        result = chrome.list_tabs()
        assert result["ok"] is False
        assert result["cdp"] is False
        assert "no CDP" in result["error"]
        assert result["window_title"] == "GitHub - Google Chrome"
        assert result["tabs"] == []

    def test_list_tabs_from_cdp(self, monkeypatch):
        monkeypatch.setattr(chrome, "cdp_request", lambda path="/json": (CDP_PAGES, None))
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "9", "name": "GitHub - Google Chrome"})
        result = chrome.list_tabs()
        assert result["ok"] is True and result["cdp"] is True
        assert result["tabs"][0]["active"] is True

    def test_activate_tab_uses_cdp_first(self, monkeypatch):
        calls = []

        def fake_cdp(path="/json"):
            calls.append(path)
            if path.startswith("/json/activate/"):
                return {"ok": True}, None
            return CDP_PAGES, None

        monkeypatch.setattr(chrome, "cdp_request", fake_cdp)
        monkeypatch.setattr(chrome, "_window_title", lambda: "GitHub - Google Chrome")
        result = chrome.activate_tab("mail.google")
        assert result["ok"] is True
        assert result["via"] == "cdp"
        assert result["tab"]["id"] == "t2"
        assert any(p.startswith("/json/activate/t2") for p in calls)

    def test_activate_tab_ui_fallback_walks_ctrl_tab(self, monkeypatch):
        titles = ["Alpha - Google Chrome"]
        keys = []

        monkeypatch.setattr(chrome, "cdp_request", lambda path="/json": (None, "down"))
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": True, "id": "1", "title": titles[0]})
        monkeypatch.setattr(chrome, "_window_title", lambda: titles[-1])
        monkeypatch.setattr(chrome.time, "sleep", lambda s: None)

        def press(key):
            keys.append(key)
            if key == "ctrl+Tab":
                nxt = {1: "Beta - Google Chrome", 2: "Wanted Tab - Google Chrome"}
                titles.append(nxt.get(len(keys), titles[-1]))
            return {"ok": True}

        monkeypatch.setattr(chrome.desktop, "press_key", press)
        result = chrome.activate_tab("Wanted")
        assert result["ok"] is True
        assert result["via"] == "ui"
        assert "ctrl+Tab" in keys
        assert "Wanted" in result["title"]


class TestChromeWindow:
    def test_is_chrome_window_titles(self):
        assert chrome.is_chrome_window({"name": "GitHub - Google Chrome"})
        assert chrome.is_chrome_window({"name": "Docs - Chromium"})
        assert chrome.is_chrome_window({"name": "Chromium"})
        assert chrome.is_chrome_window({"name": "page - Chrome"})
        assert not chrome.is_chrome_window({"name": "Firefox"})
        assert not chrome.is_chrome_window({"name": "Chrome OS article - Mozilla Firefox"})

    def test_focus_returns_id_and_title(self, monkeypatch):
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "4242", "name": "GitHub - Google Chrome"})
        monkeypatch.setattr(chrome.desktop, "focus_window",
                            lambda query, **k: {"ok": True, "target": query,
                                                "frontmost_now": "GitHub - Google Chrome"})
        result = chrome.focus()
        assert result == {"ok": True, "id": "4242", "title": "GitHub - Google Chrome"}

    def test_focus_reports_missing_window(self, monkeypatch):
        monkeypatch.setattr(chrome, "find_chrome_window", lambda: None)
        result = chrome.focus()
        assert result["ok"] is False
        assert "Chrome" in result["error"]


class TestOpenUrlAndType:
    def test_open_url_new_tab_sequence(self, monkeypatch):
        keys, typed = [], []
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": True, "id": "1", "title": "Old"})
        monkeypatch.setattr(chrome, "_window_title", lambda: "Example - Google Chrome")
        monkeypatch.setattr(chrome, "_wait_title_change",
                            lambda prev, timeout_s=10: "Example - Google Chrome")
        monkeypatch.setattr(chrome.desktop, "press_key",
                            lambda k: keys.append(k) or {"ok": True})
        monkeypatch.setattr(chrome, "type_text",
                            lambda text, human=True, rng=None: typed.append(text) or
                            {"ok": True, "typed_len": len(text)})
        monkeypatch.setattr(chrome.time, "sleep", lambda s: None)
        result = chrome.open_url("https://example.com", new_tab=True, rng=random.Random(0))
        assert result["ok"] is True
        assert keys[0] == "ctrl+t"
        assert "Return" in keys
        assert typed == ["https://example.com"]

    def test_open_url_same_tab_uses_omnibox(self, monkeypatch):
        keys = []
        monkeypatch.setattr(chrome, "focus", lambda: {"ok": True, "id": "1", "title": "Old"})
        monkeypatch.setattr(chrome, "_window_title", lambda: "Old")
        monkeypatch.setattr(chrome, "_wait_title_change", lambda prev, timeout_s=10: "New")
        monkeypatch.setattr(chrome.desktop, "press_key", lambda k: keys.append(k) or {"ok": True})
        monkeypatch.setattr(chrome, "type_text",
                            lambda text, human=True, rng=None: {"ok": True, "typed_len": len(text)})
        monkeypatch.setattr(chrome.time, "sleep", lambda s: None)
        chrome.open_url("https://x.test", new_tab=False, rng=random.Random(0))
        assert keys[0] == "ctrl+l"

    def test_type_text_human_types_per_character(self, monkeypatch):
        from workman import human
        typed = []
        monkeypatch.setattr(chrome.desktop, "type_text",
                            lambda text, delay_ms=40: typed.append(text) or
                            {"ok": True, "typed_len": len(text)})
        monkeypatch.setattr(chrome.desktop, "press_key",
                            lambda k: typed.append(f"<{k}>") or {"ok": True})
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        result = chrome.type_text("ab\n", human=True, rng=random.Random(0))
        assert result["human"] is True
        assert typed[0] == "a" and typed[1] == "b"
        assert "<Return>" in typed

    def test_type_text_non_human_is_one_shot(self, monkeypatch):
        typed = []
        monkeypatch.setattr(chrome.desktop, "type_text",
                            lambda text, delay_ms=40: typed.append((text, delay_ms)) or
                            {"ok": True, "typed_len": len(text)})
        chrome.type_text("hello", human=False)
        assert typed == [("hello", 40)]


class TestReadClickWait:
    def test_read_page_caps_text_and_lists_interactive(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus", lambda: {"ok": True, "id": "1", "title": "Page"})
        long_name = "x" * 25000
        monkeypatch.setattr(chrome.a11y, "tree", lambda **k: [
            {"app": "Google Chrome", "role": "document web", "name": long_name,
             "x": 0, "y": 0, "w": 10, "h": 10},
            {"app": "Google Chrome", "role": "link", "name": "Next",
             "x": 1, "y": 1, "w": 10, "h": 10},
            {"app": "Google Chrome", "role": "push button", "name": "OK",
             "x": 2, "y": 2, "w": 10, "h": 10},
        ])
        result = chrome.read_page()
        assert result["ok"] is True
        assert len(result["text"]) == chrome.TEXT_CAP
        names = {e["name"] for e in result["elements"]}
        assert names == {"Next", "OK"}

    def test_click_text_human_clicks_center(self, monkeypatch):
        el = {"role": "link", "name": "Pricing", "x": 10, "y": 20, "w": 40, "h": 10,
              "cx": 30, "cy": 25}
        monkeypatch.setattr(chrome, "focus", lambda: {"ok": True, "id": "1", "title": "T"})
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "1", "name": "T", "x": 0, "y": 0, "w": 800, "h": 600})
        monkeypatch.setattr(chrome, "_find_clickable", lambda text: dict(el))
        monkeypatch.setattr(chrome, "_in_content_area", lambda e, w: True)
        seen = {}
        monkeypatch.setattr(chrome, "human_click",
                            lambda x, y, rng=None: seen.update(x=x, y=y) or
                            {"ok": True, "press_ms": 80, "at": [x, y]})
        result = chrome.click_text("Pricing", rng=random.Random(0))
        assert result["ok"] is True
        assert seen == {"x": 30, "y": 25}

    def test_human_click_moves_along_path_then_presses(self, monkeypatch):
        from workman import human
        moves, downs, ups = [], [], []
        monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
        monkeypatch.setattr(human.desktop, "move",
                            lambda x, y: moves.append((x, y)) or {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_down",
                            lambda button=1, x=None, y=None: downs.append(button) or {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up",
                            lambda button=1, x=None, y=None: ups.append(button) or {"ok": True})
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        result = chrome.human_click(100, 80, rng=random.Random(1))
        assert result["ok"] is True
        assert human.PATH_STEPS[0] <= len(moves) <= human.PATH_STEPS[1]
        assert downs == [1] and ups == [1]
        assert 60 <= result["press_ms"] <= 140

    def test_wait_load_settles_when_title_stable(self, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(chrome.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(chrome.time, "sleep",
                            lambda s: clock.__setitem__("t", clock["t"] + s))
        monkeypatch.setattr(chrome, "_window_title", lambda: "Done - Google Chrome")
        monkeypatch.setattr(chrome, "cdp_request", lambda path="/json": (None, "no"))
        result = chrome.wait_load(timeout_s=5, poll_s=0.1, stable_needed=3)
        assert result["ok"] is True
        assert result["title"].startswith("Done")

    def test_wait_load_waits_out_cdp_loading(self, monkeypatch):
        clock = {"t": 0.0}
        state = {"loading": True}
        monkeypatch.setattr(chrome.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(chrome.time, "sleep",
                            lambda s: clock.__setitem__("t", clock["t"] + s))
        monkeypatch.setattr(chrome, "_window_title", lambda: "Site - Google Chrome")

        def fake_cdp(path="/json"):
            page = {"id": "1", "title": "Site", "url": "https://site.example", "type": "page",
                    "loading": state["loading"]}
            # Flip after the first couple of polls so we actually observe loading.
            if clock["t"] > 0.2:
                state["loading"] = False
            return [page], None

        monkeypatch.setattr(chrome, "cdp_request", fake_cdp)
        result = chrome.wait_load(timeout_s=5, poll_s=0.1, stable_needed=2)
        assert result["ok"] is True
        assert result["cdp"] is True
