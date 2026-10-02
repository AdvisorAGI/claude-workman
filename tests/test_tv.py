"""TV control: what would hurt is a select press that confirms a factory reset,
keys sent in a burst the set drops, a parser that misreads which row has focus,
and a navigation loop that presses select or never ends.

Every test runs against a fake ADB link (conftest makes the real TV unreachable).
FakeTv plays the picture-settings list from the synthetic fixture: D-pad up and
down move focus, a dump renders the current screen, every command and sleep is
logged in order.
"""
from __future__ import annotations

import asyncio
import io
import re
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from workman import server, tv

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_XML = (FIXTURES / "tv_ui_live_tvcenter.xml").read_text(encoding="utf-8")
LIST_XML = (FIXTURES / "tv_ui_synthetic_picture.xml").read_text(encoding="utf-8")
# Captured at import, before conftest swaps it for a blocker in every test.
REAL_CONNECT = tv._connect

# Captured from the owner's set with the read-only status command pieces.
LIVE_STATUS = """@model
MiTV-MSSP3
@android
9
@boot
kernel_panic
@uptime
2360.42 8370.76
@picture_mode
11
@backlight
50
@screen
1
@window
  mCurrentFocus=Window{567ba07 u0 com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity}
  mFocusedApp=AppWindowToken{ad10521 token=Token{37cf788 ActivityRecord{547272b u0 com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity t368}}}
@input
      inputId: com.mediatek.tvinput/.hdmi.HDMIInputService/HW2
"""


class FakeTv:
    def __init__(self, focus: int = 0, dump_ok: bool = True, xml: str = LIST_XML,
                 wrap: bool = False):
        self.root = ET.fromstring(xml[xml.index("<hierarchy"):])
        self.rows = [n for n in self.root.iter("node") if n.get("focusable") == "true"]
        self.focus = focus
        self.dump_ok = dump_ok
        self.wrap = wrap  # an endless carousel: focus always moves
        self.log: list[tuple[str, object]] = []
        self.resolve = ""
        self.png = b"\x89PNG\r\n\x1a\nfake"
        self.screencap_rc = 0
        self.settings = {"power_picture_off": "1"}
        self.settings_writable = True
        self.fail_dumps = 0  # this many dumps fail before they work again

    def render(self) -> str:
        for i, row in enumerate(self.rows):
            row.set("focused", "true" if i == self.focus else "false")
        return "<?xml version='1.0' ?>" + ET.tostring(self.root, encoding="unicode")

    def shell(self, cmd: str, timeout_s: float) -> str:
        self.log.append(("shell", cmd))
        if cmd.startswith("input keyevent "):
            code = int(cmd.split()[2])
            n = len(self.rows)
            if code == 20:
                self.focus = (self.focus + 1) % n if self.wrap else min(self.focus + 1, n - 1)
            elif code == 19:
                self.focus = (self.focus - 1) % n if self.wrap else max(self.focus - 1, 0)
            return ""
        if "uiautomator dump" in cmd:
            if not self.dump_ok or self.fail_dumps > 0:
                self.fail_dumps -= 1
                return "ERROR: could not get idle state\n"
            return "UI hierchary dumped to: /sdcard/workman-ui.xml\n" + self.render()
        if cmd.startswith("echo ok"):
            return "ok\n"
        if cmd.startswith("cmd package resolve-activity"):
            return self.resolve
        if cmd.startswith("dumpsys window"):
            return ("  mCurrentFocus=Window{1 u0 com.android.tv.settings/"
                    "com.android.tv.settings.partnercustomizer.picture.PictureActivity}\n")
        if cmd.startswith("echo @model"):
            return LIVE_STATUS
        if cmd.startswith("am start"):
            return "Starting: Intent { cmp=x }"
        if cmd.startswith("screencap"):
            return f"rc={self.screencap_rc}\n"
        if cmd.startswith("settings put global "):
            _, _, _, name, value = cmd.split()
            if self.settings_writable:
                self.settings[name] = value
            return ""
        if cmd.startswith("settings get global "):
            return self.settings.get(cmd.split()[3], "null") + "\n"
        return ""

    def pull(self, path: str, timeout_s: float) -> bytes:
        self.log.append(("pull", path))
        return self.png

    def close(self) -> None:
        pass

    def keys(self) -> list[int]:
        return [int(c.split()[2]) for kind, c in self.log
                if kind == "shell" and str(c).startswith("input keyevent ")]


