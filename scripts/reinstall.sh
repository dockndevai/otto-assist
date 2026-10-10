#!/usr/bin/env bash
# Build Orrin, install it to /Applications as the ONE canonical app, leave NO stray launchable copy
# behind, take over from any running instance, and relaunch a single clean instance.
#
# This is the reliable "update + restart to a clean instance" path. It avoids the two things that
# cause the "not listening / no volume" mess: a stray dist/Orrin.app getting launched as a second
# instance, and a broken instance you get stuck behind. Launching Orrin always wins now (the app
# takes over from any other Orrin main on start), and this script removes the stray build copy.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building"
bash packaging/pyinstaller/build.sh

echo "==> Stopping any running Orrin + clearing the single-instance lock"
pkill -9 -f "Orrin.app/Contents/MacOS/Orrin" 2>/dev/null || true
pkill -9 -f "personal-assist/.venv/bin/python" 2>/dev/null || true
sleep 1
rm -f "$HOME/Library/Application Support/Otto/orrin.lock" 2>/dev/null || true

echo "==> Installing to /Applications (and removing the stray build copy)"
rm -rf /Applications/Orrin.app
ditto dist/Orrin.app /Applications/Orrin.app
rm -rf dist/Orrin.app        # never leave a second, double-clickable app behind

echo "==> Launching the installed app (single clean instance)"
nohup /Applications/Orrin.app/Contents/MacOS/Orrin >/tmp/orrin_run.log 2>&1 &
disown
echo "==> Done. One instance, from /Applications. Logs: /tmp/orrin_run.log"
