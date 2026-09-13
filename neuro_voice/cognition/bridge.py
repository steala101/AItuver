"""選んだ行動を TurnFrame の語彙へ写す対応表。

指示書 `docs/handoffs/2026-08-01_0800_fable_instruction_action-to-frame.md` の
とおり。表に無い行動と、発話しない行動は None。
"""
from __future__ import annotations

from neuro_voice.cognition.types import ActionDecision, ActionType

ACTION_TO_FRAME: dict[ActionType, dict[str, str]] = {
    ActionType.ANSWER: {
        "response_goal": "Answer the question directly before adding one useful thought.",
        "response_shape": "answer_first", "social_mode": "neutral"},
    ActionType.ACKNOWLEDGE_EMOTION: {
        "response_goal": "Acknowledge how the person feels before anything else. Do not lead with a solution.",
        "response_shape": "acknowledge_then_answer", "social_mode": "supportive"},
    ActionType.BRIEF_ACKNOWLEDGE: {
        "response_goal": "Acknowledge in one short sentence and stop. No question, no new topic, no resumed explanation.",
        "response_shape": "brief_ack", "social_mode": "neutral"},
    ActionType.ASK_CLARIFICATION: {
        "response_goal": "Ask one concrete clarification question; do not search or guess.",
        "response_shape": "clarify_reference", "social_mode": "neutral"},
    ActionType.CHALLENGE_ASSUMPTION: {
        "response_goal": "Name the assumption you disagree with and give one reason.",
        "response_shape": "direct_take", "social_mode": "candid"},
    ActionType.CONTINUE_PREVIOUS_TOPIC: {
        "response_goal": "Pick up the unresolved thread instead of starting a new one.",
        "response_shape": "resolve_then_answer", "social_mode": "neutral"},
    ActionType.WARN: {
        "response_goal": "Warn about the immediate danger in one short sentence. Details only if asked.",
        "response_shape": "urgent_warning", "social_mode": "urgent"},
    ActionType.MAKE_LIGHT_JOKE: {
        "response_goal": "React playfully to one concrete detail, then return to the topic.",
        "response_shape": "playful_twist", "social_mode": "playful"},
    ActionType.RESUME_INTERRUPTED_RESPONSE: {
        "response_goal": "Finish the sentence you were cut off from, briefly.",
        "response_shape": "resume", "social_mode": "neutral"},
}


def frame_changes(decision: ActionDecision) -> dict[str, str] | None:
    """TurnFrame へ渡す変更。表に無い・発話しない行動は None。"""
    if not decision.speaks:
        return None
    changes = ACTION_TO_FRAME.get(decision.selected_action)
    return dict(changes) if changes else None
