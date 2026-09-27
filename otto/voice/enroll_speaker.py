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
    d = s.voice.stt.speaker_voiceprints_dir or str(OTTO_HOME / "voiceprints")
    sv = SpeakerVerifier(model_id=s.voice.stt.speaker_model, voiceprints_dir=d)
    print(f"Loading the speaker model ({s.voice.stt.speaker_model}) — first run downloads it…",
          flush=True)
    if not sv._load():
        print("Could not load the speaker model. Is `transformers`+`torch` installed?",
              file=sys.stderr)
        sys.exit(1)
    return sv, d


def enroll(name: str, clips: int, seconds: float) -> None:
    sv, d = _make_verifier()
    samples = []
    lines = ["Read this out naturally:  \"Otto, open my project and check the build.\"",
             "Now say:  \"What's on my calendar today, and remind me at five.\"",
             "And:  \"Play some music, then close the window.\"",
             "One more, any sentence in your normal voice.",
             "Last one — speak as you normally would to the assistant."]
    for i in range(clips):
        audio = _record(seconds, f"[{i + 1}/{clips}] {lines[i % len(lines)]}")
        samples.append((audio, RATE))
    print(f"\nBuilding the voiceprint for '{name}'…", flush=True)
    saved = sv.enroll(samples, name)
    if not saved:
        print("Enrollment failed (clips too short/quiet). Try again in a quiet spot.",
              file=sys.stderr)
        sys.exit(1)
    vp = sv._prints[saved]
    sims = [sv.cosine(sv.embed(a, RATE), vp) for a, _ in samples]
    print(f"\nSaved voiceprint → {d}/{saved}.npy    (enrolled voices: {sv.names})")
    print(f"  self-similarity: min={min(sims):.2f} mean={np.mean(sims):.2f} "
          f"(expect ~0.7+ for a clean enroll)")
    print("\nSet  voice.stt.speaker_verify: true  and restart Orrin. Enroll more people by "
          "re-running with --name <other>.  Tune with --check.")


def check(clips: int, seconds: float) -> None:
    sv, d = _make_verifier()
    if not sv.names:
        print("No voices enrolled yet — run enrollment first (no --check).", file=sys.stderr)
        sys.exit(1)
    print(f"Enrolled voices: {sv.names}")
    print("\n== A registered voice ==")
    mine = [sv.match(_record(seconds, f"[registered {i + 1}/{clips}] say a command"), RATE)
            for i in range(clips)]
    print("\n== NOT registered == (play a video, or an unenrolled person)")
    other = [sv.match(_record(seconds, f"[other {i + 1}/{clips}] a different voice"), RATE)
             for i in range(clips)]
    mine = [m[1] for m in mine if m is not None]
    other = [o[1] for o in other if o is not None]
    if mine and other:
        lo, hi = min(mine), max(other)
        print(f"\n  registered: {[round(m, 2) for m in mine]}  (min {lo:.2f})")
        print(f"  others    : {[round(o, 2) for o in other]}  (max {hi:.2f})")
        rec = round((lo + hi) / 2, 2)
        if lo > hi:
            print(f"\n  clean separation ✓  recommended speaker_threshold ≈ {rec}")
        else:
            print(f"\n  overlap ⚠  re-enroll in a quiet room; or set threshold ≈ {round(hi, 2)}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Enroll / test voices for Orrin speaker verification.")
    ap.add_argument("--check", action="store_true", help="measure separation instead of enrolling")
    ap.add_argument("--name", default="me", help="name for this voice (default 'me')")
    ap.add_argument("--clips", type=int, default=5, help="number of clips (default 5)")
    ap.add_argument("--seconds", type=float, default=4.0, help="seconds per clip (default 4)")
    args = ap.parse_args()
    if args.check:
        check(max(2, args.clips), args.seconds)
    else:
        enroll(args.name, max(3, args.clips), args.seconds)


if __name__ == "__main__":
    main()
