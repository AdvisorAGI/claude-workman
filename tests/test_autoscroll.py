"""Autoscroll-and-read: stitch, stop, cap. No live browser."""
from __future__ import annotations

from workman import autoscroll, chrome, human
from workman.autoscroll import (
    STOP_MAX_SCROLLS,
    STOP_NO_NEW_TEXT,
    STOP_SCROLL_POSITION,
    cap_text,
    run_autoscroll,
    stitch_chunks,
    stitch_pair,
    stop_after_step,
)


def _capture_script(frames: list[dict]):
    """Return (capture, scroll) that walk `frames` in order.

    `capture` reads the current frame; `scroll` advances the index.
    """
    state = {"i": 0}

    def capture():
        return dict(frames[min(state["i"], len(frames) - 1)])

    def scroll():
        state["i"] = min(state["i"] + 1, len(frames) - 1)

    return capture, scroll, state


class TestStitch:
    def test_no_overlap_concatenates(self):
        assert stitch_pair("A\nB\nC", "D\nE\nF", overlap_lines=3) == "A\nB\nC\nD\nE\nF"

    def test_typical_tail_head_overlap(self):
        prev = "L1\nL2\nL3\nL4\nL5"
        nxt = "L3\nL4\nL5\nL6\nL7"
        assert stitch_pair(prev, nxt, overlap_lines=3) == "L1\nL2\nL3\nL4\nL5\nL6\nL7"

    def test_full_overlap_adds_nothing(self):
        prev = "A\nB\nC\nD\nE"
        nxt = "C\nD\nE"
        assert stitch_pair(prev, nxt, overlap_lines=3) == "A\nB\nC\nD\nE"

    def test_repeated_identical_chunk_adds_nothing(self):
        chunk = "A\nB\nC\nD"
        assert stitch_pair(chunk, chunk, overlap_lines=3) == chunk
        assert stitch_chunks([chunk, chunk, chunk], overlap_lines=3) == chunk

    def test_empty_next_keeps_prev(self):
        assert stitch_pair("A\nB", "", overlap_lines=3) == "A\nB"
        assert stitch_pair("A\nB", "  \n", overlap_lines=3) == "A\nB"

    def test_empty_prev_takes_next(self):
        assert stitch_pair("", "A\nB", overlap_lines=3) == "A\nB"

    def test_skips_blank_lines_when_matching(self):
        prev = "A\n\nB\n\nC"
        nxt = "A\nB\nC\nD"
        assert stitch_pair(prev, nxt, overlap_lines=3) == "A\nB\nC\nD"

    def test_three_windows_stitch_without_gaps_or_dupes(self):
        chunks = [
            "L1\nL2\nL3\nL4\nL5",
            "L3\nL4\nL5\nL6\nL7",
            "L5\nL6\nL7\nL8\nL9",
        ]
        assert stitch_chunks(chunks, overlap_lines=3) == (
            "L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\nL9"
        )

    def test_empty_chunk_list(self):
        assert stitch_chunks([]) == ""


class TestStopCondition:
    def test_position_unchanged_wins(self):
        assert stop_after_step(
            prev_position=4, position=4, prev_text="A\nB\nC", text="A\nB\nC",
            overlap_lines=3, scrolls=1, max_scrolls=40,
        ) == STOP_SCROLL_POSITION

    def test_no_new_lines_when_position_moved_but_subset(self):
        assert stop_after_step(
            prev_position=0, position=1, prev_text="A\nB\nC\nD", text="B\nC\nD",
            overlap_lines=3, scrolls=1, max_scrolls=40,
        ) == STOP_NO_NEW_TEXT

    def test_max_scrolls_when_new_lines_and_at_limit(self):
        assert stop_after_step(
            prev_position=0, position=1, prev_text="A\nB\nC", text="B\nC\nD",
            overlap_lines=3, scrolls=40, max_scrolls=40,
        ) == STOP_MAX_SCROLLS

    def test_continue_when_new_lines_remain(self):
        assert stop_after_step(
            prev_position=0, position=1, prev_text="A\nB\nC", text="B\nC\nD",
            overlap_lines=3, scrolls=3, max_scrolls=40,
        ) is None

    def test_position_beats_max_scrolls(self):
        assert stop_after_step(
            prev_position=9, position=9, prev_text="A\nB\nC", text="B\nC\nD",
            overlap_lines=3, scrolls=40, max_scrolls=40,
        ) == STOP_SCROLL_POSITION


