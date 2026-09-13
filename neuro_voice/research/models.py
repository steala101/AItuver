"""Value objects for the autonomous research boundary.

They contain no raw conversation audio or unredacted user utterance.  The
persisted question is a safe, short topic/question produced by the caller.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import time
import uuid
from typing import Any


class ResearchMode(StrEnum):
    OFF = "OFF"
    ASK_FIRST = "ASK_FIRST"
    LOW_RISK_AUTO = "LOW_RISK_AUTO"
    FULL_AUTO_WITH_LIMITS = "FULL_AUTO_WITH_LIMITS"


class ResearchReason(StrEnum):
    UNRESOLVED_QUESTION = "UNRESOLVED_QUESTION"
    KNOWLEDGE_GAP = "KNOWLEDGE_GAP"
    GOAL_BLOCKED = "GOAL_BLOCKED"
    FAILED_ACTION = "FAILED_ACTION"
    USER_REQUEST = "USER_REQUEST"
    USER_INTEREST = "USER_INTEREST"
    PERSONAL_CURIOSITY = "PERSONAL_CURIOSITY"
    OUTDATED_KNOWLEDGE = "OUTDATED_KNOWLEDGE"
    CONFLICTING_INFORMATION = "CONFLICTING_INFORMATION"
    TOOL_RESULT_NEEDS_CONTEXT = "TOOL_RESULT_NEEDS_CONTEXT"
    GAME_EVENT_NEEDS_EXPLANATION = "GAME_EVENT_NEEDS_EXPLANATION"


class ResearchStatus(StrEnum):
    PENDING = "PENDING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    RESEARCHING = "RESEARCHING"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    LEARNED = "LEARNED"
    PARTIALLY_LEARNED = "PARTIALLY_LEARNED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    EXPIRED = "EXPIRED"


class GateDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DEFER = "DEFER"
    BLOCK = "BLOCK"
    ALREADY_KNOWN = "ALREADY_KNOWN"
    RECENTLY_RESEARCHED = "RECENTLY_RESEARCHED"


@dataclass(slots=True)
class ResearchQuestion:
    title: str
    question: str
    reason_code: str
    creator: str = "AUTONOMY"
    priority: float = 0.65
    expected_value: float = 0.65
    risk_level: str = "LOW"
    privacy_scope: str = "PERSONA_ONLY"
    source_event_ids: list[str] = field(default_factory=list)
    related_goal_ids: list[str] = field(default_factory=list)
    related_thread_ids: list[str] = field(default_factory=list)
    related_interest_ids: list[str] = field(default_factory=list)
    allowed_query_terms: list[str] = field(default_factory=list)
    forbidden_query_terms: list[str] = field(default_factory=list)
    question_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    earliest_research_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + 7 * 86400)
    attempt_count: int = 0
    status: str = ResearchStatus.PENDING.value
    last_failure_reason: str = ""
    report_policy: str = "REPORT_WHEN_RELEVANT"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchQuestion":
        permitted = {item.name for item in __import__("dataclasses").fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in permitted})


@dataclass(frozen=True, slots=True)
class ResearchGateResult:
    decision: GateDecision
    reason: str
    sanitized_query: str = ""
    redactions: tuple[str, ...] = ()
    utility: float = 0.0
    category: str = "general"

