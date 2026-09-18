# platform/macos/overlay.py
# Native Cocoa version of level_overlay.LevelOverlay. Tk cannot share the
# process with the pystray NSApplication (Tk 9 crashes with
# "-[NSApplication macOSVersion]: unrecognized selector"), so on macOS the pill
# is an NSPanel instead. Same public API as LevelOverlay; every AppKit call is
# marshalled onto the main thread, whose run loop main.py pumps via
# app.run_event_loop().
import logging
import math
import time
from typing import Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSString,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakePoint, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

logger = logging.getLogger(__name__)

WIDTH = 300
HEIGHT = 32
EDGE_MARGIN = 80
TOP_MARGIN = 40
SIDE_MARGIN = 30
LEVEL_AMP = 25.0
UPDATE_HZ = 30
FLASH_S = 0.4
FLASH_MESSAGE_S = 2.0


def _rgb(hex_color: str, alpha: float = 1.0):
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


BG = _rgb('#0d1117', 0.92)
DIM = _rgb('#30363d')
TEXT = _rgb('#c9d1d9')
GREEN = _rgb('#3fb950')
RED = _rgb('#f85149')
AMBER = _rgb('#d29922')


class _PillView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_PillView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.mode = 'hidden'
        self.level = 0.0
        self.text = ''
        self.flash_color = None
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        w, h = WIDTH, HEIGHT
        BG.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, w, h), h / 2, h / 2).fill()

        # Status dot: the unmistakable "mic is live" signal.
        dot = {'recording': RED, 'processing': AMBER}.get(self.mode, self.flash_color or GREEN)
        dot.setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(12, h / 2 - 5, 10, 10)).fill()

        bar_x, bar_w = 32, 80
        DIM.setFill()
        NSBezierPath.fillRect_(NSMakeRect(bar_x, h / 2 - 1, bar_w, 2))
        fill_w = bar_w * self.level
        if fill_w > 0:
            fill_h = max(4, (h * 0.6) * self.level + 4)
            color = self.flash_color or (AMBER if self.mode == 'processing' else GREEN)
            color.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(bar_x + (bar_w - fill_w) / 2, (h - fill_h) / 2, fill_w, fill_h), 2, 2).fill()

        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12),
                 NSForegroundColorAttributeName: TEXT}
        NSString.stringWithString_(self.text).drawAtPoint_withAttributes_(
            NSMakePoint(bar_x + bar_w + 10, (h - 15) / 2), attrs)


class _Ticker(NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_Ticker, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tick_(self, timer):
        self.owner._tick()


class NativeLevelOverlay:
    POSITIONS = ('bottom-center', 'bottom-right', 'bottom-left',
                 'top-center', 'top-right', 'top-left')

    def __init__(self, level_provider: Callable[[], float],
                 click_through: bool = True,
                 position: str = 'bottom-center'):
        self.level_provider = level_provider
        self.click_through = click_through
        self.position = position if position in self.POSITIONS else 'bottom-center'
        self.panel = None
        self.view = None
        self._timer = None
        self._ticker = None
        self._smoothed = 0.0
        self._streaming_text = ''
        self._hide_at = None

    def start(self):
        self._on_main(self._build)

    def show_recording(self):
        self._on_main(lambda: self._set_mode('recording'))

    def show_processing(self):
        self._on_main(lambda: self._set_mode('processing'))

    def hide(self):
        self._on_main(lambda: self._set_mode('hidden'))

    def flash_success(self):
        self._on_main(lambda: self._flash(GREEN, None))

    def flash_failure(self, message: str = None):
        self._on_main(lambda: self._flash(RED, message))

    def set_streaming_text(self, text: str):
        self._streaming_text = (text or '').strip()
        self._on_main(self._refresh_text)

    def set_position(self, name: str):
        if name in self.POSITIONS:
            self.position = name
            self._on_main(self._place)

    def shutdown(self):
        self._on_main(self._teardown)

    def _on_main(self, fn):
        def safe():
            try:
                fn()
            except Exception as e:
                logger.debug(f"Overlay call failed: {e}")
        AppHelper.callAfter(safe)

    def _build(self):
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setIgnoresMouseEvents_(self.click_through)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary)
        self.view = _PillView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        self.panel.setContentView_(self.view)
        self._ticker = _Ticker.alloc().initWithOwner_(self)

    def _teardown(self):
        self._stop_timer()
        if self.panel:
            self.panel.orderOut_(None)
            self.panel = None

    def _place(self):
        if not self.panel:
            return
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        f = screen.visibleFrame()
        x0, y0 = f.origin.x, f.origin.y
        w, h = f.size.width, f.size.height
        if self.position.startswith('bottom'):
            y = y0 + EDGE_MARGIN
        else:
            y = y0 + h - HEIGHT - TOP_MARGIN
        if self.position.endswith('center'):
            x = x0 + (w - WIDTH) / 2
        elif self.position.endswith('right'):
            x = x0 + w - WIDTH - SIDE_MARGIN
        else:
            x = x0 + SIDE_MARGIN
        self.panel.setFrameOrigin_(NSMakePoint(x, y))

    def _set_mode(self, mode: str):
        if not self.panel:
            return
        self.view.mode = mode
        self.view.flash_color = None
        self._hide_at = None
        if mode == 'hidden':
            self._streaming_text = ''
            self._stop_timer()
            self.panel.orderOut_(None)
            return
        self._smoothed = 0.0
        self._refresh_text()
        self._place()
        self.panel.orderFrontRegardless()
        self._start_timer()

    def _flash(self, color, message):
        if not self.panel:
            return
        self.view.mode = 'flash'
        self.view.flash_color = color
        self.view.level = 1.0
        if message:
            self.view.text = message
        self._place()
        self.panel.orderFrontRegardless()
        self.view.setNeedsDisplay_(True)
        self._hide_at = time.monotonic() + (FLASH_MESSAGE_S if message else FLASH_S)
        self._start_timer()

    def _refresh_text(self):
        if not self.view:
            return
        if self.view.mode == 'processing':
            text = 'Transcribing…'
        elif self._streaming_text:
            text = self._streaming_text
            text = '…' + text[-23:] if len(text) > 24 else text
        else:
            text = 'Listening…'
        self.view.text = text
        self.view.setNeedsDisplay_(True)

    def _start_timer(self):
        if self._timer is not None:
            return
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / UPDATE_HZ, self._ticker, 'tick:', None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

    def _stop_timer(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _tick(self):
        mode = self.view.mode
        if mode == 'flash':
            if self._hide_at and time.monotonic() >= self._hide_at:
                self._set_mode('hidden')
            return
        if mode == 'recording':
            try:
                target = min(1.0, max(0.0, float(self.level_provider()) * LEVEL_AMP))
            except Exception:
                target = 0.0
        elif mode == 'processing':
            target = 0.3 + 0.3 * abs(math.sin(time.monotonic() * math.pi))
        else:
            return
        self._smoothed = self._smoothed * 0.55 + target * 0.45
        self.view.level = self._smoothed
        self.view.setNeedsDisplay_(True)
