"""Internal machinery leaking into the spoken reply, detected by reading it.

The prompt carries a lot of internal state — plan fields, candidate lists with
scores, section headers — and models sometimes read it back out.  The response
to each incident was another sentence of prohibition, until seven places were
saying the same thing in about 300 tokens, two of them with a word-for-word
identical enumeration.

Whether a reply contains ``response_shape=`` is a property of the text, so
code decides it (第2条).  The prompt keeps one short line; this module does the
checking.

**Deliberately narrow.**  Every marker here is something a Japanese spoken
reply cannot contain by accident — an ASCII field name, a bracketed internal
tag, a section header that only exists inside our own prompt.  A false
positive silences real speech, which is far worse than a leak reaching the
ear, so anything ambiguous is left out.  「なるほど」 is not a leak, and
deciding whether it belongs is meaning, not fact — that stays with the model.
"""
from __future__ import annotations

import re

#: Markers that cannot occur in natural Japanese speech.
_MARKERS = re.compile(
    r"(?:"
    r"\[/?thought\]"                       # 思考タグ
    r"|\[/?think\]"
    r"|(?:score|engine_variant|primary_style|secondary_style|response_shape"
    r"|target_length|ask_follow_up|humor_level|social_mode|dialogue_act"
    r"|intimacy_level|creativity_level|minimum_moves|source)\s*="
    r"|候補\s*\d+\s*[:：]"
    r"|【(?:Conversation\s+Planner|Surface\s+Realizer|対話制御|Conversation\s+Critic)"
    r"|The user is asking"
    r"|Drafting (?:a )?response"
    r"|Final version"
    r"|ASK_FOLLOW_UP"
    r")",
    re.I,
)

_SENTENCE_END = re.compile(r"(?<=[。．！!？?\n])\s*")


def has_internal_leak(text: str) -> bool:
    """Whether this text exposes something that was only ever meant for us."""
    return bool(_MARKERS.search(str(text or "")))


def leak_markers(text: str) -> list[str]:
    """What was found, for logging.  Never the surrounding conversation."""
    return [match.group(0) for match in _MARKERS.finditer(str(text or ""))]


def strip_internal_leak(text: str) -> str:
    """Drop the sentences that leak, keep the ones that do not.

    Sentence granularity rather than the whole reply: a leak is usually one
    stray line at the top or bottom, and throwing away a good answer because
    of it would be its own failure (第17条).
    """
    value = str(text or "")
    if not _MARKERS.search(value):
        return value
    kept = [
        part for part in _SENTENCE_END.split(value)
        if part.strip() and not _MARKERS.search(part)
    ]
    return "".join(kept).strip()
