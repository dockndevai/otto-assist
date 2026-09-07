"""Optional transcript cleanup — strip filler/false-starts before a transcript is used.

A cheap one-shot model pass (default `haiku`) that removes "um/uh", stutters, and obvious
mis-recognitions and fixes punctuation, WITHOUT answering, summarizing, or changing meaning.
Best-effort: on any failure (or a suspiciously large rewrite) it returns the original text, so
it never blocks or corrupts input. Off by default (`voice.stt.refine`); most useful for the
dictation-paste path, where the text is inserted verbatim.
"""

from __future__ import annotations

import sys

_SYS = (
    "You clean up a raw speech-to-text transcript. Remove filler words (um, uh, er), false "
    "starts and stutters, and fix obvious mis-recognitions, capitalization, and punctuation. "
    "KEEP the speaker's wording and meaning — do NOT answer, reply, summarize, translate, "
    "explain, or add anything. Output ONLY the cleaned transcript text, nothing else."
)


async def refine_transcript(text: str, model: str = "haiku") -> str:
    """Return a cleaned version of `text`, or `text` unchanged on any problem."""
    text = (text or "").strip()
    if not text:
        return text
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ClaudeSDKClient, TextBlock)
    except Exception:
        return text
    opts = ClaudeAgentOptions(
        system_prompt=_SYS,
        setting_sources=[],
        permission_mode="default",
        **({"model": model} if model else {}),
    )
    out = ""
    try:
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        try:
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            out += block.text
        finally:
            await client.disconnect()
    except Exception as e:
        print(f"[otto] refine failed: {e!r} (using raw transcript)",
              file=sys.stderr, flush=True)
        return text
    cleaned = out.strip()
    # Guard against a refusal/expansion (the model answering instead of cleaning): if it grew
    # a lot, or came back empty, keep the original.
    if not cleaned or len(cleaned) > 3 * len(text) + 40:
        return text
    return cleaned
