"""内面状態の窓口。**既存の感情・関係性モデルを繋ぐだけで、作り直さない。**

持ち主は今までどおり:

* 声の表情づけの感情 … `TemporalSelf.AffectState`（joy/warmth/calm/…）
* 相手ごとの関係 17軸 … `RelationshipStore`
* 人格の基本軸 … `PersonalityEngine.TRAITS`
* 経験と仮説 … `EpisodicMemoryService`（Phase 3）

ここが持つのは**行動選択のための短期感情だけ**で、それもRAMのみ。
再起動で消えてよい——数分で戻る値をDBに書く意味がない。

監査で見つかった切れている配線をここで繋ぐ:

* `AffectState` には `irritation` が無い（あるのは `frustration`）。しかも
  `TemporalSelf.snapshot()` は感情を `"affect"` の下に入れる。そのため
  `CognitiveState.irritation` は**常に 0.0** だった
* 関係性17軸のうち行動選択へ届いていたのは `trust` と `comfort` の2つだけ
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from neuro_voice.cognition.internal_state import (
    AFFECT_BASELINE, DeltaTarget, DurationClass, EventAppraisal, PreferenceState,
    StateBias, StateDelta, StateDeltaGate, UnifiedInternalState, action_bias,
    appraise, decay, expression_constraints, propose_affect_deltas,
)
from neuro_voice.cognition.types import InformationType

logger = logging.getLogger(__name__)

#: `AffectState`（声の表情づけ）→ 行動選択の軸。**対応づけはここ1箇所だけ。**
#:
#: 2つのモデルを1つに統合しなかったのは、目的が違うため。声の抑揚を直したら
#: 行動選択が変わる、という状態にしたくない。
AFFECT_SOURCE_MAP: dict[str, tuple[str, float]] = {
    # 行動側の軸: (AffectState の軸, 係数)
    "irritation": ("frustration", 1.0),
    "tension": ("concern", .8),
    "valence": ("joy", .5),
    "amusement": ("joy", .6),
    "comfort": ("warmth", .6),
}
#: 会話が止まっている間に短期感情が戻る速さを測る基準。
_MAX_ELAPSED = 3600.0


class InternalStateService:
    """短期感情を持ち、差分を検算し、行動と表現へ小さく効かせる。"""

    def __init__(
        self, cfg=None, *, relationships=None, temporal_self=None, episodes=None,
    ) -> None:
        get = getattr(cfg, "get", None) or (lambda _key, default=None: default)
        self.enabled = bool(get("internal_state.enabled", False))
        self.affect_enabled = bool(get("internal_state.affect_enabled", False))
        self.relationship_updates_enabled = bool(
            get("internal_state.relationship_updates_enabled", False))
        self.preference_updates_enabled = bool(
            get("internal_state.preference_updates_enabled", False))
        self.planner_expression_enabled = bool(
            get("internal_state.planner_expression_enabled", False))
        self._relationships = relationships
        self._temporal_self = temporal_self
        self._episodes = episodes
        self._gate = StateDeltaGate()
        #: **RAMのみ。** 再起動で消えてよい（第15条相当の分離）。
        self._affect: dict[str, float] = dict(AFFECT_BASELINE)
        self._stance: dict[str, Any] = {"mode": "neutral", "playfulness": .25}
        self._preferences: dict[str, PreferenceState] = {}
        self._version = 0
        self._last_decay_at = time.monotonic()
        self._recent_deltas: list[StateDelta] = []
        self._last_appraisal: str = ""
        self._latency: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 統合ビュー
    # ------------------------------------------------------------------

    def snapshot(self, participant_id: str = "") -> UnifiedInternalState:
        """このターンの内面。**取る前に、経過した時間ぶん減衰させる。**"""
        started = time.perf_counter()
        self._apply_decay()
        relationships: dict[str, dict[str, float]] = {}
        if self._relationships is not None and participant_id:
            with contextlib.suppress(Exception):
                relationships[participant_id] = {
                    key: float(value)
                    for key, value in self._relationships.snapshot(participant_id).items()
                    if isinstance(value, (int, float))
                }
        state = UnifiedInternalState(
            version=self._version,
            affect=dict(self._affect),
            social_stance=dict(self._stance),
            current_preferences=dict(self._preferences),
            participant_relationships=relationships,
            confidence=self._affect.get("confidence", .6),
            uncertainty={"affect": .3 if not self.affect_enabled else .1},
            recent_state_deltas=list(self._recent_deltas[-8:]),
        )
        self._latency["state_snapshot_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return state

    def _apply_decay(self) -> None:
        now = time.monotonic()
        elapsed = min(_MAX_ELAPSED, max(0.0, now - self._last_decay_at))
        if elapsed < 1.0:
            return
        self._affect = decay(self._affect, elapsed_seconds=elapsed)
        self._last_decay_at = now

    def seed_from_existing(self, participant_id: str = "") -> None:
        """既存の `AffectState` を短期感情の初期値へ写す。

        **一方向だけ。** ここから `TemporalSelf` へは書き戻さない。
        双方向にすると、どちらが正なのか誰にも分からなくなる。
        """
        if self._temporal_self is None or not self.affect_enabled:
            return
        with contextlib.suppress(Exception):
            source = dict((self._temporal_self.snapshot() or {}).get("affect") or {})
            for target, (origin, weight) in AFFECT_SOURCE_MAP.items():
                if origin not in source:
                    continue
                base = AFFECT_BASELINE.get(target, .5)
                value = float(source.get(origin) or 0.0) * float(weight)
                self._affect[target] = max(0.0, min(1.0, base + value))

    # ------------------------------------------------------------------
    # 出来事 → 差分
    # ------------------------------------------------------------------

    def observe_outcome(
        self, *, decision=None, outcome=None, cognitive_state=None,
        participant_id: str = "", event_id: str = "", correction: bool = False,
        preference_signal: bool = False, playful: bool = False,
    ) -> list[StateDelta]:
        """**結果が出てから**内面を動かす。予測で先に動かさない。

        `ActionOutcome` が確定した後にだけ呼ぶ。「たぶん怒らせた」で
        関係を悪くすると、実際には何も起きていない時にも距離ができる。
        """
        if not (self.enabled and self.affect_enabled):
            return []
        started = time.perf_counter()
        appraisal = appraise(
            action=getattr(decision, "selected_action", ""),
            outcome_status=str(getattr(outcome, "status", "")),
            interrupted=bool(getattr(outcome, "interrupted", False)),
            end_signal=float(getattr(cognitive_state, "end_signal", 0.0) or 0.0),
            user_frustration=float(getattr(cognitive_state, "user_frustration", 0.0) or 0.0),
            user_distress=float(getattr(cognitive_state, "user_distress", 0.0) or 0.0),
            danger=float(getattr(cognitive_state, "danger", 0.0) or 0.0),
            correction=correction, preference_signal=preference_signal, playful=playful,
            input_confidence=float(getattr(cognitive_state, "input_confidence", 1.0) or 1.0),
        )
        self._last_appraisal = str(appraisal)
        self._latency["event_appraisal_ms"] = round((time.perf_counter() - started) * 1000, 2)

        started = time.perf_counter()
        deltas = propose_affect_deltas(appraisal, event_id=event_id)
        self._latency["state_delta_ms"] = round((time.perf_counter() - started) * 1000, 2)
        applied = self.apply(deltas, participant_id=participant_id)
        self._update_stance()
        return applied

    def apply(self, deltas: list[StateDelta], *, participant_id: str = "") -> list[StateDelta]:
        """差分をゲートへ通して反映する。**LLMは値を直接書けない。**"""
        started = time.perf_counter()
        out: list[StateDelta] = []
        for delta in deltas:
            delta.participant_id = delta.participant_id or participant_id
            current = self._current(delta)
            checked = self._gate.evaluate(delta, current)
            out.append(checked)
            if not checked.applied:
                continue
            if checked.target == DeltaTarget.GLOBAL_AFFECT:
                self._affect[checked.dimension] = max(
                    0.0, min(1.0, current + checked.applied_delta))
            elif checked.target == DeltaTarget.PARTICIPANT_RELATIONSHIP:
                self._apply_relationship(checked)
            self._version += 1
        self._recent_deltas.extend(out)
        del self._recent_deltas[:-24]
        self._latency["state_apply_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return out

    def _current(self, delta: StateDelta) -> float:
        if delta.target == DeltaTarget.GLOBAL_AFFECT:
            return float(self._affect.get(delta.dimension, AFFECT_BASELINE.get(delta.dimension, .5)))
        if delta.target == DeltaTarget.PARTICIPANT_RELATIONSHIP and self._relationships is not None:
            with contextlib.suppress(Exception):
                snapshot = self._relationships.snapshot(delta.participant_id)
                return float(snapshot.get(delta.dimension, .5))
        return .5

    def _apply_relationship(self, delta: StateDelta) -> None:
        """長期の関係値。**持ち主は `RelationshipStore` のまま。**

        不明話者では書かない——誰のものか分からない出来事で、特定の人との
        関係を変えてはいけない。
        """
        if not self.relationship_updates_enabled or self._relationships is None:
            return
        participant = str(delta.participant_id or "")
        if not participant or participant in {"unknown", "unknown_speaker", ""}:
            delta.clamp_reason = "unknown_participant"
            delta.applied_delta = .0
            return
        # **書き込みも1箇所へ。** Resolver がモードに応じた宛先を決める。
        # 各モジュールが直接 `RelationshipStore` を叩いていると、
        # 段階を変えるたびに全箇所を直すことになり、必ず1つ忘れる。
        resolver = getattr(self, "_relationship_resolver", None)
        if resolver is not None:
            with contextlib.suppress(Exception):
                ref = self._participant_ref(participant)
                outcome = resolver.apply_state_delta(
                    ref, delta.dimension, delta.applied_delta,
                    reason=delta.reason[:80],
                    # **同じ出来事を person 側へ2回入れない。**
                    event_id=(delta.source_event_ids[0]
                              if delta.source_event_ids else delta.delta_id))
                if outcome.get("error"):
                    # **片方だけ書けたのを成功扱いしない。**
                    delta.clamp_reason = "person_write_failed"
                return
        with contextlib.suppress(Exception):
            self._relationships.apply_state_delta(
                participant, delta.dimension, delta.applied_delta,
                reason=delta.reason[:80],
            )

    def attach_relationship_resolver(self, resolver, ref_factory=None) -> None:
        """関係値の宛先を Resolver へ預ける。**繋ぐまでは従来どおり。**"""
        self._relationship_resolver = resolver
        self._participant_ref_factory = ref_factory

    def _participant_ref(self, participant: str):
        factory = getattr(self, "_participant_ref_factory", None)
        if callable(factory):
            found = factory(participant)
            if found is not None:
                return found
        from neuro_voice.mind.migration import participant_ref

        return participant_ref(speaker_id=participant)

    def _update_stance(self) -> None:
        irritation = self._affect.get("irritation", .05)
        comfort = self._affect.get("comfort", .55)
        amusement = self._affect.get("amusement", .30)
        tension = self._affect.get("tension", .05)
        # 遊び心はゆっくり動かす。ターンごとに反転すると情緒不安定に見える。
        target = max(0.0, min(1.0, .5 * amusement + .5 * comfort - irritation - tension))
        current = float(self._stance.get("playfulness", .25))
        self._stance["playfulness"] = round(current + .2 * (target - current), 3)
        self._stance["mode"] = (
            "guarded" if irritation > .25 or tension > .4
            else "relaxed" if comfort > .6 else "neutral"
        )

    # ------------------------------------------------------------------
    # 好み
    # ------------------------------------------------------------------

    def observe_preference(
        self, subject: str, valence: float, *, memory_id: int = 0,
    ) -> PreferenceState | None:
        """**一度の出来事で好き嫌いを決めない。** 証拠を1つ足すだけ。"""
        if not (self.enabled and self.preference_updates_enabled) or not subject:
            return None
        state = self._preferences.setdefault(subject, PreferenceState(subject=subject))
        state.observe(valence, memory_id=memory_id)
        return state

    # ------------------------------------------------------------------
    # 行動と表現へ
    # ------------------------------------------------------------------

    def action_bias(self, state: UnifiedInternalState, *, participant_id: str = "") -> StateBias:
        if not self.enabled:
            return StateBias()
        started = time.perf_counter()
        bias = action_bias(state, participant_id=participant_id)
        self._latency["state_to_action_bias_ms"] = round(
            (time.perf_counter() - started) * 1000, 2)
        return bias

    def planner_view(
        self, state: UnifiedInternalState, *, participant_id: str = "",
        end_signal: float = .0,
    ) -> dict[str, Any]:
        """Planner へ渡す `internal_state`。**粗いラベルだけ。**"""
        if not (self.enabled and self.planner_expression_enabled):
            return {}
        started = time.perf_counter()
        view = expression_constraints(
            state, participant_id=participant_id, end_signal=end_signal)
        self._latency["state_to_planner_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return view

    # ------------------------------------------------------------------

    def reset_short_term(self) -> None:
        """短期感情だけ捨てる。**長期の関係と好みは触らない。**"""
        self._affect = dict(AFFECT_BASELINE)
        self._stance = {"mode": "neutral", "playfulness": .25}
        self._last_decay_at = time.monotonic()

    @property
    def latency_ms(self) -> dict[str, float]:
        return dict(self._latency)

    @property
    def last_appraisal(self) -> str:
        return self._last_appraisal

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "affect_enabled": self.affect_enabled,
            "relationship_updates_enabled": self.relationship_updates_enabled,
            "preference_updates_enabled": self.preference_updates_enabled,
            "planner_expression_enabled": self.planner_expression_enabled,
            "version": self._version,
            "appraisal": self._last_appraisal,
            **self.snapshot().summary(),
        }


def preference_deltas_from_episode(
    summary: str, *, memory_id: int = 0, information_type: str = "",
) -> tuple[str, float]:
    """エピソードから (話題, 好みの向き) を読む。

    **Phase 3 の `stance()` を再利用する。** 好みの判定器を別に作ると、
    片方だけ直されて食い違う。
    """
    from neuro_voice.cognition.episodic import stance

    axis, side = stance(summary)
    if not axis or not side:
        return "", .0
    # 本人の明言はそのまま、推測は弱く数える。
    weight = 1.0 if str(information_type) == str(InformationType.USER_STATEMENT) else .5
    return axis, float(side) * weight


__all__ = [
    "AFFECT_SOURCE_MAP", "DeltaTarget", "DurationClass", "EventAppraisal",
    "InternalStateService", "PreferenceState", "StateDelta",
    "UnifiedInternalState", "preference_deltas_from_episode",
]
