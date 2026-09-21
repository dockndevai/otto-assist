#!/usr/bin/env bash
# Install Otto as a login-item menu bar app via a launchd LaunchAgent.
# Idempotent: re-running reinstalls and reloads.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LABEL="com.otto.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

if [ ! -x "$PY" ]; then
  echo "Error: $PY not found. Run scripts/setup.sh first."
  exit 1
fi

echo "==> Ensuring the menu bar dependency (rumps) is installed"
"$ROOT/.venv/bin/pip" install -q -e "$ROOT[app]"

# Prefer the STABLE-SIGNED packaged app (/Applications/Orrin.app) as the launchd target: its
# code identity is constant, so macOS keeps its Microphone/Accessibility grants across reboots
# and rebuilds. Source `python -m otto.menubar` under launchd works but its mic grant is
# unstable (unsigned interpreter), so it's only the dev fallback.
APP_BIN="/Applications/Orrin.app/Contents/MacOS/Orrin"
if [ -x "$APP_BIN" ]; then
  echo "==> Target: installed app (stable-signed) — $APP_BIN"
  PROG_ARGS="        <string>$APP_BIN</string>"
else
  echo "==> Target: source (dev) — $PY -m otto.menubar"
  echo "    (Build + install /Applications/Orrin.app for a stable-signed service with persistent mic.)"
  PROG_ARGS="        <string>$PY</string>
        <string>-m</string>
        <string>otto.menubar</string>"
fi

echo "==> Writing LaunchAgent → $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$PROG_ARGS
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$ROOT/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>OTTO_LAUNCHD_LABEL</key>
        <string>$LABEL</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/otto.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/otto.err.log</string>
</dict>
</plist>
PLIST

echo "==> Loading the service"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "==> Registering Otto as an MCP server for Claude Code"
PORT="$("$PY" - <<'PY'
from otto.config import load_settings
print(load_settings().server.port)
PY
)"
if command -v claude >/dev/null 2>&1; then
  claude mcp remove otto 2>/dev/null || true
  claude mcp add --transport http --scope user otto "http://127.0.0.1:${PORT}/mcp/" \
    && echo "    Registered. Any Claude session now has speak/ask/show/notify." \
    || echo "    Could not auto-register; add manually: claude mcp add --transport http otto http://127.0.0.1:${PORT}/mcp/"
else
  echo "    'claude' not on PATH — skipped MCP registration."
fi

cat <<EOF

Otto is installed and running — look for the 🧠 in your menu bar.
It will start automatically every time you log in.

  Logs:      $LOG_DIR/otto.err.log
  Stop/undo: bash scripts/uninstall_service.sh

First launch: macOS will ask for Microphone (and, for the hotkey, Input Monitoring)
permission — grant it, then use the menu's Restart.
EOF
