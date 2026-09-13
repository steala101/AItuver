"""Authoritative per-turn conversation decision frame.

The kernel does not own memories, activities, search, or playback.  It records
how those authoritative systems influenced one response and keeps that
decision observable from input finalization through generation and TTS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import logging
import re
import threading
import time
from typing import Any, Iterable
from uuid import uuid4


logger = logging.getLogger(__name__)


class DialogueObligation(str, Enum):
    REPAIR = "repair"
    ANSWER = "answer"
    RESOLVE_REFERENCE = "resolve_reference"
    CONTINUE_ACTIVITY = "continue_activity"
    ACKNOWLEDGE_EMOTION = "acknowledge_emotion"
    RESPOND = "respond"


class ReferenceStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    CONFLICTED = "CONFLICTED"
    STALE = "STALE"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(slots=True)
class ReferenceResolutionResult:
    status: str = ReferenceStatus.NOT_REQUIRED.value
    resolved_value: str = ""
    source_turn_ids: list[str] = field(default_factory=list)
    candidate_values: list[str] = field(default_factory=list)
    confidence: float = 1.0
    ambiguity_reason: str = ""
    requires_clarification: bool = False


@dataclass(slots=True)
class SearchDecision:
    allowed: bool
    reason_code: str
    explicit_request: bool = False
    query_source: str = "none"
    blocked_by_context: bool = False
    blocked_by_privacy: bool = False
    requires_clarification: bool = False
    decided_at: float = field(default_factory=time.time)
    executed: bool = False
    executed_at: float | None = None
    query_summary: str = ""
    result_found: bool | None = None
    error_code: str = ""

    def legacy(self) -> dict[str, Any]:
        return {"allow_search": self.allowed, "reason": self.reason_code}


@dataclass(slots=True)
class MemoryInfluence:
    text: str
    effect: str
    confidence: float
    mention_allowed: bool = False
    memory_id: str = ""
    relevance: float = 0.0
    freshness: float = 0.0
    privacy_allowed: bool = True
    contradiction_status: str = "NONE"
    influence_type: str = "context"
    applied: bool = True
    rejection_reason: str = ""


@dataclass(slots=True)
class DecisionTrace:
    turn_id: str
    utterance_id: str
    decision_id: str
    conversation_id: str
    response_id: str
    source: str
    speaker_id: str
    normalized_input: str
    obligations: list[str]
    decision_priority: str
    decision_reason: str
    reference_resolution: dict[str, Any]
    search_decision: dict[str, Any]
    selected_memory_ids: list[str]
    rejected_memory_ids: list[str]
    memory_rejections: list[dict[str, str]]
    activity_state_version: int
    created_at: float
    updated_at: float
    generation_id: str = ""
    final_output_summary: str = ""
    override_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TurnFrame:
    turn_id: str
    created_at: float
    source: str
    speaker_id: str
    user_text: str
    active_topic: str
    obligations: list[str]
    response_goal: str
    response_shape: str
    social_mode: str
    utterance_id: str = ""
    decision_id: str = ""
    conversation_id: str = ""
    response_id: str = ""
    normalized_input: str = ""
    decision_priority: str = ""
    decision_reason: str = ""
    reference_resolution: ReferenceResolutionResult = field(
        default_factory=ReferenceResolutionResult
    )
    search_decision: SearchDecision = field(
        default_factory=lambda: SearchDecision(True, "normal_tool_scheduler")
    )
    activity: dict[str, Any] = field(default_factory=dict)
    activity_state_version: int = 0
    continuity: dict[str, Any] = field(default_factory=dict)
    memory_influences: list[MemoryInfluence] = field(default_factory=list)
    rejected_memory_ids: list[str] = field(default_factory=list)
    memory_rejections: list[dict[str, str]] = field(default_factory=list)
    tool_policy: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    # Continuation: how this obligation is discharged over time.  The kernel
    # decides single-vs-continuing and the relationship to any running
    # directive; it never decides what the directive should say.
    execution_mode: str = "ONE_SHOT_RESPONSE"
    directive_action: str = "NONE"
    directive_relation: str = "NEW_REQUEST"
    active_directive_id: str = ""
    plan_candidate: bool = False
    plan_candidate_reason: str = ""
    generation_id: str = ""
    final_output_summary: str = ""
    updated_at: float = field(default_factory=time.time)
    override_events: list[dict[str, Any]] = field(default_factory=list)

    def decision_trace(self) -> DecisionTrace:
        return DecisionTrace(
            turn_id=self.turn_id,
            utterance_id=self.utterance_id,
            decision_id=self.decision_id,
            conversation_id=self.conversation_id,
            response_id=self.response_id,
            source=self.source,
            speaker_id=self.speaker_id,
            normalized_input=self.normalized_input,
            obligations=list(self.obligations),
            decision_priority=self.decision_priority,
            decision_reason=self.decision_reason,
            reference_resolution=asdict(self.reference_resolution),
            search_decision=asdict(self.search_decision),
            selected_memory_ids=[
                item.memory_id for item in self.memory_influences
                if item.applied and item.memory_id
            ],
            rejected_memory_ids=list(self.rejected_memory_ids),
            memory_rejections=list(self.memory_rejections),
            activity_state_version=self.activity_state_version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            generation_id=self.generation_id,
            final_output_summary=self.final_output_summary,
            override_events=list(self.override_events),
        )

    def snapshot(self, *, public: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value["decision_trace"] = asdict(self.decision_trace())
        age_s = max(0.0, time.time() - self.updated_at)
        value["health"] = {
            "state": "STALE" if age_s > 300 else "OK",
            "age_s": round(age_s, 3),
        }
        if public:
            value.pop("user_text", None)
            value["normalized_input"] = ""
            value["decision_trace"]["normalized_input"] = ""
            for item in value.get("memory_influences", []):
                item["text"] = ""
        return value


_QUESTION = re.compile(
    r"[?？]|(?:何|なに|どこ|どれ|どの|どう|なぜ|なんで|教えて|分かる|わかる|知ってる)"
)
_REPAIR = re.compile(
    r"(?:違う|ちがう|じゃなくて|ではなく|訂正|聞き間違|言い間違|"
    r"そういう意味じゃ|さっき言った|覚えてない|忘れた|"
    r"っていう意味|という意味)"
)
#: 「今の」「前の」「さっきの」が会話を指していると言えるのは、後ろに
#: **話の単位を表す語**が来るか、そこで句が切れる時だけ。
#:
#: 裸で並べていたせいで「**今の**AIって話しててもAI感が強いんだよね」——
#: ただの感想——が会話参照と判定され、Kernelが
#: `shape=clarify_reference` / `goal="Ask one concrete clarification question"`
#: を「このターンの権威ある決定」として渡していた。ポッポがあそこで必ず
#: 聞き返すのはそのため。**同じ入力なら毎回同じ命令になるので、書き出しも
#: 固まる**（`tools/check_opening_diversity.py` の Kernel アームで、
#: 詰まった位置はこの1件だけだった）。
#:
#: 2026-07-29にCodexが直した「話してても」の部分一致と同じ形。
#: 連体詞は後ろの名詞まで見ないと、指示語かどうか決まらない。
_DISCOURSE_NOUN = r"(?:話|はなし|こと|事|やつ|奴|件|内容|続き|返事|答え|回答|質問|説明)"
_REFERENCE = re.compile(
    r"(?:それ|あれ|これ|その話|あの話|この話|"
    rf"(?:さっき|前|今)の\s*{_DISCOURSE_NOUN}|"
    r"(?:さっき|前|今)の(?=[、。！？!?\s]|$)|"
    r"おすすめした|何の話|何だった|なんだった"
    # 「何調べてたの?」「何してたの?」— asking about the assistant's own
    # previous turn.  The answer is in the conversation, never on the Web.
    r"|(?:何|なに|なん)(?:を|か)?(?:調べ|し|やっ|話し|見)(?:て)?(?:た|てた|てる|る)"
    r"|(?:なんて|何て|なんと)(?:言っ|いっ)"
    r")"
)
_EMOTION = re.compile(
    r"(?:つらい|辛い|悲しい|怖い|疲れた|嬉しい|楽しい|むかつく|腹立つ|不安)"
)
def _explicit_search_request(text: str) -> bool:
    """Delegate to the one shared request/question classifier.

    A plain substring match on 「調べて」 used to fire here, so 「何調べてたの?」
    — a question about the assistant's previous turn — was treated as a search
    order and routed to the Web.
    """
    from neuro_voice.search.intent import is_search_request

    return is_search_request(text)
_UNDER_SPECIFIED_CURRENT = re.compile(
    r"(?:今|現在|最新).{0,8}(?:いくら|値段|価格|どうなってる|何だろう|なんだろう)"
)
_CORRECTION_TARGET = re.compile(
    r"(?:違う|ちがう|そうじゃなくて|ではなく)[、,\s]*(.{1,80}?)(?:のこと|ってこと|だよ|です)?[。.!！?？]?$"
)
_QUOTED = re.compile(r"[「『\"]([^」』\"]{1,80})[」』\"]")


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _candidate_text(raw: dict[str, Any] | str) -> tuple[str, str, float]:
    if isinstance(raw, str):
        return raw.strip(), "", 0.72
    value = str(
        raw.get("resolved_value")
        or raw.get("assistant_text")
        or raw.get("text")
        or raw.get("summary")
        or raw.get("user_text")
        or ""
    ).strip()
    source_id = str(raw.get("turn_id") or raw.get("id") or "")
    score = _clamp(raw.get("score", raw.get("confidence", 0.72)))
    return value, source_id, score


#: social_mode を発話の指示へ変換する。認知カーネル(bridge.py)が設定する値。
#: 既存の one_to_one / group / close は従来どおりラベルのまま（会話の場の
#: 記述であって指示ではないため）。
_SOCIAL_MODE_HINTS: dict[str, str] = {
    "supportive": "social_mode指示: 相手の気持ちを先に受け止める。すぐ解決策へ行かない。",
    "urgent": "social_mode指示: 一文で要点だけ。前置きと冗談を入れない。",
    "playful": "social_mode指示: 軽い遊びを一つだけ。すぐ本題へ戻る。",
    "candid": "social_mode指示: 迎合せず、理由を一つ添えて率直に言う。",
}


class ConversationKernel:
    """Resolve and trace one authoritative response decision per turn."""

    _PROTECTED_FIELDS = {
        "obligations",
        "decision_priority",
        "decision_reason",
        "reference_resolution",
        "search_decision",
        "activity",
        "activity_state_version",
        "response_goal",
        "response_shape",
        "execution_mode",
        "directive_action",
    }

    def __init__(self, *, enabled: bool = True, max_frames: int = 24):
        self.enabled = bool(enabled)
        self._max_frames = max(4, int(max_frames))
        self._frames: dict[str, list[TurnFrame]] = {}
        self._by_turn: dict[str, TurnFrame] = {}
        self._by_response: dict[str, TurnFrame] = {}
        self._reference_state: dict[tuple[str, str], ReferenceResolutionResult] = {}
        self._lock = threading.RLock()

    @staticmethod
    def is_context_reference(text: str) -> bool:
        value = _normalize(text)
        return bool(_REFERENCE.search(value) and not _explicit_search_request(value))

    @classmethod
    def search_decision_for(
        cls,
        text: str,
        *,
        activity_active: bool = False,
        reference: ReferenceResolutionResult | None = None,
        privacy_blocked: bool = False,
    ) -> SearchDecision:
        value = _normalize(text)
        explicit = _explicit_search_request(value)
        if privacy_blocked:
            return SearchDecision(
                False, "privacy_boundary", explicit_request=explicit,
                blocked_by_privacy=True,
            )
        if activity_active:
            return SearchDecision(
                False, "canonical_activity_active", explicit_request=explicit,
                blocked_by_context=True,
            )
        from neuro_voice.dialogue.repair import analyze_conversation_repair

        if analyze_conversation_repair(value).is_repair or _REPAIR.search(value):
            return SearchDecision(
                False, "conversation_repair_first", explicit_request=explicit,
                blocked_by_context=True,
            )
        if cls.is_context_reference(value):
            unresolved = reference is None or reference.status != ReferenceStatus.RESOLVED.value
            return SearchDecision(
                False,
                "reference_requires_clarification" if unresolved
                else "resolved_from_conversation",
                explicit_request=explicit,
                query_source="conversation",
                blocked_by_context=True,
                requires_clarification=unresolved,
            )
        if explicit:
            return SearchDecision(
                True, "explicit_search_request", explicit_request=True,
                query_source="user",
            )
        if _UNDER_SPECIFIED_CURRENT.search(value):
            return SearchDecision(
                False, "underspecified_current_fact",
                requires_clarification=True,
            )
        return SearchDecision(True, "normal_tool_scheduler")

    @classmethod
    def tool_policy_for(
        cls, text: str, *, activity_active: bool = False
    ) -> dict[str, Any]:
        return cls.search_decision_for(
            text, activity_active=activity_active,
        ).legacy()

    @staticmethod
    def _continuation_decision(
        text: str, active_directive: Any,
    ) -> tuple[str, str, str, bool, str]:
        """Decide single-vs-continuing and the relation to a running directive.

        This is the *only* place that answer is produced.  A second classifier
        elsewhere is exactly how two engines end up talking at once.
        """
        from neuro_voice.dialogue.intent_plan import (
            classify_follow_up, continuation_candidate,
        )

        if active_directive is not None:
            action, relation, reason = classify_follow_up(text, active_directive)
            mode = str(getattr(active_directive, "execution_mode", "ONGOING_DIRECTIVE"))
            return mode, str(action), str(relation), False, reason
        nominated, reason = continuation_candidate(text)
        return (
            "ONE_SHOT_RESPONSE",
            "CREATE" if nominated else "NONE",
            "NEW_REQUEST",
            nominated,
            reason,
        )

    def _resolve_reference(
        self,
        text: str,
        *,
        source: str,
        speaker_id: str,
        candidates: Iterable[dict[str, Any] | str],
    ) -> ReferenceResolutionResult:
        from neuro_voice.dialogue.repair import analyze_conversation_repair

        repair = analyze_conversation_repair(text)
        if not self.is_context_reference(text) and not repair.is_repair and not _REPAIR.search(text):
            return ReferenceResolutionResult()

        if repair.corrected_meaning:
            result = ReferenceResolutionResult(
                status=ReferenceStatus.RESOLVED.value,
                resolved_value=repair.canonical_fact[:180],
                confidence=repair.confidence,
            )
            self._reference_state[(source, speaker_id)] = result
            return result

        correction = _CORRECTION_TARGET.search(text)
        if correction:
            resolved = _normalize(correction.group(1)).strip("「」『』\"' ")
            if resolved:
                result = ReferenceResolutionResult(
                    status=ReferenceStatus.RESOLVED.value,
                    resolved_value=resolved[:180],
                    confidence=0.99,
                )
                self._reference_state[(source, speaker_id)] = result
                return result

        prepared: list[tuple[str, str, float]] = []
        for raw in candidates:
            value, source_id, score = _candidate_text(raw)
            if not value:
                continue
            quoted = _QUOTED.findall(value)
            compact = quoted[-1] if quoted else value
            prepared.append((_normalize(compact)[:180], source_id, score))

        if not prepared:
            previous = self._reference_state.get((source, speaker_id))
            if previous and previous.status == ReferenceStatus.RESOLVED.value:
                return ReferenceResolutionResult(
                    status=ReferenceStatus.RESOLVED.value,
                    resolved_value=previous.resolved_value,
                    source_turn_ids=list(previous.source_turn_ids),
                    candidate_values=[previous.resolved_value],
                    confidence=max(0.55, previous.confidence * 0.9),
                )
            return ReferenceResolutionResult(
                status=ReferenceStatus.NOT_FOUND.value,
                confidence=0.0,
                ambiguity_reason="no_grounded_conversation_candidate",
                requires_clarification=True,
            )

        prepared.sort(key=lambda item: item[2], reverse=True)
        best = prepared[0]
        distinct = list(dict.fromkeys(item[0] for item in prepared))
        ambiguous = len(prepared) > 1 and abs(best[2] - prepared[1][2]) < 0.08
        if ambiguous:
            return ReferenceResolutionResult(
                status=ReferenceStatus.AMBIGUOUS.value,
                candidate_values=distinct[:3],
                source_turn_ids=[item[1] for item in prepared[:3] if item[1]],
                confidence=best[2],
                ambiguity_reason="multiple_similarly_ranked_conversation_candidates",
                requires_clarification=True,
            )
        result = ReferenceResolutionResult(
            status=ReferenceStatus.RESOLVED.value,
            resolved_value=best[0],
            source_turn_ids=[best[1]] if best[1] else [],
            candidate_values=distinct[:3],
            confidence=best[2],
        )
        self._reference_state[(source, speaker_id)] = result
        return result

    def build(
        self,
        text: str,
        *,
        source: str,
        speaker_id: str,
        active_topic: str = "",
        activity: dict[str, Any] | None = None,
        working_memory: dict[str, Any] | None = None,
        memories: Iterable[dict[str, Any] | str] = (),
        rejected_memories: Iterable[dict[str, Any] | str] = (),
        reference_candidates: Iterable[dict[str, Any] | str] = (),
        relationship: dict[str, Any] | None = None,
        momentum: float = 0.35,
        group: bool = False,
        active_directive: Any = None,
        turn_id: str | None = None,
        utterance_id: str | None = None,
        decision_id: str | None = None,
        conversation_id: str | None = None,
        response_id: str | None = None,
    ) -> TurnFrame:
        activity = dict(activity or {})
        working = dict(working_memory or {})
        value = _normalize(text)
        source = str(source or "local")
        speaker_id = str(speaker_id or "unknown")
        active_activity = bool(
            activity and activity.get("status") not in
            {"COMPLETED", "PAUSED", "NEEDS_REPAIR"}
        )
        reference = self._resolve_reference(
            value,
            source=source,
            speaker_id=speaker_id,
            candidates=reference_candidates,
        )

        obligations: list[DialogueObligation] = []
        from neuro_voice.dialogue.repair import analyze_conversation_repair

        repair = analyze_conversation_repair(value)
        if repair.is_repair or _REPAIR.search(value):
            obligations.append(DialogueObligation.REPAIR)
        if self.is_context_reference(value):
            obligations.append(DialogueObligation.RESOLVE_REFERENCE)
        if _QUESTION.search(value):
            obligations.append(DialogueObligation.ANSWER)
        if active_activity:
            obligations.append(DialogueObligation.CONTINUE_ACTIVITY)
        if _EMOTION.search(value):
            obligations.append(DialogueObligation.ACKNOWLEDGE_EMOTION)
        if not obligations:
            obligations.append(DialogueObligation.RESPOND)

        priority = {
            DialogueObligation.REPAIR: 0,
            DialogueObligation.RESOLVE_REFERENCE: 1,
            DialogueObligation.ANSWER: 2,
            DialogueObligation.CONTINUE_ACTIVITY: 3,
            DialogueObligation.ACKNOWLEDGE_EMOTION: 4,
            DialogueObligation.RESPOND: 5,
        }
        obligations = sorted(dict.fromkeys(obligations), key=priority.get)
        primary = obligations[0]

        if primary is DialogueObligation.REPAIR:
            goal = (
                "Replace the rejected interpretation with the user's corrected meaning, "
                "acknowledge it briefly, and stop unless the user also asked a question."
            )
            shape = "repair_and_stop"
            reason = "explicit_user_repair"
        elif DialogueObligation.RESOLVE_REFERENCE in obligations:
            if reference.requires_clarification:
                goal = "Ask one concrete clarification question; do not search or guess."
                shape = "clarify_reference"
                reason = f"reference_{reference.status.lower()}"
            else:
                goal = "Use the resolved conversation referent and answer without external search."
                shape = "resolve_then_answer"
                reason = "reference_resolved_from_conversation"
        elif DialogueObligation.ANSWER in obligations:
            goal = "Answer the question directly before adding one useful thought."
            shape = "answer_first"
            reason = "direct_question"
        elif active_activity:
            goal = "Continue only from the canonical activity state."
            shape = "activity_turn"
            reason = "canonical_activity"
        else:
            goal = str(working.get("conversation_goal") or "Respond naturally to this turn.")
            shape = "brief" if len(value) <= 12 and momentum < 0.55 else "conversational"
            reason = "normal_conversation"

        relation = str((relationship or {}).get("label") or "")
        social_mode = "group" if group else (
            "close" if relation in {"親友", "とても仲良い"} else "one_to_one"
        )

        influences: list[MemoryInfluence] = []
        for raw in memories:
            if isinstance(raw, str):
                memory_text, score, mention = raw, 0.72, False
                memory_id, freshness, relevance = "", 0.0, score
                privacy_allowed, contradiction = True, "NONE"
                influence_type = "context"
            else:
                memory_text = str(raw.get("text") or raw.get("summary") or "")
                score = _clamp(raw.get("score", raw.get("confidence", 0.72)))
                mention = bool(raw.get("mention_allowed", False))
                memory_id = str(raw.get("memory_id") or raw.get("id") or "")
                freshness = _clamp(raw.get("freshness", 0.0))
                relevance = _clamp(raw.get("relevance", score))
                privacy_allowed = bool(raw.get("privacy_allowed", True))
                contradiction = str(raw.get("contradiction_status") or "NONE")
                influence_type = str(raw.get("influence_type") or "context")
            if not memory_text or not privacy_allowed or contradiction not in {"", "NONE"}:
                continue
            effect = (
                "指示対象や過去の事実を照合する"
                if DialogueObligation.RESOLVE_REFERENCE in obligations
                else "今回の考え方・距離感へ反映する"
            )
            influences.append(MemoryInfluence(
                text=memory_text[:180],
                effect=effect,
                confidence=score,
                mention_allowed=mention,
                memory_id=memory_id,
                relevance=relevance,
                freshness=freshness,
                privacy_allowed=privacy_allowed,
                contradiction_status=contradiction,
                influence_type=influence_type,
            ))
            if len(influences) >= 3:
                break

        rejection_items: list[dict[str, str]] = []
        rejected_ids: list[str] = []
        for raw in rejected_memories:
            if isinstance(raw, str):
                memory_id, rejection = raw, "NOT_SELECTED"
            else:
                memory_id = str(raw.get("memory_id") or raw.get("id") or "")
                rejection = str(raw.get("rejection_reason") or "NOT_SELECTED")
            if memory_id:
                rejected_ids.append(memory_id)
                rejection_items.append({
                    "memory_id": memory_id,
                    "reason": rejection[:80],
                })

        search = self.search_decision_for(
            value, activity_active=active_activity, reference=reference,
        )
        execution_mode, directive_action, directive_relation, plan_candidate, plan_reason = (
            self._continuation_decision(value, active_directive)
        )
        if plan_candidate:
            # A continuing request is interpreted before it is answered.  The
            # response goal must not silently collapse back into one reply.
            goal = (
                "Interpret what the person wants to keep happening, then begin it. "
                "Do not close the topic in a single reply."
            )
            reason = f"continuation_candidate:{plan_reason}"
        created = time.time()
        turn_id = str(turn_id or uuid4().hex)
        utterance_id = str(utterance_id or uuid4().hex)
        decision_id = str(decision_id or uuid4().hex)
        response_id = str(response_id or "")
        frame = TurnFrame(
            turn_id=turn_id,
            utterance_id=utterance_id,
            decision_id=decision_id,
            conversation_id=str(
                conversation_id or f"{source}:{speaker_id}"
            ),
            response_id=response_id,
            created_at=created,
            updated_at=created,
            source=source,
            speaker_id=speaker_id,
            user_text=value,
            normalized_input=value,
            active_topic=str(active_topic or working.get("active_theme") or ""),
            obligations=[item.value for item in obligations],
            decision_priority=primary.value,
            decision_reason=reason,
            response_goal=goal,
            response_shape=shape,
            social_mode=social_mode,
            reference_resolution=reference,
            search_decision=search,
            activity=activity,
            activity_state_version=int(activity.get("state_version") or 0),
            continuity={
                "focus": str(working.get("continuing_thought") or ""),
                "open_questions": list(working.get("open_questions") or [])[:3],
            },
            memory_influences=influences,
            rejected_memory_ids=list(dict.fromkeys(rejected_ids)),
            memory_rejections=rejection_items,
            tool_policy=search.legacy(),
            execution_mode=execution_mode,
            directive_action=directive_action,
            directive_relation=directive_relation,
            active_directive_id=(
                str(getattr(active_directive, "directive_id", "") or "")
                if active_directive is not None else ""
            ),
            plan_candidate=plan_candidate,
            plan_candidate_reason=plan_reason,
            confidence={
                "activity": 1.0 if active_activity else 0.0,
                "memory": round(max(
                    (item.confidence for item in influences), default=0.0
                ), 3),
                "reference": round(reference.confidence, 3),
                "interpretation": (
                    0.92 if primary in {
                        DialogueObligation.REPAIR, DialogueObligation.ANSWER
                    } else 0.72
                ),
            },
        )
        with self._lock:
            frames = self._frames.setdefault(source, [])
            frames.append(frame)
            for expired in frames[:-self._max_frames]:
                self._by_turn.pop(expired.turn_id, None)
                if expired.response_id:
                    self._by_response.pop(expired.response_id, None)
            del frames[:-self._max_frames]
            self._by_turn[frame.turn_id] = frame
            if frame.response_id:
                self._by_response[frame.response_id] = frame
        return frame

    def current(self, source: str = "local") -> TurnFrame | None:
        with self._lock:
            frames = self._frames.get(source, [])
            return frames[-1] if frames else None

    def find(
        self, *, turn_id: str | None = None,
        response_id: str | None = None, source: str = "local",
    ) -> TurnFrame | None:
        with self._lock:
            if turn_id:
                return self._by_turn.get(turn_id)
            if response_id:
                return self._by_response.get(response_id)
            return self.current(source)

    def attach_response(self, source: str, response_id: str) -> TurnFrame | None:
        with self._lock:
            frame = self.current(source)
            if frame is None:
                return None
            if frame.response_id:
                self._by_response.pop(frame.response_id, None)
            frame.response_id = str(response_id)
            frame.updated_at = time.time()
            self._by_response[frame.response_id] = frame
            return frame

    def record_search_execution(
        self,
        *,
        source: str = "local",
        response_id: str | None = None,
        query: str = "",
        found: bool | None = None,
        error_code: str = "",
    ) -> bool:
        frame = self.find(response_id=response_id, source=source)
        if frame is None:
            return False
        frame.search_decision.executed = True
        frame.search_decision.executed_at = time.time()
        frame.search_decision.query_summary = _normalize(query)[:160]
        frame.search_decision.result_found = found
        frame.search_decision.error_code = str(error_code)[:80]
        frame.tool_policy = frame.search_decision.legacy()
        frame.updated_at = time.time()
        return True

    def record_generation(
        self, *, source: str = "local", response_id: str | None = None,
        generation_id: str = "",
    ) -> bool:
        frame = self.find(response_id=response_id, source=source)
        if frame is None:
            return False
        frame.generation_id = str(generation_id or response_id or "")
        frame.updated_at = time.time()
        return True

    def record_memory_influences(
        self,
        memories: Iterable[dict[str, Any] | str],
        *,
        rejected_memories: Iterable[dict[str, Any] | str] = (),
        source: str = "local",
        response_id: str | None = None,
    ) -> bool:
        """Enrich an existing turn after bounded asynchronous recall finishes."""
        frame = self.find(response_id=response_id, source=source)
        if frame is None:
            return False
        influences: list[MemoryInfluence] = []
        for raw in memories:
            if isinstance(raw, str):
                value = {
                    "text": raw, "score": 0.72, "mention_allowed": False,
                }
            else:
                value = raw
            text = str(value.get("text") or value.get("summary") or "")
            privacy_allowed = bool(value.get("privacy_allowed", True))
            contradiction = str(value.get("contradiction_status") or "NONE")
            if not text or not privacy_allowed or contradiction not in {"", "NONE"}:
                continue
            score = _clamp(value.get("score", value.get("confidence", 0.72)))
            influences.append(MemoryInfluence(
                text=text[:180],
                effect=(
                    "指示対象や過去の事実を照合する"
                    if DialogueObligation.RESOLVE_REFERENCE.value in frame.obligations
                    else "今回の考え方・距離感へ反映する"
                ),
                confidence=score,
                mention_allowed=bool(value.get("mention_allowed", False)),
                memory_id=str(value.get("memory_id") or value.get("id") or ""),
                relevance=_clamp(value.get("relevance", score)),
                freshness=_clamp(value.get("freshness", 0.0)),
                privacy_allowed=privacy_allowed,
                contradiction_status=contradiction,
                influence_type=str(value.get("influence_type") or "context"),
            ))
            if len(influences) >= 3:
                break
        frame.memory_influences = influences
        frame.rejected_memory_ids = []
        frame.memory_rejections = []
        for raw in rejected_memories:
            if isinstance(raw, str):
                memory_id, reason = raw, "NOT_SELECTED"
            else:
                memory_id = str(raw.get("memory_id") or raw.get("id") or "")
                reason = str(raw.get("rejection_reason") or "NOT_SELECTED")
            if memory_id:
                frame.rejected_memory_ids.append(memory_id)
                frame.memory_rejections.append({
                    "memory_id": memory_id, "reason": reason[:80],
                })
        frame.confidence["memory"] = round(max(
            (item.confidence for item in influences), default=0.0
        ), 3)
        frame.updated_at = time.time()
        return True

    def finalize_output(
        self, text: str, *, source: str = "local",
        response_id: str | None = None,
    ) -> bool:
        frame = self.find(response_id=response_id, source=source)
        if frame is None:
            return False
        frame.final_output_summary = _normalize(text)[:240]
        frame.updated_at = time.time()
        return True

    def guarded_transition(
        self,
        changes: dict[str, Any],
        *,
        source: str = "local",
        response_id: str | None = None,
        transition_reason: str = "",
    ) -> bool:
        """Apply a later-stage change only when its reason is explicit.

        This catches accidental second-engine overrides without preventing a
        legitimate state transition such as a user correction or tool result.
        """
        frame = self.find(response_id=response_id, source=source)
        if frame is None:
            return False
        protected = sorted(self._PROTECTED_FIELDS.intersection(changes))
        if protected and not transition_reason.strip():
            event = {
                "at": time.time(),
                "fields": protected,
                "reason": "missing_transition_reason",
            }
            frame.override_events.append(event)
            frame.updated_at = time.time()
            logger.error(
                "KERNEL_DECISION_OVERRIDDEN decision_id=%s fields=%s",
                frame.decision_id, ",".join(protected),
            )
            return False
        for key, value in changes.items():
            if hasattr(frame, key):
                setattr(frame, key, value)
        frame.updated_at = time.time()
        return True

    @staticmethod
    def prompt(frame: TurnFrame) -> str:
        activity = frame.activity or {}
        public = dict(activity.get("public_state") or {})
        reference = frame.reference_resolution
        lines = [
            "[CONVERSATION KERNEL - authoritative decision for this turn]",
            f"turn_id={frame.turn_id}; decision_id={frame.decision_id}; response_id={frame.response_id or 'pending'}",
            f"obligations={','.join(frame.obligations)}",
            f"decision={frame.decision_priority} ({frame.decision_reason})",
            f"response_goal={frame.response_goal}",
            f"response_shape={frame.response_shape}; social_mode={frame.social_mode}",
            # social_mode はこれまで裸のラベルだった（supportive と書いても
            # モデルには意図が届かない）。既知の値だけ短い日本語の指示にする。
            # 未知の値は何も足さない——推測で意味を与えない。
            *(
                [_SOCIAL_MODE_HINTS[frame.social_mode]]
                if frame.social_mode in _SOCIAL_MODE_HINTS else []
            ),
            (
                f"execution_mode={frame.execution_mode}; "
                f"directive_action={frame.directive_action}; "
                f"directive_relation={frame.directive_relation}"
            ),
            (
                "reference="
                f"{reference.status}; confidence={reference.confidence:.2f}; "
                f"requires_clarification={reference.requires_clarification}"
            ),
            (
                "search_decision="
                f"{'allow' if frame.search_decision.allowed else 'deny'} "
                f"({frame.search_decision.reason_code})"
            ),
        ]
        if reference.resolved_value:
            lines.append(f"resolved_reference={reference.resolved_value}")
        from neuro_voice.dialogue.repair import analyze_conversation_repair

        repair = analyze_conversation_repair(frame.user_text)
        if repair.is_repair:
            lines.append(f"authoritative_correction={repair.canonical_fact or 'previous interpretation rejected'}")
            lines.append(
                "Discard the interrupted assistant interpretation and any deferred "
                "continuation derived from it. Do not repeat or resume that answer."
            )
        if reference.candidate_values and reference.requires_clarification:
            lines.append(
                "reference_candidates=" + " | ".join(reference.candidate_values[:3])
            )
        if activity:
            lines.append(
                "canonical_activity="
                f"{activity.get('activity_name')}; status={activity.get('status')}; "
                f"version={frame.activity_state_version}; "
                f"actor={activity.get('current_actor_id')}; "
                f"required={public.get('required_kana') or 'none'}; "
                f"previous={public.get('previous_word') or 'none'}"
            )
        if frame.continuity.get("focus"):
            lines.append(f"continuity={frame.continuity['focus']}")
        for item in frame.memory_influences:
            lines.append(
                f"memory_influence(id={item.memory_id or 'none'}, "
                f"conf={item.confidence:.2f}, mention={item.mention_allowed})="
                f"{item.effect}: {item.text}"
            )
        lines.append(
            "This is internal state. Never speak IDs, labels, or numeric values. "
            "Satisfy the highest obligation first. Use memories as evidence that "
            "changes current reasoning, not as decorative callbacks. If the "
            "reference is unresolved, ask a concrete clarification and do not guess."
        )
        return "\n".join(lines)

    def snapshot(self, source: str = "local", *, public: bool = False) -> dict[str, Any]:
        frame = self.current(source)
        return frame.snapshot(public=public) if frame else {}
