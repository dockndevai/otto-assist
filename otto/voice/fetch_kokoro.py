"""Download the Kokoro model files so the local neural voice can run.

    python -m otto.voice.fetch_kokoro

Fetches the ONNX model + voice pack into ``OTTO_HOME/models/kokoro`` (or the configured
``voice.tts.kokoro_model_dir``). Idempotent — existing files are left alone. After this, set
``voice.tts.engine: kokoro`` (Settings) and restart.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from ..config import load_settings
from .tts import kokoro_model_dir

# Official kokoro-onnx model release (v1.0).
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {"kokoro-v1.0.onnx": f"{BASE}/kokoro-v1.0.onnx",
         "voices-v1.0.bin": f"{BASE}/voices-v1.0.bin"}


def _download(url: str, dest: Path) -> None:
    def hook(blocks: int, block_size: int, total: int) -> None:
        if total > 0:
            pct = min(100, blocks * block_size * 100 // total)
            print(f"\r  {dest.name}: {pct}%", end="", flush=True)
    urllib.request.urlretrieve(url, dest, reporthook=hook)  # noqa: S310 (known GitHub URL)
    print()


def main() -> int:
    mdir = Path(kokoro_model_dir(load_settings()))
    mdir.mkdir(parents=True, exist_ok=True)
    print(f"Fetching Kokoro model files into {mdir}")
    for name, url in FILES.items():
        dest = mdir / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  {name}: already present, skipping")
            continue
        try:
            _download(url, dest)
        except Exception as e:
            print(f"  {name}: FAILED ({e})", file=sys.stderr)
            return 1
    print("Done. Set voice.tts.engine to 'kokoro' (Settings) and restart Orrin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
