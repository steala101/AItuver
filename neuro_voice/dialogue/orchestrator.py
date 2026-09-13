"""Coordinates fast event/state/decision work without owning audio or LLM I/O."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from neuro_voice.dialogue.addressing import AddressingAction, AddresseeDetector, AddressingDecision
from neuro_voice.dialogue.events import ConversationEvent, ConversationEventDispatcher, ConversationEventType
from neuro_voice.dialogue.planner import ResponsePlan, ResponsePlanner
from neuro_voice.dialogue.state import ConversationState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationDecision:
    addressing: AddressingDecision
    response_plan: ResponsePlan
    state: dict[str, object]


class ConversationOrchestrator:
    def __init__(self, cfg, *, wake_words: list[str], assistant_name: str = "",
                 on_event: Callable[[ConversationEvent], None] | None = None,
                 force_response: bool = False) -> None:
        self.events = ConversationEventDispatcher()
        self.state = ConversationState()
        self._detector = AddresseeDetector(cfg, wake_words=wake_words, assistant_name=assistant_name)
        self._planner = ResponsePlanner(cfg)
        self._debug_transcripts = bool(cfg.get("conversation.debug_transcripts", False))
        self._force_response = force_response
        self.events.subscribe(self.state.update)
        if on_event is not None:
            self.events.subscribe(on_event)

    def configure_addressing(self, *, respond_all: bool | None = None,
                             wake_words: list[str] | None = None,
                             follow_up_s: float | None = None) -> None:
        self._detector.configure(
            respond_all=respond_all, wake_words=wake_words, follow_up_s=follow_up_s,
        )

    def on_speech_final(self, event: ConversationEvent) -> ConversationDecision:
        self.events.publish(event)
        # Turn continuity is based on the Discord/local input stream, never
        # on a fluctuating voiceprint label.  ``speaker_id`` remains available
        # on the event for profile resolution only.
        speaker_key = str(event.metadata.get("speaker_key") or event.speaker_id or "unknown")
        addressing = self._detector.decide(event.text, self.state, speaker_key=speaker_key, now=event.timestamp)
        # A partial STT result is never enough to start a reply.  It can,
        # however, safely preserve an explicit assistant call that the final
        # decode lost at the beginning of the same utterance.
        if event.metadata.get("partial_explicit_call"):
            addressing = AddressingDecision(
                0.98, AddressingAction.RESPOND,
                ("partial_explicit_assistant_call",), explicit_wake_word=True,
            )
        if self._force_response:
            addressing = AddressingDecision(1.0, AddressingAction.RESPOND,
                                            ("single_user_conversation",))
        plan = self._planner.plan(event.text, addressing, self.state)
        if plan.settles_exchange:
            self.state.settle_exchange()
        if addressing.explicit_wake_word:
            self.events.publish(ConversationEvent(ConversationEventType.ASSISTANT_ADDRESSED, event.source,
                                                   speaker_id=speaker_key, channel_id=event.channel_id,
                                                   metadata={"reasons": addressing.reasons}))
        if plan.should_respond:
            self.events.publish(ConversationEvent(ConversationEventType.RESPONSE_REQUESTED, event.source,
                                                   speaker_id=speaker_key, channel_id=event.channel_id,
                                                   metadata={"plan": plan.response_role, "reason": plan.reason}))
        logger.info("Conversation decision score=%.2f action=%s reasons=%s plan=%s voices=%s expected=%s speaker=%s topic=%s",
                    addressing.target_probability, addressing.decision, ",".join(addressing.reasons),
                    plan.reason, self.state.voice_human_count, self.state.expected_response_from, speaker_key,
                    self.state.active_topic[:60])
        return ConversationDecision(addressing, plan, self.state.snapshot())

    def partial_has_explicit_call(self, text: str) -> bool:
        """Check an interim transcript without mutating conversation state."""
        return self._detector.is_explicit_call(text)

    def response_started(self, *, source: str, expected_response_from: str | None = None,
                         asked_question: bool = False, channel_id: str | None = None) -> None:
        self.events.publish(ConversationEvent(ConversationEventType.RESPONSE_STARTED, source,
                                               channel_id=channel_id, metadata={
                                                   "expected_response_from": expected_response_from,
                                                   "awaiting_reply": bool(asked_question),
                                                   "asked_question": bool(asked_question),
                                               }))

    def response_finished(self, *, source: str, interrupted: bool = False,
                          channel_id: str | None = None, asked_question: bool = False,
                          expected_response_from: str | None = None,
                          assistant_text: str = "") -> None:
        typ = ConversationEventType.RESPONSE_INTERRUPTED if interrupted else ConversationEventType.RESPONSE_FINISHED
        self.events.publish(ConversationEvent(typ, source, channel_id=channel_id, metadata={
            "asked_question": bool(asked_question),
            "expected_response_from": expected_response_from,
            "assistant_text": assistant_text,
        }))
