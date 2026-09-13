"""Extensible, low-overhead events for the conversation layer.

Events carry transport identity as metadata.  ``speaker_id`` is reserved for a
voiceprint-resolved profile and must never be silently populated from a Discord
account ID.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class ConversationEventType(StrEnum):
    SPEECH_STARTED = "speech.started"
    SPEECH_PARTIAL = "speech.partial"
    SPEECH_FINAL = "speech.final"
    SPEECH_INTERRUPTED = "speech.interrupted"
    SPEAKER_DETECTED = "speaker.detected"
    SPEAKER_UNKNOWN = "speaker.unknown"
    PARTICIPANT_JOINED = "participant.joined"
    PARTICIPANT_LEFT = "participant.left"
    ASSISTANT_ADDRESSED = "assistant.addressed"
    RESPONSE_REQUESTED = "assistant.response_requested"
    RESPONSE_STARTED = "assistant.response_started"
    RESPONSE_FINISHED = "assistant.response_finished"
    RESPONSE_INTERRUPTED = "assistant.response_interrupted"
    MEMORY_RECALLED = "memory.recalled"
    MEMORY_UPDATED = "memory.updated"
    TOPIC_CHANGED = "conversation.topic_changed"
    SILENCE_DETECTED = "conversation.silence_detected"
    SYSTEM_ERROR = "system.error"


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_type: ConversationEventType | str
    source: str
    text: str = ""
    speaker_id: str | None = None
    speaker_confidence: float | None = None
    channel_id: str | None = None
    timestamp: float = field(default_factory=time.monotonic)
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def summary(self, *, include_text: bool = False) -> dict[str, Any]:
        """Return a log-safe event view; full transcripts stay opt-in."""
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "source": self.source,
            "speaker_id": self.speaker_id,
            "speaker_confidence": self.speaker_confidence,
            "channel_id": self.channel_id,
        }
        if include_text:
            data["text"] = self.text
        return data


EventHandler = Callable[[ConversationEvent], None]


class ConversationEventDispatcher:
    """Synchronous in-process dispatcher for the response critical path.

    Handlers must be small and non-blocking.  Slow work (memory recall, LLM,
    external tools) is deliberately scheduled by the orchestration layer,
    instead of being hidden in this dispatcher.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    def publish(self, event: ConversationEvent) -> None:
        for handler in tuple(self._handlers):
            handler(event)
