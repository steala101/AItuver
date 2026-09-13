"""Offline contract scoring for synthetic conversation-quality fixtures."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


HUMAN_AXES = frozenset({"naturalness", "emotional_fit", "voice_quality"})


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    explicit_search_successes: int
    unmeasured_axes: set[str]
    failed_delivery_turns: int
    total_cases: int
    case_results: tuple[CaseEvaluation, ...]
    axis_scores: dict[str, float | None]


def evaluate_events(events: Iterable[dict[str, Any]]) -> EvaluationReport:
    """Score observable contracts without pretending to judge human quality."""
    results: list[CaseEvaluation] = []
    successful_searches = 0
    failed_delivery = 0
    for index, raw in enumerate(events):
        event = dict(raw or {})
        failures: list[str] = []
        explicit = bool(event.get("explicit_search_requested", False))
        expected_failure_path = bool(event.get("expected_failure_path", False))
        attempted = bool(event.get("search_attempted", False))
        completed = bool(event.get("search_completed", False))
        grounded = bool(event.get("answer_grounded", False))
        delivered = bool(event.get("delivery_finalized", False))
        if event.get("contract_passed") is False:
            failures.append("fixture_contract_mismatch")
        if explicit and not expected_failure_path and not attempted:
            failures.append("explicit_search_not_attempted")
        if explicit and not expected_failure_path and attempted and not completed:
            failures.append("explicit_search_not_completed")
        if explicit and not expected_failure_path and completed and not grounded:
            failures.append("search_answer_not_grounded")
        if not delivered:
            failures.append("delivery_not_finalized")
            failed_delivery += 1
        if explicit and attempted and completed and grounded and delivered:
            successful_searches += 1
        results.append(CaseEvaluation(
            case_id=str(event.get("case_id") or f"case-{index + 1}"),
            passed=not failures,
            failures=tuple(failures),
        ))
    return EvaluationReport(
        explicit_search_successes=successful_searches,
        unmeasured_axes=set(HUMAN_AXES),
        failed_delivery_turns=failed_delivery,
        total_cases=len(results),
        case_results=tuple(results),
        axis_scores={axis: None for axis in HUMAN_AXES},
    )


def load_corpus(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", []) if isinstance(payload, dict) else []
    if not isinstance(cases, list):
        raise ValueError("conversation quality corpus requires a cases list")
    return [dict(item) for item in cases if isinstance(item, dict)]


def evaluate_offline_corpus(cases: Iterable[dict[str, Any]]) -> EvaluationReport:
    """Exercise the real route/outcome boundary with fixture-only search I/O."""
    from neuro_voice.cognition.search_result_presenter import (
        execute_search_route, present_search_outcome,
    )
    from neuro_voice.search.contracts import SearchDisposition
    from neuro_voice.search.intent import route_search_request

    events: list[dict[str, Any]] = []
    for raw in cases:
        case = dict(raw)
        route = route_search_request(
            str(case.get("input") or ""),
            enabled=bool(case.get("search_enabled", True)),
        )
        expected = str(case.get("expect_search") or "")

        class FixtureSearch:
            def evidence_results(self, query: str, max_results: int = 4):
                return list(case.get("fake_results", ()))[:max_results]

        attempted = route.disposition is SearchDisposition.EXECUTE
        outcome = execute_search_route(
            route, FixtureSearch(),
            is_current=lambda: not bool(case.get("stale", False)),
        )
        presented = present_search_outcome(outcome)
        route_ok = route.disposition.value == expected
        expected_outcome = str(case.get("expect_outcome") or (
            "SUCCEEDED" if attempted and case.get("fake_results") else
            "EMPTY" if attempted else route.disposition.value
        ))
        outcome_ok = outcome.status == expected_outcome
        explicit = attempted or route.disposition in {
            SearchDisposition.BLOCKED, SearchDisposition.CLARIFY,
            SearchDisposition.DEFERRED,
        }
        events.append({
            "case_id": str(case.get("id") or ""),
            "explicit_search_requested": explicit and route.disposition is SearchDisposition.EXECUTE,
            "expected_failure_path": bool(case.get("expect_outcome")),
            "contract_passed": route_ok and outcome_ok,
            "search_attempted": attempted,
            "search_completed": outcome.status in {"SUCCEEDED", "EMPTY"},
            "answer_grounded": bool(presented.grounded),
            "delivery_finalized": route_ok and outcome_ok,
        })
    return evaluate_events(events)


__all__ = [
    "CaseEvaluation", "EvaluationReport", "evaluate_events",
    "evaluate_offline_corpus", "load_corpus",
]
