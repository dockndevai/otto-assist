"""In-app dashboard window — the Orrin dashboard in a native macOS window (WKWebView),
instead of opening the user's web browser.

The menu-bar app already runs the AppKit main run loop (via rumps), so we build an NSWindow +
WKWebView on the main thread and point it at the local dashboard server (http://127.0.0.1:PORT).
Everything here MUST be called on the main thread (rumps menu callbacks already are).

Falls back gracefully: if WebKit/PyObjC isn't available, the caller opens a browser instead.
"""

from __future__ import annotations


def available() -> bool:
    try:
        import objc  # noqa: F401
        from Cocoa import NSWindow  # noqa: F401
        from WebKit import WKWebView  # noqa: F401
        return True
    except Exception:
        return False


_CLOSE_CLS = None


def _close_handler(win):
    """A WKScriptMessageHandler that closes the window when the page posts an 'orrinClose' message —
    gives in-app pages (e.g. /enroll) a working 'Done/Back' button. Lazy for pyobjc-free import."""
    global _CLOSE_CLS
    if _CLOSE_CLS is None:
        import objc
        from Foundation import NSObject

        class _CloseHandler(NSObject):
            def initWithWindow_(self, w):
                self = objc.super(_CloseHandler, self).init()
                if self is None:
                    return None
                self._w = w
                return self

            def userContentController_didReceiveScriptMessage_(self, ucc, msg):
                try:
                    self._w.performSelectorOnMainThread_withObject_waitUntilDone_(
                        "close", None, False)
                except Exception:
                    pass

        _CLOSE_CLS = _CloseHandler
    return _CLOSE_CLS.alloc().initWithWindow_(win)


class DashboardWindow:
    """A single reusable native window hosting the dashboard web app."""

    def __init__(self, url: str, title: str = "Orrin", close_bridge: bool = False) -> None:
        self.url = url
        self.title = title
        self.window = None
        self.webview = None
        self._close_bridge = close_bridge   # expose window.webkit.messageHandlers.orrinClose
        self._closer = None

    # -- public API (call on the main thread) -----------------------------
    def show(self, suffix: str = "") -> None:
        if self.window is None:
            self._build(suffix)
        else:
            # Reopening: reload so a window left blank by a load that raced the server (or any
            # transient failure) comes back with fresh content, then honor a hash target.
            try:
                self.webview.reload()
            except Exception:
                pass
            if suffix:
                self._go(suffix)
        self._front()

    def open_settings(self) -> None:
        self.show("#settings")

    # -- internals --------------------------------------------------------
    def _build(self, suffix: str = "") -> None:
        from Cocoa import (
            NSBackingStoreBuffered, NSMakeRect, NSURL, NSURLRequest,
            NSViewHeightSizable, NSViewWidthSizable, NSWindow,
            NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
            NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
        )
        from WebKit import WKWebView, WKWebViewConfiguration

        rect = NSMakeRect(0, 0, 1180, 760)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        win.setTitle_(self.title)
        win.setReleasedWhenClosed_(False)   # so we can reopen the same window
        win.center()

        cfg = WKWebViewConfiguration.alloc().init()
        if self._close_bridge:
            try:
                self._closer = _close_handler(win)
                cfg.userContentController().addScriptMessageHandler_name_(self._closer, "orrinClose")
            except Exception:
                pass
        wv = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        wv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        wv.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url + (suffix or ""))))
        win.setContentView_(wv)
        self.window = win
        self.webview = wv
        # The first load can race the server (recorder/model init delays the port binding a
        # moment) and land on a blank connection-refused page with no auto-retry. Reload a
        # couple of times shortly after so the dashboard fills in without the user reopening.
        try:
            wv.performSelector_withObject_afterDelay_("reload", None, 2.5)
            wv.performSelector_withObject_afterDelay_("reload", None, 6.0)
        except Exception:
            pass

    def _go(self, suffix: str) -> None:
        # Navigate an already-open window (e.g. to the settings hash) without a full reload.
        try:
            self.webview.evaluateJavaScript_completionHandler_(
                f"location.hash = '{suffix.lstrip('#') and '#' + suffix.lstrip('#')}'", None)
        except Exception:
            pass

    def _front(self) -> None:
        try:
            from Cocoa import NSApp, NSApplicationActivationPolicyRegular
            # A menu-bar (LSUIElement/accessory) app can't focus a window until it's at least
            # an accessory-with-windows app; Regular lets the window take focus (adds a Dock
            # icon while open, which is expected for an app window).
            NSApp().setActivationPolicy_(NSApplicationActivationPolicyRegular)
            NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        try:
            self.window.makeKeyAndOrderFront_(None)
            self.window.orderFrontRegardless()
        except Exception:
            pass


_DRAG_CLS = None