def install(monkeypatch, t: FakeTv) -> FakeTv:
    monkeypatch.setattr(tv, "_adb", lambda: t)
    monkeypatch.setattr(tv, "_sleep", lambda s: t.log.append(("sleep", s)))
    return t


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, FakeTv())


# ---- parsing ---------------------------------------------------------------
class TestParse:
    def test_live_tvcenter_dump_parses_and_compacts_to_nothing(self):
        # The HDMI screen is all layout: no text, nothing focusable.
        xml = tv.extract_hierarchy(LIVE_XML)
        assert xml is not None and xml.startswith("<hierarchy")
        assert tv.parse_ui(xml) == []
        assert tv.focused_row(tv.parse_ui(xml)) is None

    def test_live_dump_survives_surrounding_shell_noise(self):
        noisy = "UI hierchary dumped to: /sdcard/x.xml\r\n" + LIVE_XML + "\r\n"
        assert tv.extract_hierarchy(noisy) == tv.extract_hierarchy(LIVE_XML)

    def test_failed_dump_has_no_hierarchy(self):
        assert tv.extract_hierarchy("ERROR: could not get idle state") is None

    def test_focused_row_carries_its_childrens_text(self):
        rows = tv.parse_ui(tv.extract_hierarchy(LIST_XML))
        f = tv.focused_row(rows)
        assert f == {"cls": "LinearLayout", "bounds": [1180, 160, 1920, 270],
                     "focused": True, "focusable": True, "clickable": True,
                     "label": ["Picture mode", "Standard"]}

    def test_rows_are_compact_and_in_document_order(self):
        rows = tv.parse_ui(tv.extract_hierarchy(LIST_XML))
        title = rows[0]
        assert title == {"text": "Picture", "id": "decor_title", "cls": "TextView",
                         "bounds": [1230, 60, 1870, 130]}
        # No false flags, no empty strings anywhere.
        for r in rows:
            assert all(v not in ("", False, None) for v in r.values())
        assert [r["label"][0] for r in rows if "label" in r] == [
            "Picture mode", "Backlight", "Brightness", "Contrast",
            "Advanced settings", "Reset to default"]

    def test_oversized_and_malformed_input_raise_for_the_caller_to_report(self):
        with pytest.raises(ValueError):
            tv.parse_ui("<hierarchy>" + " " * tv.XML_MAX_CHARS + "</hierarchy>")
        with pytest.raises(ET.ParseError):
            tv.parse_ui("<hierarchy><node></hierarchy>")

    def test_status_fields_from_live_output(self):
        s = tv.parse_status(LIVE_STATUS)
        assert s == {"model": "MiTV-MSSP3", "android": "9", "boot_reason": "kernel_panic",
                     "uptime_s": 2360.4, "picture_mode": "11", "backlight": "50", "screen": "on",
                     "window": "com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity",
                     "focused_app": "com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity",
                     "tv_input_sessions": ["com.mediatek.tvinput/.hdmi.HDMIInputService/HW2"]}

    def test_status_null_settings_become_none(self):
        s = tv.parse_status("@picture_mode\nnull\n@backlight\n\n@uptime\ngarbage\n")
        assert s["picture_mode"] is None and s["backlight"] is None and s["uptime_s"] is None
        assert s["screen"] == "unknown"


