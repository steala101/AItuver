from types import SimpleNamespace
from dataclasses import replace

from neuro_voice.cognition.action_constraints import ActionEligibility
from neuro_voice.cognition.executor import ActionExecutionGate, obligations_to_release
from neuro_voice.cognition.executor import ActionExecutionGate
from neuro_voice.cognition.fallback import FallbackState, legacy_fallback_allowed
from neuro_voice.cognition.kernel import CognitiveKernel
from neuro_voice.cognition.state import build_state
from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.cognition.types import ActionDecision, ActionType, CognitiveEvent, EventType
from neuro_voice.pipeline import VoicePipeline


def _ending_state(*, response_required=False):
    return build_state(
        working_memory={
            "response_required": response_required,
            "response_requirement_source": "addressed",
        },
        user_state={"end_signal": 0.9, "engagement": 0.1},
    )


def _event():
    return CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="x")


def test_direct_response_contract_cannot_select_silence():
    decision = CognitiveKernel().decide(_event(), _ending_state(response_required=True))
    assert decision.selected_action is ActionType.ANSWER


def test_response_request_source_uses_existing_conversation_plan():
    plan = SimpleNamespace(
        should_respond=True,
        response_role="answer_and_expand",
        reason="addressed",
    )
    assert VoicePipeline._response_requirement(SimpleNamespace(response_plan=plan)) == (
        True, "addressed")


def test_terminal_plan_is_not_misclassified_as_response_required():
    plan = SimpleNamespace(
        should_respond=False,
        response_role="remain_silent",
        reason="user_is_ending",
    )
    assert VoicePipeline._response_requirement(SimpleNamespace(response_plan=plan)) == (
        False, "")


def test_existing_stop_intent_overrides_an_addressed_response_plan():
    plan = SimpleNamespace(
        should_respond=True,
        response_role="answer_and_expand",
        reason="addressed",
    )
    assert VoicePipeline._response_requirement(
        SimpleNamespace(response_plan=plan), "もういいよ") == (False, "closure_signal")


def test_stop_intent_tolerates_terminal_punctuation_variants():
    plan = SimpleNamespace(
        should_respond=True,
        response_role="answer_and_expand",
        reason="addressed",
    )
    decision = SimpleNamespace(response_plan=plan)
    for text in ("もういいよ", "もういいよ。", "もういいよ！"):
        assert VoicePipeline._response_requirement(decision, text) == (
            False, "closure_signal")


def test_activity_input_is_not_a_response_required_contract():
    plan = SimpleNamespace(
        should_respond=True,
        response_role="brief_answer",
        reason="activity_input_has_priority",
    )
    assert VoicePipeline._response_requirement(SimpleNamespace(response_plan=plan)) == (
        False, "activity_input_has_priority")
    decision = CognitiveKernel().decide(_event(), _ending_state(response_required=False))
    assert decision.selected_action in {
        ActionType.REMAIN_SILENT,
        ActionType.BRIEF_ACKNOWLEDGE,
    }


def test_valid_silence_has_complete_contract_and_no_fallback():
    decision = ActionDecision(
        selected_action=ActionType.REMAIN_SILENT,
        decision_reason="user_is_ending",
    )
    contract = ActionExecutionGate.silence_contract(
        decision, ActionExecutionGate.plan(decision))
    assert contract["valid"] is True
    assert contract["decision_source"] == "ACTION_SELECTOR"
    assert legacy_fallback_allowed(
        FallbackState(), technical_error=True,
        selected_action="remain_silent",
    )[0] is False


def test_incomplete_silence_is_not_a_successful_silence_and_can_fallback():
    decision = ActionDecision(selected_action=ActionType.REMAIN_SILENT)
    contract = ActionExecutionGate.silence_contract(
        decision, ActionExecutionGate.plan(decision))
    assert contract["valid"] is False
    assert legacy_fallback_allowed(FallbackState(), technical_error=True)[0] is True


def test_memory_bias_cannot_override_response_required_contract():
    class _Bias:
        def for_action(self, action):
            return 10.0 if action is ActionType.REMAIN_SILENT else 0.0

        def sources(self, _action):
            return ()

    decision = CognitiveKernel(memory_influence=_Bias()).decide(
        _event(), _ending_state(response_required=True))
    assert decision.selected_action is ActionType.ANSWER


