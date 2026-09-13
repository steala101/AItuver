"""複数の経験から、**使い回せる仮説**を1つ作る。

Reflection は人格を変える機能ではない。1回の出来事から
「ユーザーは怒りっぽい」と決めるのは、観察ではなくレッテルで、
一度貼ると剥がれない。

だからここは3つを守る。

* **1件では作らない。** 同じ事柄が複数回観測されて初めて仮説になる
* **人格ではなく振る舞いを書く。** 「怒りっぽい」ではなく
  「説明が長く同じ点を繰り返すと、会話終了を示す傾向が複数回観測された」
* **反証で確信が下がる。** 明示的な発言は仮説より常に強い

今回扱うのは `CONVERSATION_STRATEGY` の1種類だけ。ユーザーの人格分析全般へは
広げない（そこは仮説の当たり外れを本人が確かめられない領域なので、
間違ったまま固定されるリスクが高い）。
"""
from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.episodic import EpisodicMemory, MemoryStatus, contradicts
from neuro_voice.cognition.types import ActionType, InformationType


class ReflectionTarget(StrEnum):
    """何についての仮説か。

    今回実装するのは `CONVERSATION_STRATEGY` のみ。他は**枠だけ**置いてある
    ——将来使うためではなく、`CONVERSATION_STRATEGY` が何の一種なのかを
    示すため。使わない枠が増えたら消すこと。
    """

    CONVERSATION_STRATEGY = "conversation_strategy"
    SELF_FAILURE_PATTERN = "self_failure_pattern"
    USER_PREFERENCE = "user_preference"
    USER_BEHAVIOR_PATTERN = "user_behavior_pattern"
    RELATIONSHIP_PATTERN = "relationship_pattern"
    GAMEPLAY_PATTERN = "gameplay_pattern"


class ReflectionStatus(StrEnum):
    ACTIVE = "active"
    WEAKENED = "weakened"
    RETIRED = "retired"


#: これ未満まで確信が落ちた仮説は使わない。**消しはしない**——
#: 「一度そう考えて外した」ことも記録として意味がある。
RETIRE_BELOW = .25
#: 仮説を作るのに要る最低件数。1件では作らない。
MIN_SUPPORT = 3


@dataclass(slots=True)
class Reflection:
    """複数の経験から出た仮説。**必ず出典を持つ。**"""

    statement: str
    target_type: ReflectionTarget | str = ReflectionTarget.CONVERSATION_STRATEGY
    source_memory_ids: tuple[int, ...] = ()
    support_count: int = 0
    contradiction_count: int = 0
    confidence: float = .3
    #: 行動選択への補正。**小さく。** 一度の Reflection で会話は変わらない。
    action_deltas: dict[str, float] = field(default_factory=dict)
    status: ReflectionStatus | str = ReflectionStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_verified_at: float = field(default_factory=time.time)
    reflection_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def information_type(self) -> InformationType:
        """**これは推論であって、本人の発言ではない。**"""
        return InformationType.REFLECTION

    @property
    def usable(self) -> bool:
        return (
            str(self.status) == str(ReflectionStatus.ACTIVE)
            and float(self.confidence) >= RETIRE_BELOW
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "reflection_id": self.reflection_id,
            "target": str(self.target_type),
            "statement": self.statement[:120],
            "confidence": round(float(self.confidence), 2),
            "support": self.support_count,
            "contradictions": self.contradiction_count,
            "status": str(self.status),
            "deltas": {k: round(v, 3) for k, v in self.action_deltas.items()},
            "sources": list(self.source_memory_ids[:6]),
        }


# ---------------------------------------------------------------------------
# いつ走らせるか
# ---------------------------------------------------------------------------


def should_reflect(
    *, important_episodes: int = 0, repeated_failures: int = 0,
    explicit_corrections: int = 0, session_ended: bool = False,
    idle_seconds: float = .0, explicit_request: bool = False,
    min_episodes: int = 8, idle_threshold: float = 180.0,
) -> str:
    """走らせる理由。無ければ空文字。**毎ターンは走らせない。**

    理由の名前を返すのは、トレースで「なぜ内省が走ったか」を読めるようにするため。
    """
    if explicit_request:
        return "explicit_request"
    if session_ended:
        return "session_ended"
    if repeated_failures >= MIN_SUPPORT:
        return "repeated_failures"
    if explicit_corrections >= 2:
        return "repeated_corrections"
    if important_episodes >= int(min_episodes):
        return "episode_threshold"
    if idle_seconds >= float(idle_threshold) and important_episodes >= MIN_SUPPORT:
        return "idle"
    return ""


# ---------------------------------------------------------------------------
# 生成（CONVERSATION_STRATEGY のみ）
# ---------------------------------------------------------------------------

