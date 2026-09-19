#!/usr/bin/env bash
# Clean reinstall of the packaged Orrin.app from dist/ — no leftover instances, no junk.
#
#   bash scripts/reinstall_app.sh                # clean reinstall
#   bash scripts/reinstall_app.sh --reset-perms  # also clear stale TCC grants for com.orrin.app
#
# Fixes the two reinstall problems:
#   1. Old instances left running -> multiple copies fight over the mic and the STT worker's pipe
#      dies (EOFError loop -> "not listening"). We hard-kill ALL instances first.
#   2. Junk permissions -> ad-hoc rebuilds accumulate stale TCC entries. --reset-perms wipes them
#      so you start clean; with a stable signing identity (scripts/setup_signing.sh) the fresh
#      grants then PERSIST and you never reset again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_SRC="$ROOT/dist/Orrin.app"
APP_DST="/Applications/Orrin.app"
BUNDLE_ID="com.orrin.app"
RESET_PERMS="no"
[ "${1:-}" = "--reset-perms" ] && RESET_PERMS="yes"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }

[ -d "$APP_SRC" ] || { echo "✗ $APP_SRC not found — build it first: bash packaging/pyinstaller/build.sh"; exit 1; }

bold "① Quitting every running Orrin instance"
osascript -e 'tell application "Orrin" to quit' 2>/dev/null || true
sleep 1
pkill -9 -f "Orrin.app/Contents/MacOS/Orrin" 2>/dev/null || true
sleep 2
LEFT="$(pgrep -f 'Orrin.app/Contents/MacOS/Orrin' | grep -c . || true)"
echo "   instances still running: ${LEFT:-0}"

if [ "$RESET_PERMS" = "yes" ]; then
  bold "② Clearing stale privacy grants for $BUNDLE_ID"
  # Wipes accumulated ad-hoc TCC entries. You'll re-grant once after launch; with stable signing
  # those grants then persist across future rebuilds.
  tccutil reset All "$BUNDLE_ID" 2>/dev/null && echo "   TCC reset done" || echo "   (tccutil reset skipped)"
else
  bold "② Keeping existing privacy grants (pass --reset-perms to wipe stale ones)"
fi

bold "③ Replacing $APP_DST"
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true
VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DST/Contents/Info.plist" 2>/dev/null || echo '?')"
SIG="$(codesign -dvv "$APP_DST" 2>&1 | grep -i 'Authority=' | head -1)"
[ -z "$SIG" ] && SIG="$(codesign -dv "$APP_DST" 2>&1 | grep -qi 'adhoc' && echo 'Signature=ad-hoc (grants will drop on rebuild)' || echo 'unsigned')"
echo "   installed v$VER  |  $SIG"

bold "④ Launching (shell launch — so the mic recorder actually starts)"
# IMPORTANT: `open`/Finder/Dock launch of the bundle does NOT spawn the RealtimeSTT recorder
# subprocess — Orrin comes up but is DEAF. A shell launch does spawn it, and (run from your
# Terminal) inherits Terminal's stable mic grant. We launch the same way as ~/Launch Orrin.command.
nohup "$APP_DST/Contents/MacOS/Orrin" >/dev/null 2>&1 &
disown 2>/dev/null || true
PORT="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || echo 8000
from otto.config import load_settings
print(load_settings().server.port)
PY
)"
UP=""
for _ in $(seq 1 45); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null && { UP="yes"; break; }
  sleep 1
done
if [ -n "$UP" ]; then
  bold "✓ Orrin v$VER is up at http://127.0.0.1:${PORT}  (1 instance)"
else
  echo "⚠ Dashboard didn't answer yet — give it ~15s to load the model, then reload the page."
fi
echo
echo "  To listen, launch Orrin from a Terminal (or double-click ~/Launch Orrin.command) —"
echo "  NOT the Dock/Finder icon, which starts it deaf. Say \"Otto\" to wake it."
