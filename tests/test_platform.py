"""The cross-platform layer.

Every backend is importable from every OS on purpose — that is what lets these
tests check the macOS and Windows contracts from a Linux CI box, and it is why
the OS-specific handles (Quartz, WinDLL) are all loaded lazily inside functions.

What is checked here is what a port actually gets wrong: a missing contract
function, a key table that disagrees with itself, a coordinate conversion that
is off by a factor, and an unsupported capability that raises instead of
reporting.
"""
from __future__ import annotations

import sys
import zlib

import pytest

from workman import desktop
from workman.platform import backend_name, base, load
from workman.platform import darwin, linux_x11, win32

BACKENDS = {"linux_x11": linux_x11, "darwin": darwin, "win32": win32}


class TestContractParity:
    """Every backend answers the same questions, or the facade cannot be honest
    about which one is loaded."""

    @pytest.mark.parametrize("name,module", sorted(BACKENDS.items()))
    def test_required_contract_is_complete(self, name, module):
        missing = [fn for fn in base.CONTRACT
                   if not callable(getattr(module, fn, None))]
        assert not missing, f"{name} is missing {missing}"

    @pytest.mark.parametrize("name,module", sorted(BACKENDS.items()))
    def test_optional_capabilities_are_all_answered(self, name, module):
        """Optional means 'may report unsupported', not 'may be absent' — the
        facade should never have to guess why a call went nowhere."""
        missing = [fn for fn in base.OPTIONAL
                   if not callable(getattr(module, fn, None))]
        assert not missing, f"{name} is missing {missing}"

    @pytest.mark.parametrize("name,module", sorted(BACKENDS.items()))
    def test_platform_info_names_its_backend(self, name, module):
        info = module.platform_info()
        assert info["backend"] == name
        assert "modifier_super" in info, "the model needs to know what super means"

    @pytest.mark.parametrize("name,module,allowed", [
        ("linux_x11", linux_x11, {"xtest", "xdotool"}),
        ("darwin", darwin, {"quartz", "osascript", "cliclick"}),
        ("win32", win32, {"sendinput"}),
    ])
    def test_platform_info_names_its_input_channel(self, name, module, allowed):
        info = module.platform_info()
        assert info["input_channel"] in allowed

    def test_facade_exposes_the_whole_contract(self):
        missing = [fn for fn in base.CONTRACT
                   if not callable(getattr(desktop, fn, None))]
        assert not missing, f"desktop facade is missing {missing}"