#: 失敗パターン → 仮説の文と、効かせる行動補正。
#:
#: **人格ではなく、こちらの振る舞いと観測された結果を書く。**
#: 主語が「ユーザーは〜な人」になっている文はここに入れない。
_STRATEGIES: dict[str, tuple[str, dict[str, float]]] = {
    "kept_talking_after_end_signal": (
        "終了の合図の後に説明を続けると、会話がそこで途切れる場面が複数回観測された",
        {str(ActionType.CONTINUE_PREVIOUS_TOPIC): -.05,
         str(ActionType.BRIEF_ACKNOWLEDGE): .03,
         str(ActionType.REMAIN_SILENT): .03},
    ),
    "repeated_the_same_explanation": (
        "同じ点を繰り返して説明すると、会話終了を示す反応が複数回観測された",
        {str(ActionType.CONTINUE_PREVIOUS_TOPIC): -.05},
    ),
    "asked_a_question_already_answered": (
        "既に答えてもらった内容をもう一度聞くと、否定的な反応が複数回観測された",
        {str(ActionType.ASK_CLARIFICATION): -.05},
    ),
    "joked_at_a_bad_moment": (
        "相手の苛立ちが高い場面で冗談を挟むと、反応が悪化する場面が複数回観測された",
        {str(ActionType.MAKE_LIGHT_JOKE): -.06},
    ),
    "answered_on_low_confidence": (
        "聞き取りが不確かなまま答えると、訂正される場面が複数回観測された",
        {str(ActionType.ANSWER): -.04, str(ActionType.ASK_CLARIFICATION): .04},
    ),
}


def derive_conversation_strategy(
    episodes: list[EpisodicMemory] | tuple[EpisodicMemory, ...],
    *, min_support: int | None = None,
) -> Reflection | None:
    """自分の失敗の積み重ねから、会話の進め方の仮説を1つ作る。

    **1件では作らない。** `min_support` 件そろって初めて仮説になる。
    複数のパターンが閾値を超えていたら、件数が多い方を採る（毎回1つだけ）。
    """
    # 既定値を引数のデフォルトに焼き込まない。**定義時に束縛されると
    # `MIN_SUPPORT` を変えても効かなくなる**——「定数を直したのに挙動が
    # 変わらない」はいちばん時間を溶かす。
    required = MIN_SUPPORT if min_support is None else int(min_support)
    counts: Counter[str] = Counter()
    sources: dict[str, list[int]] = {}
    for memory in episodes:
        event = str(memory.event_type)
        if not event.startswith("self_failure:"):
            continue
        if str(memory.status) in {str(MemoryStatus.DELETED), str(MemoryStatus.SUPERSEDED)}:
            continue
        pattern = event.split(":", 1)[1]
        if pattern not in _STRATEGIES:
            continue
        counts[pattern] += max(1, int(memory.support_count))
        sources.setdefault(pattern, []).append(int(memory.memory_id))
    if not counts:
        return None
    pattern, support = counts.most_common(1)[0]
    if support < required:
        return None
    statement, deltas = _STRATEGIES[pattern]
    # **観測件数が増えるほど確信は上がるが、頭打ちにする。**
    # 積み上げだけで確信が1.0になると、明示的な否定で覆せなくなる。
    confidence = min(.75, .25 + .10 * support)
    return Reflection(
        statement=statement,
        target_type=ReflectionTarget.CONVERSATION_STRATEGY,
        source_memory_ids=tuple(sorted(set(sources.get(pattern, [])))),
        support_count=int(support),
        confidence=round(confidence, 3),
        action_deltas=dict(deltas),
    )


# ---------------------------------------------------------------------------
# 反証
# ---------------------------------------------------------------------------


def revise(reflection: Reflection, statement: str, *, is_user_statement: bool = True) -> Reflection:
    """新しい発言と突き合わせて、確信を上げ下げする。

    **本人の明示的な発言は仮説より常に強い。** 反証されたら確信を下げ、
    下がりきったら使うのをやめる——が、**消しはしない**。
    """
    text = str(statement or "").strip()
    if not text:
        return reflection
    if contradicts(text, reflection.statement):
        drop = .35 if is_user_statement else .12
        reflection.contradiction_count += 1
        reflection.confidence = round(max(.0, float(reflection.confidence) - drop), 3)
        if reflection.confidence < RETIRE_BELOW:
            reflection.status = ReflectionStatus.RETIRED
        else:
            reflection.status = ReflectionStatus.WEAKENED
        reflection.last_verified_at = time.time()
    return reflection


def apply_to_state(reflection: Reflection) -> dict[str, float]:
    """人格・関係性の状態へ返す小さな差分。

    **Reflection から直接文章を作らない。** 返すのは補正値だけで、
    上限は `cognition.trace.STATE_LIMITS` 側でさらに掛かる。
    """
    if not reflection.usable:
        return {}
    scale = float(reflection.confidence)
    return {
        key: round(value * scale, 4)
        for key, value in reflection.action_deltas.items()
    }
