"""Short-lived state for deciding whether the assistant should join a turn."""
from __future__ import annotations

from dataclasses import dataclass, field

from neuro_voice.dialogue.events import ConversationEvent, ConversationEventType


@dataclass(slots=True)
class ConversationState:
    active_topic: str = ""
    topic_history: list[str] = field(default_factory=list)
    participants: dict[str, str] = field(default_factory=dict)
    voice_human_count: int | None = None
    current_speaker: str | None = None
    previous_speaker: str | None = None
    assistant_turn_open: bool = False
    expected_response_from: str | None = None
    last_assistant_asked_question: bool = False
    last_assistant_text: str = ""
    activity_active: bool = False
    # A turn-based directive (game master, co-thinking) that has handed the
    # floor over and is waiting for this person's move.
    directive_waiting_for_user: bool = False
    unresolved_questions: list[str] = field(default_factory=list)
    recent_entities: list[str] = field(default_factory=list)
    conversation_mode: str = "idle"
    last_activity_at: float = 0.0
    last_assistant_at: float = 0.0
    last_user_at: float = 0.0
    _topic_before_last_speech: str = ""
    _topic_history_before_last_speech: list[str] = field(default_factory=list)

    def update(self, event: ConversationEvent) -> None:
        self.last_activity_at = event.timestamp
        typ = str(event.event_type)
        if typ == ConversationEventType.PARTICIPANT_JOINED:
            key = str(event.metadata.get("participant_key") or event.speaker_id or "")
            if key:
                self.participants[key] = str(event.metadata.get("display_name") or key)
            return
        if typ == ConversationEventType.PARTICIPANT_LEFT:
            key = str(event.metadata.get("participant_key") or event.speaker_id or "")
            self.participants.pop(key, None)
            return
        if typ == ConversationEventType.SPEECH_FINAL:
            # Keep the current turn tied to the input stream, not to a
            # voiceprint label.  Voiceprint confidence may legitimately vary
            # from one utterance to the next; Discord transport is only used
            # to keep a turn coherent, never to identify a real-world person.
            speaker = str(event.metadata.get("speaker_key") or event.speaker_id or "unknown")
            self.previous_speaker, self.current_speaker = self.current_speaker, speaker
            display = str(event.metadata.get("transport_name") or event.metadata.get("speaker_name") or speaker)
            self.participants[speaker] = display
            count = event.metadata.get("voice_human_count")
            if isinstance(count, int) and count >= 0:
                self.voice_human_count = count
            self.activity_active = bool(event.metadata.get("activity_active", False))
            self.directive_waiting_for_user = bool(
                event.metadata.get("directive_waiting_for_user", False),
            )
            self.last_user_at = event.timestamp
            self.conversation_mode = "multi_party" if len(self.participants) > 1 else "dialogue"
            self._topic_before_last_speech = self.active_topic
            self._topic_history_before_last_speech = list(self.topic_history)
            self._update_topic(event.text)
            return
        if typ == ConversationEventType.RESPONSE_STARTED:
            self.previous_speaker, self.current_speaker = self.current_speaker, "assistant"
            self.last_assistant_at = event.timestamp
            self.assistant_turn_open = bool(event.metadata.get("awaiting_reply", False))
            self.expected_response_from = event.metadata.get("expected_response_from")
            self.last_assistant_asked_question = bool(event.metadata.get("asked_question", False))
            return
        if typ in {ConversationEventType.RESPONSE_FINISHED, ConversationEventType.RESPONSE_INTERRUPTED}:
            self.last_assistant_at = event.timestamp
            if typ == ConversationEventType.RESPONSE_FINISHED:
                assistant_text = str(event.metadata.get("assistant_text") or "").strip()
                if assistant_text:
                    self.last_assistant_text = assistant_text
                self.last_assistant_asked_question = bool(event.metadata.get("asked_question", False))
                self.assistant_turn_open = self.last_assistant_asked_question
                self.expected_response_from = event.metadata.get("expected_response_from")
            if typ == ConversationEventType.RESPONSE_INTERRUPTED:
                self.assistant_turn_open = True
            return
        if typ == ConversationEventType.TOPIC_CHANGED:
            self._set_topic(str(event.metadata.get("topic") or event.text))

    def snapshot(self) -> dict[str, object]:
        return {
            "active_topic": self.active_topic,
            "participants": list(self.participants.values()),
            "voice_human_count": self.voice_human_count,
            "current_speaker": self.current_speaker,
            "previous_speaker": self.previous_speaker,
            "assistant_turn_open": self.assistant_turn_open,
            "activity_active": self.activity_active,
            "directive_waiting_for_user": self.directive_waiting_for_user,
            "expected_response_from": self.expected_response_from,
            "conversation_mode": self.conversation_mode,
            "unresolved_questions": list(self.unresolved_questions[-3:]),
        }

    def settle_exchange(self) -> None:
        """Close the floor and undo a feedback utterance becoming the topic."""
        self.assistant_turn_open = False
        self.expected_response_from = None
        self.last_assistant_asked_question = False
        self.active_topic = self._topic_before_last_speech
        self.topic_history[:] = self._topic_history_before_last_speech

    def _update_topic(self, text: str) -> None:
        compact = " ".join(text.split())
        if compact:
            self._set_topic(compact[:96])
        if "?" in text or "？" in text:
            self.unresolved_questions.append(compact[:160])
            del self.unresolved_questions[:-5]

    def _set_topic(self, topic: str) -> None:
        topic = topic.strip()
        if not topic or topic == self.active_topic:
            return
        if self.active_topic:
            self.topic_history.append(self.active_topic)
            del self.topic_history[:-8]
        self.active_topic = topic
