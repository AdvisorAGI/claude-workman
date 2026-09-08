import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import device, motion_profile, input_switch


def test_speed_scales_duration_without_changing_target():
    assert motion_profile.duration_ms((0, 0), (400, 300), 2) == pytest.approx(motion_profile.duration_ms((0, 0), (400, 300), 1) / 2, abs=1)


def test_documented_presets_are_small_copies_and_keep_direct_default():
    assert motion_profile.preset("responsive") == {"motion": "human", "speed": 1.5}
    selected = motion_profile.preset("responsive"); selected["speed"] = 4
    assert motion_profile.preset("responsive")["speed"] == 1.5
    assert motion_profile.preset("direct")["motion"] == "direct"
    with pytest.raises(ValueError):
        motion_profile.preset("undetectable")


def test_linux_smooth_move_removes_endpoint_jitter(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path))
    posted = []
    result = motion_profile.linux_move((0, 0), (100, 100), 1, lambda x, y: posted.append((x,y)),
                                      planner=lambda *a: [(0,0,0),(103,98,0)])
    assert posted[-1] == (100,100) and result["at"] == [100,100]


def test_stop_interrupts_linux_path_before_next_input(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path))
    posted = []
    def post(x,y): posted.append((x,y)); input_switch.update(mouse=False)
    with pytest.raises(input_switch.Disabled):
        motion_profile.linux_move((0,0),(100,100),1,post,planner=lambda *a:[(0,0,0),(100,100,1)])
    assert len(posted) == 1


@pytest.mark.parametrize("speed", [0, -1, 5, True, float("nan"), "fast"])
def test_invalid_speed_refused(speed):
    with pytest.raises(device.Refusal):device.validate("move", {"x":1,"y":1,"speed":speed})
