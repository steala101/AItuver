"""Obligations may resume relevant work, never search every normal turn."""
from __future__ import annotations

from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state
from neuro_voice.cognition.recall import obligation_retrieval_gate, retrieval_trigger
from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.cognition.types import ActionType
from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.cognition.initiative import OpportunityType, opportunities_from_events


def _obligation(*, status: str = "open", persona_id: str = "neuro") -> dict:
    return {
        "obligation_id": "work-1", "topic_ids": ("設定作業",),
        "goal": "設定作業を確認する", "status": status, "priority": 0.5,
        "created_at": 1.0, "due_at": 0.0, "persona_id": persona_id,
        "source_event_ids": ("event-1",),
    }


def _state(*, obligations=(), persona="neuro"):
    return build_state(
        obligations=tuple(item["goal"] for item in obligations if item.get("status") == "open"),
        obligation_contexts=obligations, active_persona_id=persona,
    )


def test_open_obligation_does_not_search_an_unrelated_math_question():
    state = _state(obligations=(_obligation(),))
    gate = obligation_retrieval_gate("2足す2は？", state)
    assert gate.considered and not gate.triggered
    assert gate.reason == "no_topic_or_entity_match"
    assert retrieval_trigger("2足す2は？", state) == ""
    candidates = CognitiveKernel().propose(
        CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="2足す2は？"), state,
    )
    assert any(item.action_type is ActionType.ANSWER for item in candidates)
    assert all(item.action_type is not ActionType.CONTINUE_PREVIOUS_TOPIC for item in candidates)


def test_open_obligation_does_not_search_an_unrelated_general_question():
    state = _state(obligations=(_obligation(),))
    assert retrieval_trigger("猫の鳴き声は？", state) == ""


def test_related_topic_allows_obligation_retrieval():
    state = _state(obligations=(_obligation(),))
    gate = obligation_retrieval_gate("前の設定作業の続きをしよう", state)
    assert gate.triggered
    assert retrieval_trigger("前の設定作業の続きをしよう", state) == "unresolved_obligation"


def test_explicit_past_reference_allows_obligation_retrieval():
    state = _state(obligations=(_obligation(),))
    gate = obligation_retrieval_gate("前に約束したこと、どうなった？", state)
    assert gate.triggered and gate.reason == "explicit_resume"


def test_resolved_cancelled_and_other_personas_do_not_trigger():
    for obligation, persona in (
        (_obligation(status="resolved"), "neuro"),
        (_obligation(status="cancelled"), "neuro"),
        (_obligation(persona_id="b"), "neuro"),
    ):
        gate = obligation_retrieval_gate("前の設定作業の続きをしよう", _state(
            obligations=(obligation,), persona=persona,
        ))
        assert not gate.triggered


def test_selected_continuation_action_is_an_explicit_allowance():
    state = _state(obligations=(_obligation(),))
    assert obligation_retrieval_gate(
        "設定の件", state, action=ActionType.CONTINUE_PREVIOUS_TOPIC,
    ).triggered


def test_trace_keeps_only_obligation_gate_diagnostics():
    trace = CognitiveTrace(
        obligation_retrieval_considered=True,
        obligation_retrieval_triggered=False,
        obligation_retrieval_reason="no_topic_or_entity_match",
        obligation_candidate_count=1,
        obligation_relevance_score=0.0,
    )
    diagnostics = trace.snapshot()["memory"]["diagnostics"]
    assert diagnostics["obligation_retrieval_considered"]
    assert not diagnostics["obligation_retrieval_triggered"]
    assert diagnostics["obligation_candidate_count"] == 1


def test_due_obligation_becomes_an_initiative_opportunity_not_normal_recall():
    state = _state(obligations=(_obligation(),))
    assert retrieval_trigger("", state) == ""
    opportunities = opportunities_from_events((AttentionEvent(
        event_type=AttentionEventType.OBLIGATION_DUE, source="test",
        summary="due", occurred_at=10.0,
    ),), now=10.0)
    assert opportunities and opportunities[0].opportunity_type is OpportunityType.RESUME_OBLIGATION


def test_due_obligation_is_wired_to_attention_without_copying_its_text():
    from neuro_voice.pipeline import VoicePipeline

    pipeline = VoicePipeline.__new__(VoicePipeline)
    observed: list[dict] = []
    pipeline._observe_attention = lambda **kwargs: observed.append(kwargs) or True
    context = _obligation()
    context["due_at"] = 1.0
    state = build_state(
        obligations=(context["goal"],), obligation_contexts=(context,), active_persona_id="neuro",
    )
    assert VoicePipeline._schedule_due_obligation_attention(pipeline, state) == 1
    assert observed[0]["event_type"] is AttentionEventType.OBLIGATION_DUE
    assert observed[0]["summary"] == "due_obligation"
