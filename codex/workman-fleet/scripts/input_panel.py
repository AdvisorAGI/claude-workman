"""Small local Workman on/off window. Run only when the user wants the panel.

Reads only Workman's two switch booleans; it never observes keyboard/mouse events.
Closing this window leaves the chosen switches in effect, including OFF.
"""
import sys
import input_switch


def change(mouse=None, keyboard=None):
    result = input_switch.update(**{k: v for k, v in {"mouse": mouse, "keyboard": keyboard}.items() if v is not None})
    import os, uuid, learning
    node = os.environ.get("WORKMAN_FLEET_NODE")
    if node in learning.NODES:
        learning.append(learning.journal_path(), [{"id": uuid.uuid4().hex, "node": node,
            "action": "input", "ts": learning.now(), "ok": True, "code": "ok",
            "project": "workman", "task": "input-control", "session": "user-control-panel",
            "permissions": {k + "_enabled": v for k, v in result.items()}}])
    return result


def main():
    import fcntl
    p = input_switch.path().with_suffix(".panel-lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    panel_lock = p.open("a")
    try:
        fcntl.flock(panel_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    if sys.platform == "darwin":
        from AppKit import (NSApplication, NSWindow, NSButton, NSTextField, NSObject,
                            NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
                            NSBackingStoreBuffered)
        from Foundation import NSTimer
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(0)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((70, 80), (420, 220)), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, NSBackingStoreBuffered, False)
        win.setTitle_("Workman — Mouse & Keyboard")
        label = NSTextField.labelWithString_("")
        label.setFrame_(((20, 170), (380, 30)))
        win.contentView().addSubview_(label)
        class Controls(NSObject):
            def windowWillClose_(self, notification): app.terminate_(None)
            def stop_(self, sender): change(False, False); self.refresh_(None)
            def start_(self, sender): change(True, True); self.refresh_(None)
            def mouse_(self, sender): change(mouse=not input_switch.state()["mouse"]); self.refresh_(None)
            def keyboard_(self, sender): change(keyboard=not input_switch.state()["keyboard"]); self.refresh_(None)
            def refresh_(self, sender):
                s = input_switch.state()
                label.setStringValue_(f"Mouse: {'ON' if s['mouse'] else 'OFF'}     Keyboard: {'ON' if s['keyboard'] else 'OFF'}")
        controller = Controls.alloc().init()
        win.setDelegate_(controller)
        for title, action, x, y in [("STOP BOTH", "stop:", 20, 115), ("Enable both", "start:", 220, 115), ("Toggle mouse", "mouse:", 20, 60), ("Toggle keyboard", "keyboard:", 220, 60)]:
            button = NSButton.alloc().initWithFrame_(((x, y), (180, 40)))
            button.setTitle_(title); button.setTarget_(controller); button.setAction_(action)
            win.contentView().addSubview_(button)
        note = NSTextField.labelWithString_("Your physical mouse and keyboard always remain usable.")
        note.setFrame_(((20, 15), (400, 25))); win.contentView().addSubview_(note)
        controller.refresh_(None)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.5, controller, "refresh:", None, True)
        win.makeKeyAndOrderFront_(None); app.activateIgnoringOtherApps_(True); app.run()
    else:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib
        win = Gtk.Window(title="Workman — Mouse & Keyboard")
        win.set_default_size(400, 230); win.set_border_width(16)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); win.add(box)
        label = Gtk.Label(); box.pack_start(label, False, False, 0)
        def refresh():
            s = input_switch.state()
            label.set_text(f"Mouse: {'ON' if s['mouse'] else 'OFF'}     Keyboard: {'ON' if s['keyboard'] else 'OFF'}")
            return True
        for title, callback in [("STOP BOTH", lambda: change(False, False)), ("Enable both", lambda: change(True, True)), ("Toggle mouse", lambda: change(mouse=not input_switch.state()["mouse"])), ("Toggle keyboard", lambda: change(keyboard=not input_switch.state()["keyboard"]))]:
            b = Gtk.Button(label=title); b.connect("clicked", lambda _, cb=callback: (cb(), refresh()))
            box.pack_start(b, False, False, 0)
        box.pack_start(Gtk.Label(label="Your physical input always remains usable."), False, False, 0)
        win.connect("destroy", Gtk.main_quit)
        refresh(); GLib.timeout_add(500, refresh); win.show_all(); Gtk.main()


if __name__ == "__main__":
    main()
