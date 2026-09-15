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
