"""Fast, explainable assistant-addressee detection."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from neuro_voice.dialogue.state import ConversationState


class AddressingAction(StrEnum):
    RESPOND = "respond"
    BRIEF = "brief"
    WAIT_FOR_HUMAN = "wait_for_human"
    OPTIONAL_JOIN = "optional_join"
    ACK_ONLY = "ack_only"
    OBSERVE = "observe"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class AddressingDecision:
    target_probability: float
    decision: AddressingAction
    reasons: tuple[str, ...]
    explicit_wake_word: bool = False
    ambiguous: bool = False
    wait_ms: int | None = None
    addressee: str = "uncertain"


class AddresseeDetector:
    """Rule-first detector.  It is deliberately conservative in group chats."""

    # ``ポッポ`` is often transcribed as these short near-homophones in a
    # compressed Discord stream.  Keep this list deliberately narrow: it is
    # only enabled for the Poppo persona (or when one of the aliases is in the
    # configured wake words), so a person with an unrelated name is not
    # accidentally treated as the assistant.
    _POPPO_CALL_ALIASES = ("ポッポ", "ぽっぽ", "ポポ", "ぽぽ", "ポッポー", "ぽっぽー", "ポポー", "ぽぽー", "ポチ", "ぽち")

    _QUESTION = re.compile(r"[?？]|(?:どう|なに|何|どれ|いつ|なぜ|なんで|教えて|できる|思う)$")
    _DIRECT = re.compile(r"(?:君|きみ|そっち|AI|あなた|お前|おまえ)(?:は|なら|に|、|,|$)")
    _AI_CONTEXT = re.compile(r"(?:記憶|前に言った|さっき言った|検索|設定|AI|ポッポ|ニューロ)")
    _FOLLOW_UP_CUE = re.compile(r"^(?:それで|それ|じゃあ|じゃ|あと|さっき|今の|つまり|でも|けど|ところで|そういえば)")
    _ACKNOWLEDGEMENT = frozenset({
        "うん", "うんうん", "はい", "ええ", "はーい", "なるほど", "なるほどね",
        "そう", "そうだね", "わかった", "了解", "りょうかい", "あー", "ああ",
    })

    def __init__(self, cfg, *, wake_words: list[str], assistant_name: str = ""):
        self._respond_all = bool(cfg.get("discord.respond_all", False))
        self._respond_threshold = float(cfg.get("conversation.addressing.respond_threshold", 0.75))
        self._brief_threshold = float(cfg.get("conversation.addressing.brief_threshold", 0.45))
        self._observe_threshold = float(cfg.get("conversation.addressing.observe_threshold", 0.35))
        self._follow_up_s = float(cfg.get("discord.follow_up_s", 15.0))
        # This is intentionally longer than the immediate follow-up window.
        # A natural continuation can come after a short pause while the user
        # thinks, but it must still be tied to the same Discord input stream.
        self._continuation_s = max(
            self._follow_up_s,
            float(cfg.get("conversation.addressing.continuation_s", 45.0)),
        )
        self._group_enabled = bool(cfg.get("group_conversation.enabled", True))
        self._group_response_threshold = float(cfg.get("group_conversation.response_threshold", .65))
        self._group_wait_threshold = float(cfg.get("group_conversation.wait_threshold", .40))
        self._group_optional_threshold = float(cfg.get("group_conversation.optional_join_threshold", .55))
        self._human_priority_ms = int(cfg.get("group_conversation.human_priority_default_ms", 850))
        self._assistant_name = assistant_name
        self._set_wake_words(wake_words)

    def configure(self, *, respond_all: bool | None = None,
                  wake_words: list[str] | None = None,
                  follow_up_s: float | None = None) -> None:
        """Apply Discord settings without rebuilding short-lived turn state."""
        if respond_all is not None:
            self._respond_all = bool(respond_all)
        if wake_words is not None:
            self._set_wake_words(wake_words)
        if follow_up_s is not None:
            self._follow_up_s = max(0.0, float(follow_up_s))

    def _set_wake_words(self, wake_words: list[str]) -> None:
        words = [*wake_words, self._assistant_name]
        normalized_words = {self._normalize(word) for word in words if str(word).strip()}
        poppo_names = {self._normalize(word) for word in self._POPPO_CALL_ALIASES}
        if normalized_words & poppo_names:
            words.extend(self._POPPO_CALL_ALIASES)
        self._wake_words = tuple(self._normalize(word) for word in words if str(word).strip())

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s\u3000、,。.!！?？]", "", text).lower()

    def decide(self, text: str, state: ConversationState, *, speaker_key: str | None = None,
               now: float | None = None) -> AddressingDecision:
        raw = text.strip()
        normalized = self._normalize(raw)
        reasons: list[str] = []
        if self._respond_all:
            return AddressingDecision(0.98, AddressingAction.RESPOND, ("respond_all_enabled",))
        explicit = self._explicit_call(raw, normalized)
        if explicit:
            return AddressingDecision(0.98, AddressingAction.RESPOND, ("explicit_assistant_call",), True)
        if self._is_acknowledgement(raw):
            # A short acknowledgement is conversational feedback, not a new
            # turn.  Do not make the assistant answer it just because it has
            # spoken immediately beforehand.
            return AddressingDecision(0.0, AddressingAction.IGNORE, ("short_acknowledgement",), addressee="self_talk")
        # With exactly one human in the VC, this is equivalent to the local
        # microphone conversation: a wake word before every turn is unnatural.
        # Multi-person channels continue through the conservative rules below.
        if state.voice_human_count == 1:
            return AddressingDecision(0.90, AddressingAction.RESPOND, ("single_human_conversation",))

        score = 0.0
        question = bool(self._QUESTION.search(raw))
        if question:
            score += 0.18
            reasons.append("utterance_is_question")
        direct_expression = bool(self._DIRECT.search(raw))
        if direct_expression:
            score += 0.28
            reasons.append("assistant_direct_expression")
        assistant_related = bool(self._AI_CONTEXT.search(raw))
        follow_up_cue = bool(self._FOLLOW_UP_CUE.search(raw))
        if assistant_related:
            score += 0.18
            reasons.append("assistant_related_context")
        # A direct question about the assistant must not be suppressed simply
        # because another participant was the previous speaker.  This covers
        # natural turns such as "ポッポ、今何しようとした？" without changing
        # the conservative rule for questions addressed to another human.
        assistant_is_topic = any(
            wake and (f"{wake}が" in normalized or f"{wake}の" in normalized)
            for wake in self._wake_words
        )
        if question and assistant_related and not assistant_is_topic:
            score += 0.52
            reasons.append("assistant_related_question")
        if state.assistant_turn_open:
            score += 0.38
            reasons.append("conversation_turn_is_open")
        if (state.assistant_turn_open and state.expected_response_from
                and state.expected_response_from == speaker_key):
            score += 0.28
            reasons.append("assistant_asked_previous_question")
        # A no-wake follow-up is safe only when the assistant explicitly left
        # the floor open with a question.  Treating every next utterance as a
        # reply made the bot interrupt normal Discord conversations.
        if state.previous_speaker == "assistant" and state.assistant_turn_open:
            score += 0.22
            reasons.append("assistant_spoke_previous_turn")
        continuation_is_addressed = (
            state.assistant_turn_open
            or (assistant_related and question and not assistant_is_topic)
            or direct_expression
        )
        same_input_stream = bool(state.expected_response_from and state.expected_response_from == speaker_key)
        recent_assistant_turn = bool(
            now is not None and state.last_assistant_at
            and now - state.last_assistant_at <= self._follow_up_s
        )
        # One human in the VC is a true dialogue.  Discord's membership cache
        # can briefly be empty with the direct DAVE backend, so a single heard
        # input stream is the safe fallback until a second person actually
        # speaks.  In a group, require the stronger continuation cue to avoid
        # joining a conversation between humans.
        single_human_dialogue = (
            state.voice_human_count == 1
            or (state.voice_human_count in {None, 0} and len(state.participants) == 1)
        )
        in_group = bool(state.voice_human_count and state.voice_human_count > 1)
        if recent_assistant_turn and not continuation_is_addressed:
            if same_input_stream and single_human_dialogue:
                continuation_is_addressed = True
                score += 0.58
                reasons.append("single_human_active_exchange")
            elif in_group and same_input_stream and question and follow_up_cue:
                continuation_is_addressed = True
                score += 0.30
                reasons.append("group_topic_follow_up_after_assistant")
            elif in_group and not self._looks_human_directed(raw, state, speaker_key):
                # 3人以上の会話でも、自分が直近まで参加していた輪の中の発話は
                # 名前で呼ばれなくても会話の流れの一部として扱う。素の発言は
                # BRIEF帯 (短い反応・相槌)、質問や自分宛ての続きはRESPOND帯へ。
                # 別の人へ名指しされた発話はこの加点を受けない。
                continuation_is_addressed = True
                score += 0.34
                reasons.append("group_conversation_flow")
        if (continuation_is_addressed and now is not None and state.last_assistant_at
                and now - state.last_assistant_at <= self._follow_up_s):
            score += 0.25
            reasons.append("within_follow_up_window")
            if same_input_stream:
                # This is source-stream continuity, not identity resolution.
                # It lets a user answer or react naturally after the assistant
                # speaks even when a voiceprint is unavailable or fluctuates.
                score += 0.30
                reasons.append("same_input_stream_after_assistant")

        if (continuation_is_addressed and now is not None and state.last_assistant_at
                and now - state.last_assistant_at <= self._continuation_s
                and state.expected_response_from == speaker_key
                and now - state.last_assistant_at > self._follow_up_s):
            # Preserve the conversation thread after the normal 15-second
            # follow-up window.  This uses the Discord stream only for turn
            # continuity; it does not infer a real-world identity.
            score += 0.58
            reasons.append("same_input_stream_continuation")

        # 追従窓の外でも、直前の話題の続きなら弱く加点する (単独ではOBSERVE
        # 止まり。相槌や、質問との組み合わせでの復帰用)。active_topic はこの
        # 発話自身で更新済みのため、一つ前の話題 (topic_history) と比較する。
        previous_topic = str(state.topic_history[-1] if state.topic_history else "")
        if in_group and not continuation_is_addressed and len(previous_topic) >= 4:
            if self._topic_overlap(raw, previous_topic) >= 0.30:
                score += 0.22
                reasons.append("active_topic_overlap")

        human_directed = self._looks_human_directed(raw, state, speaker_key)
        if human_directed:
            score -= 0.48
            reasons.append("another_participant_is_addressed")
        elif (question and not assistant_related
              and state.previous_speaker not in {None, "assistant", speaker_key}
              and not state.assistant_turn_open):
            score -= 0.18
            reasons.append("human_conversation_continues")

        score = max(0.0, min(1.0, score))
        if self._group_enabled and in_group:
            if human_directed:
                return AddressingDecision(score, AddressingAction.IGNORE, tuple(reasons), addressee="direct_to_human")
            if explicit or (state.assistant_turn_open and state.expected_response_from == speaker_key):
                return AddressingDecision(max(score, .98 if explicit else .80), AddressingAction.RESPOND,
                                          tuple(reasons), explicit_wake_word=explicit, addressee="direct_to_ai")
            # A high-confidence continuation of the assistant's immediately
            # preceding turn is already a conversational answer, not an open
            # question for other humans to race for.  Preserve this before the
            # general human-priority window below.
            if question and continuation_is_addressed and score >= self._group_response_threshold:
                return AddressingDecision(score, AddressingAction.RESPOND, tuple(reasons),
                                          addressee="direct_to_ai")
            if question and (score >= self._group_wait_threshold or not human_directed):
                # An unaddressed question in a group gets a short human-first
                # window; the bridge owns cancellation if someone answers.
                return AddressingDecision(score, AddressingAction.WAIT_FOR_HUMAN, tuple(reasons + ["open_question_human_priority"]),
                                          ambiguous=True, wait_ms=self._human_priority_ms, addressee="open_to_group")
            if score >= self._group_optional_threshold and not question:
                return AddressingDecision(score, AddressingAction.OPTIONAL_JOIN, tuple(reasons), addressee="likely_to_ai")
        if score >= self._respond_threshold:
            action = AddressingAction.RESPOND
        elif score >= self._brief_threshold:
            action = AddressingAction.BRIEF
        elif score >= self._observe_threshold:
            action = AddressingAction.OBSERVE
        else:
            action = AddressingAction.IGNORE
        return AddressingDecision(score, action, tuple(reasons), ambiguous=action is AddressingAction.OBSERVE)

    @classmethod
    def _is_acknowledgement(cls, text: str) -> bool:
        compact = re.sub(r"[\s\u3000、。,.!?！？〜～]", "", text).lower()
        return compact in cls._ACKNOWLEDGEMENT

    def _explicit_call(self, raw: str, normalized: str) -> bool:
        for wake in self._wake_words:
            if not wake or wake not in normalized:
                continue
            # Mentioning "ポッポが言ってた" is a topic, not an invocation.
            index = normalized.find(wake)
            suffix = normalized[index + len(wake):index + len(wake) + 2]
            if suffix.startswith(("が", "は", "の")):
                continue
            if index == 0 or raw[max(0, raw.find(wake) - 1):raw.find(wake)] in {"", "、", ","}:
                return True
            if suffix.startswith(("なら", "に", "どう", "教えて", "聞いて")):
                return True
        return False

    def is_explicit_call(self, text: str) -> bool:
        """Fast, state-free wake-word check for an interim STT hypothesis."""
        raw = text.strip()
        return bool(raw) and self._explicit_call(raw, self._normalize(raw))

    @staticmethod
    def _topic_overlap(text: str, topic: str) -> float:
        """文字バイグラムの重なりで話題の連続性を測る (0.0〜1.0)。"""
        def bigrams(s: str) -> set[str]:
            s = re.sub(r"[\s　、。,.!?！？〜～]", "", s)
            return {s[i:i + 2] for i in range(len(s) - 1)}

        tb, qb = bigrams(topic), bigrams(text)
        if not tb or not qb:
            return 0.0
        return len(tb & qb) / min(len(tb), len(qb))

    def _looks_human_directed(self, raw: str, state: ConversationState, speaker_key: str | None) -> bool:
        for key, name in state.participants.items():
            if key in {speaker_key, "assistant"} or not name:
                continue
            if name in raw and (raw.startswith(name) or f"{name}、" in raw or f"{name}," in raw):
                return True
        return False