class TestBackendSelection:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_BACKEND", "windows")
        assert backend_name() == "win32"

    def test_unknown_override_is_rejected_loudly(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_BACKEND", "beos")
        with pytest.raises(ValueError):
            backend_name()

    @pytest.mark.parametrize("platform,expected", [
        ("linux", "linux_x11"), ("darwin", "darwin"), ("win32", "win32"),
        ("freebsd13", "linux_x11"),
    ])
    def test_auto_selection_follows_sys_platform(self, monkeypatch, platform, expected):
        monkeypatch.delenv("WORKMAN_BACKEND", raising=False)
        monkeypatch.setattr(sys, "platform", platform)
        assert backend_name() == expected

    def test_load_is_cached(self):
        assert load("darwin") is load("mac")


class TestUnsupportedIsAResultNotACrash:
    def test_windows_backend_on_linux_reports_instead_of_raising(self, monkeypatch):
        """The single most important failure mode: an agent asking a backend
        for something the host cannot do must get a readable answer."""
        monkeypatch.setattr("workman.platform.active", lambda: win32)
        monkeypatch.setattr("workman.desktop.active", lambda: win32)
        out = desktop.focus_window("anything")
        assert out["ok"] is False
        assert out["unsupported"] is True
        assert "Windows" in out["error"]

    def test_missing_optional_capability_is_reported(self, monkeypatch):
        class Bare:
            __name__ = "bare"

        monkeypatch.setattr("workman.desktop.active", lambda: Bare())
        out = desktop.workspaces()
        assert out["ok"] is False and out["unsupported"] is True

    def test_workspaces_unsupported_on_mac_and_windows_carries_a_workaround(self):
        for module in (darwin, win32):
            out = module.workspaces()
            assert out["unsupported"] is True
            assert "press_key" in out["hint"]


class TestModifiersAndCombos:
    @pytest.mark.parametrize("raw,expected", [
        (["cmd"], ["super"]), (["Command"], ["super"]), (["option"], ["alt"]),
        (["CTRL", "shift"], ["ctrl", "shift"]), (["win"], ["super"]),
        (["ctrl", "control"], ["ctrl"]),
    ])
    def test_aliases_canonicalise(self, raw, expected):
        canonical, unknown = base.normalize_modifiers(raw)
        assert canonical == expected and unknown == []

    def test_unknown_modifier_is_returned_not_dropped(self):
        canonical, unknown = base.normalize_modifiers(["ctrl", "hyper"])
        assert canonical == ["ctrl"] and unknown == ["hyper"]

    @pytest.mark.parametrize("combo,mods,key", [
        ("Return", [], "Return"),
        ("ctrl+c", ["ctrl"], "c"),
        ("ctrl+shift+t", ["ctrl", "shift"], "t"),
        ("cmd+space", ["super"], "space"),
        ("ctrl+super", ["ctrl"], "super"),
        ("+", [], "+"),
    ])
    def test_split_combo(self, combo, mods, key):
        assert base.split_combo(combo) == (mods, key)

    def test_a_non_modifier_prefix_is_not_treated_as_a_combo(self):
        assert base.split_combo("a+b") == ([], "a+b")


class TestKeyTables:
    """A key table is exactly the kind of thing that is wrong in one entry and
    silently types the wrong character for a year."""

    @pytest.mark.parametrize("name,code", [("Return", 36), ("Tab", 48),
                                           ("Escape", 53), ("a", 0), ("z", 6),
                                           ("KP_0", 82), ("F5", 96)])
    def test_darwin_keycodes(self, name, code):
        assert darwin.keycode_for(name)[0] == code

    def test_darwin_shifted_punctuation_asks_for_shift(self):
        code, needs_shift = darwin.keycode_for("plus")
        assert code == darwin._KEYCODES["equal"] and needs_shift is True

    def test_darwin_capital_letter_is_the_letter_plus_shift(self):
        assert darwin.keycode_for("A") == (0, True)

    def test_darwin_unknown_key_is_none_not_a_wrong_guess(self):
        assert darwin.keycode_for("HyperTurbo") == (None, False)

    @pytest.mark.parametrize("name,vk", [("Return", 0x0D), ("tab", 0x09),
                                         ("escape", 0x1B), ("a", 0x41),
                                         ("f5", 0x74), ("kp_0", 0x60),
                                         ("super", 0x5B)])
    def test_win32_virtual_keys(self, name, vk):
        assert win32.vk_for(name) == vk

    def test_win32_unknown_key_is_none(self):
        assert win32.vk_for("HyperTurbo") is None

    def test_win32_arrows_are_flagged_extended(self):
        """Without KEYEVENTF_EXTENDEDKEY the arrow keys arrive as numpad digits."""
        for arrow in ("left", "right", "up", "down"):
            assert win32.vk_for(arrow) in win32.EXTENDED


class TestWin32Structures:
    def test_input_union_is_big_enough_for_a_mouse_event(self):
        import ctypes

        assert ctypes.sizeof(win32.INPUT) >= ctypes.sizeof(win32.MOUSEINPUT) + 4

    def test_unicode_typing_uses_scan_codes_not_virtual_keys(self):
        inp = win32._key_input(0, unit=ord("é"))
        assert inp.ki.wVk == 0
        assert inp.ki.wScan == ord("é")
        assert inp.ki.dwFlags & win32.KEYEVENTF_UNICODE


class TestImageHelpers:
    def test_png_encode_produces_a_readable_png(self):
        rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])  # 2x2
        data = base.png_encode(2, 2, rgb)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert data[12:16] == b"IHDR"
        assert int.from_bytes(data[16:20], "big") == 2
        assert int.from_bytes(data[20:24], "big") == 2
        assert data[-8:-4] == b"IEND"

    def test_png_idat_round_trips_to_the_original_rows(self):
        rgb = bytes(range(12))
        data = base.png_encode(2, 2, rgb)
        start = data.index(b"IDAT") + 4
        end = len(data) - 12  # trailing CRC + IEND chunk
        raw = zlib.decompress(data[start:end])
        # Each row is prefixed with its filter byte (0 = None).
        assert raw == b"\x00" + rgb[:6] + b"\x00" + rgb[6:]

    def test_png_encode_rejects_a_short_buffer(self):
        with pytest.raises(ValueError):
            base.png_encode(4, 4, b"\x00" * 10)

    def test_bgra_to_rgb_swaps_channels_and_drops_alpha(self):
        bgra = bytes([1, 2, 3, 255, 4, 5, 6, 255])  # two pixels, 2x1
        assert base.bgra_to_rgb(bgra, 2, 1) == bytes([3, 2, 1, 6, 5, 4])

    def test_bgra_bottom_up_flips_rows(self):
        bgra = bytes([1, 1, 1, 0] + [2, 2, 2, 0])  # 1x2, rows differ
        assert base.bgra_to_rgb(bgra, 1, 2, bottom_up=True) == bytes([2, 2, 2, 1, 1, 1])

    def test_downscale_halves_and_samples(self):
        # 2x2 of distinct colours down to a single pixel takes the first sample.
        rgb = bytes([10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40])
        out, w, h = base.downscale_rgb(rgb, 2, 2, max_dim=1)
        assert (w, h) == (1, 1) and out == bytes([10, 10, 10])

    def test_downscale_leaves_an_already_small_image_alone(self):
        rgb = bytes([1, 2, 3])
        assert base.downscale_rgb(rgb, 1, 1, max_dim=64) == (rgb, 1, 1)

    def test_resize_png_returns_input_when_no_budget_is_given(self):
        assert base.resize_png(b"not-a-png", None) == b"not-a-png"


class TestLinuxBackendStillWorks:
    """The port must not have moved the ground under the platform in use."""

    def test_linux_reexports_are_the_original_functions(self):
        from workman import x11

        assert linux_x11.click is x11.click
        assert linux_x11.screenshot is x11.screenshot

    def test_platform_info_reports_tool_availability(self):
        info = linux_x11.platform_info()
        assert set(info["tools"]) >= {"xdotool", "ffmpeg"}
        assert info["display"]

    def test_wayland_session_is_called_out(self, monkeypatch):
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        info = linux_x11.platform_info()
        assert info["wayland"] is True and "XWayland" in info["notes"]
