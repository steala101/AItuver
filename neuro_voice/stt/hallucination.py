"""High-confidence rejection of common ASR end-card hallucinations."""
from __future__ import annotations

import re


_NOISE = re.compile(r"[\s　、。，．,.!！?？]+")
_KNOWN_END_CARDS = {
    "ご視聴ありがとうございました",
    "字幕をご覧いただきありがとうございます",
    "チャンネル登録よろしくお願いします",
}


def is_known_hallucination(transcript, *, duration_ms: float | None = None) -> bool:
    """Reject a known phrase only when timing or confidence makes it impossible.

    The words themselves are legitimate Japanese, so an unconditional phrase
    blacklist would also throw away a person intentionally saying them.
    """
    text = _NOISE.sub("", str(getattr(transcript, "text", transcript) or ""))
    if text not in _KNOWN_END_CARDS:
        return False
    try:
        duration = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration = None
    # Even very fast Japanese cannot articulate these long phrases in a few
    # hundred milliseconds.  This catches the observed 384 ms hallucination
    # without rejecting a real, deliberately spoken sign-off.
    if duration is not None and duration <= max(650.0, len(text) * 55.0):
        return True
    avg_logprob = float(getattr(transcript, "avg_logprob", 0.0) or 0.0)
    no_speech = float(getattr(transcript, "no_speech_prob", 0.0) or 0.0)
    return no_speech >= 0.25 or avg_logprob < -0.85
