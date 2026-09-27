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


class DashboardWindow:
    """A single reusable native window hosting the dashboard web app."""

    def __init__(self, url: str, title: str = "Orrin") -> None:
        self.url = url
        self.title = title
        self.window = None
        self.webview = None

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


class FloatingCharacter:
    """The on-screen Orrin character: a small borderless, transparent, always-on-top panel that
    hosts the /character page and floats over everything (incl. full-screen apps) without stealing
    focus. Draggable by its body. Reactive to voice state via the page's own /ws subscription.

    Main-thread only (rumps callbacks are). Falls back silently if WebKit/PyObjC isn't available."""

    def __init__(self, url: str, width: int = 300, height: int = 340) -> None:
        self.url = url          # http://127.0.0.1:PORT/character
        self.w = width
        self.h = height
        self.window = None
        self.webview = None

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

    def _build(self) -> None:
        from Cocoa import (
            NSBackingStoreBuffered, NSColor, NSMakeRect, NSScreen, NSStatusWindowLevel, NSURL,
            NSURLRequest, NSViewHeightSizable, NSViewWidthSizable, NSPanel,
            NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
        )
        from WebKit import WKWebView, WKWebViewConfiguration

        # Bottom-right of the main screen's visible area (above the Dock), a small margin in.
        try:
            vf = NSScreen.mainScreen().visibleFrame()
            x = vf.origin.x + vf.size.width - self.w - 24
            y = vf.origin.y + 24
        except Exception:
            x, y = 900, 60
        win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, self.w, self.h),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setHasShadow_(False)
        win.setLevel_(NSStatusWindowLevel)            # always on top
        win.setMovableByWindowBackground_(True)       # drag Orrin to reposition
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

        cfg = WKWebViewConfiguration.alloc().init()
        wv = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, self.w, self.h), cfg)
        wv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        for key in ("drawsBackground", "_drawsBackground"):   # make the webview itself transparent
            try:
                wv.setValue_forKey_(False, key)
                break
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