# ---- danger list -----------------------------------------------------------
class TestDanger:
    def test_list_holds_every_owner_term(self):
        assert set(tv.DANGER_TERMS) == {
            "factory reset", "reset", "erase", "remove account", "oem unlocking",
            "revoke usb debugging", "factory menu", "format", "wipe"}

    @pytest.mark.parametrize("text, term", [
        ("Factory reset", "factory reset"),
        ("Factory data reset", "reset"),
        ("Reset to default", "reset"),
        ("RESET", "reset"),
        ("Erase everything", "erase"),
        ("Remove account", "remove account"),
        ("OEM unlocking", "oem unlocking"),
        ("Revoke USB debugging authorizations", "revoke usb debugging"),
        ("Factory menu", "factory menu"),
        ("Format as device storage", "format"),
        ("Wipe data", "wipe"),
        ("FactoryResetActivity", "factory reset"),
        ("factory_reset", "factory reset"),
        ("com.android.tv.settings/.device.storage.ResetActivity", "reset"),
    ])
    def test_dangerous_text_is_caught(self, text, term):
        assert tv.danger_hit(text) == term

    @pytest.mark.parametrize("text", [
        "Picture preset", "Device information", "Picture mode", "Backlight",
        "Advanced settings", "Contrast", "", "com.mediatek.wwtv.tvcenter",
        "com.android.tv.settings/.partnercustomizer.picture.PictureActivity",
    ])
    def test_ordinary_text_passes(self, text):
        assert tv.danger_hit(text) is None

    def test_any_of_several_texts_trips_it(self):
        assert tv.danger_hit("Advanced", "", "Wipe cache") == "wipe"


# ---- select refusal --------------------------------------------------------
class TestSelect:
    def test_dangerous_target_is_refused_before_anything_is_sent(self, fake):
        out = tv.select("Reset to default")
        assert out["ok"] is False and out["error"] == "danger_refused"
        assert out["matched"] == "reset" and out["pressed"] is False
        assert fake.log == []

    def test_benign_target_landing_on_dangerous_focus_is_refused(self, fake, capsys):
        # "default" is harmless alone but only matches the reset row.
        out = tv.select("default")
        assert out["ok"] is False and out["error"] == "danger_refused"
        assert out["focused"]["label"] == ["Reset to default"]
        assert 23 not in fake.keys()
        assert fake.keys() == [20] * 5
        assert "workman.tv event=select_refused" in capsys.readouterr().err

    def test_dangerous_resource_id_on_the_focused_row_is_refused(self, monkeypatch):
        bad, n = re.subn(r'resource-id=""(?=[^>]*bounds="\[1180,490\]\[1920,600\]")',
                         'resource-id="com.x:id/wipe_all"', LIST_XML)
        assert n == 1
        t = install(monkeypatch, FakeTv(xml=bad))
        out = tv.select("Contrast")
        assert out["error"] == "danger_refused" and out["matched"] == "wipe"
        assert 23 not in t.keys()

    def test_benign_select_presses_center_once_after_navigation(self, fake):
        out = tv.select("Contrast")
        assert out["ok"] is True and out["pressed"] == "DPAD_CENTER"
        assert out["steps"] == ["DPAD_DOWN"] * 3
        assert fake.keys() == [20, 20, 20, 23]

    def test_unreachable_target_presses_nothing_final(self, fake):
        out = tv.select("Sound", max_steps=40)
        assert out["ok"] is False and out["pressed"] is False
        assert 23 not in fake.keys()


