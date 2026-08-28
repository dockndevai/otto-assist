"""Entry point for the bundled Orrin.app — starts the menu-bar assistant.

PyInstaller freezes this module; it boots the same menu-bar app the CLI runs.
"""
import os

# Launched from Finder/Spotlight, a .app inherits a minimal PATH (/usr/bin:/bin:…) that omits
# Homebrew — so the Claude Agent SDK can't find the user's `claude` CLI (or ffmpeg). Prepend the
# common install locations so "bring your own Claude" works. (Same fix as the Homebrew wrapper.)
_extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
          os.path.expanduser("~/.local/bin")]
_path = os.environ.get("PATH", "")
os.environ["PATH"] = os.pathsep.join(_extra + ([_path] if _path else []))

from otto.menubar import main

if __name__ == "__main__":
    main()
