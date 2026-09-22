"""Enroll your voice for speaker verification, and check how well it separates you from others.

    python -m otto.voice.enroll_speaker            # record a few clips → save your voiceprint
    python -m otto.voice.enroll_speaker --check    # measure your-voice vs other-voice separation

Enrollment records several short clips of you speaking, averages their x-vectors into a voiceprint,
and saves it to OTTO_HOME/voiceprint.npy (or voice.stt.speaker_voiceprint). Then set
`voice.stt.speaker_verify: true` and restart Orrin — hands-free will only act on your voice.

`--check` records a few clips of YOU, then a few of "not you" (play a video, or have someone else
talk), and prints the cosine similarities + a recommended threshold, so you can tune
`speaker_threshold` before trusting the gate. Nothing is enrolled in check mode.

Everything is local (transformers x-vector model + torch, already bundled) — no account, no upload.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

RATE = 16000
CHUNK = 1024


def _record(seconds: float, prompt: str) -> np.ndarray:
    """Record `seconds` of 16 kHz mono audio from the default mic → float32 in [-1, 1]."""
    import pyaudio
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                     input=True, frames_per_buffer=CHUNK)
    print(f"\n{prompt}\n  recording {seconds:.0f}s — speak now…", flush=True)
    for i in range(3, 0, -1):
        print(f"   {i}…", end=" ", flush=True); time.sleep(0.4)
    print("go!", flush=True)
    frames = []
    for _ in range(int(RATE / CHUNK * seconds)):
        frames.append(stream.read(CHUNK, exception_on_overflow=False))
    stream.stop_stream(); stream.close(); pa.terminate()
    print("  done.", flush=True)
    return np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0


def _make_verifier():
    from ..config import OTTO_HOME, load_settings
    from .speaker import SpeakerVerifier
    s = load_settings()
    vp = s.voice.stt.speaker_voiceprint or str(OTTO_HOME / "voiceprint.npy")
    sv = SpeakerVerifier(model_id=s.voice.stt.speaker_model, voiceprint_path=vp)
    print(f"Loading the speaker model ({s.voice.stt.speaker_model}) — first run downloads it…",
          flush=True)
    if not sv._load():
        print("Could not load the speaker model. Is `transformers`+`torch` installed?",
              file=sys.stderr)
        sys.exit(1)
    return sv, vp


def enroll(clips: int, seconds: float) -> None:
    sv, vp = _make_verifier()
    samples = []
    lines = ["Read this out naturally:  \"Otto, open my project and check the build.\"",
             "Now say:  \"What's on my calendar today, and remind me at five.\"",
             "And:  \"Play some music, then close the window.\"",
             "One more, any sentence in your normal voice.",
             "Last one — speak as you normally would to the assistant."]
    for i in range(clips):
        audio = _record(seconds, f"[{i + 1}/{clips}] {lines[i % len(lines)]}")
        samples.append((audio, RATE))
    print("\nBuilding your voiceprint…", flush=True)
    if sv.enroll(samples) is None:
        print("Enrollment failed (clips too short/quiet). Try again in a quiet spot.",
              file=sys.stderr)
        sys.exit(1)
    # Self-consistency: how similar each clip is to the averaged print (sanity check).
    sims = [sv.cosine(sv.embed(a, RATE), sv._voiceprint) for a, _ in samples]
    print(f"\nSaved voiceprint → {vp}")
    print(f"  self-similarity: min={min(sims):.2f} mean={np.mean(sims):.2f} "
          f"(expect ~0.85+ for a clean enroll)")
    print("\nNext: set  voice.stt.speaker_verify: true  in your config and restart Orrin.")
    print("Tune with:  python -m otto.voice.enroll_speaker --check")


def check(clips: int, seconds: float) -> None:
    sv, vp = _make_verifier()
    if sv._voiceprint is None:
        print("No voiceprint yet — run enrollment first (no --check).", file=sys.stderr)
        sys.exit(1)
    print("\n== YOUR voice ==")
    mine = [sv.verify(_record(seconds, f"[you {i + 1}/{clips}] say a command to Otto"), RATE)
            for i in range(clips)]
    print("\n== NOT you == (play a video, or have someone else talk)")
    other = [sv.verify(_record(seconds, f"[other {i + 1}/{clips}] let a different voice speak"),
                       RATE) for i in range(clips)]
    mine = [m for m in mine if m is not None]
    other = [o for o in other if o is not None]
    print("\n--- results (cosine similarity to your voiceprint) ---")
    print(f"  you   : {[round(m, 2) for m in mine]}  (min {min(mine):.2f})")
    print(f"  others: {[round(o, 2) for o in other]}  (max {max(other):.2f})")
    if mine and other:
        lo, hi = min(mine), max(other)
        if lo > hi:
            rec = round((lo + hi) / 2, 2)
            print(f"\n  clean separation ✓  recommended speaker_threshold ≈ {rec} "
                  f"(you ≥ {lo:.2f}, others ≤ {hi:.2f})")
        else:
            print(f"\n  overlap ⚠  your min ({lo:.2f}) ≤ others' max ({hi:.2f}). Re-enroll in a "
                  f"quiet room with more clips, or accept some error at threshold ≈ {round(hi, 2)}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Enroll / test your voice for Orrin speaker verification.")
    ap.add_argument("--check", action="store_true", help="measure separation instead of enrolling")
    ap.add_argument("--clips", type=int, default=5, help="number of clips (default 5)")
    ap.add_argument("--seconds", type=float, default=4.0, help="seconds per clip (default 4)")
    args = ap.parse_args()
    if args.check:
        check(max(2, args.clips), args.seconds)
    else:
        enroll(max(3, args.clips), args.seconds)


if __name__ == "__main__":
    main()
