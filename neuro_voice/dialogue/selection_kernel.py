"""Bounded competition between conversational moves.

The kernel compares *ways of responding* before the single main LLM is
called.  It never generates prose and never stores hidden reasoning.  This is
therefore cheap enough for realtime local and Discord conversation while
still making the chosen move observable and learnable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any

from neuro_voice.dialogue.conversation_contract import (
    ASK_QUESTION,
    BRIEF_REACTION,
    DIRECT_ANSWER,
    EXTEND_TOPIC,
    HONEST_RECALL,
    PLAYFUL_REACTION,
    REPAIR,
    SAY_NOT_REMEMBERED,
    SHARE_OPINION,
    SUPPORT,
)


_ACK = re.compile(
    r"^(?:うん+|はい+|そう|そうだね|なるほど|了解|わかった|分かった|"
    r"ありがとう|へえ+|ほう|ああ|おっけー|オッケー)[。！!？?、,\s]*$"
)


@dataclass(frozen=True, slots=True)
class MoveCandidate:
    move: str
    score: float
    accepted: bool
    reasons: tuple[str, ...]
    dimensions: dict[str, float]

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SelectionResult:
    selected_move: str
    candidates: tuple[MoveCandidate, ...]
    reason_codes: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class ConversationSelectionKernel:
    """Choose one response strategy from contextual, socially safe options."""

    _MOVES = (
        DIRECT_ANSWER, BRIEF_REACTION, SHARE_OPINION, PLAYFUL_REACTION,
        EXTEND_TOPIC, ASK_QUESTION,
    )

    @staticmethod
    def _forced(
        *, intent: str, recall_requested: bool, recall_grounded: bool,
    ) -> str | None:
        if intent == "correction":
            return REPAIR
        if intent == "support":
            return SUPPORT
        if recall_requested:
            return HONEST_RECALL if recall_grounded else SAY_NOT_REMEMBERED
        return None

    def select(
        self,
        *,
        text: str,
        intent: str,
        primary_style: str,
        target_length: str,
        allow_follow_up: bool,
        momentum: float,
        recent_moves: list[str] | tuple[str, ...] = (),
        learned_weights: dict[str, float] | None = None,
        group: bool = False,
        has_memory: bool = False,
        has_association: bool = False,
        recall_requested: bool = False,
        recall_grounded: bool = False,
    ) -> SelectionResult:
        forced = self._forced(
            intent=intent,
            recall_requested=recall_requested,
            recall_grounded=recall_grounded,
        )
        if forced:
            candidate = MoveCandidate(
                forced, 1.0, True, ("hard_obligation",),
                {"obligation": 1.0, "relevance": 1.0},
            )
            return SelectionResult(forced, (candidate,), ("hard_obligation",))

        value = str(text or "").strip()
        is_ack = bool(_ACK.fullmatch(value))
        learned = learned_weights or {}
        recent = list(recent_moves)[-5:]
        candidates: list[MoveCandidate] = []
        for move in self._MOVES:
            relevance = .45
            continuity = .48 + min(.25, max(0.0, momentum - .35) * .45)
            social_fit = .72
            novelty = .72
            repeat_cost = 0.0
            reasons: list[str] = []

            if intent == "question":
                relevance += .48 if move == DIRECT_ANSWER else -.18
                reasons.append("question_requires_answer")
            elif intent in {"preference", "statement", "story"}:
                if move == SHARE_OPINION:
                    relevance += .28
                if move == EXTEND_TOPIC:
                    continuity += .18
            if is_ack:
                relevance += .52 if move == BRIEF_REACTION else -.34
                continuity -= .20 if move in {EXTEND_TOPIC, ASK_QUESTION} else 0.0
                reasons.append("acknowledgement_closure")
            if target_length == "short":
                relevance += .34 if move == BRIEF_REACTION else -.12
            if primary_style == "playful" and move == PLAYFUL_REACTION:
                relevance += .34
            if primary_style in {"opinionated", "reflective", "cautious"} and move == SHARE_OPINION:
                relevance += .24
            if has_association and move == EXTEND_TOPIC:
                continuity += .15
                reasons.append("grounded_association_available")
            if has_memory and move == EXTEND_TOPIC:
                continuity += .07
            if move == ASK_QUESTION:
                if not allow_follow_up:
                    social_fit = .05
                    reasons.append("follow_up_not_allowed")
                else:
                    relevance += .12
                if group:
                    social_fit -= .18
                    reasons.append("group_floor_cost")
            if move == PLAYFUL_REACTION and intent in {"support", "correction"}:
                social_fit = .0
                reasons.append("serious_context")

            if move in recent[-1:]:
                novelty -= .48
                reasons.append("immediate_repeat_penalty")
                if not (
                    (intent == "question" and move == DIRECT_ANSWER)
                    or (is_ack and move == BRIEF_REACTION)
                ):
                    repeat_cost = .14
            elif move in recent:
                novelty -= .18
                repeat_cost = .05
            weight = max(.70, min(1.30, float(learned.get(f"move:{move}", 1.0))))
            total = (
                .42 * relevance + .25 * continuity + .20 * social_fit
                + .13 * novelty
            ) * weight - repeat_cost
            accepted = social_fit >= .20
            if not accepted:
                total -= .50
            candidates.append(MoveCandidate(
                move=move,
                score=round(total, 4),
                accepted=accepted,
                reasons=tuple(dict.fromkeys(reasons)),
                dimensions={
                    "relevance": round(relevance, 3),
                    "continuity": round(continuity, 3),
                    "social_fit": round(social_fit, 3),
                    "novelty": round(novelty, 3),
                    "learned_weight": round(weight, 3),
                },
            ))

        ranked = sorted(
            candidates,
            key=lambda item: (item.accepted, item.score, -self._MOVES.index(item.move)),
            reverse=True,
        )
        selected = ranked[0]
        return SelectionResult(
            selected.move,
            tuple(ranked),
            tuple(dict.fromkeys((*selected.reasons, "highest_bounded_utility"))),
        )
