"""認知ループの型。**LLMに永続状態を持たせないための境界**。

LLMは意味理解・推論・言語化を提供する交換可能なサービスであって、
自己・記憶・関係性・感情・目標の持ち主ではない。持ち主は既存のモジュール
（`Mind` / `Relationships` / `TemporalSelf` / `WorkingMemory` /
`ConversationKernel`）で、ここはそれらを**参照するための共通の形**だけを定義する。

新しい保管場所は作らない。既存の型と重複させない:

* イベントは `dialogue/events.py` の `ConversationEvent` を包む
* 行動候補の点数付けは `dialogue/selection_kernel.py` を使う
* 「このターンの義務」は `dialogue/kernel.py` の `TurnFrame` が持っている

ここが足すのは、**文章を作る前に「何をするか」を決めて記録する**部分だけ。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """音声・映像・システム・内部を1つの形へ揃える。

    既存の `ConversationEventType` と1対1で対応するものはそちらを正本とし、
    ここでは**会話以外の入力**（映像・ゲーム・道具の結果）を足すために使う。
    """

    USER_UTTERANCE = "user_utterance"
    AI_INTERRUPTED = "ai_interrupted"
    SPEAKER_CHANGED = "speaker_changed"
    ASR_UNCERTAIN = "asr_uncertain"
    VISUAL_CHANGE = "visual_change"
    GAME_DANGER = "game_danger"
    SILENCE_TIMEOUT = "silence_timeout"
    TOOL_RESULT = "tool_result"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"


class InformationType(StrEnum):
    """その情報が**どこから来たか**。

    推測をユーザーの発言として保存しないための区別。既存の記憶形式は壊さず、
    メタデータとして添えるだけにする。
    """

    OBSERVATION = "observation"
    USER_STATEMENT = "user_statement"
    INFERENCE = "inference"
    REFLECTION = "reflection"
    SYSTEM_FACT = "system_fact"


class ActionType(StrEnum):
    """**文章を作る前に決めるもの。**

    `response_shape` や文体は行動ではない。行動が決まってから、
    Conversation Planner が中身を、Surface Realizer が言い方を決める。
    """

    ANSWER = "answer"
    ACKNOWLEDGE_EMOTION = "acknowledge_emotion"
    #: 終了シグナルへの短い了承。共感とは別物——「わかった」で終える。
    BRIEF_ACKNOWLEDGE = "brief_acknowledge"
    # -- 自分から動く行動 (Phase 5) ------------------------------------
    #
    # **自発発話専用の Action Selector は作らない。** 同じ候補の列に
    # 並べて比べる。別系統にすると、通常会話と自発発話でどちらが優先かを
    # 決める場所がもう1つ増えて、必ず食い違う。
    COMMENT = "comment"
    REACT = "react"
    REMIND = "remind"
    RESUME_OBLIGATION = "resume_obligation"
    LIGHT_FOLLOW_UP = "light_follow_up"
    SHARE_MEMORY = "share_memory"
    ASK_CLARIFICATION = "ask_clarification"
    CHALLENGE_ASSUMPTION = "challenge_assumption"
    CONTINUE_PREVIOUS_TOPIC = "continue_previous_topic"
    # -- 出入りへの反応 (Phase 6D) ---------------------------------------
    #
    # **挨拶も他の候補と同じ列に並べる。** 固定文を即座に流す経路にすると、
    # 相手が話している最中でも、危険警告の途中でも、必ず声が出る。
    GREET = "greet"
    #: 「居るのは分かっているよ」を短く示すだけ。挨拶より軽い。
    ACKNOWLEDGE_PRESENCE = "acknowledge_presence"
    FAREWELL = "farewell"
    WARN = "warn"
    MAKE_LIGHT_JOKE = "make_light_joke"
    # -- 道具を使う行動 (Phase 7) -----------------------------------------
    #
    # **これらも同じ候補の列に並べる。** 計画から直接ツールを呼ぶ経路を
    # 作ると、会話・警告・自発発話との優先順位を決める場所がもう1つ増え、
    # 「話している最中に外部へ書き込む」が起きる。
    EXECUTE_TOOL = "execute_tool"
    ASK_CONFIRMATION = "ask_confirmation"
    #: やってよいか聞かずに「こうしたらどうか」と言うだけ。実行しない。
    SUGGEST_ACTION = "suggest_action"
    CANCEL_PLAN = "cancel_plan"
    #: 待つ。沈黙とは違い、**計画はまだ生きている**。
    WAIT = "wait"
    # -- 道具の結果を伝える行動 (Phase 7B) --------------------------------
    #
    # **ツール完了を必ず喋る固定処理を作らない。** 結果が出たことは
    # 出来事の1つで、話す価値があるかは他の候補と比べて決める。
    # ただしユーザーが待っている操作は、成功も失敗も伝える。
    REPORT_TOOL_SUCCESS = "report_tool_success"
    REPORT_TOOL_FAILURE = "report_tool_failure"
    REPORT_PARTIAL_RESULT = "report_partial_result"
    #: **やり直すかを聞く。** 勝手に別の手段へ切り替えない。
    ASK_TOOL_RECOVERY_CONFIRMATION = "ask_tool_recovery_confirmation"
    #: やったかどうか分からない時。**成功と断定しない。**
    REPORT_UNKNOWN_OUTCOME = "report_unknown_outcome"
    REMAIN_SILENT = "remain_silent"
    STORE_MEMORY = "store_memory"
    RETRIEVE_MEMORY = "retrieve_memory"
    RESUME_INTERRUPTED_RESPONSE = "resume_interrupted_response"
    ABANDON_INTERRUPTED_RESPONSE = "abandon_interrupted_response"


class SpeechPolicy(StrEnum):
    """その行動が発話を伴うか。**既定は禁止側**。

    これまでは「発話経路がまず走り、沈黙は空文字で表現される」形だった。
    それだと沈黙は失敗と区別がつかないし、fallbackが復活させてしまう。
    発話は**許可された時だけ**行う。
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


