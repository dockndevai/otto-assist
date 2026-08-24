#!/usr/bin/env bash
# Remove the Jarvis login-item menu bar service.
set -euo pipefail

LABEL="com.jarvis.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Stopping and removing the service"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Done. Jarvis will no longer start at login. (Any running 🧠 can be quit from its menu.)"
