#!/usr/bin/env bash
# One-time setup for Otto on macOS.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> System prerequisites (portaudio + ffmpeg)"
if command -v brew >/dev/null 2>&1; then
  brew list portaudio >/dev/null 2>&1 || brew install portaudio
  brew list ffmpeg    >/dev/null 2>&1 || brew install ffmpeg
else
  echo "    Homebrew not found — install it from https://brew.sh, then re-run."
fi

echo "==> Python virtualenv (.venv)"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null

echo "==> Core dependencies"
pip install -e .

echo "==> Voice dependencies (Whisper ears + TTS mouth + hotkey)"
pip install -e ".[voice]" || echo "    Voice extras failed — text path will still work."

echo "==> Pre-downloading the Whisper model (this can take a minute)"
MODEL="$(python - <<'PY'
from otto.config import load_settings
print(load_settings().voice.stt.model)
PY
)"
python - "$MODEL" <<'PY' || echo "    Model prefetch skipped (will download on first run)."
import sys
from faster_whisper import WhisperModel
WhisperModel(sys.argv[1])
print("    Whisper model ready:", sys.argv[1])
PY

echo "==> .env"
[ -f .env ] || cp .env.example .env

cat <<'EOF'

Setup complete.

Next:
  1. (Optional) edit .env for ANTHROPIC_API_KEY / ELEVENLABS_API_KEY, and config.yaml.
  2. Grant your terminal Microphone access: System Settings → Privacy & Security → Microphone.
  3. Run:  source .venv/bin/activate && python run.py
EOF
