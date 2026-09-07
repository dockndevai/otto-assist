"""Dictation-anywhere: type transcribed speech into the focused app.

Puts the text on the clipboard, sends ⌘V to the frontmost app, then restores the previous
clipboard — so dictating doesn't clobber what you had copied (the trick Voicebox uses). macOS
only; the ⌘V keystroke needs Accessibility permission for System Events. Best-effort: returns
False on any failure and always tries to restore the clipboard.
"""

from __future__ import annotations

import subprocess
import sys
import time

_PASTE_AS = 'tell application "System Events" to keystroke "v" using command down'


def paste_text(text: str, restore_delay: float = 0.2) -> bool:
    """Insert `text` into the focused app via ⌘V, preserving the user's clipboard. Returns
    True if the paste keystroke was sent."""
    text = text or ""
    if not text:
        return False
    prev: str | None = None
    try:
        prev = subprocess.run(["pbpaste"], capture_output=True, text=True,
                              timeout=2).stdout
    except Exception:
        prev = None
    ok = False
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=2, check=True)
        subprocess.run(["osascript", "-e", _PASTE_AS], timeout=3, check=True)
        ok = True
    except Exception as e:
        print(f"[otto] dictation paste failed: {e!r}", file=sys.stderr, flush=True)
    finally:
        if prev is not None:
            # let the paste land before we put the old clipboard back
            time.sleep(restore_delay)
            try:
                subprocess.run(["pbcopy"], input=prev, text=True, timeout=2)
            except Exception:
                pass
    return ok
