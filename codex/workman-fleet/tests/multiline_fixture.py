"""Harmless native Mac editor: observes only its own text, stores length/digest."""
import hashlib,json,os,sys
from pathlib import Path
import AppKit as A
import Foundation as F
out=Path(sys.argv[1]);app=A.NSApplication.sharedApplication();app.setActivationPolicy_(0)
class Controller(F.NSObject):
    def windowWillClose_(self,notification):app.terminate_(None)
    def textDidChange_(self,notification):self.save_(None)
    def save_(self,sender):
        text=str(editor.string());out.write_text(json.dumps({'chars':len(text),'sha256':hashlib.sha256(text.encode()).hexdigest(),'pid':os.getpid()}));out.chmod(0o600)
    def close_(self,sender):self.save_(None);app.terminate_(None)
controller=Controller.alloc().init()
menu=A.NSMenu.alloc().init();item=A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('Edit',None,'');edit=A.NSMenu.alloc().initWithTitle_('Edit')
for title,action,key in [('Select All','selectAll:','a'),('Paste','paste:','v')]:edit.addItem_(A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title,action,key))
item.setSubmenu_(edit);menu.addItem_(item);app.setMainMenu_(menu)
window=A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(F.NSMakeRect(60,200,540,330),A.NSWindowStyleMaskTitled|A.NSWindowStyleMaskClosable,A.NSBackingStoreBuffered,False);window.setTitle_('Workman v1.1 Multiline Check')
window.setDelegate_(controller)
editor=A.NSTextView.alloc().initWithFrame_(F.NSMakeRect(20,75,500,230));editor.setRichText_(False);editor.setFont_(A.NSFont.monospacedSystemFontOfSize_weight_(17,0));editor.setDelegate_(controller);window.contentView().addSubview_(editor)
button=A.NSButton.alloc().initWithFrame_(F.NSMakeRect(340,20,170,35));button.setTitle_('Close test');button.setTarget_(controller);button.setAction_('close:');window.contentView().addSubview_(button)
window.makeKeyAndOrderFront_(None);app.activateIgnoringOtherApps_(True);controller.save_(None);app.run()