# ---- keys ------------------------------------------------------------------
class TestKeys:
    def test_one_key_per_shell_call_with_spacing_between(self, fake):
        out = tv.key(["UP", "dpad_down", "KEYCODE_DPAD_LEFT"], spacing_s=0)
        assert out["ok"] is True and out["sent"] == ["DPAD_UP", "DPAD_DOWN", "DPAD_LEFT"]
        assert out["spacing_s"] == tv.MIN_SPACING_S
        key_cmds = [c for k, c in fake.log if k == "shell" and c.startswith("input keyevent")]
        assert key_cmds == ["input keyevent 19", "input keyevent 20", "input keyevent 21"]
        # Between every pair of key presses there is a sleep of at least the minimum.
        idx = [i for i, (k, c) in enumerate(fake.log)
               if k == "shell" and c.startswith("input keyevent")]
        for a, b in zip(idx, idx[1:]):
            gaps = [s for k, s in fake.log[a + 1:b] if k == "sleep"]
            assert gaps and min(gaps) >= tv.MIN_SPACING_S

    def test_spacing_is_clamped(self):
        assert tv.clamp_spacing(0) == tv.MIN_SPACING_S
        assert tv.clamp_spacing(99) == tv.MAX_SPACING_S
        assert tv.clamp_spacing("x") == tv.MIN_SPACING_S
        assert tv.clamp_spacing(float("nan")) == tv.MIN_SPACING_S
        assert tv.clamp_spacing(0.5) == 0.5

    def test_returns_the_new_focus(self, fake):
        out = tv.key(["DOWN"])
        assert out["focused"]["label"] == ["Backlight", "50"]
        assert out["window"].endswith("PictureActivity")

    def test_unknown_key_sends_nothing(self, fake):
        out = tv.key(["DOWN", "rm -rf /"])
        assert out["ok"] is False and out["error"] == "unknown_keys"
        assert fake.log == []

    def test_too_many_keys_sends_nothing(self, fake):
        out = tv.key(["DOWN"] * (tv.MAX_KEYS + 1))
        assert out["error"] == "too_many_keys" and fake.log == []

    def test_center_on_dangerous_focus_is_refused(self, fake):
        fake.focus = 5  # "Reset to default"
        out = tv.key(["OK"])
        assert out["ok"] is False and out["error"] == "danger_refused"
        assert out["sent"] == [] and 23 not in fake.keys()

    def test_center_after_moves_checks_the_focus_it_reached(self, fake):
        fake.focus = 3
        out = tv.key(["DOWN", "DOWN", "DPAD_CENTER"])
        assert out["error"] == "danger_refused"
        assert out["sent"] == ["DPAD_DOWN", "DPAD_DOWN"] and fake.keys() == [20, 20]

    def test_center_is_refused_when_focus_cannot_be_read(self, fake):
        fake.dump_ok = False
        out = tv.key(["ENTER"])
        assert out["error"] == "select_unverified" and fake.keys() == []

    def test_center_on_benign_focus_goes_through(self, fake):
        out = tv.key(["DPAD_CENTER"])
        assert out["ok"] is True and fake.keys() == [23]

    @pytest.mark.parametrize("name", ["POWER", "KEYCODE_POWER", "SLEEP"])
    def test_power_and_sleep_are_not_keys(self, fake, name):
        # Real power-off drops Wi-Fi for good; it is only reachable via power_off.
        out = tv.key(["DOWN", name])
        assert out["error"] == "unknown_keys" and fake.log == []
        assert "POWER" not in tv.KEYCODES and "SLEEP" not in tv.KEYCODES

    def test_normalize_keys(self):
        assert tv.normalize_keys(["ok", "Mute", "keycode_home", "volume-up"]) == (
            ["DPAD_CENTER", "VOLUME_MUTE", "HOME", "VOLUME_UP"], [])
        assert tv.normalize_keys(["NOPE"]) == ([], ["NOPE"])


class TestRemoteFallback:
    def test_keys_go_over_the_remote_when_adb_is_down(self, monkeypatch):
        sent = []
        monkeypatch.setattr(tv, "_send_keys_remote",
                            lambda codes, spacing: sent.append((codes, spacing)) or
                            {"is_on": True, "current_app": "com.x"})
        out = tv.key(["HOME", "VOLUME_DOWN"])
        assert out["ok"] is True and out["transport"] == "remote"
        assert sent == [([3, 25], tv.MIN_SPACING_S)]
        assert out["focused"] is None and "never reach the TV" in out["adb_error"]

    def test_select_keys_are_refused_over_the_remote(self, monkeypatch):
        sent = []
        monkeypatch.setattr(tv, "_send_keys_remote", lambda *a: sent.append(a) or {})
        out = tv.key(["DOWN", "OK"])
        assert out["error"] == "select_unverified" and sent == []

    def test_remote_failure_is_reported_not_raised(self):
        out = tv.key(["HOME"])  # conftest blocks both transports
        assert out["ok"] is False and out["sent"] == []