_SPEECH_POLICY: dict[ActionType, SpeechPolicy] = {
    ActionType.ANSWER: SpeechPolicy.REQUIRED,
    ActionType.ACKNOWLEDGE_EMOTION: SpeechPolicy.REQUIRED,
    ActionType.BRIEF_ACKNOWLEDGE: SpeechPolicy.REQUIRED,
    ActionType.ASK_CLARIFICATION: SpeechPolicy.REQUIRED,
    ActionType.CHALLENGE_ASSUMPTION: SpeechPolicy.REQUIRED,
    ActionType.CONTINUE_PREVIOUS_TOPIC: SpeechPolicy.REQUIRED,
    ActionType.WARN: SpeechPolicy.REQUIRED,
    ActionType.MAKE_LIGHT_JOKE: SpeechPolicy.OPTIONAL,
    # 自分から動く行動。**どれも「話してもよい」であって「話すべき」ではない。**
    # OPTIONAL にしてあるのは、選ばれても最後の再検証で静かに取り消せるように
    # するため（相手が話し始めた、話題が閉じた、など）。
    ActionType.COMMENT: SpeechPolicy.OPTIONAL,
    ActionType.REACT: SpeechPolicy.OPTIONAL,
    ActionType.REMIND: SpeechPolicy.OPTIONAL,
    ActionType.RESUME_OBLIGATION: SpeechPolicy.OPTIONAL,
    ActionType.LIGHT_FOLLOW_UP: SpeechPolicy.OPTIONAL,
    ActionType.SHARE_MEMORY: SpeechPolicy.OPTIONAL,
    # **挨拶も「話してよい」であって「話すべき」ではない。**
    # `REQUIRED` にすると、選ばれた時点で発話が確定し、
    # 直前の再検証（相手が話し始めた等）で取り消せなくなる。
    ActionType.GREET: SpeechPolicy.OPTIONAL,
    ActionType.ACKNOWLEDGE_PRESENCE: SpeechPolicy.OPTIONAL,
    ActionType.FAREWELL: SpeechPolicy.OPTIONAL,
    ActionType.RESUME_INTERRUPTED_RESPONSE: SpeechPolicy.REQUIRED,
    # 道具を使う行動 (Phase 7)。
    # **実行は発話ではない。** 実行したことを話すかどうかは、
    # 結果が出てから別の候補として並べ直す。
    ActionType.EXECUTE_TOOL: SpeechPolicy.FORBIDDEN,
    # 確認は**聞かなければ始まらない**ので REQUIRED。
    ActionType.ASK_CONFIRMATION: SpeechPolicy.REQUIRED,
    ActionType.SUGGEST_ACTION: SpeechPolicy.OPTIONAL,
    ActionType.CANCEL_PLAN: SpeechPolicy.OPTIONAL,
    ActionType.WAIT: SpeechPolicy.FORBIDDEN,
    # 結果を伝える行動 (Phase 7B)。**どれも OPTIONAL。**
    # `REQUIRED` にすると、選ばれた時点で発話が確定し、直前の再検証
    # （相手が話し始めた、話題が閉じた）で取り消せなくなる。
    ActionType.REPORT_TOOL_SUCCESS: SpeechPolicy.OPTIONAL,
    ActionType.REPORT_TOOL_FAILURE: SpeechPolicy.OPTIONAL,
    ActionType.REPORT_PARTIAL_RESULT: SpeechPolicy.OPTIONAL,
    ActionType.ASK_TOOL_RECOVERY_CONFIRMATION: SpeechPolicy.OPTIONAL,
    ActionType.REPORT_UNKNOWN_OUTCOME: SpeechPolicy.OPTIONAL,
    ActionType.REMAIN_SILENT: SpeechPolicy.FORBIDDEN,
    ActionType.STORE_MEMORY: SpeechPolicy.FORBIDDEN,
    # 情報を取るだけ。取った内容を後続の発話行動へ渡すのは構わない。
    ActionType.RETRIEVE_MEMORY: SpeechPolicy.FORBIDDEN,
    ActionType.ABANDON_INTERRUPTED_RESPONSE: SpeechPolicy.FORBIDDEN,
}


