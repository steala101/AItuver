"""Choose how recalled memories may influence a response.

Retrieval and spoken mention are intentionally separate decisions.  A memory
can help the model maintain context without being surfaced to the user.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryUse:
    record: dict[str, Any]
    score: float
    mention_allowed: bool
    relevance: float = 0.0
    freshness: float = 0.0
    privacy_allowed: bool = True
    contradiction_status: str = "NONE"
    influence_type: str = "context"


@dataclass(frozen=True, slots=True)
class MemoryRejection:
    record: dict[str, Any]
    reason: str


_NEGATION = re.compile(
    r"(?:ない|なくなった|やめた|辞めた|嫌い|違う|訂正|今はもう|以前とは違)"
)
_TERMS = re.compile(r"[A-Za-z0-9一-龥ぁ-んァ-ヶー]{2,}")


def _term_fragments(text: str) -> set[str]:
    fragments: set[str] = set()
    for token in _TERMS.findall(str(text or "").lower()):
        fragments.add(token)
        if len(token) >= 4:
            fragments.update(token[i:i + 3] for i in range(len(token) - 2))
    return fragments


class MemoryRecallPolicy:
    def __init__(self, mention_score: float = 0.88) -> None:
        self._mention_score = max(0.0, min(1.0, float(mention_score)))

    def evaluate(
        self,
        records: list[dict[str, Any]],
        *,
        current_text: str = "",
        now: float | None = None,
    ) -> tuple[list[MemoryUse], list[MemoryRejection]]:
        now = time.time() if now is None else float(now)
        ranked: list[MemoryUse] = []
        rejected: list[MemoryRejection] = []
        current_terms = _term_fragments(current_text)
        correction_like = bool(_NEGATION.search(str(current_text or "")))
        for record in records:
            relevance = max(0.0, min(1.0, float(record.get("score", 0.0))))
            importance = max(1, min(5, int(record.get("importance", 3)))) / 5.0
            created = float(record.get("created_at", now) or now)
            age_days = max(0.0, now - created) / 86400.0
            recency = math.exp(-age_days / 180.0)
            uses = max(0, int(record.get("access_count", 0) or 0))
            score = min(1.0, relevance * 0.84 + importance * 0.10 + recency * 0.04 + min(uses, 20) * 0.001)
            memory_terms = _term_fragments(str(record.get("text") or ""))
            contradicts_current = bool(
                correction_like and current_terms and memory_terms
                and current_terms.intersection(memory_terms)
            )
            if contradicts_current:
                rejected.append(MemoryRejection(
                    record, "CURRENT_INPUT_CONTRADICTION",
                ))
                continue
            # A fresh, highly relevant memory may be mentioned naturally.  A
            # weaker candidate remains internal context only.
            ranked.append(MemoryUse(
                record=record,
                score=round(score, 3),
                mention_allowed=score >= self._mention_score,
                relevance=round(relevance, 3),
                freshness=round(recency, 3),
            ))
        return (
            sorted(ranked, key=lambda item: item.score, reverse=True),
            rejected,
        )

    def rank(
        self,
        records: list[dict[str, Any]],
        *,
        now: float | None = None,
        current_text: str = "",
    ) -> list[MemoryUse]:
        selected, _ = self.evaluate(
            records, current_text=current_text, now=now,
        )
        return selected
