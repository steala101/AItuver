"""Deterministic response policy; the LLM only receives the resulting plan."""
from __future__ import annotations

from dataclasses import dataclass
import random
import time
from enum import StrEnum

from neuro_voice.dialogue.addressing import AddressingAction, AddressingDecision
from neuro_voice.dialogue.state import ConversationState
from neuro_voice.dialogue.turn_closure import TurnClosurePolicy, TurnDisposition


class ResponseRole(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    BRIEF_ANSWER = "brief_answer"
    ANSWER_AND_EXPAND = "answer_and_expand"
    CONTINUE_PREVIOUS_TOPIC = "continue_previous_topic"
    REMAIN_SILENT = "remain_silent"


@dataclass(frozen=True, slots=True)
class ResponsePlan:
    should_respond: bool
    response_role: ResponseRole
    target_length: str
    depth: int
    should_ask_followup: bool
    should_recall_memory: bool
    should_change_topic: bool
    interruption_priority: str
    reason: str
    settles_exchange: bool = False
    direct_reply: str | None = None

    @property
    def requires_response_contract(self) -> bool:
        """Whether an existing dialogue intent requires a spoken answer.

        ``should_respond`` also covers actionable activity input and optional
        acknowledgements.  Those must reach their owner, but must not prevent a
        valid end signal from selecting silence or a brief acknowledgement.
        Only the planner's addressed answer/continuation intents carry the
        stronger cognitive ANSWER contract.
        """
        return (
            self.should_respond
            and self.reason == "addressed"
            and self.response_role in {
                ResponseRole.ANSWER_AND_EXPAND,
                ResponseRole.CONTINUE_PREVIOUS_TOPIC,
            }
        )


class ResponsePlanner:
    def __init__(self, cfg) -> None:
        self._default_length = str(cfg.get("conversation.response_length", "short"))
        self._depth = max(0, int(cfg.get("conversation.response_depth", 1)))
        # 複数人会話で、宛先が曖昧 (OBSERVE帯) な発話にたまに相槌だけ打つ。
        # 黙り込まず「輪の中にいる」感を出すための控えめな存在表示。
        self._group_aizuchi = bool(cfg.get("conversation.group_aizuchi.enabled", True))
        self._group_aizuchi_p = max(0.0, min(1.0, float(
            cfg.get("conversation.group_aizuchi.probability", 0.35))))
        self._group_aizuchi_interval_s = max(0.0, float(
            cfg.get("conversation.group_aizuchi.min_interval_s", 20.0)))
        self._turn_closure = TurnClosurePolicy(cfg)

    def plan(self, text: str, decision: AddressingDecision, state: ConversationState) -> ResponsePlan:
        closure = self._turn_closure.decide(text, decision, state)
        if closure.disposition is TurnDisposition.SILENCE:
            return ResponsePlan(
                False, ResponseRole.REMAIN_SILENT, "none", 0, False, False, False,
                "normal", closure.reason, settles_exchange=True,
            )
        if closure.disposition is TurnDisposition.BRIEF_ACK:
            return ResponsePlan(
                True, ResponseRole.ACKNOWLEDGEMENT, "minimal", 0, False, False, False,
                "normal", closure.reason, settles_exchange=True,
                direct_reply=closure.direct_reply,
            )
        if closure.reason in {"activity_input_has_priority", "actionable_confirmation"}:
            # The addressee detector deliberately ignores bare acknowledgements.
            # In an active game or after an actionable yes/no question, however,
            # that same word is canonical input and must reach the command layer.
            return ResponsePlan(
                True, ResponseRole.BRIEF_ANSWER, "short", 0, False, False, False,
                "normal", closure.reason,
            )
        if decision.decision is AddressingAction.ACK_ONLY:
            return ResponsePlan(True, ResponseRole.ACKNOWLEDGEMENT, "minimal", 0, False, False, False,
                                "normal", "ack_only")
        if decision.decision is AddressingAction.OBSERVE and self._should_group_aizuchi(state):
            return ResponsePlan(True, ResponseRole.ACKNOWLEDGEMENT, "minimal", 0, False, False, False,
                                "normal", "group_aizuchi")
        if decision.decision in {AddressingAction.IGNORE, AddressingAction.OBSERVE, AddressingAction.WAIT_FOR_HUMAN}:
            return ResponsePlan(False, ResponseRole.REMAIN_SILENT, "none", 0, False, False, False,
                                "normal", "not_addressed" if decision.decision is AddressingAction.IGNORE else "ambiguous")
        if decision.decision in {AddressingAction.BRIEF, AddressingAction.OPTIONAL_JOIN}:
            return ResponsePlan(True, ResponseRole.BRIEF_ANSWER, "short", 0, False, False, False,
                                "normal", "ambiguous_but_relevant")
        continuation = state.assistant_turn_open and state.active_topic
        role = ResponseRole.CONTINUE_PREVIOUS_TOPIC if continuation else ResponseRole.ANSWER_AND_EXPAND
        is_question = "?" in text or "？" in text or any(word in text for word in ("なぜ", "どうして", "詳しく", "理由"))
        depth = self._depth if is_question else min(1, self._depth)
        length = self._default_length if is_question else "short"
        return ResponsePlan(True, role, length, depth,
                            False, bool(state.active_topic or state.unresolved_questions), False,
                            "normal", "addressed")

    def _should_group_aizuchi(self, state: ConversationState) -> bool:
        if not self._group_aizuchi:
            return False
        if not (state.voice_human_count and state.voice_human_count > 1):
            return False
        now = time.time()
        if state.last_assistant_at and now - state.last_assistant_at < self._group_aizuchi_interval_s:
            return False  # 相槌の打ちすぎ防止 (直近に自分が話したばかり)
        return random.random() < self._group_aizuchi_p
