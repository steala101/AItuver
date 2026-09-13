"""思い出す**必要がある時だけ**思い出す。そして、思い出した結果を行動へ効かせる。

これまでの想起（`Mind._recall`）は、毎ターン意味検索を回して結果をプロンプトへ
足していた。2つ問題がある。

1. **挨拶でも検索が走る。** 「おはよう」に長期記憶は要らない
2. **プロンプトへ足すだけで終わっている。** 記憶が行動の選択を変えていない。
   過去に同じ質問をして怒られていても、次のターンでまた質問できてしまう

ここは (a) 検索する理由があるかを先に判定し、(b) 少数だけ順位付けし、
(c) **行動候補の点数として効かせる**。プロンプトへ渡すのは最後で、
そこも短い構造化された数件だけ（生の会話履歴は投げない）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.episodic import EpisodicMemory, RETRIEVABLE, similarity
from neuro_voice.cognition.types import ActionType

# ---------------------------------------------------------------------------
# 想起する理由
# ---------------------------------------------------------------------------

#: 過去への参照。「前に」「この前」「覚えてる？」
_PAST_REFERENCE = re.compile(
    r"(?:前|以前|この前|さっき|昨日|先週|去年|いつか)(?:の|に|は|も|、|話|言|聞)|"
    r"覚えて(?:る|いる|ますか|ない)|忘れ(?:た|てない|ちゃった)|"
    r"(?:って|と)言ってた|前回"
)
#: 人物・プロジェクト名らしき塊。2文字以上のカタカナ/漢字/英字。
# A proper noun alone is not a recall request.  The old fallback used this
# pattern to search on ordinary Japanese sentences, because most contain a
# three-character noun.  Keep retrieval opt-in through an explicit past
# reference or a labelled recall question instead.
_LABELED_RECALL = re.compile(
    r"(?:合言葉|計画名|番号|機器名|認証名|識別名).{0,24}?(?:教|覚|前|以前|計測|名前)"
)


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """何を手がかりに探すか。**現在の状態から組む。**"""

    text: str = ""
    topic: str = ""
    participant: str = ""
    goal: str = ""
    keys: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    trigger: str = ""
    #: 過去の失敗と照合したい時のパターン名。
    failure_patterns: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        """トレース用。**本文は入れない**（第12条）。"""
        return {
            "trigger": self.trigger,
            "topic": self.topic[:40],
            "keys": list(self.keys[:6]),
            "obligations": len(self.obligations),
            "chars": len(self.text),
        }


@dataclass(frozen=True, slots=True)
class ObligationRetrievalGate:
    """Whether an open obligation is relevant to this user turn.

    A per-turn response obligation (``answer``/``respond``) is not eligible:
    it describes work created *for this turn*, not prior work to retrieve.
    """

    considered: bool = False
    triggered: bool = False
    reason: str = "not_applicable"
    candidate_count: int = 0
    relevance_score: float = 0.0


def obligation_retrieval_gate(
    text: str, state: Any = None, *, action: ActionType | str | None = None,
) -> ObligationRetrievalGate:
    """Allow obligation recall only for an explicit or topic-relevant resume."""
    contexts = _active_obligation_contexts(state)
    if not contexts:
        return ObligationRetrievalGate(reason="no_active_obligation")
    value = str(text or "").strip()
    if _PAST_REFERENCE.search(value):
        return ObligationRetrievalGate(
            considered=True, triggered=True, reason="explicit_resume",
            candidate_count=len(contexts), relevance_score=1.0,
        )
    if action is not None and str(action) in {
        str(ActionType.CONTINUE_PREVIOUS_TOPIC), str(ActionType.RESUME_OBLIGATION),
    }:
        return ObligationRetrievalGate(
            considered=True, triggered=True, reason="selected_continuation_action",
            candidate_count=len(contexts), relevance_score=1.0,
        )
    score = max((_obligation_relevance(value, item, state) for item in contexts), default=0.0)
    return ObligationRetrievalGate(
        considered=True, triggered=score > 0.0,
        reason="topic_or_entity_match" if score > 0.0 else "no_topic_or_entity_match",
        candidate_count=len(contexts), relevance_score=round(score, 3),
    )


def _active_obligation_contexts(state: Any) -> tuple[dict[str, Any], ...]:
    raw = tuple(getattr(state, "obligation_contexts", ()) or ())
    active_persona = str(getattr(state, "active_persona_id", "") or "")
    if not raw:
        # Compatibility for tests and callers that have only the legacy
        # string list.  These strings are treated as prior work, never as the
        # ConversationKernel's current response obligations.
        raw = tuple({"obligation_id": f"legacy:{index}", "topic_ids": (item,),
                     "status": "open", "persona_id": active_persona}
                    for index, item in enumerate(
                        tuple(getattr(state, "unresolved_obligations", ()) or ())))
    accepted: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "open") or "open").lower()
        owner = str(item.get("persona_id", "") or "")
        if status in {"resolved", "cancelled", "expired"}:
            continue
        if active_persona and owner and owner != active_persona:
            continue
        accepted.append(item)
    return tuple(accepted)


def _obligation_relevance(text: str, obligation: dict[str, Any], state: Any) -> float:
    """Small deterministic overlap check; generic one-character matches do not count."""
    value = str(text or "").replace(" ", "")
    if len(value) < 2:
        return 0.0
    topics = [str(item) for item in (obligation.get("topic_ids") or ())]
    topics += [str(obligation.get(name, "") or "") for name in ("topic", "goal")]
    for topic in topics:
        normalized = topic.replace(" ", "")
        if len(normalized) >= 2 and (normalized in value or value in normalized):
            return 1.0
        for index in range(max(0, len(normalized) - 1)):
            if normalized[index:index + 2] in value:
                return 0.7
    return 0.0


def retrieval_trigger(
    text: str, state: Any = None, *, action: ActionType | str | None = None,
) -> str:
    """**探す理由**。無ければ空文字を返し、そのターンは検索しない。

    「毎ターン全記憶を取得しない」の実体はここ。理由の名前を返すのは、
    トレースで「なぜ検索が走ったか」を読めるようにするため。
    """
    if action is not None and str(action) == str(ActionType.RETRIEVE_MEMORY):
        return "action_retrieve_memory"
    value = str(text or "").strip()
    if state is not None:
        # 打ち切りの合図と強い苛立ちを、未完了の義務より**先に**見る。
        # 逆順にしていたせいで、義務が1つ残っているだけで「もういいよ」が
        # `unresolved_obligation` に化けていた（Phase 4 の preflight で発覚）。
        # 話を切りたがっている場面は、義務の再開より優先して扱う。
        if float(getattr(state, "end_signal", 0.0) or 0.0) > .5:
            return "similar_to_past_failure"
        if float(getattr(state, "user_frustration", 0.0) or 0.0) > .5:
            return "relationship_event"
        gate = obligation_retrieval_gate(value, state, action=action)
        if gate.triggered:
            return "unresolved_obligation"
    if _PAST_REFERENCE.search(value):
        return "past_reference"
    if _LABELED_RECALL.search(value):
        return "labelled_recall"
    return ""


def build_query(
    text: str, state: Any = None, *, trigger: str = "",
    participant: str = "", failure_patterns: tuple[str, ...] = (),
) -> RecallQuery:
    value = str(text or "").strip()
    # Query keywords are only relevant after the trigger has already allowed
    # retrieval.  Do not use generic noun detection as a trigger.
    keys = ()
    return RecallQuery(
        text=value,
        topic=str(getattr(state, "current_topic", "") or ""),
        participant=participant,
        goal=str(getattr(state, "current_goal", "") or ""),
        keys=keys,
        obligations=tuple(getattr(state, "unresolved_obligations", ()) or ()),
        trigger=trigger,
        failure_patterns=failure_patterns,
    )


# ---------------------------------------------------------------------------
# 順位付け
# ---------------------------------------------------------------------------

#: ランキングの重み。**ここ以外に書かない**（`kernel.WEIGHTS` と同じ方針）。
RANKING_WEIGHTS: dict[str, float] = {
    "semantic_relevance": 1.00,
    "recency": 0.35,
    "importance": 0.60,
    "emotional_match": 0.25,
    "relationship_relevance": 0.25,
    "goal_relevance": 0.40,
    "confidence": 0.50,
    "access_penalty": 0.20,
}
#: 半減期（秒）。2週間で関連度が半分になる。約束や明示的な好みは
#: `importance` が高いので、これだけでは沈まない。
_HALF_LIFE = 14 * 86400.0


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    memory: EpisodicMemory
    score: float = .0
    components: dict[str, float] = field(default_factory=dict)
    relevance: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.memory.snapshot(),
            "score": round(float(self.score), 3),
            "relevance": self.relevance,
        }


def _recency(occurred_at: float, *, now: float | None = None) -> float:
    age = max(0.0, (now if now is not None else time.time()) - float(occurred_at or 0.0))
    return 0.5 ** (age / _HALF_LIFE)


def _relevance_label(memory: EpisodicMemory, query: RecallQuery) -> str:
    """何のために出てきた記憶か。Planner はこれを見て使い方を決める。"""
    event = str(memory.event_type)
    if event.startswith("self_failure:"):
        return "repetition_avoidance"
    if event == "promise":
        return "unfinished_obligation"
    if event in {"preference", "correction"}:
        return "response_style"
    if query.topic and query.topic in memory.summary:
        return "topic_continuity"
    return "background"


def rank(
    memories: list[EpisodicMemory] | tuple[EpisodicMemory, ...],
    query: RecallQuery, *, limit: int = 3, now: float | None = None,
    min_score: float = .12,
) -> list[ScoredMemory]:
    """**少数だけ返す。** 全部返すのは検索していないのと同じ。"""
    haystack = f"{query.text} {query.topic} {' '.join(query.keys)}".strip()
    scored: list[ScoredMemory] = []
    for memory in memories:
        if str(memory.status) not in {str(item) for item in RETRIEVABLE}:
            continue
        semantic = similarity(haystack, memory.summary)
        for key in query.keys:
            if key and key in memory.summary:
                semantic = max(semantic, .55)
        # 過去の失敗は、パターン名の一致で引く（文字列の似方では引けない）。
        for pattern in query.failure_patterns:
            if pattern and pattern in str(memory.event_type):
                semantic = max(semantic, .8)
        if query.obligations and str(memory.event_type) == "promise":
            semantic = max(semantic, .5)
        components = {
            "semantic_relevance": semantic,
            "recency": _recency(memory.occurred_at, now=now),
            "importance": float(memory.importance),
            "emotional_match": float(memory.emotional_salience),
            "relationship_relevance": float(memory.relationship_salience),
            "goal_relevance": float(memory.goal_relevance),
            "confidence": float(memory.confidence),
            # 何度も引かれているものばかり出すと、会話が過去に固定される。
            "access_penalty": -min(1.0, float(memory.access_count) / 20.0),
        }
        total = sum(RANKING_WEIGHTS.get(k, 1.0) * v for k, v in components.items())
        # 手がかりが1つも当たらない記憶は、重要度が高くても出さない。
        if semantic <= 0.0 and not query.failure_patterns:
            continue
        normalized = total / max(1e-6, sum(RANKING_WEIGHTS.values()))
        if normalized < min_score:
            continue
        scored.append(ScoredMemory(
            memory=memory, score=round(normalized, 4), components=components,
            relevance=_relevance_label(memory, query),
        ))
    scored.sort(key=lambda item: (-item.score, -item.memory.importance))
    return scored[:max(1, int(limit))]


def planner_payload(scored: list[ScoredMemory] | tuple[ScoredMemory, ...]) -> list[dict[str, Any]]:
    """Conversation Planner へ渡す `relevant_memories`。

    **短く、出典付きで、数件だけ。** 生の会話履歴は渡さない。
    """
    return [item.memory.planner_view(item.relevance) for item in scored]


# ---------------------------------------------------------------------------
# 行動選択への影響
#
# **プロンプトへ足すだけでは足りない。** 点数として効かせて、
# どの記憶がどのスコアを動かしたかをトレースへ残す。
# ---------------------------------------------------------------------------

#: 記憶1件が動かしてよい上限。**上書きではなく補正**。
#: 安全警告や明示的な質問を押しのけない大きさに留める。
MAX_ADJUSTMENT = .30


@dataclass(frozen=True, slots=True)
class MemoryInfluence:
    """どの行動を、どれだけ、どの記憶の根拠で動かすか。"""

    adjustments: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def for_action(self, action: ActionType | str) -> float:
        return float(self.adjustments.get(str(action), 0.0))

    def sources(self, action: ActionType | str) -> tuple[int, ...]:
        return tuple(self.evidence.get(str(action), ()))

    @property
    def empty(self) -> bool:
        return not self.adjustments

    def snapshot(self) -> dict[str, Any]:
        return {
            "adjustments": {k: round(v, 3) for k, v in self.adjustments.items()},
            "evidence": {k: list(v) for k, v in self.evidence.items()},
        }


#: 自分の失敗パターン → その失敗に繋がった行動。
#: 同じ状況で同じ行動へ、小さなペナルティを掛ける。
FAILURE_TO_ACTION: dict[str, tuple[str, float]] = {
    "kept_talking_after_end_signal": (str(ActionType.CONTINUE_PREVIOUS_TOPIC), -.12),
    "asked_a_question_already_answered": (str(ActionType.ASK_CLARIFICATION), -.15),
    "repeated_the_same_explanation": (str(ActionType.CONTINUE_PREVIOUS_TOPIC), -.12),
    "joked_at_a_bad_moment": (str(ActionType.MAKE_LIGHT_JOKE), -.18),
    "answered_on_low_confidence": (str(ActionType.ANSWER), -.10),
}


def influence_from(
    scored: list[ScoredMemory] | tuple[ScoredMemory, ...],
    reflections: list[Any] | tuple[Any, ...] = (),
) -> MemoryInfluence:
    """取得した記憶と Reflection から、行動ごとの補正を作る。

    **1件で大きく動かさない。** 何度も観測された事柄だけが積み上がって
    意味のある大きさになる（`MAX_ADJUSTMENT` で頭打ち）。
    """
    adjustments: dict[str, float] = {}
    evidence: dict[str, list[int]] = {}

    def add(action: str, delta: float, memory_id: int) -> None:
        current = adjustments.get(action, 0.0) + delta
        adjustments[action] = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, current))
        evidence.setdefault(action, [])
        if memory_id and memory_id not in evidence[action]:
            evidence[action].append(memory_id)

    for item in scored:
        memory = item.memory
        event = str(memory.event_type)
        weight = max(.3, float(memory.confidence))
        if event.startswith("self_failure:"):
            pattern = event.split(":", 1)[1]
            mapped = FAILURE_TO_ACTION.get(pattern)
            if mapped:
                action, delta = mapped
                add(action, delta * weight * max(1, memory.support_count) ** .5,
                    memory.memory_id)
        elif event == "promise":
            # 未完了の約束は、話題を続ける側と、答える側の両方へ効く。
            add(str(ActionType.CONTINUE_PREVIOUS_TOPIC), .12 * weight, memory.memory_id)
        elif event in {"preference", "correction"}:
            # 明示された好みは Planner 側で言い方に効く。行動としては
            # 「聞き返しを減らす」だけに留める——好みが質問の可否を決めない。
            if "質問" in memory.summary or "一問一答" in memory.summary:
                add(str(ActionType.ASK_CLARIFICATION), -.10 * weight, memory.memory_id)

    for reflection in reflections:
        for action, delta in (getattr(reflection, "action_deltas", None) or {}).items():
            add(str(action), float(delta) * float(getattr(reflection, "confidence", .5)),
                int(getattr(reflection, "source_memory_ids", (0,))[0] or 0)
                if getattr(reflection, "source_memory_ids", ()) else 0)

    return MemoryInfluence(
        adjustments={k: round(v, 4) for k, v in adjustments.items() if abs(v) >= .005},
        evidence={k: tuple(v) for k, v in evidence.items() if v},
    )
