"""Measure what the prompt is made of, and how much of it repeats.

The observed first-token latency is 2.5s at p50, which is roughly 80% of the
time between a person finishing a sentence and hearing a reply.  A prompt of
~6900 tokens takes about that long to evaluate on a local 12B, so the
hypothesis is that the whole delay is prompt evaluation.

If that is right, the remedy is not to cut the prompt but to stop making the
server re-evaluate text it has already seen: an inference server reuses the
key/value cache for whatever *leading* part of the prompt is byte-identical to
the previous request.  A prompt whose stable instructions sit at the front and
whose volatile blocks sit at the back is cheap; one where a per-turn block is
inserted near the top is not, because everything after the insertion differs.

So the number that matters is not "how big is the prompt" but **"how much of
the front of it is unchanged since last turn"**.  This module measures both,
and nothing here changes what is sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from neuro_voice.llm.context_budget import estimate_tokens, messages_tokens

#: Enough of a block's opening to recognise it in a log line.
_LABEL = re.compile(r"^[\s【\[]*([^\n】\]]{0,28})")


def label_of(message: dict) -> str:
    """A short name for one prompt block, taken from its own first line."""
    role = str(message.get("role") or "?")
    content = str(message.get("content") or "")
    if role != "system":
        return role
    match = _LABEL.match(content.strip())
    head = (match.group(1) if match else "").strip() or content.strip()[:24]
    return f"system:{head[:28]}"


@dataclass(frozen=True, slots=True)
class BlockProfile:
    label: str
    tokens: int
    chars: int


@dataclass(slots=True)
class PromptProfile:
    """One prompt, broken down, plus how much of it repeats the last one."""

    blocks: list[BlockProfile] = field(default_factory=list)
    total_tokens: int = 0
    #: Leading tokens identical to the previous prompt — the reusable part.
    reusable_prefix_tokens: int = 0
    #: How many whole leading messages were identical.
    reusable_messages: int = 0
    #: First message index that differed, for pointing at the culprit.
    first_changed_label: str = ""
    #: Inside the biggest block: which of its sections actually changed.
    sections: list[dict] = field(default_factory=list)
    biggest_label: str = ""

    @property
    def reuse_ratio(self) -> float:
        if self.total_tokens <= 0:
            return 0.0
        return round(self.reusable_prefix_tokens / self.total_tokens, 3)

    def snapshot(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "reusable_prefix_tokens": self.reusable_prefix_tokens,
            "reuse_ratio": self.reuse_ratio,
            "reusable_messages": self.reusable_messages,
            "first_changed": self.first_changed_label,
            "biggest": self.biggest_label,
            "stable_section_tokens": self.stable_section_tokens,
            "sections": list(self.sections),
            "blocks": [
                {"label": b.label, "tokens": b.tokens} for b in self.blocks
            ],
        }

    @property
    def stable_section_tokens(self) -> int:
        """Tokens inside the biggest block that did *not* change this turn."""
        return sum(row["tokens"] for row in self.sections if not row["changed"])

    def summary(self) -> str:
        """One log line: the split, then the four biggest blocks."""
        biggest = sorted(self.blocks, key=lambda b: -b.tokens)[:4]
        parts = " / ".join(f"{b.label}={b.tokens}" for b in biggest)
        return (
            f"合計={self.total_tokens}tok "
            f"再利用可={self.reusable_prefix_tokens}tok ({self.reuse_ratio:.0%}) "
            f"最初に変わった所={self.first_changed_label or '(なし)'} | {parts}"
        )

    def section_summary(self) -> str:
        """Which parts of the biggest block are worth hoisting in front of it."""
        if not self.sections:
            return ""
        changed = [row for row in self.sections if row["changed"]]
        stable = [row for row in self.sections if not row["changed"]]
        show = lambda rows: "、".join(  # noqa: E731 - single expression, used twice
            f"{row['name']}({row['tokens']})" for row in sorted(
                rows, key=lambda r: -r["tokens"],
            )[:6]
        ) or "なし"
        return (
            f"{self.biggest_label} の内訳: "
            f"変わらない={self.stable_section_tokens}tok [{show(stable)}] / "
            f"変わった=[{show(changed)}]"
        )


#: Sections inside one concatenated system message.  The Mind context is
#: assembled as `"\n\n".join(parts)` where each part opens with its own 【…】
#: heading, so one 5000-token message is really a dozen blocks glued together.
_SECTION = re.compile(r"(?m)^(?:【([^】\n]{1,40})】|\[([A-Z][^\]\n]{1,70})\])")


def split_sections(content: str) -> list[tuple[str, str]]:
    """Break a concatenated system message into its named sections."""
    text = str(content or "")
    marks = list(_SECTION.finditer(text))
    if not marks:
        return [("(見出しなし)", text)]
    sections: list[tuple[str, str]] = []
    if marks[0].start() > 0:
        head = text[:marks[0].start()].strip()
        if head:
            sections.append(("(冒頭)", head))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        name = (mark.group(1) or mark.group(2) or "").strip()
        sections.append((name[:28], text[mark.start():end]))
    return sections


def section_report(previous: str, current: str) -> list[dict]:
    """Per-section size and whether it changed since the previous turn.

    This is what decides where each section belongs: a section that is
    identical turn after turn can sit in the cacheable prefix, one that
    changes every turn has to go after it.
    """
    old = dict(split_sections(previous)) if previous else {}
    rows = []
    for name, body in split_sections(current):
        rows.append({
            "name": name,
            "tokens": estimate_tokens(body),
            "changed": old.get(name) != body,
            "new": name not in old,
        })
    return rows


def _common_prefix_chars(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class PromptProfiler:
    """Remembers the previous prompt so the reusable prefix can be measured."""

    def __init__(self) -> None:
        self._previous: list[dict] = []

    def profile(self, messages: list[dict]) -> PromptProfile:
        current = list(messages or [])
        blocks = [
            BlockProfile(
                label=label_of(message),
                tokens=estimate_tokens(str(message.get("content") or "")),
                chars=len(str(message.get("content") or "")),
            )
            for message in current
        ]
        total = messages_tokens(current)

        reusable_tokens = 0
        reusable_messages = 0
        first_changed = ""
        for index, message in enumerate(current):
            if index >= len(self._previous):
                first_changed = blocks[index].label if index < len(blocks) else ""
                break
            old = str(self._previous[index].get("content") or "")
            new = str(message.get("content") or "")
            if old == new and self._previous[index].get("role") == message.get("role"):
                reusable_tokens += blocks[index].tokens
                reusable_messages += 1
                continue
            # A partly shared block still shortens the evaluation.
            shared = _common_prefix_chars(old, new)
            if shared:
                reusable_tokens += estimate_tokens(new[:shared])
            first_changed = blocks[index].label
            break
        else:
            first_changed = ""

        # The biggest block is the one worth taking apart: if most of it is
        # unchanged, it is in the wrong place rather than too large.
        sections: list[dict] = []
        biggest_label = ""
        if blocks:
            index = max(range(len(blocks)), key=lambda i: blocks[i].tokens)
            biggest_label = blocks[index].label
            previous_body = ""
            if index < len(self._previous):
                previous_body = str(self._previous[index].get("content") or "")
            sections = section_report(
                previous_body, str(current[index].get("content") or ""),
            )

        self._previous = [
            {"role": m.get("role"), "content": str(m.get("content") or "")}
            for m in current
        ]
        return PromptProfile(
            blocks=blocks,
            total_tokens=total,
            reusable_prefix_tokens=reusable_tokens,
            reusable_messages=reusable_messages,
            first_changed_label=first_changed,
            sections=sections,
            biggest_label=biggest_label,
        )

    def reset(self) -> None:
        self._previous = []
