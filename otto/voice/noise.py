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


# Computer-directed action verbs. An utterance that (after stripping polite/filler lead-ins) STARTS
# with one of these is a deliberate command — trust it and skip the flaky model gate, so real
# chained follow-ups like "close the calculator" / "open chrome" are never dropped as ambient.
_COMMAND_VERBS = {
    "open", "close", "click", "double-click", "right-click", "tap", "type", "run", "show", "hide",
    "go", "navigate", "play", "pause", "resume", "stop", "start", "switch", "read", "write", "edit",
    "search", "find", "look", "delete", "remove", "move", "drag", "scroll", "press", "select",
    "copy", "paste", "cut", "save", "send", "add", "create", "make", "set", "turn", "mute", "unmute",
    "focus", "bring", "take", "launch", "quit", "compute", "calculate", "list", "get", "put",
    "restart", "reload", "refresh", "clear", "undo", "redo", "zoom", "maximize", "minimize",
    "fix", "build", "check", "commit", "push", "pull", "install", "update", "deploy", "test",
    "summarize", "explain", "tell", "give", "remind", "schedule", "call", "email", "message",
    "screenshot", "capture", "record", "translate", "define", "count", "sort", "filter", "enable",
    "disable", "connect", "disconnect", "download", "upload", "print", "draw", "generate",
    # Hinglish imperatives (#i18n Tier 1): kholo=open, band=close, chalao=play, dikhao=show,
    # likho=type, karo=do, batao=tell, dhundo=search, banao=make, bhejo=send, khojo=find,
    # padho=read, chalu=start, bandh=close, ruko=stop.
    "kholo", "band", "bandh", "chalao", "chalaao", "dikhao", "dikhaao", "likho", "karo",
    "batao", "bataao", "dhundo", "dhundho", "khojo", "banao", "banaao", "bhejo", "padho",
    "chalu", "kholdo", "kardo", "dedo", "laao", "lao", "suno",
}
# Polite request prefixes (multi-word) peeled before checking the verb: "can you open…".
_POLITE_PREFIXES = ("can you", "could you", "would you", "will you", "can we", "could we",
                    "hey otto", "ok otto", "okay otto", "hey orrin")
# Single-word lead-ins to peel: STT artifacts ("to close…"), politeness, filler, direct address.
# Deliberately EXCLUDES subject pronouns (i/me/we/you) and desiderative verbs (want/need) — those
# signal a DECLARATIVE statement ("I start therapy…"), not an imperative command, so peeling them
# would misread ambient narration as a command.
_COMMAND_LEADINS = {
    "to", "okay", "ok", "so", "um", "uh", "well", "please", "pls", "now", "then", "and", "also",
    "just", "hey", "hi", "otto", "orrin", "yo", "kindly", "quickly",
}


def looks_like_command(text: str) -> bool:
    """Fast, deterministic: does this read as a deliberate command to the assistant? True for
    imperatives that start with an action verb (after peeling polite/filler/address lead-ins) and
    for anything explicitly addressed to Orrin. Used to skip the model verify gate on the
    hands-free path so genuine chained commands aren't dropped (the gate over-rejects short ones).
    Conservative on declaratives ("I start my therapy") so ambient narration still hits the gate."""
    n = _norm(text)
    if not n:
        return False
    # Peel a polite multi-word prefix first ("can you open…" → "open…").
    for pre in _POLITE_PREFIXES:
        if n == pre:
            return False                       # just politeness, no command
        if n.startswith(pre + " "):
            n = n[len(pre) + 1:]
            break
    words = n.split()
    if not words:
        return False
    # Explicit address to Orrin with content ("otto, do X") is a command.
    if words[0] in ("otto", "orrin") and len(words) >= 2:
        return True
    # Peel remaining single-word filler (never strip the whole utterance away).
    i = 0
    while i < len(words) - 1 and words[i] in _COMMAND_LEADINS:
        i += 1
    return words[i] in _COMMAND_VERBS


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
    "You are a filter for speech-to-text output that may be hallucinated from background noise, "
    "or picked up from MEDIA playing near the mic (music, a video, a podcast, a TV show). "
    "Decide whether the transcript is a coherent, intentional request or statement a person would "
    "actually say TO a voice assistant right now. NOT real: random fragments, word salad, "
    "grammatical-but-nonsensical phrases, AND song lyrics / dialogue / narration overheard from "
    "media (e.g. 'I'm gonna be the one who will be', 'I'm gonna get a little bit of a break', "
    "'baby don't hurt me') — these are not addressed to the assistant even though they're "
    "grammatical. A real command asks for something, asks a question, or gives the assistant an "
    "instruction. Reply with EXACTLY one word: YES (real) or NO (noise/media)."
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