def speech_policy(action: ActionType) -> SpeechPolicy:
    """知らない行動は**話さない側**へ倒す。"""
    return _SPEECH_POLICY.get(action, SpeechPolicy.FORBIDDEN)


#: 発話を伴わない行動。Conversation Planner を呼ぶ必要がない。
SILENT_ACTIONS = frozenset(
    action for action, policy in _SPEECH_POLICY.items()
    if policy is SpeechPolicy.FORBIDDEN
)


class ExecutionStatus(StrEnum):
    """やってみた結果。**沈黙は失敗ではない。**"""

    SPOKEN_COMPLETED = "spoken_completed"
    SILENT_COMPLETED = "silent_completed"
    INTERNAL_COMPLETED = "internal_completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CognitiveEvent:
    """入力を1つの形へ正規化したもの。"""

    event_type: EventType | str
    source: str = "local"
    actor_id: str = ""
    content: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    related_event_ids: tuple[str, ...] = ()
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @classmethod
    def from_conversation_event(cls, event, **overrides) -> "CognitiveEvent":
        """既存の `ConversationEvent` を包む。新しい入口を作らないため。"""
        mapping = {
            "speech.final": EventType.USER_UTTERANCE,
            "speech.interrupted": EventType.AI_INTERRUPTED,
            "assistant.response_interrupted": EventType.AI_INTERRUPTED,
            "speaker.detected": EventType.SPEAKER_CHANGED,
            "conversation.silence_detected": EventType.SILENCE_TIMEOUT,
        }
        event_type = mapping.get(str(event.event_type), str(event.event_type))
        data = {
            "event_type": event_type,
            "source": getattr(event, "source", "local"),
            "actor_id": getattr(event, "speaker_id", "") or "",
            "content": getattr(event, "text", "") or "",
            "confidence": (
                1.0 if getattr(event, "speaker_confidence", None) is None
                else float(event.speaker_confidence)
            ),
            "metadata": dict(getattr(event, "metadata", {}) or {}),
            "event_id": getattr(event, "event_id", uuid.uuid4().hex[:12]),
        }
        data.update(overrides)
        return cls(**data)

    def summary(self) -> dict[str, Any]:
        """ログ用。**本文は入れない**（第12条）。"""
        return {
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "source": self.source,
            "confidence": round(float(self.confidence), 3),
            "chars": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """比べられる形にした行動案。

    `score_components` を残すのは、**なぜ選ばれたかを後から読む**ため。
    合計点だけだと、重みを直した時に何が変わったのか分からない。
    """

    action_type: ActionType
    target: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    score_components: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0
    confidence: float = 1.0
    blocking_conditions: tuple[str, ...] = ()
    # -- 出どころ (Phase 5) ---------------------------------------------
    #
    # **どこから来た候補かを候補自身が持つ。** 自発発話が通常会話と同じ列に
    # 並ぶので、選ばれた後に「これは誰が言い出したのか」を辿れないと、
    # Speech Gate が検証しようがない。
    source_type: str = ""
    source_event_ids: tuple[str, ...] = ()
    topic_id: str = ""
    participant_ids: tuple[str, ...] = ()
    #: 期限。**過ぎた候補で喋らない**（状況はもう変わっている）。
    expires_at: float | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_conditions)

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) > self.expires_at

    def snapshot(self) -> dict[str, Any]:
        payload = {
            "action": str(self.action_type),
            "score": round(float(self.total_score), 3),
            "components": {k: round(float(v), 3) for k, v in self.score_components.items()},
            "reasons": list(self.reasons),
            "blocked": list(self.blocking_conditions),
        }
        if self.source_type:
            payload["source"] = self.source_type
            payload["events"] = list(self.source_event_ids[:3])
            payload["topic"] = self.topic_id
        return payload


