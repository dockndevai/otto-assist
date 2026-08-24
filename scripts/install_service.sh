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
        <string>$PY</string>
        <string>-m</string>
        <string>otto.menubar</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$ROOT/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
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

cat <<EOF

Otto is installed and running — look for the 🧠 in your menu bar.
It will start automatically every time you log in.

  Logs:      $LOG_DIR/otto.err.log
  Stop/undo: bash scripts/uninstall_service.sh

First launch: macOS will ask for Microphone (and, for the hotkey, Input Monitoring)
permission — grant it, then use the menu's Restart.
EOF
