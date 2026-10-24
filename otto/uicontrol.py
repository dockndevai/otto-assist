"""Local UI automation — click / type / keys / scroll directly on the machine, in-process, no engine.

Uses Quartz **CGEvent** to synthesize mouse/keyboard/scroll input and **AXUIElement**
(ApplicationServices) to find an on-screen element by role + label so "click the Send button" resolves
to a point. Accessibility permission is required — every entry point checks it and, if missing, opens
the Settings pane rather than silently no-op'ing. This only ever runs on an explicit user command
(via the local router); it is never reachable from autonomous/background turns.

CGEvent and AXUIElement both use top-left global display coordinates, so an element's center from
`find_element` is directly clickable with `click`.
"""
from __future__ import annotations

import re

# Mac virtual keycodes for the keys we name. Letters/digits + the common special keys.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31,
    "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "spacebar": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "forwarddelete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
}

# Modifier name → Quartz CGEventFlags mask (resolved lazily to keep import light).
_MOD_NAMES = {
    "cmd": "Command", "command": "Command", "⌘": "Command", "meta": "Command", "super": "Command",
    "shift": "Shift", "⇧": "Shift",
    "ctrl": "Control", "control": "Control", "⌃": "Control",
    "opt": "Alternate", "option": "Alternate", "alt": "Alternate", "⌥": "Alternate",
}

# Element labels that make a click consequential (ask before clicking).
_DESTRUCTIVE_UI = re.compile(
    r"\b(delete|remove|discard|erase|trash|format|send|reply|forward|publish|post|submit|"
    r"buy|pay|purchase|order|checkout|confirm|accept|agree|sign out|log out|unsubscribe|"
    r"delete forever|move to trash|empty trash|shut ?down|restart)\b", re.I)

# Interactive AX roles we'll target for a "click".
_CLICK_ROLES = {"AXButton", "AXMenuButton", "AXMenuItem", "AXPopUpButton", "AXCheckBox",
                "AXRadioButton", "AXLink", "AXTab", "AXToolbarButton", "AXCell", "AXImage",
                "AXStaticText"}


def parse_combo(combo: str) -> tuple[int, list[str]] | None:
    """'cmd+s' / 'control c' / 'enter' → (keycode, [modifier names]). None if the key is unknown."""
    parts = [p for p in re.split(r"[+\s]+", (combo or "").strip().lower()) if p]
    if not parts:
        return None
    key, mod_parts = parts[-1], parts[:-1]
    code = _KEYCODES.get(key)
    if code is None:
        return None
    mods = [_MOD_NAMES[p] for p in mod_parts if p in _MOD_NAMES]
    return code, mods


def is_consequential(label: str) -> bool:
    return bool(_DESTRUCTIVE_UI.search(label or ""))


class UIControl:
    """In-process mouse/keyboard/AX control. All methods require Accessibility."""

    def __init__(self) -> None:
        self._q = None  # Quartz module, imported lazily

    # ---- availability --------------------------------------------------------
    def _quartz(self):
        if self._q is None:
            import Quartz
            self._q = Quartz
        return self._q

    def accessible(self) -> bool:
        try:
            from . import capabilities
            return capabilities.is_granted("accessibility")
        except Exception:
            return True   # can't probe → let it try

    # ---- mouse ---------------------------------------------------------------
    def _post(self, ev) -> None:
        q = self._quartz()
        if ev is not None:
            q.CGEventPost(q.kCGHIDEventTap, ev)

    def move(self, x: float, y: float) -> None:
        q = self._quartz()
        self._post(q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, (x, y), q.kCGMouseButtonLeft))

    def click(self, x: float, y: float, *, double: bool = False, right: bool = False) -> None:
        q = self._quartz()
        btn = q.kCGMouseButtonRight if right else q.kCGMouseButtonLeft
        down = q.kCGEventRightMouseDown if right else q.kCGEventLeftMouseDown
        up = q.kCGEventRightMouseUp if right else q.kCGEventLeftMouseUp
        self.move(x, y)
        for i in range(2 if double else 1):
            de = q.CGEventCreateMouseEvent(None, down, (x, y), btn)
            ue = q.CGEventCreateMouseEvent(None, up, (x, y), btn)
            if double:
                q.CGEventSetIntegerValueField(de, q.kCGMouseEventClickState, i + 1)
                q.CGEventSetIntegerValueField(ue, q.kCGMouseEventClickState, i + 1)
            self._post(de)
            self._post(ue)

    def scroll(self, dx: int, dy: int) -> None:
        q = self._quartz()
        self._post(q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 2, dy, dx))

    # ---- keyboard ------------------------------------------------------------
    def type_text(self, s: str) -> None:
        q = self._quartz()
        for ch in (s or ""):
            for is_down in (True, False):
                ev = q.CGEventCreateKeyboardEvent(None, 0, is_down)
                q.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                self._post(ev)

    def key(self, combo: str) -> bool:
        parsed = parse_combo(combo)
        if parsed is None:
            return False
        code, mods = parsed
        q = self._quartz()
        flags = 0
        for m in mods:
            flags |= getattr(q, f"kCGEventFlagMask{m}", 0)
        for is_down in (True, False):
            ev = q.CGEventCreateKeyboardEvent(None, code, is_down)
            if flags:
                q.CGEventSetFlags(ev, flags)
            self._post(ev)
        return True

    # ---- accessibility element search ----------------------------------------
    def front_pid(self) -> int | None:
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app else None
        except Exception:
            return None

    def find_element(self, query: str, *, role: str | None = None, max_nodes: int = 500) -> dict | None:
        """Breadth-first walk the frontmost app's AX tree for an interactive element whose label
        contains `query`. Returns {title, role, center:(x,y), consequential} or None."""
        q = (query or "").strip().lower()
        if not q:
            return None
        pid = self.front_pid()
        if pid is None:
            return None
        try:
            from ApplicationServices import AXUIElementCreateApplication
        except Exception:
            return None
        root = AXUIElementCreateApplication(pid)
        queue, seen = [root], 0
        while queue and seen < max_nodes:
            el = queue.pop(0)
            seen += 1
            r = self._ax(el, "AXRole") or ""
            label = " ".join(str(x) for x in (
                self._ax(el, "AXTitle"), self._ax(el, "AXDescription"),
                self._ax(el, "AXValue"), self._ax(el, "AXHelp")) if x)
            if q in label.lower() and (r in _CLICK_ROLES if role is None else r == role):
                center = self._ax_center(el)
                if center:
                    title = (str(self._ax(el, "AXTitle") or self._ax(el, "AXDescription") or query)).strip()
                    return {"title": title, "role": r, "center": center,
                            "consequential": is_consequential(label)}
            kids = self._ax(el, "AXChildren")
            if kids:
                queue.extend(kids)
        return None

    def _ax(self, el, attr: str):
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            err, val = AXUIElementCopyAttributeValue(el, attr, None)
            return val if err == 0 else None
        except Exception:
            return None

    def _ax_center(self, el) -> tuple[float, float] | None:
        try:
            from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
            pos, size = self._ax(el, "AXPosition"), self._ax(el, "AXSize")
            if pos is None or size is None:
                return None
            okp, p = AXValueGetValue(pos, kAXValueCGPointType, None)
            oks, s = AXValueGetValue(size, kAXValueCGSizeType, None)
            if not (okp and oks):
                return None
            return (float(p.x) + float(s.width) / 2.0, float(p.y) + float(s.height) / 2.0)
        except Exception:
            return None