# ---- navigation ------------------------------------------------------------
def _row(b):
    return {"bounds": b, "focusable": True}


class TestNextKey:
    F = [100, 100, 200, 200]

    @pytest.mark.parametrize("target, key", [
        ([100, 300, 200, 400], "DPAD_DOWN"),
        ([100, 0, 200, 50], "DPAD_UP"),
        ([300, 100, 400, 200], "DPAD_RIGHT"),
        ([0, 100, 50, 200], "DPAD_LEFT"),
        ([100, 200, 200, 300], "DPAD_DOWN"),        # touching edge counts as below
        ([210, 900, 300, 1000], "DPAD_DOWN"),       # far below, a little right
    ])
    def test_steers_toward_the_target(self, target, key):
        assert tv.next_key(_row(self.F), _row(target)) == key

    def test_falls_back_to_the_other_axis_when_one_is_stuck(self):
        t = _row([210, 900, 300, 1000])
        assert tv.next_key(_row(self.F), t, tried={"DPAD_DOWN"}) == "DPAD_RIGHT"
        assert tv.next_key(_row(self.F), t, tried={"DPAD_DOWN", "DPAD_RIGHT"}) is None

    def test_explores_without_a_target(self):
        assert tv.next_key(_row(self.F), None) == "DPAD_DOWN"
        assert tv.next_key(None, None, explore="DPAD_UP") == "DPAD_UP"
        assert tv.next_key(_row(self.F), None, tried={"DPAD_DOWN"}) is None

    def test_never_returns_a_select_key(self):
        boxes = [[x, y, x + 50, y + 50] for x in (0, 100, 300) for y in (0, 100, 300)]
        for f in boxes:
            for t in boxes:
                for tried in (set(), {"DPAD_DOWN"}, {"DPAD_DOWN", "DPAD_RIGHT"}):
                    k = tv.next_key(_row(f), _row(t), tried=tried)
                    assert k is None or k in tv.NAV_KEYS


class TestFocusText:
    def test_walks_down_to_the_row(self, fake):
        out = tv.focus_text("contrast")
        assert out["ok"] is True and out["match"] == "exact"
        assert out["steps"] == ["DPAD_DOWN"] * 3 and fake.keys() == [20, 20, 20]

    def test_walks_up_too(self, fake):
        fake.focus = 4
        out = tv.focus_text("Backlight")
        assert out["ok"] is True and out["steps"] == ["DPAD_UP"] * 3

    def test_already_focused_presses_nothing(self, fake):
        out = tv.focus_text("Picture mode")
        assert out["ok"] is True and out["steps"] == [] and fake.keys() == []

    def test_exact_beats_substring(self, fake):
        # "Picture" is the page title (exact, not focusable); the focused row
        # only contains it, so focus_text must not stop on "Picture mode".
        out = tv.focus_text("picture")
        assert out["ok"] is False
        assert out["target_visible"] is True

    def test_missing_text_explores_both_ways_then_stops(self, fake):
        out = tv.focus_text("Sound", max_steps=40)
        assert out["ok"] is False and out["error"] == "not_found"
        # 5 down + 1 stuck, 5 up + 1 stuck: the whole list, once each way.
        assert fake.keys() == [20] * 6 + [19] * 6
        assert 23 not in fake.keys()

    def test_max_steps_bounds_the_presses(self, fake):
        out = tv.focus_text("Sound", max_steps=3)
        assert out["error"] == "not_focused" and out["reason"] == "max_steps"
        assert len(fake.keys()) == 3

    def test_max_steps_is_capped_on_an_endless_carousel(self, monkeypatch):
        t = install(monkeypatch, FakeTv(wrap=True))
        out = tv.focus_text("Sound", max_steps=10_000)
        assert out["error"] == "not_focused" and out["reason"] == "max_steps"
        assert len(t.keys()) == tv.MAX_STEPS and 23 not in t.keys()

    def test_bad_text_is_rejected_without_touching_the_tv(self, fake):
        assert tv.focus_text("")["error"] == "bad_text"
        assert tv.focus_text("x" * (tv.MAX_TARGET_LEN + 1))["error"] == "bad_text"
        assert fake.log == []

    def test_dump_failure_is_reported_with_uiautomators_reason(self, fake):
        fake.dump_ok = False
        out = tv.focus_text("Contrast")
        assert out["ok"] is False and out["error"] == "ui_dump_failed"
        assert "could not get idle state" in out["detail"]
        dumps = [c for k, c in fake.log if k == "shell" and "uiautomator dump" in c]
        assert len(dumps) == tv.DUMP_ATTEMPTS

    def test_one_missed_dump_is_retried_after_a_pause(self, fake):
        fake.fail_dumps = 1
        out = tv.elements()
        assert out["ok"] is True and out["count"] == 17
        assert ("sleep", tv.DUMP_BACKOFF_S) in fake.log