def _drag_handler(win):
    """A WKScriptMessageHandler that drags the window when the page posts a 'drag' message on
    pointerdown. Defined lazily so this module imports without pyobjc present."""
    global _DRAG_CLS
    if _DRAG_CLS is None:
        import objc
        from Foundation import NSObject

        class _DragHandler(NSObject):
            def initWithWindow_(self, w):
                self = objc.super(_DragHandler, self).init()
                if self is None:
                    return None
                self._w = w
                return self

            def userContentController_didReceiveScriptMessage_(self, ucc, msg):
                try:
                    from Cocoa import NSApp
                    self._w.performWindowDragWithEvent_(NSApp().currentEvent())
                except Exception:
                    pass

        _DRAG_CLS = _DragHandler
    return _DRAG_CLS.alloc().initWithWindow_(win)


class FloatingCharacter:
    """Orrin's on-screen HUD (Wispr-Flow style): a small, draggable, always-on-top glass pill in a
    corner that shows a one-line live readout of what Orrin is hearing + its state, with a little
    red-panda avatar. Floats over full-screen apps without stealing focus; drag it anywhere (native
    window drag via a page message handler). Self-updates from the /ws feed; reposition presets in
    the menu ("Move Orrin ▸ corners").

    Main-thread only (rumps callbacks are). Falls back silently if WebKit/PyObjC isn't available."""

    def __init__(self, url: str, width: int = 340, height: int = 72) -> None:
        self.url = url          # http://127.0.0.1:PORT/hud
        self.w = width
        self.h = height
        self.window = None
        self.webview = None
        self.corner = "br"      # br|bl|tr|tl
        self._drag = None       # native drag message handler (kept referenced)

    def toggle(self) -> None:
        if self.window is not None and self.window.isVisible():
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        if self.window is None:
            self._build()
        try:
            self.window.orderFrontRegardless()
        except Exception:
            pass

    def hide(self) -> None:
        if self.window is not None:
            try:
                self.window.orderOut_(None)
            except Exception:
                pass

    @property
    def visible(self) -> bool:
        return bool(self.window is not None and self.window.isVisible())

    def _corner_origin(self, corner: str):
        from Cocoa import NSScreen
        m = 20
        try:
            vf = NSScreen.mainScreen().visibleFrame()
        except Exception:
            return (900, 60)
        right = vf.origin.x + vf.size.width - self.w - m
        left = vf.origin.x + m
        bottom = vf.origin.y + m
        top = vf.origin.y + vf.size.height - self.h - m
        return {
            "br": (right, bottom), "bl": (left, bottom),
            "tr": (right, top), "tl": (left, top),
        }.get(corner, (right, bottom))

    def move_to(self, corner: str) -> None:
        """Reposition to a screen corner (br/bl/tr/tl)."""
        self.corner = corner
        if self.window is None:
            self.show()
            return
        try:
            from Cocoa import NSMakePoint
            x, y = self._corner_origin(corner)
            self.window.setFrameOrigin_(NSMakePoint(x, y))
            self.window.orderFrontRegardless()
        except Exception:
            pass

    def _build(self) -> None:
        from Cocoa import (
            NSBackingStoreBuffered, NSColor, NSMakeRect, NSStatusWindowLevel, NSURL,
            NSURLRequest, NSViewHeightSizable, NSViewWidthSizable, NSPanel,
            NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
        )
        from WebKit import WKWebView, WKWebViewConfiguration

        x, y = self._corner_origin(self.corner)
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, self.w, self.h),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(False)
        win.setLevel_(NSStatusWindowLevel)            # always on top
        win.setFloatingPanel_(True)
        win.setBecomesKeyOnlyIfNeeded_(True)          # don't steal focus from the front app
        win.setHidesOnDeactivate_(False)
        try:
            win.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorStationary)   # float over all Spaces + full-screen apps
        except Exception:
            pass

        # Native drag: the page posts "drag" on pointerdown and we drag the window with the event,
        # so the small pill can be moved anywhere (a webview eats mouse events, so window-background
        # dragging alone won't work).
        cfg = WKWebViewConfiguration.alloc().init()
        try:
            self._drag = _drag_handler(win)
            cfg.userContentController().addScriptMessageHandler_name_(self._drag, "drag")
        except Exception:
            self._drag = None
        wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, self.w, self.h), cfg)
        wv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        wv.setOpaque_(False)                           # transparent webview (so the panel shows through)
        try:
            wv.setBackgroundColor_(NSColor.clearColor())
        except Exception:
            pass
        for key in ("drawsBackground", "_drawsBackground"):   # KVC transparency (private but stable)
            try:
                wv.setValue_forKey_(False, key)
            except Exception:
                continue
        try:
            wv.setUnderPageBackgroundColor_(NSColor.clearColor())
        except Exception:
            pass
        wv.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))
        win.setContentView_(wv)
        win.orderFrontRegardless()
        self.window = win
        self.webview = wv
        # First load can race the server coming up — reload shortly after so the character fills in.
        try:
            wv.performSelector_withObject_afterDelay_("reload", None, 2.5)
        except Exception:
            pass
