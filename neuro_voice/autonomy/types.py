"""Typed, privacy-aware values used by the autonomous action layer."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AutonomyEventType(StrEnum):
    HUMAN_UTTERANCE_FINALIZED = "human_utterance_finalized"
    HUMAN_SPEECH_STARTED = "human_speech_started"
    HUMAN_SPEECH_ENDED = "human_speech_ended"
    AI_SPEECH_FINISHED = "ai_speech_finished"
    QUESTION_UNANSWERED = "question_unanswered"
    SILENCE_STARTED = "silence_started"
    SILENCE_CONTINUED = "silence_continued"
    PERSON_JOINED = "person_joined"
    PERSON_LEFT = "person_left"
    GAME_EVENT = "game_event"
    GAME_GOAL_PROGRESS = "game_goal_progress"
    GAME_GOAL_COMPLETED = "game_goal_completed"
    GAME_GOAL_BLOCKED = "game_goal_blocked"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    TOOL_FAILED = "tool_failed"
    MEMORY_CALLBACK_DUE = "memory_callback_due"
    OPEN_THREAD_DUE = "open_thread_due"
    AGENDA_ITEM_DUE = "agenda_item_due"
    PRIVACY_CONTEXT_CHANGED = "privacy_context_changed"
    CONVERSATION_TOPIC_CHANGED = "conversation_topic_changed"


class ActionType(StrEnum):
    ANSWER = "answer"
    ASK_FOLLOWUP = "ask_followup"
    COMMENT = "comment"
    MAKE_JOKE = "make_joke"
    RECALL_OPEN_THREAD = "recall_open_thread"
    REPORT_RESULT = "report_result"
    START_TOPIC = "start_topic"
    USE_TOOL = "use_tool"
    ACT_IN_GAME = "act_in_game"
    UPDATE_MEMORY = "update_memory"
    UPDATE_GOAL = "update_goal"
    ACKNOWLEDGE = "acknowledge"
    WAIT = "wait"
    DO_NOTHING = "do_nothing"


class TriggerReason(StrEnum):
    OBSERVED_EVENT = "observed_event"
    EXPLICIT_CONTINUATION = "explicit_continuation"
    SELF_INITIATED_TOPIC = "self_initiated_topic"
    OPEN_THREAD_DUE = "open_thread_due"
    GOAL_PROGRESS = "goal_progress"
    GOAL_COMPLETED = "goal_completed"
    GOAL_BLOCKED = "goal_blocked"
    TOOL_RESULT_READY = "tool_result_ready"
    RELEVANT_MEMORY_CALLBACK = "relevant_memory_callback"
    SOCIAL_CALLBACK = "social_callback"
    ENVIRONMENT_CHANGED = "environment_changed"
    SILENCE_WITH_RELEVANT_CONTEXT = "silence_with_relevant_context"


class PrivacyScope(StrEnum):
    PUBLIC = "public"
    CURRENT_CONVERSATION = "current_conversation"
    SOURCE_AUDIENCE_ONLY = "source_audience_only"
    OWNER_AND_AI = "owner_and_ai"
    DO_NOT_STORE = "do_not_store"


@dataclass(frozen=True, slots=True)
class AutonomyEvent:
    event_type: AutonomyEventType
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    speaker_id: str | None = None
    conversation_id: str | None = None
    generation_id: str | None = None
    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION
    timestamp: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def expired(self, *, now: float | None = None) -> bool:
        return self.expires_at is not None and (time.monotonic() if now is None else now) > self.expires_at

    @property
    def text(self) -> str:
        return str(self.payload.get("text", "") or "")


@dataclass(slots=True)
class ActionCandidate:
    action_type: ActionType
    trigger_reason: TriggerReason
    relevance: float = 0.0
    usefulness: float = 0.0
    goal_progress: float = 0.0
    novelty: float = 0.5
    personality_fit: float = 0.5
    social_value: float = 0.0
    timing_quality: float = 0.0
    continuity: float = 0.0
    interruption_cost: float = 0.0
    repetition: float = 0.0
    privacy_risk: float = 0.0
    hallucination_risk: float = 0.0
    verbosity_risk: float = 0.0
    social_dominance: float = 0.0
    stale_event_penalty: float = 0.0
    target_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    utility: float = 0.0

    @property
    def should_speak(self) -> bool:
        return self.action_type in {
            ActionType.ANSWER, ActionType.ASK_FOLLOWUP, ActionType.COMMENT,
            ActionType.MAKE_JOKE, ActionType.RECALL_OPEN_THREAD,
            ActionType.REPORT_RESULT, ActionType.START_TOPIC, ActionType.ACKNOWLEDGE,
        }
