"""Pure contracts shared by Local and Discord Web-search routes.

The values in these objects are process-local.  Evidence text and the query are
deliberately omitted from ordinary Trace serialization by their consumers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class SearchDisposition(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    EXECUTE = "EXECUTE"
    BLOCKED = "BLOCKED"
    CLARIFY = "CLARIFY"
    REUSE_EVIDENCE = "REUSE_EVIDENCE"
    DEFERRED = "DEFERRED"


class SearchAnswerMode(str, Enum):
    BRIEF = "BRIEF"
    EXPLANATION = "EXPLANATION"
    ENDING = "ENDING"
    OFFICIAL_URL = "OFFICIAL_URL"


@dataclass(frozen=True, slots=True)
class SearchRouteDecision:
    disposition: SearchDisposition
    reason_code: str
    query: str = ""
    answer_mode: SearchAnswerMode = SearchAnswerMode.BRIEF


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    evidence_id: str
    result_id: str
    start: int
    end: int
    text: str
    content_hash: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    status: str
    route: SearchRouteDecision
    results: tuple[Any, ...] = ()
    evidence: tuple[EvidenceSpan, ...] = ()
    answerability: str = "NONE"
    error_code: str = ""
    latency_ms: float = 0.0


@dataclass(slots=True)
class EvidenceCacheEntry:
    outcome: SearchOutcome
    persona_id: str
    surface: str
    audience: str
    epoch: int
    source_turn_id: str
    expires_at: float


class SearchEvidenceCache:
    """Small, process-local evidence continuation store.

    It owns no persistent state.  A caller supplies monotonic time so tests and
    application code share the same expiry semantics.
    """

    def __init__(self, *, ttl_seconds: float = 600.0, clock: Callable[[], float]):
        self._ttl = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._entries: dict[tuple[str, str, str], EvidenceCacheEntry] = {}

    def put(
        self, outcome: SearchOutcome, *, persona_id: str, surface: str,
        audience: str, epoch: int, source_turn_id: str,
    ) -> None:
        if outcome.status != "SUCCEEDED" or not outcome.evidence:
            return
        key = (str(persona_id), str(surface), str(audience))
        self._entries[key] = EvidenceCacheEntry(
            outcome=outcome, persona_id=key[0], surface=key[1], audience=key[2],
            epoch=int(epoch), source_turn_id=str(source_turn_id),
            expires_at=self._clock() + self._ttl,
        )

    def get(
        self, *, persona_id: str, surface: str, audience: str, epoch: int,
    ) -> EvidenceCacheEntry | None:
        key = (str(persona_id), str(surface), str(audience))
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.epoch != int(epoch) or entry.expires_at <= self._clock():
            self._entries.pop(key, None)
            return None
        return entry

    def clear(self, *, persona_id: str | None = None, surface: str | None = None) -> None:
        for key in list(self._entries):
            if persona_id is not None and key[0] != str(persona_id):
                continue
            if surface is not None and key[1] != str(surface):
                continue
            self._entries.pop(key, None)


__all__ = [
    "EvidenceSpan", "SearchAnswerMode", "SearchDisposition",
    "SearchEvidenceCache", "SearchOutcome", "SearchRouteDecision",
]
