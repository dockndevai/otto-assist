"""Make common developer tool locations findable to Orrin's child processes.

A Finder/launchd-launched app inherits a minimal PATH (often just /usr/bin:/bin), so Node-based
MCP servers (e.g. `npx @playwright/mcp`) and Homebrew tools can't be spawned by the brain. Call
`augment_path()` at startup to prepend the usual bin dirs (Homebrew, /usr/local, ~/.local, and
the newest nvm/fnm node) to PATH so any tool subprocess the agent launches can find them.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path


def augment_path() -> None:
    home = Path.home()
    candidates = ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin")]
    # Newest installed Node from nvm / fnm (so `node`/`npx`-based MCP servers can start).
    for pat in (".nvm/versions/node/*/bin", ".fnm/node-versions/*/installation/bin",
                "Library/Application Support/fnm/node-versions/*/installation/bin"):
        hits = sorted(glob.glob(str(home / pat)))
        if hits:
            candidates.append(hits[-1])
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep) if cur else []
    add = [c for c in candidates if os.path.isdir(c) and c not in parts]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + parts)
