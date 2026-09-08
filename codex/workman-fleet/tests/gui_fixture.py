"""Temporary input target. Retains only counter, length and digest, never text.

Run explicitly for a live test; Close test exits this fixture only. No observers
of other apps, no network or clipboard access, no automatic start/schedule.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
STATE = {"clicks": 0, "chars": 0, "sha256": hashlib.sha256(b"").hexdigest(), "pid": os.getpid()}


def save(text=None):
    if text is not None:
        STATE.update(chars=len(text), sha256=hashlib.sha256(text.encode()).hexdigest())
    OUT.write_text(json.dumps(STATE))
    os.chmod(OUT, 0o600)


if sys.platform == "darwin":
    import AppKit as A
    import Foundation as F
    import objc

    class Controller(F.NSObject):
        def windowWillClose_(self, notification):
            app.terminate_(None)
        def clicked_(self, sender):
            STATE["clicks"] += 1
            label.setStringValue_(f"Clicks: {STATE['clicks']}")
            save()

        def controlTextDidChange_(self, notification):
            save(str(entry.stringValue()))

        def close_(self, sender):
            save(str(entry.stringValue()))
            app.stop_(None)
            app.abortModal()

    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    # A bare NSApplication has no Edit menu/key equivalents. Supply the same
    # responder-chain Select All command as an ordinary Mac editor; otherwise
    # cmd+a is an invalid test of the automation backend.
    menu = A.NSMenu.alloc().init()
    edit_item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
    edit_menu = A.NSMenu.alloc().initWithTitle_("Edit")
    select_all = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
    edit_menu.addItem_(select_all)
    edit_item.setSubmenu_(edit_menu)
    menu.addItem_(edit_item)
    app.setMainMenu_(menu)
    window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        F.NSMakeRect(60, 200, 420, 230), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable | A.NSWindowStyleMaskResizable | A.NSWindowStyleMaskMiniaturizable, A.NSBackingStoreBuffered, False)
    window.setTitle_("Workman v1.1 Input Check")
    controller = Controller.alloc().init()
    window.setDelegate_(controller)
    label = A.NSTextField.labelWithString_("Clicks: 0")
    label.setFrame_(F.NSMakeRect(25, 175, 350, 25))
    entry = A.NSTextField.alloc().initWithFrame_(F.NSMakeRect(25, 110, 370, 36))
    entry.setPlaceholderString_("Harmless test text")
    entry.setDelegate_(controller)
    for title, sel, x in [("Mark click", "clicked:", 25), ("Close test", "close:", 235)]:
        b = A.NSButton.alloc().initWithFrame_(F.NSMakeRect(x, 35, 160, 40))
        b.setTitle_(title); b.setBezelStyle_(A.NSBezelStyleRounded)
        b.setTarget_(controller); b.setAction_(sel)
        window.contentView().addSubview_(b)
    window.contentView().addSubview_(label); window.contentView().addSubview_(entry)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    save()
    app.run()
else:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    window = Gtk.Window(title="Workman v1.1 Input Check")
    window.set_default_size(420, 230); window.move(35, 220)
    window.connect("destroy", Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    box.set_border_width(20)
    label = Gtk.Label(label="Clicks: 0")
    entry = Gtk.Entry(); entry.set_placeholder_text("Harmless test text")
    entry.connect("changed", lambda e: save(e.get_text()))
    button = Gtk.Button(label="Mark click")
    def clicked(b):
        STATE["clicks"] += 1; label.set_text(f"Clicks: {STATE['clicks']}"); save()
    button.connect("clicked", clicked)
    close = Gtk.Button(label="Close test"); close.connect("clicked", lambda _: window.destroy())
    for w in [label, entry, button, close]: box.pack_start(w, True, True, 0)
    window.add(box); window.show_all(); save(); Gtk.main()
