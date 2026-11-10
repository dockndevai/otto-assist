"""Download the Smart Turn v3 model so learned turn detection can run.

    python -m otto.voice.fetch_turn_model

Fetches the ~8 MB INT8 ONNX from Pipecat's `smart-turn-v3` release into ``OTTO_HOME/models/smart-turn``
(or the configured ``voice.stt.turn_model_dir``) via huggingface_hub (already a dependency). The repo
publishes a few ONNX variants across versions; we discover the .onnx files in the repo and prefer a
quantized/CPU build for size, saving it as ``smart-turn-v3.onnx``. Idempotent — an existing file is
left alone. After this, set ``voice.stt.native_audio: true`` (and keep ``turn_model: auto``) and
restart; Smart Turn then drives end-of-turn and the regex endpointer becomes the fallback.

The model is BSD-2 licensed (github.com/pipecat-ai/smart-turn).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ..config import load_settings
from .turn_model import turn_model_dir

# Repos to try, in order. Each is scanned for its .onnx files (names vary by version).
REPOS = ["pipecat-ai/smart-turn-v3"]
DEST_NAME = "smart-turn-v3.onnx"


def _pick_onnx(files: list[str]) -> str | None:
    onnx = [f for f in files if f.lower().endswith(".onnx")]
    if not onnx:
        return None
    # Prefer a quantized / CPU build (smaller) when several are offered.
    def rank(f: str) -> tuple[int, str]:
        fl = f.lower()
        quant = any(k in fl for k in ("int8", "q8", "quant", "cpu"))
        return (0 if quant else 1, f)
    return sorted(onnx, key=rank)[0]


def main() -> int:
    mdir = Path(turn_model_dir(load_settings()))
    mdir.mkdir(parents=True, exist_ok=True)
    dest = mdir / DEST_NAME
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  {DEST_NAME}: already present in {mdir}, skipping")
        return 0

    try:
        from huggingface_hub import hf_hub_download, list_repo_files
    except Exception as e:
        print(f"huggingface_hub not available ({e}); install the voice extra.", file=sys.stderr)
        return 1

    print(f"Fetching Smart Turn v3 model into {mdir}")
    for repo in REPOS:
        try:
            pick = _pick_onnx(list_repo_files(repo))
            if not pick:
                print(f"  {repo}: no .onnx found, trying next", file=sys.stderr)
                continue
            local = hf_hub_download(repo_id=repo, filename=pick)
            shutil.copyfile(local, dest)
            print(f"  got {repo}/{pick} → {dest}")
            print("Done. Set voice.stt.native_audio: true (Settings) and restart Orrin.")
            return 0
        except Exception as e:
            print(f"  {repo}: FAILED ({e})", file=sys.stderr)
    print("Could not fetch the model from any known repo.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
