"""Entry point for the bundled Orrin.app — starts the menu-bar assistant.

PyInstaller freezes this module; it just boots the same menu-bar app the CLI runs.
"""
from otto.menubar import main

if __name__ == "__main__":
    main()
