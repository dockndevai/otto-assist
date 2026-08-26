#!/usr/bin/env bash
# Otto — one-command installer + runner.
#
#   bash install.sh
#
# Does everything end to end: system prerequisites, a Python virtualenv with all
# dependencies, the Whisper model, then installs Otto as a login-item menu bar app
# (launchd), starts it, waits until the dashboard answers, registers Otto's MCP with
# Claude, and opens the dashboard. Safe to re-run — every step is idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }

bold "▶ Otto installer"
echo "  Repo: $ROOT"
echo

# 1) Dependencies, virtualenv, Whisper model.
bold "① Setup (prerequisites, virtualenv, models)"
bash scripts/setup.sh
echo

# 2) Install + start the menu-bar service (launchd) and register the MCP.
bold "② Install & start the menu-bar service"
bash scripts/install_service.sh
echo

# 3) Post-install runner check: wait for the dashboard to answer, then open it.
PORT="$("$ROOT/.venv/bin/python" - <<'PY'
from otto.config import load_settings
print(load_settings().server.port)
PY
)"
URL="http://127.0.0.1:${PORT}"

bold "③ Waiting for Otto to come up at ${URL}"
UP=""
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "$URL/" 2>/dev/null; then UP="yes"; break; fi
  sleep 1
done

if [ -n "$UP" ]; then
  bold "✓ Otto is running."
  open "$URL" 2>/dev/null || true
else
  bold "⚠ Otto didn't answer yet."
  echo "  It may still be downloading a model or waiting on the Microphone permission."
  echo "  Check the log:  tail -f logs/otto.err.log"
fi

cat <<EOF

Next:
  • Look for the 🤖 Otto icon in your menu bar (grey = idle).
  • First run: grant Microphone permission when macOS asks, then menu ▸ Restart.
  • Dashboard: ${URL}   (settings live in the ⚙ drawer there)
  • Talk: say your wake word ("hey otto"), or click the mic / just type.

Manage:
  • Restart / Quit:  from the menu bar (Quit fully stops it — no relaunch).
  • Uninstall:       bash scripts/uninstall_service.sh
EOF
