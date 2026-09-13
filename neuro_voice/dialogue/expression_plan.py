"""Unified voice/body expression intent with a no-op avatar boundary."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VoiceExpression:
    emotion: str
    delivery: str
    intensity: float
    pace: float
    energy: float
    laugh_cue: bool = False


@dataclass(frozen=True, slots=True)
class AvatarExpressionIntent:
    expression: str
    intensity: float
    gesture: str
    gaze: str
    posture: str
    interruptible: bool = True
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class UnifiedExpressionPlan:
    response_id: str
    source: str
    conversation_move: str
    voice: VoiceExpression
    avatar: AvatarExpressionIntent
    version: int = 1

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class ExpressionSink(Protocol):
    def submit(self, plan: UnifiedExpressionPlan) -> None: ...


class NullExpressionSink:
    """Default boundary until an avatar runtime is explicitly connected."""

    def submit(self, plan: UnifiedExpressionPlan) -> None:
        return None


def build_expression_plan(plan: dict[str, Any], *, source: str) -> UnifiedExpressionPlan:
    emotion = str(plan.get("ai_emotion") or "calm")
    primary = str(plan.get("primary_style") or "direct")
    temperature = str(plan.get("temperature") or "casual")
    move = str(plan.get("conversation_move") or "EXTEND_TOPIC")
    energy = max(.05, min(1.0, float(plan.get("target_energy", .5))))
    if primary == "playful":
        delivery = "playful"
    elif temperature == "lively":
        delivery = "lively"
    elif temperature == "quiet":
        delivery = "soft"
    elif temperature == "serious":
        delivery = "serious"
    elif primary in {"empathetic", "quiet"}:
        delivery = "gentle"
    elif primary == "cautious":
        delivery = "calm"
    else:
        delivery = "warm" if temperature == "engaged" else "neutral"
    pace = {
        "lively": 1.08, "playful": 1.04, "serious": .95,
        "gentle": .94, "calm": .90, "warm": .97, "soft": .92,
    }.get(delivery, 1.0)
    expression = {
        "excited": "delighted", "happy": "smile", "surprised": "surprised",
        "concerned": "concerned", "interested": "curious",
        "gentle": "soft_smile", "calm": "neutral",
    }.get(emotion, "neutral")
    gesture = {
        "PLAYFUL_REACTION": "small_tease",
        "SHARE_OPINION": "thoughtful_beat",
        "SUPPORT": "gentle_nod",
        "REPAIR": "small_nod",
        "ASK_QUESTION": "curious_tilt",
    }.get(move, "conversational")
    return UnifiedExpressionPlan(
        response_id=str(plan.get("response_id") or ""),
        source=str(source or plan.get("source") or ""),
        conversation_move=move,
        voice=VoiceExpression(
            emotion=emotion, delivery=delivery,
            intensity=round(.35 + energy * .55, 3),
            pace=pace, energy=round(energy, 3),
            laugh_cue=move == "PLAYFUL_REACTION" and energy >= .58,
        ),
        avatar=AvatarExpressionIntent(
            expression=expression,
            intensity=round(.30 + energy * .55, 3),
            gesture=gesture,
            gaze="speaker",
            posture="engaged" if energy >= .55 else "relaxed",
            enabled=False,
        ),
    )
