"""Global push-to-talk hotkey via a main-thread NSEvent monitor (PyObjC / AppKit).

Why not pynput: pynput's macOS backend makes Carbon input-source calls on a background
thread, which SIGTRAP-aborts the packaged (.app) build. NSEvent monitors are installed on the
MAIN thread and their handler runs on the main run loop — safe inside the rumps menu-bar app.

`start()` MUST be called on the main thread (the menu bar installs it there, like the mic
prompt). Observing key events from other apps needs macOS **Input Monitoring** (and/or
Accessibility) permission; without it the monitors simply never fire (they don't crash).
"""

from __future__ import annotations

import sys
from typing import Callable

from ..config import Settings

# deviceIndependentModifierFlags bits (AppKit NSEventModifierFlag*)
_SHIFT, _CTRL, _ALT, _CMD = 1 << 17, 1 << 18, 1 << 19, 1 << 20
_ALL_MODS = _SHIFT | _CTRL | _ALT | _CMD
_MOD_TOKENS = {
    "cmd": _CMD, "command": _CMD, "super": _CMD, "win": _CMD,
    "ctrl": _CTRL, "control": _CTRL,
    "alt": _ALT, "opt": _ALT, "option": _ALT,
    "shift": _SHIFT,
}
# NSEventType
_KEYDOWN, _KEYUP = 10, 11
# NSEventMask (1 << type)
_MASK_KEYDOWN, _MASK_KEYUP = 1 << _KEYDOWN, 1 << _KEYUP


def parse_chord(spec: str) -> tuple[int, str]:
    """"<cmd>+<shift>+j" -> (required-modifier mask, target key char lowercased)."""
    mask, key = 0, ""
    for raw in (spec or "").split("+"):
        tok = raw.strip().strip("<>").lower()
        if not tok:
            continue
        if tok in _MOD_TOKENS:
            mask |= _MOD_TOKENS[tok]
        else:
            key = tok[:1]
    return mask, key


class MacHotkey:
    def __init__(self, settings: Settings, loop, on_press: Callable[[], None],
                 on_release: Callable[[], None]) -> None:
        self.settings = settings
        self._loop = loop
        self._on_press = on_press
        self._on_release = on_release
        self._mask, self._key = parse_chord(settings.voice.hotkey.push_to_talk)
        self._down = False
        self._global = None
        self._local = None

    def start(self) -> None:
        """Install the global + local key monitors. Call on the MAIN thread."""
        if not self._key:
            print(f"[otto] hotkey: could not parse chord "
                  f"{self.settings.voice.hotkey.push_to_talk!r}", file=sys.stderr, flush=True)
            return
        try:
            from AppKit import NSEvent
        except Exception as e:
            print(f"[otto] hotkey: AppKit unavailable ({e})", file=sys.stderr, flush=True)
            return
        mask = _MASK_KEYDOWN | _MASK_KEYUP
        # Global monitor: fires when another app is focused (returns nothing).
        self._global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, self._handle)
        # Local monitor: fires when Orrin's own window is focused; must return the event.
        self._local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask, self._handle_local)
        print(f"[otto] global push-to-talk armed ({self.settings.voice.hotkey.push_to_talk})",
              file=sys.stderr, flush=True)

    def _handle_local(self, event):
        self._handle(event)
        return event      # don't swallow the key for the focused app

    def _handle(self, event):
        try:
            etype = int(event.type())
            if etype not in (_KEYDOWN, _KEYUP):
                return
            chars = (event.charactersIgnoringModifiers() or "").lower()
            if chars[:1] != self._key:
                return
            if etype == _KEYDOWN:
                mods = int(event.modifierFlags()) & _ALL_MODS
                if mods == self._mask and not self._down:
                    self._down = True
                    self._loop.call_soon_threadsafe(self._on_press)
            else:  # key up of the trigger key ends the hold (mods may already be released)
                if self._down:
                    self._down = False
                    self._loop.call_soon_threadsafe(self._on_release)
        except Exception:
            pass

    def stop(self) -> None:
        try:
            from AppKit import NSEvent
            for m in (self._global, self._local):
                if m is not None:
                    NSEvent.removeMonitor_(m)
        except Exception:
            pass
        self._global = self._local = None
