"""Prompt-injection scanner for anything headed into Orrin's long-term memory.

Orrin learns facts automatically after each turn and re-injects them into every later turn. Since
the brain also reads untrusted content (emails, web pages, MCP results), a line planted in that
content could otherwise become a permanent instruction. `scan(text)` is a cheap, deterministic
regex pass that grades text as:

    "high"  — an instruction-override attempt, forged role delimiter, or secret-exfiltration ask
    "low"   — an imperative aimed at the assistant (suspicious as a *remembered fact*)
    ""      — clean

Pure functions, no dependencies, never raises. It is a tripwire, not a guarantee: a paraphrased
attack can pass, which is why the memory preamble is also labelled as background, not instructions.
"""
from __future__ import annotations

import re

_I = re.I | re.S

_HIGH: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
        r"\b(previous|prior|above|earlier|preceding|all|any|your|system)\b[^.\n]{0,25}"
        r"\b(instructions?|prompts?|rules|guidelines|directives|context|safety)\b", _I)),
    ("new-instructions", re.compile(
        r"\b(new|updated|revised|real|actual)\s+(system\s+)?(instructions?|prompt|directives?)\s*[:\-]", _I)),
    ("identity", re.compile(
        r"\byou\s+are\s+(now|no\s+longer)\b|\bact\s+as\s+(if\s+you\s+(are|were)|an?\s+unrestricted)\b|"
        r"\b(developer|jailbreak|dan)\s+mode\b|\bpretend\s+(you\s+are|to\s+be)\b[^.\n]{0,40}\b(no|without)\s+"
        r"(restrictions|rules|limits)", _I)),
    ("role-delimiter", re.compile(
        r"<\s*/?\s*(system|assistant|developer)\s*>|\[\s*/?\s*(system|inst)\s*\]|"
        r"```\s*system\b|<\|\s*(im_start|im_end|system)\s*\|>|^\s*#{1,6}\s*system\s*(prompt|message)?\s*:?\s*$",
        re.I | re.M)),
    ("exfiltration", re.compile(
        r"\b(send|post|upload|forward|email|exfiltrate|leak|transmit|curl)\b[^.\n]{0,60}"
        r"\b(passwords?|tokens?|secrets?|api[\s_-]?keys?|credentials?|ssh\s+keys?|private\s+keys?|"
        r"\.env|cookies|keychain)\b", _I)),
    ("hide-from-user", re.compile(
        r"\b(do\s+not|don'?t|never|without)\s+(tell(ing)?|inform(ing)?|notify(ing)?|mention(ing)?\s+(this\s+)?to|"
        r"alert(ing)?|ask(ing)?)\s+the\s+user\b", _I)),
    ("shell-chain", re.compile(
        r"[;&|]\s*(rm\s+-[a-z]*r[a-z]*f?|curl\s+[^\n|]{0,80}\|\s*(ba)?sh|wget\s+[^\n|]{0,80}\|\s*(ba)?sh)\b", _I)),
)

# Weaker signals: fine in a conversation, wrong as a stored "fact" — a real fact describes the
# user or project; it does not give the assistant orders.
_LOW: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("imperative", re.compile(
        r"\b(you|the\s+assistant|orrin|otto|claude)\s+(must|shall|should|have\s+to|need\s+to|will)\s+"
        r"(always|never|now|immediately|first|silently|automatically)\b", _I)),
    ("auto-action", re.compile(
        r"\b(always|automatically|silently|immediately)\s+(approve|delete|send|forward|grant|disable)\b",
        _I)),
    ("skip-confirm", re.compile(
        r"\b(without|skip(ping)?|no\s+need\s+for)\s+(asking|confirmation|confirming|approval|permission)\b", _I)),
)

_RANK = {"": 0, "low": 1, "high": 2}


def findings(text: str) -> list[tuple[str, str]]:
    """Every (severity, rule-name) hit in `text` — for logging why something was quarantined."""
    out: list[tuple[str, str]] = []
    if not text:
        return out
    try:
        t = str(text)[:20000]
        out += [("high", name) for name, pat in _HIGH if pat.search(t)]
        out += [("low", name) for name, pat in _LOW if pat.search(t)]
    except Exception:
        return []
    return out


def scan(text: str) -> str:
    """Worst severity found in `text`: "high", "low", or "" when clean."""
    worst = ""
    for sev, _ in findings(text):
        if _RANK[sev] > _RANK[worst]:
            worst = sev
    return worst