@dataclass(frozen=True, slots=True)
class ActionDecision:
    """選んだ結果。**長い思考過程は残さない。**

    後から「なぜこの行動だったか」をたどれる短い構造だけ持つ。
    """

    selected_action: ActionType
    #: 主要行動に添える1つだけの補助行動。**任意個は連結しない。**
    #: 「共感してから答える」のような組でしか要らないので、2つで足りる。
    supporting_action: ActionType | None = None
    rejected_actions: tuple[ActionCandidate, ...] = ()
    decision_reason: str = ""
    state_snapshot_id: str = ""
    event_ids: tuple[str, ...] = ()
    confidence: float = 1.0
    parameters: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: どのターンの決定か。古い非同期出力を弾くために出力側も持ち回る。
    turn_id: str = ""
    # -- 選ばれた候補の出どころ (Phase 5) --------------------------------
    #
    # Speech Gate が「この決定は自発発話か、通常会話か」を見て条件を変える。
    # 決定まで持ち上げないと、最終出口で判定材料が無い。
    source_type: str = ""
    source_event_ids: tuple[str, ...] = ()
    topic_id: str = ""
    participant_ids: tuple[str, ...] = ()
    expires_at: float | None = None

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) > self.expires_at

    @property
    def speech_policy(self) -> "SpeechPolicy":
        """主要と補助を合わせた最終的な発話可否。**矛盾は禁止側へ倒す。**

        `REMAIN_SILENT` に発話する補助行動が付いた決定は矛盾している。
        安全側（話さない）を採り、呼び出し側が警告を残せるよう
        `contradictory` で分かるようにする。
        """
        primary = speech_policy(self.selected_action)
        if primary is SpeechPolicy.FORBIDDEN:
            return SpeechPolicy.FORBIDDEN
        return primary

    @property
    def contradictory(self) -> bool:
        """沈黙と発話が同時に指定されている決定。"""
        if self.supporting_action is None:
            return False
        return (
            speech_policy(self.selected_action) is SpeechPolicy.FORBIDDEN
            and speech_policy(self.supporting_action) is not SpeechPolicy.FORBIDDEN
        )

    @property
    def speaks(self) -> bool:
        return self.speech_policy is not SpeechPolicy.FORBIDDEN

    def snapshot(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "selected": str(self.selected_action),
            "supporting": str(self.supporting_action) if self.supporting_action else "",
            "reason": self.decision_reason,
            "confidence": round(float(self.confidence), 3),
            "state_snapshot_id": self.state_snapshot_id,
            "rejected": [c.snapshot() for c in self.rejected_actions[:4]],
        }


@dataclass(slots=True)
class ActionOutcome:
    """やってみてどうなったか。**これが状態へ戻らないと閉ループにならない。**"""

    action_id: str
    status: str = ExecutionStatus.SPOKEN_COMPLETED
    turn_id: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    interrupted: bool = False
    user_reaction: str = ""
    tool_result: dict[str, Any] | None = None
    error: str = ""
    observable_effects: dict[str, Any] = field(default_factory=dict)
    #: 何を呼んで、何を呼ばなかったか。沈黙が本当に沈黙だったかの証拠。
    speech_generated: bool = False
    planner_called: bool = False
    realizer_called: bool = False
    tts_called: bool = False
    pending_response_cleared: bool = False
    turn_closed: bool = False
    topic_closed: bool = False
    affected_obligation_ids: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        """**沈黙は失敗ではない。**"""
        return self.status in {
            ExecutionStatus.SPOKEN_COMPLETED,
            ExecutionStatus.SILENT_COMPLETED,
            ExecutionStatus.INTERNAL_COMPLETED,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "turn_id": self.turn_id,
            "status": str(self.status),
            "interrupted": self.interrupted,
            "error": self.error[:120],
            "speech_generated": self.speech_generated,
            "planner_called": self.planner_called,
            "realizer_called": self.realizer_called,
            "tts_called": self.tts_called,
            "pending_response_cleared": self.pending_response_cleared,
            "turn_closed": self.turn_closed,
            "topic_closed": self.topic_closed,
            "obligations": list(self.affected_obligation_ids),
            "effects": dict(self.observable_effects),
        }
