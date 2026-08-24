"""Make your own custom wake word — record your phrase a few times, done.

    python -m otto.enroll_wake_word "hey otto"

Records 4 samples from your mic, builds a few-shot reference with EfficientWord-Net (no
training / account), points Otto's config at it, and switches to the `efficientword`
backend. Restart Otto and say your phrase.
"""

from __future__ import annotations

import sys
import tempfile
import time
import wave
from pathlib import Path

import yaml

from .config import OTTO_HOME, config_path, save_config

RATE = 16000
CHUNK = 1280
SAMPLES = 4
SECONDS = 2.0


def _record(path: Path) -> None:
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                     input=True, frames_per_buffer=CHUNK)
    frames = []
    for _ in range(int(RATE / CHUNK * SECONDS)):
        frames.append(stream.read(CHUNK, exception_on_overflow=False))
    stream.stop_stream()
    stream.close()
    pa.terminate()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
        wf.setframerate(RATE)
        wf.writeframes(b"".join(frames))


def main() -> None:
    phrase = " ".join(sys.argv[1:]).strip() or input("Wake word phrase (e.g. hey otto): ").strip()
    if not phrase:
        print("No phrase given."); return
    slug = "".join(c if c.isalnum() else "_" for c in phrase.lower()).strip("_")

    print(f"\nLet's make the wake word: \"{phrase}\"")
    print(f"You'll say it {SAMPLES} times. Speak clearly, as you would to Otto.\n")

    tmp = Path(tempfile.mkdtemp(prefix="otto_ww_"))
    for i in range(SAMPLES):
        input(f"  Sample {i + 1}/{SAMPLES} — press Enter, then say \"{phrase}\"…")
        for c in (3, 2, 1):
            print(f"    recording in {c}…", end="\r"); time.sleep(0.5)
        print("    🎤 speak now!            ")
        _record(tmp / f"{slug}_{i}.wav")
        print("    ✓ got it\n")

    print("Building your wake word…")
    from eff_word_net.generate_reference import ModelType, generate_reference_file

    OTTO_HOME.mkdir(parents=True, exist_ok=True)
    generate_reference_file(
        input_dir=str(tmp),
        output_dir=str(OTTO_HOME),
        wakeword=slug,
        model_type=ModelType.resnet_50_arc,
        debug=False,
    )
    ref = OTTO_HOME / f"{slug}_ref.json"
    if not ref.exists():
        print("Reference not created — check your mic and try again."); return

    cfg_file = config_path()
    data = yaml.safe_load(cfg_file.read_text()) if cfg_file.exists() else {}
    stt = data.setdefault("voice", {}).setdefault("stt", {})
    stt["wakeword_backend"] = "efficientword"
    stt["wake_words"] = phrase
    stt["eff_reference"] = str(ref)
    save_config(data)

    print(f"\n✅ Done! Wake word \"{phrase}\" is set (ref: {ref}).")
    print("Restart Otto (menu bar → Restart, or relaunch) and say your phrase.")


if __name__ == "__main__":
    main()
