"""L1: live humanness run on Xvfb :99 only (throwaway chromium, no debug port).

Launches chromium on :99 with a fresh mktemp profile, drives the fixture with
Human Mode (seed 7), reads the verdict off the window title, then kills only
that chromium and removes only that profile dir.
"""
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

DISPLAY = ":99"
os.environ.update(WORKMAN_DISPLAY=DISPLAY, DISPLAY=DISPLAY,
                  WORKMAN_LEARN_ROOT="/tmp/workman-learn-99")
os.environ.pop("WAYLAND_DISPLAY", None)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from PIL import Image, ImageChops  # noqa: E402

from workman import human, x11, xtest  # noqa: E402

FIXTURE = "file://" + os.path.join(REPO, "tests", "fixtures", "humanness.html")
TARGET, FIELD, SUMMARISE = (0xcf, 0xe8, 0xff), (0xff, 0xf2, 0xcc), (0xd9, 0xf7, 0xbe)

# Hard guard: every input path must be :99, never the owner's :0.
assert x11.DISPLAY == DISPLAY, x11.DISPLAY
ch = xtest.channel(DISPLAY)
assert ch is not None and ch.display_name == DISPLAY, "no :99 channel"
xt = x11._xt()
assert xt is not None and xt.display_name == DISPLAY, "x11 channel not on :99"

# Snap chromium has a private /tmp; a dir under its common dir is the same
# path on both sides, so the throwaway profile is real and removable.
base = os.path.expanduser("~/snap/chromium/common")
tmp = tempfile.mkdtemp(prefix="workman-l1-", dir=base if os.path.isdir(base) else None)


def ours():
    found = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                cmd = fh.read().split(b"\0")
        except OSError:
            continue
        if any(tmp.encode() in a for a in cmd):
            found.append((int(d), cmd))
    return found


def browser_pid():
    for pid, cmd in ours():
        if not any(a.startswith(b"--type=") for a in cmd) and b"chrome" in cmd[0]:
            return pid
    return None


def bbox(img, rgb, tol=3, min_run=40):
    """The largest solid block of this colour. A plain getbbox() is wrong here:
    subpixel-antialiased text leaves stray pixels of the same colour far away."""
    bands = [b.point(lambda v, c=c: 255 if abs(v - c) <= tol else 0)
             for b, c in zip(img.split(), rgb)]
    m = ImageChops.multiply(ImageChops.multiply(bands[0], bands[1]), bands[2])
    W, H = m.size
    rows = [m.crop((0, y, W, y + 1)).histogram()[255] for y in range(H)]
    best, start = (0, 0), None
    for y, n in enumerate(rows + [0]):
        if n >= min_run:
            start = y if start is None else start
        elif start is not None:
            best = max(best, (start, y), key=lambda r: r[1] - r[0])
            start = None
    y0, y1 = best
    if y1 - y0 < 10:
        return None
    band = m.crop((0, y0, W, y1))
    cols = [band.crop((x, 0, x + 1, y1 - y0)).histogram()[255] for x in range(W)]
    xs = [x for x, n in enumerate(cols) if n >= (y1 - y0) // 2]
    return (xs[0], y0, xs[-1] + 1, y1) if xs else None


def xdo(*args):
    return subprocess.run(["xdotool", *args], env={**os.environ, "DISPLAY": DISPLAY},
                          capture_output=True, text=True, timeout=10).stdout.strip()


env = dict(os.environ)
log = open("/tmp/l1-chromium.log", "wb")
proc = subprocess.Popen(
    ["chromium-browser", f"--user-data-dir={tmp}", "--no-first-run", "--no-default-browser-check",
     "--password-store=basic", "--ozone-platform=x11", "--window-size=1400,900",
     "--window-position=0,0", FIXTURE],
    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
    start_new_session=True)
report = {"profile_dir": tmp, "popen_pid": proc.pid}
try:
    deadline = time.time() + 60
    wid = ""
    while time.time() < deadline and not wid:
        wid = xdo("search", "--name", "workman humanness detector").split("\n")[0]
        if not wid:
            time.sleep(0.5)
    if not wid:
        raise SystemExit("chromium window never appeared on :99")
    report["browser_pid"] = browser_pid()
    report["window"] = wid
    time.sleep(2.0)  # first paint
    img = Image.open(io.BytesIO(ch.screenshot(compress_level=1))).convert("RGB")
    boxes = {"target": bbox(img, TARGET), "field": bbox(img, FIELD), "summarise": bbox(img, SUMMARISE)}
    report["boxes"] = boxes
    if not all(boxes.values()):
        img.save("/tmp/l1-shot.png")
        raise SystemExit(f"colour boxes not found: {boxes}")

    def aim(b):
        return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2, float(min(b[2] - b[0], b[3] - b[1]))

    human.set_mode(True, seed=7)
    steps = {}
    x, y, w = aim(boxes["target"])
    steps["click_target"] = human.human_click(x, y, target_px=w)
    x, y, w = aim(boxes["field"])
    steps["click_field"] = human.human_click(x, y, target_px=w)
    steps["type"] = human.human_type("Hello World, 42!")
    fb = boxes["field"]
    with open("/tmp/l1-field.png", "wb") as fh:  # proof of where the text went
        fh.write(ch.screenshot(region=(max(0, fb[0] - 120), fb[1] - 8, fb[2] - fb[0] + 140,
                                       fb[3] - fb[1] + 16), compress_level=6))
    x, y, w = aim(boxes["summarise"])
    steps["click_summarise"] = human.human_click(x, y, target_px=w)
    report["steps"] = {k: {kk: v.get(kk) for kk in ("ok", "points", "duration_ms", "press_ms",
                                                   "typed_len", "error")} for k, v in steps.items()}
    time.sleep(1.0)
    title = (ch.active_window_info() or {}).get("name", "")
    if not title.startswith(("HUMAN", "SCRIPTED")):
        title = xdo("getwindowname", wid)
    report["title_head"] = title[:12]
    verdict = json.loads(title[title.index("{"):title.rindex("}") + 1])
    report["verdict_word"] = title.split(" ", 1)[0]
    report["verdict"] = verdict
    ch_ = verdict.get("click_hold_ms") or {}
    kd = verdict.get("key_dwell_ms") or {}
    report["acceptance"] = {
        "title_HUMAN": report["verdict_word"] == "HUMAN",
        "events_untrusted_0": verdict.get("events_untrusted") == 0,
        "teleports_0": verdict.get("pointer_teleports_over_80px") == 0,
        "capitals_with_shift": verdict.get("capitals_with_shift") == verdict.get("capitals"),
        "empty_code_0": verdict.get("keys_with_empty_code") == 0,
        "click_hold_60_140": bool(ch_) and ch_.get("min", 0) >= 60 and ch_.get("max", 999) <= 140,
        "key_dwell_min_ge_35": bool(kd) and kd.get("min", 0) >= 35,
    }
finally:
    killed = []
    for pid in {browser_pid(), proc.pid} - {None}:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    end = time.time() + 15
    while ours() and time.time() < end:
        time.sleep(0.3)
    left = [p for p, _ in ours()]
    for pid in left:  # only processes carrying this run's unique profile path
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    report["cleanup"] = {"sigterm": killed, "sigkill_leftovers": left,
                         "profile_removed": not os.path.exists(tmp)}
    print(json.dumps(report, default=str))