# ---- launch, status, screenshot, elements ----------------------------------
class TestLaunch:
    @pytest.mark.parametrize("target, parsed", [
        ("com.mediatek.wwtv.tvcenter", ("com.mediatek.wwtv.tvcenter", None)),
        ("com.android.tv.settings/.partnercustomizer.picture.PictureActivity",
         ("com.android.tv.settings", ".partnercustomizer.picture.PictureActivity")),
        ("  com.a.b/com.a.b.Main  ", ("com.a.b", "com.a.b.Main")),
    ])
    def test_parse_valid(self, target, parsed):
        assert tv.parse_launch_target(target) == parsed

    @pytest.mark.parametrize("target", [
        "", "settings", "com.a.b; reboot", "com.a.b/$(id)", "com.a.b/.Main extra",
        "com.a.b/.Main\nreboot", "../etc", "com.a.b/",
    ])
    def test_parse_rejects_anything_a_shell_would_read(self, target):
        assert tv.parse_launch_target(target) is None

    def test_bad_target_sends_nothing(self, fake):
        assert tv.launch("com.a.b; reboot")["error"] == "bad_target"
        assert fake.log == []

    def test_dangerous_activity_is_refused(self, fake):
        out = tv.launch("com.android.tv.settings/.device.storage.ResetActivity")
        assert out["error"] == "danger_refused" and fake.log == []

    def test_package_resolves_its_tv_launcher(self, fake):
        fake.resolve = ("priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n"
                        "com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity\n")
        out = tv.input_hdmi()
        assert out["ok"] is True
        assert out["component"] == "com.mediatek.wwtv.tvcenter/com.mitv.livetv.nav.TurnkeyUiMainActivity"
        assert ("shell", "am start -n " + out["component"]) in fake.log

    def test_package_without_launcher_is_an_error(self, fake):
        out = tv.launch("com.example.none")
        assert out["error"] == "no_launcher"
        assert not any(str(c).startswith("am start") for _, c in fake.log)


