"""Where a system block goes, so the server can reuse what it already read.

Measured on this machine, 40 turns:

    初トークン ≈ 1114ms + 0.241ms × (キャッシュされなかったトークン数)

An inference server keeps the key/value cache from the previous request and
reuses whatever *leading* part of the new prompt is byte-identical.  So a
block inserted near the front costs not only its own tokens but every token
after it — the persona is reusable, and nothing else is.

Every insertion site in the pipeline already carried a comment saying the
block belonged next to the user turn.  The code put it at index 1 instead.
This module is that comment, executable.

Nothing here decides *what* is sent; it only decides *where*.
"""
from __future__ import annotations

from typing import Any

Message = dict[str, Any]


def last_user_index(messages: list[Message]) -> int:
    """Index of the final user turn, or the end when there is none."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return len(messages)


def insert_before_user_turn(messages: list[Message], content: str) -> list[Message]:
    """Place a per-turn block as late as possible.

    Late means two good things at once: everything before it stays identical
    to last turn and can be reused, and the block sits closest to the words it
    is about, which is where a model weighs it most.
    """
    text = str(content or "").strip()
    if not text:
        return list(messages)
    assembled = list(messages)
    position = last_user_index(assembled)
    assembled.insert(position, {"role": "system", "content": text})
    return assembled


def insert_after_stable_prefix(messages: list[Message], content: str) -> list[Message]:
    """Place a block that does not change between turns.

    Only for content that is byte-identical turn after turn.  Anything that
    varies belongs in :func:`insert_before_user_turn`; putting it here would
    invalidate the cache for the whole prompt, which is the bug this module
    exists to fix.
    """
    text = str(content or "").strip()
    if not text:
        return list(messages)
    assembled = list(messages)
    position = 0
    while position < len(assembled) and assembled[position].get("role") == "system":
        position += 1
    assembled.insert(position, {"role": "system", "content": text})
    return assembled


def volatile_tokens(messages: list[Message], *, estimate) -> int:
    """Tokens sitting at or after the first per-turn block.

    A rough health number: this is what the server has to re-read every turn
    no matter how well the cache works.
    """
    index = last_user_index(messages)
    return sum(
        estimate(str(message.get("content") or ""))
        for message in messages[index:]
    )
