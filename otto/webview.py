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
    """The on-screen Orrin character: a small borderless, TRANSPARENT, always-on-top panel that
    hosts the /character page and floats over everything (incl. full-screen apps) without stealing
    focus. It is CLICK-THROUGH — the panel never intercepts a click, so buttons/apps behind it stay
    usable; the character is purely visual and reacts to voice state via the page's own /ws feed.
    Reposition it from the menu (corners), since a click-through window can't be dragged.

    Main-thread only (rumps callbacks are). Falls back silently if WebKit/PyObjC isn't available."""

    def __init__(self, url: str, width: int = 190, height: int = 230) -> None:
        self.url = url          # http://127.0.0.1:PORT/character
        self.w = width
        self.h = height
        self.window = None
        self.webview = None
        self.corner = "br"      # br|bl|tr|tl
        self._roam_countdown = 6.0     # seconds until the next autonomous wander
        self._last_state = None        # last visual state pushed to the page

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

    # -- autonomy: react to state + wander the screen ---------------------
    def set_state(self, s: str) -> None:
        """Push a visual state to the character page (idle/listening/thinking/speaking/sleeping)."""
        if self.webview is None or s == self._last_state:
            return
        self._last_state = s
        try:
            self.webview.evaluateJavaScript_completionHandler_(
                f"window.orrinSetState && window.orrinSetState({s!r})", None)
        except Exception:
            pass

    def tick(self, dt: float, *, mode: str, char_state: str, roam: bool,
             interval: float, speed: float) -> None:
        """Called ~1x/sec by the menu bar. Drives the visual state and autonomous roaming.
        mode: idle | busy | attending | sleeping."""
        if self.window is None or not self.visible:
            return
        self.set_state(char_state)
        # Wander only when idle or busy; settle while attending to you or napping.
        if not roam or mode in ("attending", "sleeping"):
            return
        self._roam_countdown -= dt
        if self._roam_countdown <= 0:
            import random as _r
            dur = self._roam(busy=(mode == "busy"), speed=speed)
            base = interval * (0.5 if mode == "busy" else 1.0)
            # next hop only after this glide finishes (+ a rest), so moves never overlap
            self._roam_countdown = max(dur + 1.5, base * (0.7 + _r.random() * 0.6))

    def _roam(self, *, busy: bool, speed: float) -> float:
        """Glide the panel to a new random spot on screen (eased, native animation). Returns the
        glide duration in seconds (0 if it couldn't move)."""
        import random as _r

        from Cocoa import NSAnimationContext, NSMakePoint, NSScreen
        try:
            vf = NSScreen.mainScreen().visibleFrame()
        except Exception:
            return 0.0
        m = 12
        x0 = vf.origin.x + m
        x1 = vf.origin.x + vf.size.width - self.w - m
        y0 = vf.origin.y + m
        y1 = vf.origin.y + vf.size.height - self.h - m
        if x1 <= x0 or y1 <= y0:
            return 0.0
        cur = self.window.frame().origin
        # Busy = bigger, snappier hops; idle = gentle drift near where it is.
        if busy:
            tx, ty = _r.uniform(x0, x1), _r.uniform(y0, y1)
        else:
            span_x, span_y = (x1 - x0) * 0.35, (y1 - y0) * 0.35
            tx = min(x1, max(x0, cur.x + _r.uniform(-span_x, span_x)))
            ty = min(y1, max(y0, cur.y + _r.uniform(-span_y, span_y)))
        dist = ((tx - cur.x) ** 2 + (ty - cur.y) ** 2) ** 0.5
        dur = max(1.2, min(6.0, dist / (180.0 * max(0.3, speed)))) * (0.7 if busy else 1.0)
        try:
            NSAnimationContext.beginGrouping()
            NSAnimationContext.currentContext().setDuration_(dur)
            self.window.animator().setFrameOrigin_(NSMakePoint(tx, ty))
            NSAnimationContext.endGrouping()
        except Exception:
            try:
                self.window.setFrameOrigin_(NSMakePoint(tx, ty))
            except Exception:
                pass
        return dur

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
        win.setIgnoresMouseEvents_(True)              # CLICK-THROUGH — never block what's behind it
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