class TestReadTools:
    def test_status_parses_and_reports_transports(self, fake, monkeypatch, tmp_path):
        (tmp_path / "adbkey").write_text("PRIVATE-KEY-MATERIAL")
        (tmp_path / "adbkey.pub").write_text("PUBLIC-KEY-MATERIAL")
        monkeypatch.setenv("WORKMAN_TV_KEYDIR", str(tmp_path))
        out = tv.status()
        assert out["ok"] is True and out["model"] == "MiTV-MSSP3"
        assert out["boot_reason"] == "kernel_panic"
        assert out["reachable"] == {"adb": False, "remote": False}
        assert out["keys_present"] == {"adb": True, "remote": False}
        assert "KEY-MATERIAL" not in str(out)

    def test_elements_compact_with_focus(self, fake):
        out = tv.elements(limit=3)
        # Title + 6 focusable rows + the 10 TextViews inside them.
        assert out["ok"] is True and out["count"] == 17 and out["truncated"] is True
        assert len(out["elements"]) == 3
        assert out["focused"]["label"] == ["Picture mode", "Standard"]

    def test_screenshot_pulls_and_cleans_up(self, fake):
        out = tv.screenshot()
        assert out["ok"] is True and out["png"] == fake.png
        cmds = [c for k, c in fake.log if k == "shell"]
        assert cmds[0].startswith("screencap -p ") and cmds[-1].startswith("rm -f ")

    def test_screenshot_rejects_non_png(self, fake):
        fake.png = b"not a png"
        assert tv.screenshot()["error"] == "screencap_failed"

    def test_hdmi_screen_capture_failure_is_named_not_blank(self, fake):
        # Measured live: screencap exits 1 with a 0-byte file on the HDMI input.
        fake.screencap_rc = 1
        out = tv.screenshot()
        assert out["ok"] is False and out["error"] == "screencap_failed"
        assert "exited 1" in out["detail"] and out["window"].endswith("PictureActivity")
        assert not any(kind == "pull" for kind, _ in fake.log)
        assert [c for k, c in fake.log if k == "shell"][1].startswith("rm -f ")


class TestNeverRaises:
    """With both transports blocked (conftest), every entry point answers."""

    @pytest.mark.parametrize("call", [
        lambda: tv.status(), lambda: tv.elements(), lambda: tv.screenshot(),
        lambda: tv.key(["HOME"]), lambda: tv.focus_text("Contrast"),
        lambda: tv.select("Contrast"), lambda: tv.launch("com.a.b"),
        lambda: tv.input_hdmi(), lambda: tv.screen("status"), lambda: tv.screen("off"),
        lambda: tv.power_off(confirm=True),
    ])
    def test_returns_an_error_dict(self, call):
        out = call()
        assert isinstance(out, dict) and out["ok"] is False and out.get("error")

    def test_missing_key_never_generates_one(self, monkeypatch, tmp_path):
        # Stub client modules: the real connect path runs up to the key check,
        # and a device object is never built.
        built = []
        device_mod = types.ModuleType("adb_shell.adb_device")
        device_mod.AdbDeviceTcp = lambda *a, **k: built.append(a)
        signer_mod = types.ModuleType("adb_shell.auth.sign_pythonrsa")
        signer_mod.PythonRSASigner = lambda *a: object()
        for name, mod in (("adb_shell", types.ModuleType("adb_shell")),
                          ("adb_shell.auth", types.ModuleType("adb_shell.auth")),
                          ("adb_shell.adb_device", device_mod),
                          ("adb_shell.auth.sign_pythonrsa", signer_mod)):
            monkeypatch.setitem(sys.modules, name, mod)
        keydir = tmp_path / "tv-keys"
        keydir.mkdir()
        monkeypatch.setenv("WORKMAN_TV_KEYDIR", str(keydir))
        with pytest.raises(tv.TvError) as exc:
            REAL_CONNECT()
        assert exc.value.code == "adb_key_missing"
        assert built == [] and list(keydir.iterdir()) == []


# ---- screen and power ------------------------------------------------------
class TestScreen:
    def test_state_mapping_is_inverted_on_purpose(self):
        assert tv.screen_state("1") == "on" and tv.screen_state("0") == "off"
        assert tv.screen_state("null") == "unknown" and tv.screen_state(None) == "unknown"

    def test_status_only_reads(self, fake):
        assert tv.screen("status") == {"ok": True, "screen": "on"}
        assert not any("settings put" in str(c) for _, c in fake.log)

    @pytest.mark.parametrize("state, value", [("off", "0"), ("ON", "1")])
    def test_switch_writes_the_setting_and_reads_it_back(self, fake, state, value):
        out = tv.screen(state)
        assert out == {"ok": True, "screen": state.lower()}
        cmds = [c for k, c in fake.log if k == "shell"]
        assert cmds == [f"settings put global power_picture_off {value}",
                        "settings get global power_picture_off"]

    def test_write_that_does_not_stick_is_reported(self, fake):
        fake.settings_writable = False
        out = tv.screen("off")
        assert out["ok"] is False and out["error"] == "screen_not_applied"
        assert out["screen"] == "on"

    def test_bad_state_touches_nothing(self, fake):
        assert tv.screen("dim")["error"] == "bad_state" and fake.log == []


