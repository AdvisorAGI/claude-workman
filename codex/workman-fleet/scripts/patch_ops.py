"""Scoped fixes inside the permission-holding Workman.app operation module.

AXFrontmost uses the existing human Accessibility grant. No privacy writes or
SSH-process input fallback. Drag always releases its own pressed mouse button.
"""
import hashlib, json, os, sys, time
from pathlib import Path

FOCUS = '    ok = bool(ra and ra.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))\n'
FOCUS_NEW = FOCUS + '''    # Workman fleet v1.1: request actual focus using the existing AX grant.
    if AX.AXIsProcessTrusted():
        AX.AXUIElementSetAttributeValue(AX.AXUIElementCreateApplication(pid), "AXFrontmost", True)
'''
DRAG = '''    _motion.press(*start)
    time.sleep(0.05)
    dragged = _motion.glide(x2, y2, duration_ms=duration_ms, humanize=hm, dragging=True)
    end = _motion.clamp_to_display(float(x2), float(y2))
    time.sleep(0.03)
    _motion.release(*end)
'''
DRAG_NEW = '''    _motion.press(*start)
    # Workman fleet v1.1: release even when motion fails.
    try:
        time.sleep(0.05)
        dragged = _motion.glide(x2, y2, duration_ms=duration_ms, humanize=hm, dragging=True)
        time.sleep(0.03)
    finally:
        _motion.release(*_motion.cursor_pos())
'''
KEY_FLAGS = '''    if flags:
        Quartz.CGEventSetFlags(ev, flags)
'''
KEY_FLAGS_NEW = '''    # Workman fleet v1.1: explicit flags, including zero, prevent inheritance.
    Quartz.CGEventSetFlags(ev, flags)
'''
UNICODE = '''        Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
'''
UNICODE_NEW = '''        Quartz.CGEventSetFlags(ev, 0)  # Workman fleet v1.1: literal Unicode input.
''' + UNICODE
KEY_PAIR = '''    _post_key(keycode, True, flags)
    time.sleep(_rng.uniform(0.03, 0.07) if _human() else 0.02)
    _post_key(keycode, False, flags)
'''
KEY_PAIR_NEW = '''    _post_key(keycode, True, flags)
    # Workman fleet v1.1: release our key even if its dwell fails.
    try:
        time.sleep(_rng.uniform(0.03, 0.07) if _human() else 0.02)
    finally:
        _post_key(keycode, False, 0)
'''
SETTINGS = '''
def open_permission_settings(grant: str) -> dict:
    """Workman fleet v1.1: one missing grant, approved only by the human."""
    panes = {"screen_recording": "Privacy_ScreenCapture", "accessibility": "Privacy_Accessibility"}
    if grant not in panes:
        raise ValueError("unsupported permission")
    before = permissions()
    if before.get(grant):
        return {"already_granted": True, "opened": False, "permissions": before}
    if grant == "screen_recording":
        Quartz.CGRequestScreenCaptureAccess()
    else:
        AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True})
    from Foundation import NSURL
    url = NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?" + panes[grant])
    opened = bool(NSWorkspace.sharedWorkspace().openURL_(url))
    return {"opened": opened, "permission": grant, "permissions": permissions(), "human_action_required": True}

'''


def patched(text):
    for old, new in [(FOCUS, FOCUS_NEW), (DRAG, DRAG_NEW), (KEY_FLAGS, KEY_FLAGS_NEW), (UNICODE, UNICODE_NEW), (KEY_PAIR, KEY_PAIR_NEW)]:
        if new in text: continue
        if text.count(old) != 1: raise ValueError("unrecognized helper operation; inspect before patching")
        text = text.replace(old, new)
    if 'def open_permission_settings(' not in text:
        marker = '# --------------------------------------------------------------------------- dispatch'
        if text.count(marker) != 1 or text.count('    "request_permissions": request_permissions,') != 1:
            raise ValueError("unrecognized helper dispatch")
        text = text.replace(marker, SETTINGS + marker)
        text = text.replace('    "request_permissions": request_permissions,', '    "request_permissions": request_permissions,\n    "open_permission_settings": open_permission_settings,')
    return text


def main():
    path = Path(sys.argv[1]).resolve() / "atmos_computer/ops.py"
    old = path.read_bytes(); new = patched(old.decode()).encode()
    if new == old: print(json.dumps({"path": str(path), "unchanged": True})); return
    compile(new, str(path), "exec")
    backup = Path.home() / ".claude/tools/workman-fleet-v1.1/backups" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) / "ops.py"
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb") as f: os.chmod(backup, 0o600); f.write(old)
    if backup.read_bytes() != old: raise ValueError("backup verification failed")
    tmp = path.with_suffix(".fleet-new"); tmp.write_bytes(new); os.chmod(tmp, path.stat().st_mode & 0o777); os.replace(tmp, path)
    if path.read_bytes() != new: raise ValueError("installed verification failed")
    print(json.dumps({"path": str(path), "backup": str(backup), "previous_sha256": hashlib.sha256(old).hexdigest(), "sha256": hashlib.sha256(new).hexdigest()}))


if __name__ == "__main__": main()
