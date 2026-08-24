#!/usr/bin/env bash
# Install a trained openWakeWord model (e.g. "hey otto") into Otto and switch the live
# config to use it, then restart the service.
#
# Usage:  bash scripts/install_wake_word.sh /path/to/hey_otto.onnx
#
# Get the model file from the free Colab notebook — see docs/HEY_OTTO.md.
set -euo pipefail

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "Usage: bash scripts/install_wake_word.sh /path/to/hey_otto.onnx"
  echo "  (download the model from Colab first — see docs/HEY_OTTO.md)"
  exit 1
fi

OTTO_HOME="${OTTO_HOME:-$HOME/Library/Application Support/Otto}"
mkdir -p "$OTTO_HOME"
EXT="${SRC##*.}"                       # onnx or tflite
DEST="$OTTO_HOME/$(basename "$SRC")"
cp "$SRC" "$DEST"
echo "==> Installed model: $DEST"

CFG="$OTTO_HOME/config.yaml"
if [ ! -f "$CFG" ]; then
  echo "No live config at $CFG — start Otto once, then re-run."
  exit 1
fi

# Point the config at the model. (These keys already exist in the template.)
python3 - "$CFG" "$DEST" "$EXT" <<'PY'
import sys, yaml
cfg_path, model, ext = sys.argv[1], sys.argv[2], sys.argv[3]
d = yaml.safe_load(open(cfg_path)) or {}
stt = d.setdefault("voice", {}).setdefault("stt", {})
stt["wakeword_backend"] = "oww"
stt["oww_model_path"] = model
stt["oww_inference_framework"] = "tflite" if ext == "tflite" else "onnx"
yaml.safe_dump(d, open(cfg_path, "w"), sort_keys=False)
print("==> Updated config:", cfg_path)
PY

echo "==> Restarting Otto"
launchctl kickstart -k "gui/$(id -u)/com.otto.assistant" 2>/dev/null \
  || echo "   (service not installed as a login item — restart Otto however you run it)"

echo ""
echo "Done. Say \"hey otto\" — the trained model detects it acoustically, no Whisper needed."