class TestPowerOff:
    def test_without_confirm_nothing_is_sent(self, fake):
        out = tv.power_off()
        assert out["ok"] is False and out["error"] == "confirm_required"
        assert "physical remote" in out["warning"] and fake.log == []
        assert tv.power_off(confirm="yes")["error"] == "confirm_required"

    def test_confirmed_sends_power_once_over_adb(self, fake):
        out = tv.power_off(confirm=True)
        assert out["ok"] is True and out["transport"] == "adb"
        assert fake.keys() == [tv.POWER_KEYCODE] and "physical remote" in out["warning"]

    def test_falls_back_to_the_remote_when_adb_is_down(self, monkeypatch):
        sent = []
        monkeypatch.setattr(tv, "_send_keys_remote", lambda codes, spacing: sent.append(codes) or {})
        out = tv.power_off(confirm=True)
        assert out["ok"] is True and out["transport"] == "remote" and sent == [[26]]

    def test_both_transports_down_is_an_error(self):
        out = tv.power_off(confirm=True)
        assert out["ok"] is False and out["pressed"] is False


# ---- server registration ---------------------------------------------------
class TestServer:
    def test_tv_tools_are_registered_and_no_shell_tool(self):
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert {"tv_status", "tv_screenshot", "tv_elements", "tv_key", "tv_focus_text",
                "tv_select", "tv_launch", "tv_input_hdmi", "tv_screen", "tv_power_off"} <= names
        assert not any(re.search(r"tv_.*shell", n) for n in names)

    def test_screenshot_is_downscaled_without_touching_desktop_view(self, monkeypatch):
        PIL = pytest.importorskip("PIL.Image")
        buf = io.BytesIO()
        PIL.new("RGB", (1920, 1080), "navy").save(buf, "PNG")

        class FakeModule:
            @staticmethod
            def screenshot():
                return {"ok": True, "png": buf.getvalue(), "bytes": len(buf.getvalue())}

        monkeypatch.setattr(server, "tv", FakeModule)
        before = dict(server._LAST_VIEW)
        meta, image = server.tv_screenshot()
        assert meta["ok"] is True and meta["resized"] is True and meta["screen"] == [1920, 1080]
        assert max(meta["view"]) <= 1568 and "png" not in meta
        assert image.data[:4] == b"\x89PNG"
        assert server._LAST_VIEW == before

    def test_screenshot_error_passes_through(self):
        out = server.tv_screenshot()
        assert len(out) == 1 and out[0]["ok"] is False

    def test_power_off_tool_defaults_to_the_warning(self):
        out = server.tv_power_off()
        assert out["error"] == "confirm_required" and "physical remote" in out["warning"]

    def test_unexpected_exception_becomes_an_answer(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("bug")
        monkeypatch.setattr(tv, "status", boom)
        monkeypatch.setattr(tv, "screenshot", boom)
        assert server.tv_status() == {"ok": False, "error": "tv_failed", "detail": "RuntimeError: bug"}
        assert server.tv_screenshot()[0]["error"] == "tv_failed"

    def test_unavailable_module_answers_instead_of_breaking(self, monkeypatch):
        monkeypatch.setattr(server, "tv", server._Unavailable("tv", ImportError("nope")))
        assert server.tv_status()["error"] == "module_unavailable"
        assert server.tv_screenshot()[0]["error"] == "module_unavailable"