def test_closure_only_removes_answer_and_continue_from_eligible_actions():
    eligibility = ActionEligibility.from_turn(
        closure_signal=True, closure_confidence=.9, closure_is_affirmative=False,
        direct_question=False, explicit_answer_request=False,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    assert eligibility.response_obligation == "OPTIONAL"
    assert eligibility.eligible_actions == (
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE)


def test_constraint_validator_rejects_injected_answer_before_planner():
    eligibility = ActionEligibility.from_turn(
        closure_signal=True, closure_confidence=.9, closure_is_affirmative=False,
        direct_question=False, explicit_answer_request=False,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    state = _ending_state(response_required=True)
    state = replace(state, working_context={
        **state.working_context, "action_eligibility": eligibility.snapshot(),
    })
    kernel = CognitiveKernel()
    candidates = kernel.propose(_event(), state)
    candidates = [
        replace(item, total_score=99.0) if item.action_type is ActionType.ANSWER else item
        for item in candidates
    ]
    decision = kernel.decide(_event(), state, candidates)
    constraint = decision.parameters["action_constraint"]
    assert constraint["initially_selected_action"] == "answer"
    assert constraint["action_constraint_result"] == "INVALID_ACTION_CORRECTED"
    assert decision.selected_action in {
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE,
    }


def test_closure_with_response_request_allows_brief_ack_only():
    eligibility = ActionEligibility.from_turn(
        closure_signal=True, closure_confidence=.9, closure_is_affirmative=False,
        direct_question=False, explicit_answer_request=True,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    assert eligibility.response_obligation == "REQUIRED"
    assert eligibility.eligible_actions == (ActionType.BRIEF_ACKNOWLEDGE,)


def test_direct_question_keeps_answer_eligible():
    eligibility = ActionEligibility.from_turn(
        closure_signal=True, closure_confidence=.9, closure_is_affirmative=False,
        direct_question=True, explicit_answer_request=False,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    assert eligibility.eligible_actions == (ActionType.ANSWER,)


def test_affirmative_closure_uses_brief_ack_without_erasing_task_context():
    eligibility = ActionEligibility.from_turn(
        closure_signal=True, closure_confidence=.9, closure_is_affirmative=True,
        direct_question=False, explicit_answer_request=False,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    assert eligibility.eligible_actions == (ActionType.BRIEF_ACKNOWLEDGE,)
    plan = ActionExecutionGate.plan(ActionDecision(
        selected_action=ActionType.BRIEF_ACKNOWLEDGE))
    assert obligations_to_release(("next_task", "finish_interrupted_response"), plan) == (
        "finish_interrupted_response",)


def test_low_confidence_closure_does_not_install_a_hard_answer_constraint():
    eligibility = ActionEligibility.from_turn(
        closure_signal=False, closure_confidence=.3, closure_is_affirmative=False,
        direct_question=False, explicit_answer_request=False,
        new_task_instruction=False, safety_warning=False,
        closure_response_policy="adaptive",
    )
    assert eligibility.eligible_actions == ()
    assert eligibility.action_selection_reason == "closure_signal_low_confidence"


def test_brief_acknowledge_uses_a_speech_plan_without_answer_conversion():
    decision = ActionDecision(selected_action=ActionType.BRIEF_ACKNOWLEDGE)
    plan = ActionExecutionGate.plan(decision)
    assert plan.route == "speech"
    assert plan.speech_allowed is True
    text = VoicePipeline._brief_acknowledgement_text()
    assert text.count("。") == 1
    assert "?" not in text and "？" not in text


def test_trace_exposes_silence_contract_without_content():
    trace = CognitiveTrace(
        selected_action="remain_silent",
        action_decision_id="decision-1",
        decision_source="ACTION_SELECTOR",
        silence_reason_code="user_is_ending",
        suppression_reason="action_selector_remain_silent",
    )
    silence = trace.snapshot()["silence"]
    assert silence["action_decision_id"] == "decision-1"
    assert silence["suppression_reason"] == "action_selector_remain_silent"
    trace.closure_signal = True
    trace.closure_confidence = .9
    trace.response_obligation = "OPTIONAL"
    trace.eligible_actions = ["remain_silent", "brief_acknowledge"]
    trace.initially_selected_action = "answer"
    trace.action_constraint_result = "INVALID_ACTION_CORRECTED"
    trace.corrected_action = "remain_silent"
    trace.final_selected_action = "remain_silent"
    constraints = trace.snapshot()["action_constraints"]
    assert constraints["closure_signal"] is True
    assert constraints["corrected_action"] == "remain_silent"
