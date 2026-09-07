"""Reject Whisper hallucinations so ambient noise doesn't become a command.

Two layers:

* :func:`is_noise` — a free, synchronous heuristic that drops the obvious junk faster-whisper
  emits on silence/noise: empty or punctuation-only text, one-word fillers, and the looping
  repetition Whisper falls into ("I was a company, I started on a tour of my team" ×6).
* :func:`is_real_command` — a cheap model gate for the *hands-free* path, which catches the
  hard case the heuristic can't: grammatical-but-nonsensical hallucinations ("check the guitar
  back duty"). Fail-open (returns True on any error) so a real command is never lost to a hiccup.
"""

from __future__ import annotations

import re
import sys
from collections import Counter

# Short phrases faster-whisper commonly hallucinates on silence (lowercased, punctuation-stripped).
_HALLUCINATION_PHRASES = {
    "", "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "thankyou", "please subscribe", "subscribe", "like and subscribe", "bye", "bye bye",
    "okay", "ok", "hmm", "mhm", "uh", "um", "oh", "huh", "yeah", "so", "the", "i", "a",
}


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", (text or "")).strip().lower()


def is_noise(text: str) -> bool:
    """True if `text` is almost certainly hallucinated noise, not a real utterance."""
    t = (text or "").strip()
    if not t:
        return True
    n = _norm(t)
    if not n or len(n) <= 2:                      # empty / punctuation-only / "s." / "r."
        return True
    words = n.split()
    if n in _HALLUCINATION_PHRASES:
        return True
    if len(words) < 2 and n in _HALLUCINATION_PHRASES:
        return True
    # Whisper looping: a repeated 3-word shingle, or one token dominating the utterance.
    if len(words) >= 6:
        shingles = [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]
        if shingles and max(Counter(shingles).values()) >= 3:
            return True
        if Counter(words).most_common(1)[0][1] > len(words) * 0.5:
            return True
    return False


_VERIFY_SYS = (
    "You are a filter for speech-to-text output that may be hallucinated from background noise. "
    "Decide whether the transcript is a coherent, intentional request or statement a person "
    "would actually say to a voice assistant. Random fragments, word salad, or grammatical-but-"
    "nonsensical phrases are NOT real. Reply with EXACTLY one word: YES or NO."
)


async def is_real_command(text: str, model: str = "haiku") -> bool:
    """Model gate for hands-free input: is this a real command or a hallucination? Fail-open."""
    t = (text or "").strip()
    if not t:
        return False
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ClaudeSDKClient, TextBlock)
    except Exception:
        return True
    opts = ClaudeAgentOptions(system_prompt=_VERIFY_SYS, setting_sources=[],
                              permission_mode="default",
                              **({"model": model} if model else {}))
    out = ""
    try:
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        try:
            await client.query(t)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            out += b.text
        finally:
            await client.disconnect()
    except Exception as e:
        print(f"[otto] command verify failed: {e!r} (letting it through)",
              file=sys.stderr, flush=True)
        return True
    ans = out.strip().lower()
    return not ans.startswith("no")               # anything but a clear NO passes
