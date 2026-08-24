#!/usr/bin/env bash
# Remove the Otto login-item menu bar service.
set -euo pipefail

LABEL="com.otto.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Stopping and removing the service"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "==> Unregistering the Otto MCP from Claude Code"
command -v claude >/dev/null 2>&1 && claude mcp remove otto 2>/dev/null || true

echo "Done. Otto will no longer start at login. (Any running 🧠 can be quit from its menu.)"
