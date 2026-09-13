"""Keeping the prompt small enough that the answer still fits.

A context window is shared between what we send and what the model may say
back.  Trimming history by *turn count* ignores that turns differ wildly in
size: a few paragraphs of story narration fill a 4096-token window in half a
dozen exchanges, and then generation stops after a handful of characters with
``finish_reason=length`` — the assistant appears to cut off mid-sentence.

So the budget is counted in tokens, and space for the reply is reserved before
any history is admitted.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_ASCII = re.compile(r"[\x00-\x7F]")


def estimate_tokens(text: str) -> int:
    """Approximate token count, deliberately erring on the high side.

    Japanese characters are roughly one token each for the tokenizers in use,
    while ASCII runs about four characters per token.  Underestimating here
    means truncation, so the estimate leans pessimistic.
    """
    value = str(text or "")
    if not value:
        return 0
    ascii_chars = len(_ASCII.findall(value))
    other = len(value) - ascii_chars
    return int(ascii_chars / 3.5 + other * 1.05) + 1


def messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    total = 0
    for message in messages or ():
        total += estimate_tokens(str(message.get("content", "")))
        total += 4        # role/separator overhead per message
    return total


#: Headroom above max_tokens for the chat template, tool blocks and the
#: estimator's own error.  Reserving exactly max_tokens leaves no slack.
_RESERVE_MARGIN = 256


def resolve_reserve_tokens(
    configured: Any, *, max_tokens: int, context_tokens: int,
) -> int:
    """How much of the window to keep free for the reply.

    This must follow ``max_tokens``: they are two halves of one decision.  A
    hardcoded reserve silently under-provisions the moment someone raises the
    reply length, and the symptom is a sentence stopping mid-word — which looks
    nothing like a configuration problem.
    """
    max_tokens = max(1, int(max_tokens))
    context_tokens = max(1, int(context_tokens))
    derived = max_tokens + _RESERVE_MARGIN
    if configured in (None, "", "auto", "Auto", "AUTO"):
        reserve = derived
    else:
        try:
            reserve = int(configured)
        except (TypeError, ValueError):
            logger.warning(
                "llm.context_reserve_tokens が数値ではありません (%r)。自動値 %d を使います",
                configured, derived,
            )
            reserve = derived
        if reserve < max_tokens:
            logger.warning(
                "llm.context_reserve_tokens=%d は llm.max_tokens=%d より小さく、"
                "応答が途中で切れます。%d へ引き上げます。",
                reserve, max_tokens, derived,
            )
            reserve = derived
    if reserve >= context_tokens:
        # Nothing would be left for the prompt at all.
        capped = max(1, context_tokens // 2)
        logger.warning(
            "出力予約 %d が num_ctx=%d を占め切ります。%d へ制限します。",
            reserve, context_tokens, capped,
        )
        reserve = capped
    return reserve


@dataclass(slots=True)
class BudgetResult:
    messages: list[dict[str, Any]]
    dropped: int = 0
    estimated_tokens: int = 0
    budget: int = 0

    @property
    def trimmed(self) -> bool:
        return self.dropped > 0


def fit_messages(
    messages: list[dict[str, Any]],
    *,
    context_tokens: int,
    reserve_tokens: int,
    keep_recent: int = 2,
    slack_ratio: float = 0.0,
) -> BudgetResult:
    """Drop the oldest dialogue until the prompt leaves room for a reply.

    System messages are never dropped: they carry the persona, the turn's
    authoritative decision and any retrieved evidence, and losing them silently
    changes who the assistant is.  Only the oldest user/assistant exchanges go,
    which is the same information the conversation history was already
    forgetting — just measured properly.

    ``slack_ratio`` trims further than strictly necessary, on purpose.  Cutting
    to exactly the budget means cutting again next turn at a slightly different
    place, and a history whose first message keeps changing cannot be reused by
    the server: measured, the boundary moved every single turn and held reuse
    at 30-35%.  Trimming with slack means several turns can pass with an
    identical prefix before the next cut is needed.
    """
    original = list(messages or [])
    budget = max(256, int(context_tokens) - max(0, int(reserve_tokens)))
    if messages_tokens(original) <= budget:
        return BudgetResult(original, 0, messages_tokens(original), budget)
    # Only the cut itself uses slack; whether to cut at all uses the real
    # budget, so a prompt that already fits is never trimmed.
    budget = max(256, int(budget * (1.0 - max(0.0, min(0.5, float(slack_ratio))))))

    system_indices = [
        index for index, message in enumerate(original)
        if message.get("role") == "system"
    ]
    dialogue_indices = [
        index for index, message in enumerate(original)
        if message.get("role") != "system"
    ]
    # The most recent exchanges are what the reply must actually answer.
    protected = set(system_indices) | set(dialogue_indices[-max(1, keep_recent):])
    keep = set(protected)
    total = sum(
        estimate_tokens(str(original[index].get("content", ""))) + 4
        for index in protected
    )
    for index in reversed(dialogue_indices):
        if index in keep:
            continue
        cost = estimate_tokens(str(original[index].get("content", ""))) + 4
        if total + cost > budget:
            continue
        keep.add(index)
        total += cost

    fitted = [message for index, message in enumerate(original) if index in keep]
    dropped = len(original) - len(fitted)
    if dropped:
        logger.info(
            "文脈長のため古い会話を%d件除外 (見積%dtok / 予算%dtok / ctx=%d)",
            dropped, messages_tokens(original), budget, context_tokens,
        )
    return BudgetResult(fitted, dropped, total, budget)
