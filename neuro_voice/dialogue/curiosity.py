"""Deterministic gate for optional, natural follow-up questions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CuriosityDecision:
    score: float
    should_ask: bool
    reason: str


class CuriosityEngine:
    def __init__(self, min_turn_interval: int = 3, threshold: float = 0.62):
        self._min_turn_interval = max(1, int(min_turn_interval))
        self._threshold = float(threshold)

    def decide(
        self, *, text: str, topics: list[str], profile: dict[str, Any],
        momentum: float, turn_index: int,
    ) -> CuriosityDecision:
        if "?" in text or "？" in text:
            return CuriosityDecision(0.0, False, "user_question")
        if len(text.strip()) < 12:
            return CuriosityDecision(0.0, False, "brief_turn")
        last = int(profile.get("last_curiosity_turn", -99))
        if turn_index - last < self._min_turn_interval:
            return CuriosityDecision(0.0, False, "cooldown")
        model = profile.get("model", {})
        known = set(model.get("facts", {}).get("interests", []))
        topic_novelty = min(1.0, len(topics) / 3)
        interest_overlap = 1.0 if any(t in known for t in topics) else 0.35
        timing = max(0.0, min(1.0, (momentum - 0.30) / 0.55))
        information_gap = 0.75 if len(text) >= 28 else 0.45
        score = round(0.35 * information_gap + 0.25 * topic_novelty + 0.20 * interest_overlap + 0.20 * timing, 3)
        return CuriosityDecision(score, score >= self._threshold, "engagement_score")

    @staticmethod
    def record_actual_question(profile: dict[str, Any], reply: str, turn_index: int) -> None:
        if "?" in reply or "？" in reply:
            profile["last_curiosity_turn"] = int(turn_index)
