"""持続する内面。**毎ターン初期化された人格に戻らないための層。**

これまでも感情（`TemporalSelf.AffectState`）と関係性（`RelationshipState` 17軸）は
持っていた。監査して分かったのは、**それが行動選択へほとんど届いていない**こと。

* `CognitiveState.irritation` は `affect_state["irritation"]` を読んでいたが、
  `AffectState` にその名前の軸は無い（あるのは `frustration`）。しかも
  `TemporalSelf.snapshot()` は感情を `"affect"` の下に入れて返す。
  **つまり irritation は常に 0.0 だった**——冗談を抑える罰は一度も効いていない
* 関係性17軸のうち、行動選択が読めるのは `trust` と `comfort` の2つだけ。
  残り15軸は計算され、減衰し、保存され、**プロンプト文字列にしかならない**

だからここは新しい感情モデルを作る場所ではない。**既にある値を行動へ繋ぎ、
変化の仕方に規律を与える**場所。

規律は3つ。

1. **三層を混ぜない。** 短期感情（数分）／セッション状態／長期関係は別物で、
   短期から長期を直接書き換えない
2. **変化は必ず差分（`StateDelta`）として通す。** LLMに状態値を書かせない
3. **上書きではなく小さな補正。** 感情が安全警告・終了制御・Speech Gate を
   越えられないことを、構造で保証する
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.trace import STATE_LIMITS, StateLimit
from neuro_voice.cognition.types import ActionType, InformationType

# ---------------------------------------------------------------------------
# 三層
# ---------------------------------------------------------------------------


class DurationClass(StrEnum):
    """その変化がどれくらい持つか。**永続化するかどうかもこれで決まる。**"""

    #: 数秒〜数分。RAMのみ。再起動で消えてよい。
    MOMENTARY = "momentary"
    #: 会話中は続く。軽いチェックポイントまで。
    SESSION = "session"
    #: 複数の経験と Reflection でしか動かない。DBへ永続化。
    LONG_TERM = "long_term"


class DeltaTarget(StrEnum):
    GLOBAL_AFFECT = "global_affect"
    SELF_STATE = "self_state"
    PARTICIPANT_RELATIONSHIP = "participant_relationship"
    TOPIC_PREFERENCE = "topic_preference"
    CONVERSATION_STRATEGY = "conversation_strategy"


#: **短期感情から長期関係値を直接動かさない。**
#:
#: 一瞬の苛立ちが信頼を削るようだと、機嫌で人を評価することになる。
#: 長期側を動かせるのは、エピソードか Reflection を根拠に持つ差分だけ。
_LONG_TERM_TARGETS = frozenset({
    DeltaTarget.PARTICIPANT_RELATIONSHIP, DeltaTarget.TOPIC_PREFERENCE,
    DeltaTarget.CONVERSATION_STRATEGY,
})
#: 長期側を動かしてよい出どころ。推測だけでは動かせない。
_LONG_TERM_SOURCES = frozenset({
    str(InformationType.OBSERVATION), str(InformationType.USER_STATEMENT),
    str(InformationType.REFLECTION),
})


class EventAppraisal(StrEnum):
    """出来事の意味。**特定の文字列ではなく、行動と結果と文脈から決める。**"""

    USER_PREFERENCE_SIGNAL = "user_preference_signal"
    RELATIONSHIP_SIGNAL = "relationship_signal"
    SUCCESS = "success"
    FAILURE = "failure"
    CORRECTION = "correction"
    BOUNDARY_SIGNAL = "boundary_signal"
    PLAYFUL_INTERACTION = "playful_interaction"
    HOSTILITY = "hostility"
    GAME_EVENT = "game_event"
    NEUTRAL_EVENT = "neutral_event"


# ---------------------------------------------------------------------------
# 短期感情
# ---------------------------------------------------------------------------

#: 短期感情の軸と基準値。**基準値へ戻るのが既定の振る舞い。**
#:
#: 既存の `AffectState`（joy/warmth/calm/…）とは目的が違う。あちらは声の
#: 表情づけ、ここは行動選択の入力。名前を揃えず**別の軸として持つ**のは、
#: 片方を調整したらもう片方が壊れる状態を避けるため。対応づけは
#: `mind/internal.py` で1箇所だけ行う。
AFFECT_BASELINE: dict[str, float] = {
    "valence": .55,
    "arousal": .35,
    "tension": .05,
    "curiosity": .55,
    "interest": .50,
    "amusement": .30,
    "irritation": .05,
    "caution": .10,
    "comfort": .55,
    "confidence": .60,
}
#: 短期感情の半減期（秒）。**放っておけば戻る。**
#: 苛立ちは早く冷め、警戒はもう少し残る——これは意図的な非対称。
AFFECT_HALF_LIFE: dict[str, float] = {
    "irritation": 240.0,
    "tension": 300.0,
    "arousal": 180.0,
    "amusement": 300.0,
    "caution": 900.0,
    "confidence": 1800.0,
}
_DEFAULT_HALF_LIFE = 600.0

#: 1イベントで動かしてよい量。既存の `STATE_LIMITS` に無い軸だけ足す。
#: **既存の上限を書き換えない**——信頼の非対称（失うのは速く戻すのは遅い）は
#: そのまま残す。
_AFFECT_LIMITS: dict[str, StateLimit] = {
    name: StateLimit(baseline=value, max_positive=.06, max_negative=.06, recovery=.0)
    for name, value in AFFECT_BASELINE.items()
}
_AFFECT_LIMITS["irritation"] = StateLimit(baseline=.05, max_positive=.04, max_negative=.06, recovery=.0)
_AFFECT_LIMITS["caution"] = StateLimit(baseline=.10, max_positive=.05, max_negative=.05, recovery=.0)
_AFFECT_LIMITS["confidence"] = StateLimit(baseline=.60, max_positive=.04, max_negative=.06, recovery=.0)

#: 状態の帯。**一度の出来事で帯を2つ跨がせない**（慣性）。
#: neutral → slight → high の順にしか上がらない。
BANDS: tuple[tuple[float, str], ...] = ((.25, "neutral"), (.55, "slight"), (1.01, "high"))


def band(value: float) -> str:
    for edge, name in BANDS:
        if float(value) < edge:
            return name
    return BANDS[-1][1]


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


# ---------------------------------------------------------------------------
# 差分
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StateDelta:
    """状態の変化1つ。**必ず出どころと理由を持つ。**

    `proposed_delta` と `applied_delta` を分けてあるのは、**どれだけ削られたか**
    を読めるようにするため。合計だけ見ていると、上限に張り付いていることに
    気づけない。
    """

    target: DeltaTarget | str
    dimension: str
    proposed_delta: float
    applied_delta: float = .0
    confidence: float = 1.0
    reason: str = ""
    duration_class: DurationClass | str = DurationClass.MOMENTARY
    decay_policy: str = "half_life"
    source_type: InformationType | str = InformationType.OBSERVATION
    source_event_ids: tuple[str, ...] = ()
    source_memory_ids: tuple[int, ...] = ()
    participant_id: str = ""
    clamp_reason: str = ""
    delta_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def applied(self) -> bool:
        return abs(float(self.applied_delta)) > 1e-6

    def snapshot(self) -> dict[str, Any]:
        return {
            "target": str(self.target),
            "dimension": self.dimension,
            "proposed": round(float(self.proposed_delta), 4),
            "applied": round(float(self.applied_delta), 4),
            "clamp_reason": self.clamp_reason,
            "duration": str(self.duration_class),
            "source_type": str(self.source_type),
            "participant": self.participant_id,
            "reason": self.reason[:60],
            "events": list(self.source_event_ids[:3]),
            "memories": list(self.source_memory_ids[:3]),
        }


class StateDeltaGate:
    """**状態を書き換えてよいかを決める唯一の場所。**

    LLM が意味評価を助けることはあっても、最終的な変化量はここが決める。
    型検証 → confidence → 三層の整合 → 慣性 → 上限 → 重複、の順に落とす。
    """

    #: これ未満の確からしさの提案は捨てる。
    MIN_CONFIDENCE = .55

    def __init__(self, limits: dict[str, StateLimit] | None = None) -> None:
        self._limits = {**STATE_LIMITS, **_AFFECT_LIMITS, **(limits or {})}
        #: 同じイベントを二度適用しないための記録。(event_id, target, dimension)
        self._applied: set[tuple[str, str, str]] = set()

    def _key(self, delta: StateDelta) -> tuple[str, str, str] | None:
        if not delta.source_event_ids:
            return None
        return (delta.source_event_ids[0], str(delta.target),
                f"{delta.participant_id}:{delta.dimension}")

    def evaluate(self, delta: StateDelta, current: float) -> StateDelta:
        """1つの差分を検算して `applied_delta` を埋める。**破壊的に更新する。**"""
        try:
            proposed = float(delta.proposed_delta)
        except (TypeError, ValueError):
            delta.applied_delta, delta.clamp_reason = .0, "not_a_number"
            return delta
        if proposed != proposed or abs(proposed) == float("inf"):
            delta.applied_delta, delta.clamp_reason = .0, "not_finite"
            return delta
        if float(delta.confidence) < self.MIN_CONFIDENCE:
            delta.applied_delta, delta.clamp_reason = .0, "low_confidence"
            return delta

        # **短期の出来事で長期の値を動かさない。**
        if (delta.target in _LONG_TERM_TARGETS
                and str(delta.duration_class) != str(DurationClass.LONG_TERM)):
            delta.applied_delta, delta.clamp_reason = .0, "short_term_cannot_move_long_term"
            return delta
        if (str(delta.duration_class) == str(DurationClass.LONG_TERM)
                and str(delta.source_type) not in _LONG_TERM_SOURCES):
            delta.applied_delta, delta.clamp_reason = .0, "long_term_needs_grounded_source"
            return delta
        if (str(delta.duration_class) == str(DurationClass.LONG_TERM)
                and not (delta.source_memory_ids or delta.source_event_ids)):
            # 根拠のIDを持たない長期変化は残さない（後から何も辿れない）。
            delta.applied_delta, delta.clamp_reason = .0, "long_term_needs_evidence_id"
            return delta

        key = self._key(delta)
        if key is not None and key in self._applied:
            delta.applied_delta, delta.clamp_reason = .0, "duplicate_event"
            return delta

        limit = self._limits.get(delta.dimension) or StateLimit()
        after = limit.apply(_clamp(current), proposed)
        applied = round(after - _clamp(current), 4)
        reason = ""
        if abs(applied) < abs(proposed) - 1e-6:
            reason = "per_event_limit"
        # **慣性**: 一度の出来事で帯を2つ跨がない。
        # neutral からいきなり high へは行けない（信頼→不信、安心→敵対）。
        before_band, after_band = band(current), band(after)
        if before_band != after_band:
            names = [name for _edge, name in BANDS]
            if abs(names.index(after_band) - names.index(before_band)) > 1:
                after = _clamp(BANDS[names.index(before_band)][0] - 1e-4
                               if applied < 0 else BANDS[names.index(before_band)][0])
                applied = round(after - _clamp(current), 4)
                reason = "band_inertia"
        delta.applied_delta = applied
        delta.clamp_reason = reason
        if key is not None and delta.applied:
            self._applied.add(key)
        return delta


# ---------------------------------------------------------------------------
# 統合ビュー
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UnifiedInternalState:
    """いまの内面。**値の持ち主は各モジュールのまま**（第5条）。

    `CognitiveState` が「このターンの入力」の統合ビューなのに対して、
    ここは「持続している自分」の統合ビュー。両方あるのは、前者がターンごとに
    捨てられるのに対して**こちらは持ち越される**ため。
    """

    state_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    version: int = 0
    updated_at: float = field(default_factory=time.time)
    #: 短期感情。RAMのみ。
    affect: dict[str, float] = field(default_factory=lambda: dict(AFFECT_BASELINE))
    #: セッション中の構え。
    social_stance: dict[str, Any] = field(default_factory=dict)
    active_concerns: tuple[str, ...] = ()
    #: 話題ごとの好み。連続値と根拠で持つ（`PreferenceState`）。
    current_preferences: dict[str, "PreferenceState"] = field(default_factory=dict)
    #: 相手ごとの関係。`RelationshipStore` の写し。
    participant_relationships: dict[str, dict[str, float]] = field(default_factory=dict)
    confidence: float = .6
    uncertainty: dict[str, float] = field(default_factory=dict)
    recent_state_deltas: list[StateDelta] = field(default_factory=list)

    # -- 読み取り -------------------------------------------------------

    def value(self, dimension: str, participant_id: str = "") -> float:
        """短期感情を優先し、無ければ相手ごとの関係値を見る。"""
        if dimension in self.affect:
            return _clamp(self.affect[dimension])
        relationship = self.participant_relationships.get(participant_id or "", {})
        return _clamp(relationship.get(dimension, AFFECT_BASELINE.get(dimension, .5)))

    def relationship(self, participant_id: str) -> dict[str, float]:
        return dict(self.participant_relationships.get(participant_id, {}))

    def preference(self, subject: str) -> "PreferenceState | None":
        return self.current_preferences.get(subject)

    def summary(self) -> dict[str, Any]:
        """トレース用。**本文も生の履歴も入れない**（第12条）。"""
        return {
            "state_id": self.state_id,
            "version": self.version,
            "affect": {k: round(v, 3) for k, v in self.affect.items()},
            "bands": {k: band(v) for k, v in self.affect.items() if v != AFFECT_BASELINE.get(k)},
            "stance": dict(self.social_stance),
            "concerns": list(self.active_concerns[:4]),
            "participants": sorted(self.participant_relationships),
            "preferences": {k: v.snapshot() for k, v in self.current_preferences.items()},
            "deltas": [d.snapshot() for d in self.recent_state_deltas[-6:]],
        }


@dataclass(slots=True)
class PreferenceState:
    """好みは**連続値と根拠**で持つ。二値化しない。

    「一度楽しかった」で大好きになり、「一度嫌なことがあった」で嫌いになる
    のは、好みではなく直近の気分。`evidence_count` が増えて初めて
    `intensity` と `confidence` が上がる。
    """

    subject: str
    valence: float = .0            # -1.0 〜 +1.0
    intensity: float = .0          # 0.0 〜 1.0
    confidence: float = .2
    evidence_count: int = 0
    contradiction_count: int = 0
    source_memory_ids: tuple[int, ...] = ()
    last_updated_at: float = field(default_factory=time.time)

    def observe(self, valence: float, *, memory_id: int = 0, weight: float = 1.0) -> None:
        """1つの証拠を足す。**1件では確信も強さも上がりきらない。**"""
        signed = max(-1.0, min(1.0, float(valence)))
        if signed and self.valence and (signed > 0) != (self.valence > 0):
            self.contradiction_count += 1
        else:
            self.evidence_count += 1
        # 移動平均。直近1件で符号がひっくり返らない重みにしてある。
        step = .25 * float(weight)
        self.valence = max(-1.0, min(1.0, self.valence + step * (signed - self.valence)))
        total = self.evidence_count + self.contradiction_count
        self.intensity = min(1.0, abs(self.valence) * min(1.0, total / 4.0))
        agreement = self.evidence_count / max(1, total)
        self.confidence = round(min(.9, .2 + .15 * min(total, 4) * agreement), 3)
        if memory_id:
            self.source_memory_ids = tuple(
                dict.fromkeys((*self.source_memory_ids, int(memory_id))))[-8:]
        self.last_updated_at = time.time()

    @property
    def label(self) -> str:
        """人へ見せる時の言い方。**「好き／嫌い」で断定しない。**"""
        if self.confidence < .35 or self.intensity < .2:
            return "unclear"
        if self.valence > .3:
            return "leans_positive"
        if self.valence < -.3:
            return "leans_negative"
        return "mixed"

    def snapshot(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "label": self.label,
            "valence": round(self.valence, 3), "intensity": round(self.intensity, 3),
            "confidence": self.confidence,
            "evidence": self.evidence_count, "contradictions": self.contradiction_count,
            "memories": list(self.source_memory_ids[:4]),
        }


# ---------------------------------------------------------------------------
# 意味評価
# ---------------------------------------------------------------------------


def appraise(
    *, action: ActionType | str = "", outcome_status: str = "",
    interrupted: bool = False, end_signal: float = .0,
    user_frustration: float = .0, user_distress: float = .0,
    danger: float = .0, correction: bool = False,
    preference_signal: bool = False, playful: bool = False,
    input_confidence: float = 1.0,
) -> EventAppraisal:
    """出来事の意味。**特定の文字列だけで決めない。**

    行動・結果・相手の状態・危険度から決める。並び順が優先順位で、
    上にあるものほど**取り返しがつかない**側。
    """
    if float(danger) > .6:
        return EventAppraisal.GAME_EVENT
    if correction:
        return EventAppraisal.CORRECTION
    if float(end_signal) > .5:
        # 打ち切りの合図は敵意ではない。**境界の表明**として扱う。
        return EventAppraisal.BOUNDARY_SIGNAL
    if preference_signal:
        return EventAppraisal.USER_PREFERENCE_SIGNAL
    if str(outcome_status) in {"failed", "cancelled"} or interrupted:
        return EventAppraisal.FAILURE
    if float(user_frustration) > .5 or float(user_distress) > .5:
        return EventAppraisal.RELATIONSHIP_SIGNAL
    if playful:
        return EventAppraisal.PLAYFUL_INTERACTION
    if float(input_confidence) < .55:
        return EventAppraisal.NEUTRAL_EVENT
    if str(outcome_status) in {"spoken_completed", "completed", "silent_completed"}:
        return EventAppraisal.SUCCESS
    return EventAppraisal.NEUTRAL_EVENT


#: 意味評価 → 短期感情の提案量。**どれも小さい。**
#:
#: 敵意（HOSTILITY）に対しても irritation を上げないのは意図的。
#: 苛立ちで応じると、そこから先の判断が全部それに引きずられる。
#: 上げるのは caution（警戒）——距離を取るのと、腹を立てるのは違う。
APPRAISAL_TO_AFFECT: dict[str, dict[str, float]] = {
    EventAppraisal.FAILURE: {"irritation": .03, "confidence": -.04, "tension": .03},
    EventAppraisal.BOUNDARY_SIGNAL: {"arousal": -.05, "caution": .02, "amusement": -.04},
    EventAppraisal.CORRECTION: {"confidence": -.05, "caution": .03},
    EventAppraisal.SUCCESS: {"valence": .02, "confidence": .02, "tension": -.03},
    EventAppraisal.PLAYFUL_INTERACTION: {"amusement": .05, "valence": .03, "comfort": .02},
    EventAppraisal.RELATIONSHIP_SIGNAL: {"tension": .04, "caution": .02, "amusement": -.05},
    EventAppraisal.HOSTILITY: {"caution": .05, "tension": .05, "comfort": -.03},
    EventAppraisal.GAME_EVENT: {"arousal": .06, "interest": .04},
    EventAppraisal.USER_PREFERENCE_SIGNAL: {"interest": .03, "confidence": .02},
    EventAppraisal.NEUTRAL_EVENT: {},
}


def propose_affect_deltas(
    appraisal: EventAppraisal | str, *, event_id: str = "",
    confidence: float = .8, reason: str = "",
) -> list[StateDelta]:
    """意味評価から短期感情の差分を作る。**長期側へは一切触らない。**"""
    return [
        StateDelta(
            target=DeltaTarget.GLOBAL_AFFECT, dimension=dimension,
            proposed_delta=amount, confidence=confidence,
            reason=reason or f"appraisal:{appraisal}",
            duration_class=DurationClass.MOMENTARY,
            source_type=InformationType.OBSERVATION,
            source_event_ids=(event_id,) if event_id else (),
        )
        for dimension, amount in APPRAISAL_TO_AFFECT.get(str(appraisal), {}).items()
    ]


def decay(
    affect: dict[str, float], *, elapsed_seconds: float,
    half_life: dict[str, float] | None = None,
) -> dict[str, float]:
    """**放っておけば基準値へ戻る。** 感情を永久固定しない。

    長期の関係値はここでは触らない。時間が経っただけで信頼が消えるのは
    おかしいし、回復は良い経験・謝罪・訂正で起きるべきものなので。
    """
    if elapsed_seconds <= 0:
        return dict(affect)
    lives = half_life or AFFECT_HALF_LIFE
    out: dict[str, float] = {}
    for name, value in affect.items():
        base = AFFECT_BASELINE.get(name, .5)
        factor = .5 ** (float(elapsed_seconds) / max(1.0, lives.get(name, _DEFAULT_HALF_LIFE)))
        out[name] = round(base + (float(value) - base) * factor, 4)
    return out


# ---------------------------------------------------------------------------
# 行動選択への接続
# ---------------------------------------------------------------------------

#: 内面が行動候補を動かしてよい上限。**記憶の補正と同じ大きさに揃えてある。**
MAX_STATE_BIAS = .20

#: 感情が触れてはいけない行動。
#:
#: **`REMAIN_SILENT` を感情で選ばせない。** 沈黙は会話上の選択であって、
#: 不機嫌の表明でも罰でもない。同じ理由で `WARN` も動かさない——
#: 嫌いな相手への警告を弱めるのは、いちばんやってはいけないこと。
PROTECTED_ACTIONS = frozenset({
    str(ActionType.WARN), str(ActionType.REMAIN_SILENT),
    str(ActionType.ABANDON_INTERRUPTED_RESPONSE),
})


@dataclass(frozen=True, slots=True)
class StateBias:
    adjustments: dict[str, float] = field(default_factory=dict)
    dimensions_used: tuple[str, ...] = ()

    def for_action(self, action: ActionType | str) -> float:
        return float(self.adjustments.get(str(action), .0))

    @property
    def empty(self) -> bool:
        return not self.adjustments

    def snapshot(self) -> dict[str, Any]:
        return {
            "adjustments": {k: round(v, 3) for k, v in self.adjustments.items()},
            "dimensions": list(self.dimensions_used),
        }


def action_bias(state: UnifiedInternalState, *, participant_id: str = "") -> StateBias:
    """内面から行動候補への**小さな**補正。

    ここが上書きになった瞬間、感情で安全機能が変わる設計になる。
    `PROTECTED_ACTIONS` を触らないことと `MAX_STATE_BIAS` の頭打ちで、
    構造的に防ぐ。
    """
    adjustments: dict[str, float] = {}
    used: list[str] = []

    def add(action: ActionType, amount: float, dimension: str) -> None:
        # **合計してから一度だけ頭打ちにする。**
        # 足すたびに丸めると、あとから来る逆向きの補正が飲み込まれる
        # （苛立ちで −.24 → 上限で −.20 → 面白さで +.20 → 0 になっていた）。
        key = str(action)
        if key in PROTECTED_ACTIONS or abs(amount) < .005:
            return
        adjustments[key] = adjustments.get(key, .0) + amount
        if dimension not in used:
            used.append(dimension)

    irritation = state.value("irritation", participant_id)
    caution = state.value("caution", participant_id)
    curiosity = state.value("curiosity", participant_id)
    amusement = state.value("amusement", participant_id)
    comfort = state.value("comfort", participant_id)
    confidence = state.value("confidence", participant_id)
    playfulness = float(state.social_stance.get("playfulness", .25))

    if irritation > AFFECT_BASELINE["irritation"] + .05:
        # 少し苛立っている時は、同じ話を続けない。短く返す方へ寄せる。
        weight = irritation - AFFECT_BASELINE["irritation"]
        add(ActionType.CONTINUE_PREVIOUS_TOPIC, -.6 * weight, "irritation")
        add(ActionType.BRIEF_ACKNOWLEDGE, .3 * weight, "irritation")
        add(ActionType.MAKE_LIGHT_JOKE, -.8 * weight, "irritation")
    if caution > AFFECT_BASELINE["caution"] + .05:
        # 警戒している時に断定しない。確かめる側へ寄せる。
        weight = caution - AFFECT_BASELINE["caution"]
        add(ActionType.ANSWER, -.5 * weight, "caution")
        add(ActionType.ASK_CLARIFICATION, .5 * weight, "caution")
        add(ActionType.CHALLENGE_ASSUMPTION, -.6 * weight, "caution")
    if confidence < AFFECT_BASELINE["confidence"] - .05:
        weight = AFFECT_BASELINE["confidence"] - confidence
        add(ActionType.ANSWER, -.4 * weight, "confidence")
        add(ActionType.ASK_CLARIFICATION, .4 * weight, "confidence")
    if curiosity > AFFECT_BASELINE["curiosity"] + .05:
        # **好奇心で質問を増やさない。** 増やすのは自分から述べる側。
        add(ActionType.CONTINUE_PREVIOUS_TOPIC, .3 * (curiosity - AFFECT_BASELINE["curiosity"]),
            "curiosity")
    if (amusement > AFFECT_BASELINE["amusement"] and playfulness > .35
            and comfort > .55 and irritation < .25 and state.value("tension") < .35):
        # 冗談は**全部そろった時だけ**。面白がっていても、少しでも
        # 苛立ちや緊張があるなら軽口は増やさない
        # （`expression_constraints` の humor_allowed と同じ条件）。
        add(ActionType.MAKE_LIGHT_JOKE,
            .4 * (amusement - AFFECT_BASELINE["amusement"]), "amusement")
    return StateBias(
        adjustments={
            key: round(max(-MAX_STATE_BIAS, min(MAX_STATE_BIAS, value)), 4)
            for key, value in adjustments.items() if abs(value) >= .005
        },
        dimensions_used=tuple(used),
    )


# ---------------------------------------------------------------------------
# Planner への接続
# ---------------------------------------------------------------------------


def _level(value: float, low: float = .35, high: float = .65) -> str:
    if value < low:
        return "low"
    if value < high:
        return "medium"
    return "high"


def expression_constraints(
    state: UnifiedInternalState, *, participant_id: str = "",
    end_signal: float = .0,
) -> dict[str, Any]:
    """Planner へ渡す内面。**粗いラベルだけ。生の数値も履歴も渡さない。**

    反映先は口調・距離感・返答量・断定の強さに限る。ここから文章そのものを
    作らせない（それをやると、感情が中身を決めてしまう）。
    """
    irritation = state.value("irritation", participant_id)
    caution = state.value("caution", participant_id)
    valence = state.value("valence", participant_id)
    arousal = state.value("arousal", participant_id)
    tension = state.value("tension", participant_id)
    comfort = state.value("comfort", participant_id)
    amusement = state.value("amusement", participant_id)
    relationship = state.relationship(participant_id)
    playfulness = float(state.social_stance.get("playfulness", .25))
    familiarity = float(relationship.get("familiarity", .1))

    humor_allowed = bool(
        amusement > .25 and playfulness > .3 and comfort > .5
        and irritation < .25 and tension < .35 and end_signal <= .5
    )
    if irritation > .25 or tension > .4:
        mode = "guarded"
    elif end_signal > .5:
        mode = "winding_down"
    elif comfort > .6 and caution < .2:
        mode = "relaxed"
    else:
        mode = "neutral"
    return {
        "affect": {
            "valence": "slightly_positive" if valence > .6 else
                       "slightly_negative" if valence < .45 else "neutral",
            "arousal": _level(arousal),
            "tension": _level(tension, .2, .45),
        },
        "stance": {"mode": mode, "playfulness": _level(playfulness, .3, .6)},
        "relationship": {
            "familiarity": _level(familiarity, .25, .6),
            "comfort": _level(comfort),
            "caution": _level(caution, .2, .45),
        },
        "expression_constraints": {
            "humor_allowed": humor_allowed,
            # **強度は控えめ側へ倒す。** 内面が強いほど表現も強い、にすると
            # 少しの苛立ちが語気に出る。
            "intensity": "subtle" if mode in {"guarded", "winding_down"} else
                         "moderate" if arousal > .5 else "subtle",
            "verbosity": "short" if (irritation > .25 or end_signal > .5) else
                         "medium",
            "assertiveness": "hedged" if (caution > .3 or
                                          state.value("confidence") < .5) else "plain",
        },
    }