class TestTruncation:
    def test_cap_cuts_and_flags(self):
        text, truncated = cap_text("abcdef", cap=4)
        assert text == "abcd"
        assert truncated is True

    def test_cap_untouched_when_under(self):
        text, truncated = cap_text("abc", cap=10)
        assert text == "abc"
        assert truncated is False

    def test_default_cap_is_200000(self):
        huge = "x" * (autoscroll.TEXT_CAP + 17)
        text, truncated = cap_text(huge)
        assert truncated is True
        assert len(text) == autoscroll.TEXT_CAP

    def test_run_flags_truncated_stitched_text(self):
        state = {"i": 0}

        def capture():
            state["i"] += 1
            return {"text": f"chunk-{state['i']}\n" + ("Z" * 40), "position": state["i"]}

        def scroll():
            pass

        result = run_autoscroll(capture, scroll, max_scrolls=4, text_cap=50,
                                overlap_lines=3)
        assert result["truncated"] is True
        assert len(result["text"]) == 50
        assert result["text"] == stitch_chunks(result["chunks"])[:50]


class TestRunLoop:
    def test_stops_when_position_does_not_advance(self):
        frames = [
            {"text": "A\nB\nC", "position": 0},
            {"text": "A\nB\nC", "position": 0},
        ]
        capture, scroll, state = _capture_script(frames)
        scrolls = {"n": 0}

        def counting_scroll():
            scrolls["n"] += 1
            scroll()

        result = run_autoscroll(capture, counting_scroll, max_scrolls=10)
        assert result["stopped_because"] == STOP_SCROLL_POSITION
        assert result["scrolls"] == 1
        assert result["chunks"] == ["A\nB\nC"]
        assert result["text"] == "A\nB\nC"
        assert result["truncated"] is False
        assert scrolls["n"] == 1

    def test_stops_when_text_stops_adding_lines(self):
        frames = [
            {"text": "A\nB\nC\nD", "position": 0},
            {"text": "B\nC\nD", "position": 1},
        ]
        capture, scroll, _ = _capture_script(frames)
        result = run_autoscroll(capture, scroll, max_scrolls=10, overlap_lines=3)
        assert result["stopped_because"] == STOP_NO_NEW_TEXT
        assert result["scrolls"] == 1
        assert result["chunks"] == ["A\nB\nC\nD"]
        assert result["text"] == "A\nB\nC\nD"

    def test_repeated_identical_chunk_is_no_new_text_if_position_moves(self):
        chunk = "A\nB\nC"
        frames = [
            {"text": chunk, "position": 0},
            {"text": chunk, "position": 1},
        ]
        capture, scroll, _ = _capture_script(frames)
        result = run_autoscroll(capture, scroll, max_scrolls=8, overlap_lines=3)
        assert result["stopped_because"] == STOP_NO_NEW_TEXT
        assert result["text"] == chunk
        assert result["chunks"] == [chunk]

    def test_stops_at_max_scrolls_with_advancing_content(self):
        state = {"i": -1}

        def capture():
            state["i"] += 1
            i = state["i"]
            return {"text": "\n".join(f"L{j}" for j in range(i, i + 5)), "position": i}

        def scroll():
            pass

        result = run_autoscroll(capture, scroll, max_scrolls=5, overlap_lines=3)
        assert result["stopped_because"] == STOP_MAX_SCROLLS
        assert result["scrolls"] == 5
        assert result["text"] == "\n".join(f"L{j}" for j in range(0, 10))
        assert len(result["chunks"]) == 6  # initial + 5 advances

    def test_max_scrolls_zero_is_just_the_first_capture(self):
        capture, scroll, _ = _capture_script([{"text": "only", "position": 0}])
        called = {"n": 0}

        def no_scroll():
            called["n"] += 1

        result = run_autoscroll(capture, no_scroll, max_scrolls=0)
        assert result["stopped_because"] == STOP_MAX_SCROLLS
        assert result["scrolls"] == 0
        assert result["text"] == "only"
        assert called["n"] == 0

    def test_settle_runs_once_per_scroll(self):
        frames = [
            {"text": "A\nB\nC", "position": 0},
            {"text": "B\nC\nD", "position": 1},
            {"text": "C\nD\nE", "position": 2},
        ]
        capture, scroll, _ = _capture_script(frames)
        settles = []
        result = run_autoscroll(
            capture, scroll, max_scrolls=2, overlap_lines=3,
            settle=lambda: settles.append(1),
        )
        assert result["scrolls"] == 2
        assert settles == [1, 1]

    def test_screenshot_callback_records_each_capture(self):
        state = {"i": -1}

        def capture():
            state["i"] += 1
            i = state["i"]
            return {"text": "\n".join(f"L{j}" for j in range(i, i + 3)), "position": i}

        paths = []

        def shot():
            path = f"/tmp/s{len(paths)}.png"
            paths.append(path)
            return path

        result = run_autoscroll(capture, lambda: None, max_scrolls=2,
                                overlap_lines=3, screenshot=shot)
        assert result["screenshots"] == paths
        assert len(paths) == 3  # initial + 2 scrolls

    def test_missing_position_defaults_to_text(self):
        frames = [{"text": "same"}, {"text": "same"}]
        capture, scroll, _ = _capture_script(frames)
        result = run_autoscroll(capture, scroll, max_scrolls=4)
        assert result["stopped_because"] == STOP_SCROLL_POSITION
        assert result["scrolls"] == 1


