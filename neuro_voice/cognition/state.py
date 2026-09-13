"""内部状態の**統合ビュー**。新しい保管場所は作らない。

これまで Conversation Planner も LLM も、関係性は `Relationships`、感情は
`TemporalSelf`、義務は `TurnFrame`、作業記憶は `WorkingMemory`、と
別々のモジュールを直接読み回っていた。読み手が増えるたびに配線が増える。

ここは**読み取り専用のスナップショット**を1つ作るだけで、値は持たない。
持ち主は今までどおり各モジュールのまま（第5条）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.types import InformationType


@dataclass(frozen=True, slots=True)
class CognitiveState:
    """行動を選ぶのに要る分だけ。全部を写さない。"""

    working_context: dict[str, Any] = field(default_factory=dict)
    current_goal: str = ""
    unresolved_obligations: tuple[str, ...] = ()
    #: Read-only metadata for prior work only; current-turn response duties
    #: are deliberately excluded by Mind.cognitive_state().
    obligation_contexts: tuple[dict[str, Any], ...] = ()
    active_persona_id: str = ""
    self_state: dict[str, Any] = field(default_factory=dict)
    user_state: dict[str, Any] = field(default_factory=dict)
    relationship_state: dict[str, Any] = field(default_factory=dict)
    affect_state: dict[str, Any] = field(default_factory=dict)
    world_state: dict[str, Any] = field(default_factory=dict)
    uncertainty: dict[str, float] = field(default_factory=dict)
    recent_actions: tuple[str, ...] = ()
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # -- 行動選択が実際に見る値 ---------------------------------------
    #
    # ここを **プロパティにして名前を1箇所へ寄せている**のは、元のキー名が
    # モジュールごとに違うため。選択側がキー名を知っていると、片方を直した
    # 時にもう片方が静かに壊れる。

    @property
    def trust(self) -> float:
        return _clamp(self.relationship_state.get("trust", .5))

    @property
    def comfort(self) -> float:
        return _clamp(self.relationship_state.get("comfort", .5))

    @property
    def user_distress(self) -> float:
        return _clamp(self.user_state.get("distress", 0.0))

    @property
    def user_frustration(self) -> float:
        return _clamp(self.user_state.get("frustration", 0.0))

    @property
    def user_engagement(self) -> float:
        """会話を続けたい度合い。低いのに話を広げない。"""
        return _clamp(self.user_state.get("engagement", .5))

    @property
    def end_signal(self) -> float:
        """「もういいよ」のような打ち切りの合図。"""
        return _clamp(self.user_state.get("end_signal", 0.0))

    @property
    def response_required(self) -> bool:
        """ConversationKernel がこの入力への応答を要求しているか。"""
        return bool(self.working_context.get("response_required", False))

    @property
    def response_requirement_source(self) -> str:
        """本文を含まない、応答必須判定の出典。"""
        return str(self.working_context.get("response_requirement_source", "") or "")

    @property
    def closure_is_affirmative(self) -> bool:
        """「もういいよ、それで」型。打ち切りではなく**同意**。"""
        return bool(self.user_state.get("closure_is_affirmative", False))

    @property
    def is_group(self) -> bool:
        """聞いている人がいる場か。無音が続くと事故に見える。"""
        return bool(self.world_state.get("group", False))

    @property
    def current_topic(self) -> str:
        return str(self.working_context.get("active_theme", "") or "")

    @property
    def ambiguity(self) -> float:
        """「どう思う？」が何を指すか、候補が複数あるか。"""
        return _clamp(self.working_context.get("ambiguity", 0.0))

    @property
    def irritation(self) -> float:
        """自分の苛立ち。**ここは長らく常に 0.0 を返していた。**

        `affect_state` に渡されていたのは `TemporalSelf.snapshot()` の**全体**で、
        感情は `"affect"` の下に入っている。さらに `AffectState` に
        `irritation` という軸は無い（あるのは `frustration`）。
        つまり冗談を抑える罰も、苛立ち時の抑制も、一度も効いていなかった。

        入れ子と別名の両方を吸収する。値の持ち主は `TemporalSelf` と
        `InternalStateService` のままで、ここは読み方を1箇所へ寄せるだけ。
        """
        return _clamp(self._affect("irritation", "frustration"))

    def _affect(self, *names: str) -> float:
        """感情の軸を、入れ子と別名を吸収して読む。"""
        nested = self.affect_state.get("affect")
        sources = [self.affect_state]
        if isinstance(nested, dict):
            sources.append(nested)
        for source in sources:
            for name in names:
                if name in source:
                    return source[name]
        return 0.0

    # -- 内面の残りの軸 -------------------------------------------------
    #
    # 関係性17軸のうち、行動選択が読めるのは trust と comfort だけだった。
    # 残りは計算され、減衰し、保存され、**プロンプト文字列にしかならない**
    # 状態だった。行動へ効かせるものだけをここへ出す。

    @property
    def caution(self) -> float:
        """警戒。高いのに断定しない。**苛立ちとは別物**——距離を取るのと、
        腹を立てるのは違う。"""
        return _clamp(self._affect("caution") or self.relationship_state.get("caution", .10))

    @property
    def tension(self) -> float:
        return _clamp(self._affect("tension", "concern")
                      or self.relationship_state.get("tension", 0.0))

    @property
    def playfulness(self) -> float:
        return _clamp(self.relationship_state.get("playfulness", .25))

    @property
    def familiarity(self) -> float:
        return _clamp(self.relationship_state.get("familiarity", .10))

    @property
    def self_confidence(self) -> float:
        """自分の確信。入力の聞き取りやすさ（`input_confidence`）とは別。"""
        value = self._affect("confidence")
        if value:
            return _clamp(value)
        return _clamp(self.self_state.get("confidence", .60))

    @property
    def curiosity(self) -> float:
        return _clamp(self.self_state.get("curiosity", .5))

    @property
    def input_confidence(self) -> float:
        """聞き取りの確かさ。低いのに断定させないため。"""
        return _clamp(self.uncertainty.get("input", 1.0))

    @property
    def interrupted_response(self) -> str:
        """中断された自分の発話。空なら中断していない。"""
        return str(self.world_state.get("interrupted_response", "") or "")

    @property
    def danger(self) -> float:
        return _clamp(self.world_state.get("danger", 0.0))

    def summary(self) -> dict[str, Any]:
        """ログ用。**本文は入れない**（第12条）。"""
        return {
            "snapshot_id": self.snapshot_id,
            "goal": self.current_goal[:60],
            "obligations": list(self.unresolved_obligations),
            "trust": round(self.trust, 2),
            "comfort": round(self.comfort, 2),
            # **推定であって本人の申告ではない**ことを型で示す（原則B）。
            "distress_source": str(InformationType.INFERENCE),
            "distress": round(self.user_distress, 2),
            "frustration": round(self.user_frustration, 2),
            "engagement": round(self.user_engagement, 2),
            "end_signal": round(self.end_signal, 2),
            "ambiguity": round(self.ambiguity, 2),
            "topic": self.current_topic[:40],
            "irritation": round(self.irritation, 2),
            "caution": round(self.caution, 2),
            "tension": round(self.tension, 2),
            "familiarity": round(self.familiarity, 2),
            "playfulness": round(self.playfulness, 2),
            "self_confidence": round(self.self_confidence, 2),
            "curiosity": round(self.curiosity, 2),
            "input_confidence": round(self.input_confidence, 2),
            "danger": round(self.danger, 2),
            "interrupted": bool(self.interrupted_response),
            "recent_actions": list(self.recent_actions[-4:]),
        }


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def build_state(
    *,
    working_memory: dict[str, Any] | None = None,
    relationship: dict[str, Any] | None = None,
    affect: dict[str, Any] | None = None,
    obligations: tuple[str, ...] | list[str] = (),
    obligation_contexts: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    active_persona_id: str = "",
    goal: str = "",
    user_state: dict[str, Any] | None = None,
    world_state: dict[str, Any] | None = None,
    uncertainty: dict[str, float] | None = None,
    recent_actions: tuple[str, ...] | list[str] = (),
    self_state: dict[str, Any] | None = None,
) -> CognitiveState:
    """既存モジュールの snapshot を1つへまとめる。**値は変えない。**"""
    return CognitiveState(
        working_context=dict(working_memory or {}),
        current_goal=str(goal or (working_memory or {}).get("conversation_goal") or ""),
        unresolved_obligations=tuple(str(item) for item in obligations),
        obligation_contexts=tuple(dict(item) for item in obligation_contexts
                                  if isinstance(item, dict)),
        active_persona_id=str(active_persona_id or ""),
        self_state=dict(self_state or {}),
        user_state=dict(user_state or {}),
        relationship_state=dict(relationship or {}),
        affect_state=dict(affect or {}),
        world_state=dict(world_state or {}),
        uncertainty=dict(uncertainty or {}),
        recent_actions=tuple(str(item) for item in recent_actions)[-8:],
    )
