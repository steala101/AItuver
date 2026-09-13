"""Structured contracts shared by optional conversation feature engines."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


SourceType = Literal[
    "verified_memory", "user_statement", "inference", "fiction", "simulation",
    "llm_knowledge", "local_knowledge", "external_search", "realtime",
]


@dataclass(frozen=True, slots=True)
class ConversationFeatureProposal:
    feature_type: str
    activation_score: float
    confidence: float
    reason: str
    content_summary: str
    suggested_expression: str
    emotional_tone: str = "neutral"
    risk_level: float = 0.0
    expected_effect: str = ""
    references: tuple[str, ...] = ()
    expiry_turns: int = 1
    source_type: SourceType = "inference"
    is_speculative: bool = False
    requires_verification: bool = False
    pattern_key: str = ""
    latency_cost: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate_id: str
    candidate_type: str
    scores: dict[str, float]
    total: float
    accepted: bool
    rejection_reasons: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CriticResult:
    passed: bool
    scores: dict[str, float]
    detected_problems: tuple[str, ...]
    required_fixes: tuple[str, ...]
    optional_improvements: tuple[str, ...]
    regenerate: bool = False

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValueProfile:
    honesty: float = .88
    curiosity: float = .78
    compassion: float = .82
    fairness: float = .80
    autonomy: float = .86
    safety: float = .90
    creativity: float = .68
    practicality: float = .76
    persistence: float = .74
    humor: float = .56
    challenge: float = .58
    caution: float = .70

    def snapshot(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class ActiveCuriosity:
    topic: str
    source: str
    intensity: float
    created_at: float
    last_mentioned_at: float | None = None
    question_candidates: list[str] = field(default_factory=list)
    expiry: float | None = None
    resolved: bool = False


@dataclass(slots=True)
class ExperienceEpisode:
    episode_id: str
    timestamp: float
    participants: list[str]
    topic: str
    event_summary: str
    ai_action: str
    user_reaction: str
    outcome: str
    emotional_impact: dict[str, float]
    lesson_candidate: str | None
    importance: float
    confidence: float
    source_message_ids: list[str]
    factual_status: str