class TestPickContainer:
    def test_selector_picks_matching_element(self):
        nodes = [
            {"role": "document web", "name": "Page", "x": 0, "y": 0, "w": 800, "h": 600},
            {"role": "region", "name": "thread", "x": 10, "y": 80, "w": 400, "h": 400},
        ]
        box = autoscroll.pick_container(nodes, selector="#thread")
        assert box["via"] == "selector"
        assert box["name"] == "thread"
        assert box["h"] == 400

    def test_tallest_scrollable_when_no_selector(self):
        nodes = [
            {"role": "frame", "name": "Chrome", "x": 0, "y": 0, "w": 1000, "h": 900},
            {"role": "document web", "name": "Doc", "x": 0, "y": 80, "w": 800, "h": 700},
            {"role": "link", "name": "tiny", "x": 1, "y": 1, "w": 40, "h": 12},
        ]
        box = autoscroll.pick_container(
            nodes, selector=None,
            fallback={"x": 0, "y": 80, "w": 800, "h": 700, "via": "document"},
        )
        assert box["via"] == "tallest"
        assert box["name"] == "Doc"

    def test_fallback_document_when_nothing_qualifies(self):
        fallback = {"x": 0, "y": 90, "w": 800, "h": 500, "via": "document"}
        box = autoscroll.pick_container([], selector=None, fallback=fallback)
        assert box["via"] == "document"
        assert box["h"] == 500
        assert box["cy"] == 90 + 250


class TestChromeWiring:
    def test_autoscroll_read_wraps_engine(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": True, "id": "1", "title": "T"})
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "1", "x": 0, "y": 0, "w": 800, "h": 600})
        monkeypatch.setattr(chrome, "_chrome_nodes", lambda: [
            {"role": "document web", "name": "Hello", "x": 10, "y": 100,
             "w": 700, "h": 400},
        ])
        seen = {}

        def fake_run(capture, scroll, **kw):
            seen["kw"] = kw
            cap = capture()
            seen["cap"] = cap
            return {"text": cap["text"], "chunks": [cap["text"]], "scrolls": 0,
                    "stopped_because": STOP_SCROLL_POSITION, "truncated": False}

        monkeypatch.setattr(chrome.autoscroll, "run_autoscroll", fake_run)
        human.reset()
        result = chrome.autoscroll_read()
        assert result["ok"] is True
        assert result["human"] is False
        assert result["text"] == "Hello"
        assert result["stopped_because"] == STOP_SCROLL_POSITION
        assert "container" in result

    def test_human_true_uses_human_scroll(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": True, "id": "1", "title": "T"})
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "1", "x": 0, "y": 0, "w": 800, "h": 600})
        monkeypatch.setattr(chrome, "_chrome_nodes", lambda: [
            {"role": "document web", "name": "Line A\nLine B\nLine C",
             "x": 0, "y": 90, "w": 800, "h": 500},
        ])
        calls = []
        monkeypatch.setattr(chrome.human, "human_scroll",
                            lambda direction, amount=3, x=None, y=None, rng=None:
                            calls.append(("human", direction, amount, x, y)) or
                            {"ok": True, "human": True})
        monkeypatch.setattr(chrome.x11, "scroll_at",
                            lambda *a, **k: calls.append(("direct", a, k)) or
                            {"ok": True})
        monkeypatch.setattr(chrome.time, "sleep", lambda s: None)
        monkeypatch.setattr(chrome, "_sleep_ms", lambda ms: None)
        human.reset()
        result = chrome.autoscroll_read(max_scrolls=1, human=True)
        assert result["ok"] is True
        assert result["human"] is True
        assert any(c[0] == "human" for c in calls)
        assert not any(c[0] == "direct" for c in calls)

    def test_to_top_scrolls_up_until_stuck(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": True, "id": "1", "title": "T"})
        monkeypatch.setattr(chrome, "find_chrome_window",
                            lambda: {"id": "1", "x": 0, "y": 0, "w": 800, "h": 600})
        texts = iter(["bottom", "middle", "top", "top"])
        monkeypatch.setattr(chrome.autoscroll, "lines_from_nodes",
                            lambda *a, **k: next(texts, "top"))
        monkeypatch.setattr(chrome, "_chrome_nodes", lambda: [])
        seen = []
        monkeypatch.setattr(chrome, "_scroll_container",
                            lambda container, direction, amount, use_human:
                            seen.append(direction) or {"ok": True})
        monkeypatch.setattr(chrome, "_settle", lambda *a, **k: None)
        human.reset()
        result = chrome.autoscroll_to_top()
        assert result["ok"] is True
        assert seen == ["up", "up", "up"]
        assert result["stopped_because"] == STOP_SCROLL_POSITION
        assert result["scrolls"] == 3

    def test_focus_failure_is_returned(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus",
                            lambda: {"ok": False, "error": "no Chrome/Chromium window"})
        result = chrome.autoscroll_read()
        assert result["ok"] is False
        assert "Chrome" in result["error"]
