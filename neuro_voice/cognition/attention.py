"""いま何に注意を向けているか。**常時LLMを呼ぶことではない。**

「常に考えている」を素直に実装すると、毎フレームLLMへ問い合わせる形になる。
それはローカルGPUを焼くだけで、判断の質は上がらない（第16条）。

ここがやるのは3つだけ。

1. 音声・映像・ゲーム・記憶・時間経過を**1つのイベントの形へ揃える**
2. **短い構造化状態**として注意を持ち回る（内部思考文もChain of Thoughtも保存しない）
3. 同時に起きた出来事から**いま向くべき対象を1つ選ぶ**

3番目にヒステリシスを入れてあるのは、これが無いと一瞬の変化で注目対象が
毎フレーム入れ替わり、話しかけても途中で別の話を始める挙動になるため。

**発話するかどうかはここでは決めない。** それは Action Selector の仕事で、
ここは「何に向くか」までを担当する。境界をずらすと、注意の層が事実上の
発話判断を持ってしまう。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# イベント
# ---------------------------------------------------------------------------


class AttentionEventType(StrEnum):
    """注意を引きうる出来事。**既存のイベント型を置き換えない。**

    `dialogue/events.py` の `ConversationEvent` も `autonomy/types.py` の
    `AutonomyEvent` もそのまま。ここはそれらを**同じ物差しで比べるため**の
    共通の形で、正本ではない。
    """

    USER_SPEECH_STARTED = "user_speech_started"
    USER_SPEECH_ENDED = "user_speech_ended"
    DIRECT_QUESTION = "direct_question"
    GROUP_SPEECH = "group_speech"
    VISUAL_CHANGE = "visual_change"
    GAME_EVENT = "game_event"
    DANGER_EVENT = "danger_event"
    TASK_PROGRESS = "task_progress"
    OBLIGATION_DUE = "obligation_due"
    MEMORY_RELEVANCE = "memory_relevance"
    SILENCE_THRESHOLD = "silence_threshold"
    ASSISTANT_SPEECH_STARTED = "assistant_speech_started"
    ASSISTANT_SPEECH_INTERRUPTED = "assistant_speech_interrupted"
    TOPIC_CLOSED = "topic_closed"
    #: 誰かが入ってきた／出ていった。**これ自体は発話の理由にならない。**
    #: 入退室のたびに必ず挨拶する実装にしないため、注意層まででいったん
    #: 止め、話すかどうかは Action Selector と Speech Gate に決めさせる。
    PARTICIPANT_CHANGE = "participant_change"


class FocusKind(StrEnum):
    """いま向いている先。"""

    USER_SPEECH = "user_speech"
    GROUP_CONVERSATION = "group_conversation"
    GAMEPLAY = "gameplay"
    DANGER = "danger"
    SHARED_TASK = "shared_task"
    PAUSED_CONVERSATION = "paused_conversation"
    IDLE = "idle"


#: イベント → 注目対象。1対1で決まるものだけ。
_EVENT_FOCUS: dict[str, FocusKind] = {
    AttentionEventType.DANGER_EVENT: FocusKind.DANGER,
    AttentionEventType.DIRECT_QUESTION: FocusKind.USER_SPEECH,
    AttentionEventType.USER_SPEECH_STARTED: FocusKind.USER_SPEECH,
    AttentionEventType.USER_SPEECH_ENDED: FocusKind.USER_SPEECH,
    AttentionEventType.GROUP_SPEECH: FocusKind.GROUP_CONVERSATION,
    AttentionEventType.GAME_EVENT: FocusKind.GAMEPLAY,
    AttentionEventType.VISUAL_CHANGE: FocusKind.GAMEPLAY,
    AttentionEventType.OBLIGATION_DUE: FocusKind.SHARED_TASK,
    AttentionEventType.TASK_PROGRESS: FocusKind.SHARED_TASK,
    AttentionEventType.MEMORY_RELEVANCE: FocusKind.PAUSED_CONVERSATION,
    AttentionEventType.SILENCE_THRESHOLD: FocusKind.PAUSED_CONVERSATION,
    AttentionEventType.TOPIC_CLOSED: FocusKind.IDLE,
}

#: **優先順位の段**。小さいほど先。
#:
#: 点数ではなく段にしているのは、危険警告が「たまたま点が高かった」で
#: 選ばれる形にしたくないため。どれだけ面白いゲームイベントが積み上がっても、
#: 危険を押しのけられない。
TIERS: dict[str, int] = {
    AttentionEventType.DANGER_EVENT: 0,
    AttentionEventType.DIRECT_QUESTION: 1,
    AttentionEventType.USER_SPEECH_STARTED: 2,
    AttentionEventType.USER_SPEECH_ENDED: 2,
    AttentionEventType.GROUP_SPEECH: 2,
    AttentionEventType.ASSISTANT_SPEECH_INTERRUPTED: 2,
    AttentionEventType.OBLIGATION_DUE: 3,
    AttentionEventType.GAME_EVENT: 4,
    AttentionEventType.TASK_PROGRESS: 4,
    AttentionEventType.MEMORY_RELEVANCE: 5,
    AttentionEventType.SILENCE_THRESHOLD: 6,
    AttentionEventType.VISUAL_CHANGE: 7,
    AttentionEventType.ASSISTANT_SPEECH_STARTED: 8,
    AttentionEventType.TOPIC_CLOSED: 8,
}
_DEFAULT_TIER = 9


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


@dataclass(frozen=True, slots=True)
class AttentionEvent:
    """注意の候補になる出来事1つ。**本文は要約だけ**（第12条）。

    `deduplication_key` を必須の考え方として持っているのは、
    **全フレーム・全ASR断片をイベントにしないため**。同じ意味の変化が
    毎秒10回届くなら、それは1つの出来事として扱う。
    """

    event_type: AttentionEventType | str
    source: str = "local"
    summary: str = ""
    participant_ids: tuple[str, ...] = ()
    topic_ids: tuple[str, ...] = ()
    salience: float = .5
    novelty: float = .5
    urgency: float = .0
    confidence: float = 1.0
    expected_duration: float = .0
    occurred_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    deduplication_key: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def tier(self) -> int:
        return TIERS.get(str(self.event_type), _DEFAULT_TIER)

    @property
    def focus(self) -> FocusKind:
        return _EVENT_FOCUS.get(str(self.event_type), FocusKind.IDLE)

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) > self.expires_at

    def dedup(self) -> str:
        """同じ出来事とみなす鍵。指定が無ければ種別と話題で作る。"""
        if self.deduplication_key:
            return self.deduplication_key
        return f"{self.event_type}:{'|'.join(self.topic_ids[:2])}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": str(self.event_type),
            "source": self.source,
            "tier": self.tier,
            "salience": round(self.salience, 3),
            "novelty": round(self.novelty, 3),
            "urgency": round(self.urgency, 3),
            "confidence": round(self.confidence, 3),
            "topics": list(self.topic_ids[:3]),
            "chars": len(self.summary),
        }


# ---------------------------------------------------------------------------
# 間引き
# ---------------------------------------------------------------------------

#: これ未満の重要度のイベントは、そもそも注意の候補にしない。
MIN_SALIENCE = .15
#: 同じ鍵のイベントを再び受け付けるまでの秒数。
#: **同じ意味の変化が毎秒10回届いても、1つの出来事として扱う。**
DEDUP_WINDOW_SECONDS = 4.0
#: 保持する候補の上限。古いものから捨てる。
MAX_PENDING = 12


class EventIntake:
    """イベントの入口。**ここで落とすものを明示しておく。**

    落とす側を書いておかないと、「なんとなく重要そう」で全部通ってしまい、
    Focus Manager が毎フレーム別のものを見る状態になる。
    """

    def __init__(
        self, *, min_salience: float = MIN_SALIENCE,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
    ) -> None:
        self.min_salience = float(min_salience)
        self.dedup_window = float(dedup_window)
        self._seen: dict[str, float] = {}
        #: 落とした理由の内訳。診断で「なぜ何も起きないのか」を読むため。
        self.dropped: dict[str, int] = {}

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def accept(self, event: AttentionEvent, *, now: float | None = None) -> bool:
        """このイベントを注意の候補にしてよいか。"""
        moment = time.monotonic() if now is None else now
        if event.expired(now=moment):
            self._drop("expired")
            return False
        # 危険と直接質問は重要度で落とさない。**取りこぼす方が高くつく。**
        if event.tier > 1 and event.salience < self.min_salience:
            self._drop("below_salience")
            return False
        if event.confidence < .3:
            self._drop("low_confidence")
            return False
        key = event.dedup()
        last = self._seen.get(key)
        if last is not None and moment - last < self.dedup_window and event.tier > 1:
            self._drop("duplicate")
            return False
        self._seen[key] = moment
        if len(self._seen) > 128:
            for old in sorted(self._seen, key=self._seen.get)[:64]:
                self._seen.pop(old, None)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"dropped": dict(self.dropped), "keys": len(self._seen)}


# ---------------------------------------------------------------------------
# 注意状態
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContinuousAttentionState:
    """いま向いている先と、その周辺。**短い構造化状態だけ。**

    内部思考文も Chain of Thought も保存しない。保存すると、次にそれを
    プロンプトへ入れたくなり、結局「考えた形跡」を毎ターン積むことになる。
    """

    version: int = 0
    updated_at: float = field(default_factory=time.monotonic)
    active_focus: FocusKind | str = FocusKind.IDLE
    secondary_focuses: tuple[FocusKind | str, ...] = ()
    participant_focus: str = ""
    current_topic_ids: tuple[str, ...] = ()
    current_goal_ids: tuple[str, ...] = ()
    recent_event_ids: tuple[str, ...] = ()
    world_state_summary: dict[str, Any] = field(default_factory=dict)
    silence_duration: float = .0
    user_activity_state: str = "idle"
    assistant_activity_state: str = "idle"
    #: まだ扱われていない、期限内の候補。**発話機会そのものではない。**
    pending_opportunities: tuple[AttentionEvent, ...] = ()
    #: 直近に自分から動いた履歴。連発を抑えるための材料。
    recent_initiative_history: tuple[tuple[str, float], ...] = ()
    #: いまの注目対象になってからの経過を測る基準。
    focus_since: float = field(default_factory=time.monotonic)

    @property
    def focus_age(self) -> float:
        return max(0.0, time.monotonic() - self.focus_since)

    def initiatives_within(self, seconds: float, *, now: float | None = None) -> int:
        """直近 `seconds` 秒に自分から動いた回数。

        **未来の記録は数えない。** 単に `now - at <= seconds` と書くと、
        時計が巻き戻った時や記録の順序が乱れた時に、未来の項目が
        すべて「直近」に見えて連発の抑制が効かなくなる。
        """
        moment = time.monotonic() if now is None else now
        return sum(1 for _kind, at in self.recent_initiative_history
                   if 0.0 <= moment - at <= float(seconds))

    def summary(self) -> dict[str, Any]:
        """トレース用。**本文は入れない**（第12条）。"""
        return {
            "version": self.version,
            "focus": str(self.active_focus),
            "focus_age": round(self.focus_age, 2),
            "secondary": [str(x) for x in self.secondary_focuses[:3]],
            "participant": self.participant_focus,
            "topics": list(self.current_topic_ids[:3]),
            "silence": round(self.silence_duration, 1),
            "user": self.user_activity_state,
            "assistant": self.assistant_activity_state,
            "pending": len(self.pending_opportunities),
            "initiatives_60s": self.initiatives_within(60.0),
        }


#: ユーザー／自分の活動状態を、イベントから読む。
_USER_ACTIVITY = {
    AttentionEventType.USER_SPEECH_STARTED: "speaking",
    AttentionEventType.USER_SPEECH_ENDED: "idle",
    AttentionEventType.DIRECT_QUESTION: "awaiting_answer",
    AttentionEventType.GROUP_SPEECH: "speaking",
}
_ASSISTANT_ACTIVITY = {
    AttentionEventType.ASSISTANT_SPEECH_STARTED: "speaking",
    AttentionEventType.ASSISTANT_SPEECH_INTERRUPTED: "interrupted",
    AttentionEventType.TOPIC_CLOSED: "idle",
}


def apply_event(
    state: ContinuousAttentionState, event: AttentionEvent, *,
    now: float | None = None,
) -> ContinuousAttentionState:
    """1つのイベントを注意状態へ取り込む。**注目対象はここでは変えない。**

    向き先を決めるのは `FocusManager`。取り込みと選択を同じ関数に混ぜると、
    「イベントが来た順で注目が動く」——つまりヒステリシスが効かなくなる。
    """
    moment = time.monotonic() if now is None else now
    kind = str(event.event_type)
    pending = [item for item in state.pending_opportunities if not item.expired(now=moment)]
    pending.append(event)
    del pending[:-MAX_PENDING]

    topics = tuple(dict.fromkeys((*event.topic_ids, *state.current_topic_ids)))[:6]
    silence = .0 if kind != AttentionEventType.SILENCE_THRESHOLD else float(
        event.expected_duration or state.silence_duration)
    if kind in {AttentionEventType.USER_SPEECH_STARTED,
                AttentionEventType.USER_SPEECH_ENDED,
                AttentionEventType.DIRECT_QUESTION,
                AttentionEventType.GROUP_SPEECH}:
        silence = .0

    state.version += 1
    state.updated_at = moment
    state.pending_opportunities = tuple(pending)
    state.recent_event_ids = tuple((*state.recent_event_ids, event.event_id))[-8:]
    state.current_topic_ids = topics
    state.silence_duration = silence
    state.user_activity_state = _USER_ACTIVITY.get(kind, state.user_activity_state)
    state.assistant_activity_state = _ASSISTANT_ACTIVITY.get(
        kind, state.assistant_activity_state)
    if event.participant_ids:
        state.participant_focus = event.participant_ids[0]
    return state


def note_initiative(
    state: ContinuousAttentionState, kind: str, *, now: float | None = None,
) -> ContinuousAttentionState:
    """自分から動いたことを記録する。**連発を抑える材料。**"""
    moment = time.monotonic() if now is None else now
    state.recent_initiative_history = tuple(
        (*state.recent_initiative_history, (str(kind), moment)))[-12:]
    return state


# ---------------------------------------------------------------------------
# 注目対象の選択
# ---------------------------------------------------------------------------

#: 点数の重み。**ここ以外に書かない**（`kernel.WEIGHTS` と同じ方針）。
FOCUS_WEIGHTS: dict[str, float] = {
    "urgency": 1.60,
    "salience": 1.00,
    "novelty": 0.45,
    "goal_relevance": 0.80,
    "participant_relevance": 0.55,
    "relationship_relevance": 0.35,
    "temporal_relevance": 0.50,
    "confidence": 0.60,
    "staleness": 0.70,
    "interruption_cost": 0.90,
}
#: 乗り換えに必要な差。これ未満なら現状維持。**ヒステリシスはこれ1つ。**
#:
#: 最初は「現状維持へ加点」と「乗り換えに差を要求」の2つを入れていたが、
#: テストを書いたら**加点だけで全部決まっていて、差の判定が一度も通らなかった**
#: （加点 .12 に対し、点数は重みの合計で割るので現実的な差は .0〜.2）。
#: 同じ目的の仕組みが2つあると、片方が効いていないことに気づけない。
#:
#: 値は点数の実際の広がりから決めてある。urgency の満点差で約 .21、
#: salience で約 .13。**.05 は「はっきり上」だが「絶対に届かない」ではない**大きさ。
SWITCH_MARGIN = .05
#: 乗り換えを許さない最短時間（秒）。**一瞬の変化で毎フレーム切り替わらない。**
MIN_DWELL_SECONDS = 1.5
#: 古さで関連度が半分になるまでの秒数。
_STALENESS_HALF_LIFE = 20.0


@dataclass(frozen=True, slots=True)
class FocusDecision:
    focus: FocusKind | str
    event: AttentionEvent | None = None
    score: float = .0
    components: dict[str, float] = field(default_factory=dict)
    switched: bool = False
    reason: str = ""
    runners_up: tuple[tuple[str, float], ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            "focus": str(self.focus),
            "event": self.event.snapshot() if self.event is not None else None,
            "score": round(float(self.score), 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "switched": self.switched,
            "reason": self.reason,
            "runners_up": [[k, round(v, 3)] for k, v in self.runners_up[:3]],
        }


class FocusManager:
    """同時に起きた出来事から、**いま向くべき対象を1つ選ぶ。**

    段（`TIERS`）が先、点数が後。危険はどれだけ面白いゲームイベントが
    積み上がっても押しのけられない。

    同じ段の中でだけヒステリシスが効く——**乗り換えには差（`switch_margin`）を
    要求し、乗り換えた直後（`min_dwell`）は動かさない。** その2つだけ。

    以前は「現状維持へ加点」も足していたが、テストを書いたら
    **加点だけで全部決まっていて、差の判定が一度も通っていなかった。**
    同じ目的の仕組みを2つ置くと、片方が死んでいることに気づけない。
    """

    def __init__(
        self, *, weights: dict[str, float] | None = None,
        switch_margin: float = SWITCH_MARGIN,
        min_dwell: float = MIN_DWELL_SECONDS,
    ) -> None:
        self.weights = {**FOCUS_WEIGHTS, **(weights or {})}
        self.switch_margin = float(switch_margin)
        self.min_dwell = float(min_dwell)

    # ------------------------------------------------------------------

    def _components(
        self, event: AttentionEvent, state: ContinuousAttentionState, *,
        now: float, goals: tuple[str, ...] = (), relationship: float = .5,
    ) -> dict[str, float]:
        age = max(0.0, now - event.occurred_at)
        # 古い出来事ほど、いま向く理由が薄い。
        staleness = 1.0 - .5 ** (age / _STALENESS_HALF_LIFE)
        topic_hit = bool(set(event.topic_ids) & set(state.current_topic_ids))
        goal_hit = bool(set(event.topic_ids) & set(goals or state.current_goal_ids))
        participant_hit = bool(
            state.participant_focus and state.participant_focus in event.participant_ids)
        # 自分が話している最中に別のものへ向くのは、言い切ってからにする。
        interrupting = state.assistant_activity_state == "speaking" and event.tier > 1
        return {
            "urgency": _clamp(event.urgency),
            "salience": _clamp(event.salience),
            "novelty": _clamp(event.novelty),
            "goal_relevance": .8 if goal_hit else .0,
            "participant_relevance": .7 if participant_hit else .0,
            "relationship_relevance": _clamp(relationship) - .5,
            "temporal_relevance": .6 if topic_hit else .0,
            "confidence": _clamp(event.confidence),
            "staleness": -staleness,
            "interruption_cost": -(.6 if interrupting else .0),
        }

    def score(
        self, event: AttentionEvent, state: ContinuousAttentionState, *,
        now: float | None = None, goals: tuple[str, ...] = (),
        relationship: float = .5,
    ) -> tuple[float, dict[str, float]]:
        moment = time.monotonic() if now is None else now
        components = self._components(
            event, state, now=moment, goals=goals, relationship=relationship)
        total = sum(self.weights.get(key, 1.0) * value for key, value in components.items())
        # 重みの合計で割って 0〜1 付近へ寄せる。閾値を人が読める大きさにするため。
        return round(total / max(1e-6, sum(self.weights.values())), 4), components

    # ------------------------------------------------------------------

    def select(
        self, state: ContinuousAttentionState, *, now: float | None = None,
        goals: tuple[str, ...] = (), relationship: float = .5,
    ) -> FocusDecision:
        """`pending_opportunities` から向き先を1つ決める。

        **状態は書き換えない。** 反映するかは呼び出し側が決める
        （`commit` を使う）。決定と適用を分けておくと、決めた理由だけを
        ログに残して適用しない、という使い方ができる。
        """
        moment = time.monotonic() if now is None else now
        live = [item for item in state.pending_opportunities if not item.expired(now=moment)]
        if not live:
            return FocusDecision(
                focus=state.active_focus, reason="no_live_event",
                switched=False)

        scored: list[tuple[int, float, dict[str, float], AttentionEvent]] = []
        for event in live:
            value, components = self.score(
                event, state, now=moment, goals=goals, relationship=relationship)
            scored.append((event.tier, value, components, event))
        scored.sort(key=lambda item: (item[0], -item[1]))
        tier, best_score, components, best = scored[0]
        runners = tuple((str(item[3].focus), item[1]) for item in scored[1:4])

        if best.focus == state.active_focus:
            return FocusDecision(
                focus=best.focus, event=best, score=best_score, components=components,
                switched=False, reason="stayed", runners_up=runners)

        # --- 乗り換えるかどうか ---
        # 段が上がる（より緊急な種類が来た）なら、無条件で乗り換える。
        incumbent_tier = min(
            (item[0] for item in scored if item[3].focus == state.active_focus),
            default=_DEFAULT_TIER)
        if tier < incumbent_tier:
            return FocusDecision(
                focus=best.focus, event=best, score=best_score, components=components,
                switched=True, reason=f"higher_tier({tier}<{incumbent_tier})",
                runners_up=runners)

        dwell = max(0.0, moment - state.focus_since)
        if dwell < self.min_dwell:
            return FocusDecision(
                focus=state.active_focus, event=best, score=best_score,
                components=components, switched=False,
                reason=f"min_dwell({dwell:.1f}s<{self.min_dwell}s)", runners_up=runners)

        incumbent_best = max(
            (item[1] for item in scored if item[3].focus == state.active_focus),
            default=.0)
        if best_score - incumbent_best < self.switch_margin:
            return FocusDecision(
                focus=state.active_focus, event=best, score=best_score,
                components=components, switched=False,
                reason=f"within_margin({best_score - incumbent_best:.3f})",
                runners_up=runners)
        return FocusDecision(
            focus=best.focus, event=best, score=best_score, components=components,
            switched=True, reason="outscored_incumbent", runners_up=runners)

    @staticmethod
    def commit(
        state: ContinuousAttentionState, decision: FocusDecision, *,
        now: float | None = None,
    ) -> ContinuousAttentionState:
        """決定を注意状態へ反映する。**切り替わった時だけ滞在時間を戻す。**"""
        moment = time.monotonic() if now is None else now
        if decision.switched:
            state.secondary_focuses = tuple(dict.fromkeys(
                (state.active_focus, *state.secondary_focuses)))[:3]
            state.active_focus = decision.focus
            state.focus_since = moment
        state.version += 1
        state.updated_at = moment
        return state


__all__ = [
    "MAX_PENDING", "MIN_DWELL_SECONDS", "MIN_SALIENCE", "TIERS",
    "AttentionEvent", "AttentionEventType", "ContinuousAttentionState",
    "EventIntake", "FocusDecision", "FocusKind", "FocusManager",
    "apply_event", "note_initiative",
]
