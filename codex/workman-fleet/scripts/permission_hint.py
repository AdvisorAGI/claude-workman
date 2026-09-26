"""Short-lived, static, click-through permission callout. No input observers."""
import json,sys,time


def placement(target, screen, width=230, height=60):
    x,y,w,h=target; sx,sy,sw,sh=screen
    candidates=[(x-width-12,y),(x+w+12,y),(x,y-height-12),(x,y+h+12)]
    for px,py in candidates:
        px=max(sx,min(px,sx+sw-width));py=max(sy,min(py,sy+sh-height))
        if px+width<=x or px>=x+w or py+height<=y or py>=y+h:
            return px,py,width,height
    return None # Plain text is preferable to covering the control.


def main():
    args=json.load(sys.stdin)
    if sys.platform!='darwin' or time.time()-args['observed']>30: return
    from AppKit import NSApplication,NSPanel,NSScreen,NSTextField,NSColor,NSFont,NSWindowStyleMaskBorderless,NSBackingStoreBuffered
    from Foundation import NSTimer,NSObject
    app=NSApplication.sharedApplication();app.setActivationPolicy_(1)
    screen=NSScreen.mainScreen().frame();size=(float(screen.size.width),float(screen.size.height))
    rect=placement(args['target'],(0,0,*size))
    if rect is None:return
    x,y,w,h=rect
    panel=NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(((x,size[1]-y-h),(w,h)),NSWindowStyleMaskBorderless,NSBackingStoreBuffered,False)
    panel.setIgnoresMouseEvents_(True);panel.setLevel_(25);panel.setOpaque_(True)
    panel.setHidesOnDeactivate_(False);panel.setFloatingPanel_(True)
    panel.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(.12,1))
    label=NSTextField.labelWithString_('1. Screen Recording\nHuman: enable Workman' if args['grant']=='screen_recording' else '2. Accessibility\nHuman: enable Workman')
    label.setFrame_(((8,7),(w-16,h-14)));label.setTextColor_(NSColor.whiteColor());label.setFont_(NSFont.systemFontOfSize_(16));panel.contentView().addSubview_(label)
    class End(NSObject):
        def close_(self,timer):app.terminate_(None)
    end=End.alloc().init()
    # Static for every reduced-motion preference. Expire even if the owner waits.
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(min(20,max(1,30-(time.time()-args['observed']))),end,'close:',None,False)
    panel.orderFrontRegardless();app.run()


if __name__=='__main__':main()
