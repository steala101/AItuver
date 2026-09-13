"""自分から話す**機会**を作る。**作った時点では話さない。**

ここが返すのは「話す価値がありそうな候補」であって、発話の決定ではない。
決めるのは Action Selector で、そこには通常会話・記憶・内面の候補も並ぶ。

**自発発話専用の Action Selector を作らない**のが要点。別系統にすると、
通常会話と自発発話のどちらを優先するかを決める場所がもう1つ増えて、
必ず食い違う。同じ列に並べて同じ物差しで比べる。

もう1つの要点は**沈黙を常に候補に入れる**こと。機会があることと、
話すべきことは別。ここを外すと「機会を見つけた＝話す」になり、
結局うるさいAIになる。

**「一定秒数黙ったら必ず話す」は作らない。** 時間は発話の理由にならない。
沈黙が長いこと自体は、未完了の約束や意味のある状況と結びついて初めて
候補になる。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.cognition.types import ActionCandidate, ActionType

# ---------------------------------------------------------------------------
# 機会
# ---------------------------------------------------------------------------


class OpportunityType(StrEnum):
    REACT_TO_EVENT = "react_to_event"
    COMMENT_ON_GAME = "comment_on_game"
    RESUME_OBLIGATION = "resume_obligation"
    REMIND = "remind"
    SHARE_RELEVANT_MEMORY = "share_relevant_memory"
    LIGHT_FOLLOW_UP = "light_follow_up"
    CONTINUE_SHARED_TOPIC = "continue_shared_topic"
    ACKNOWLEDGE_CHANGE = "acknowledge_change"
    #: 誰かが入ってきた。**候補であって、発話の決定ではない。**
    GREET_PARTICIPANT = "greet_participant"
    #: 「居るのは分かっているよ」を短く示すだけ。挨拶より軽い。
    ACKNOWLEDGE_PRESENCE = "acknowledge_presence"
    #: 誰かが出ていった。**毎回言う必要はない。**
    FAREWELL = "farewell"
    #: **沈黙も機会の一種。** 「何もしない」を選択肢として明示的に持つ。
    REMAIN_SILENT = "remain_silent"


#: 機会 → 行動。1対1で対応させ、変換の規則を1箇所に置く。
OPPORTUNITY_ACTION: dict[str, ActionType] = {
    OpportunityType.REACT_TO_EVENT: ActionType.REACT,
    OpportunityType.COMMENT_ON_GAME: ActionType.COMMENT,
    OpportunityType.RESUME_OBLIGATION: ActionType.RESUME_OBLIGATION,
    OpportunityType.REMIND: ActionType.REMIND,
    OpportunityType.SHARE_RELEVANT_MEMORY: ActionType.SHARE_MEMORY,
    OpportunityType.LIGHT_FOLLOW_UP: ActionType.LIGHT_FOLLOW_UP,
    OpportunityType.CONTINUE_SHARED_TOPIC: ActionType.CONTINUE_PREVIOUS_TOPIC,
    OpportunityType.ACKNOWLEDGE_CHANGE: ActionType.BRIEF_ACKNOWLEDGE,
    OpportunityType.GREET_PARTICIPANT: ActionType.GREET,
    OpportunityType.ACKNOWLEDGE_PRESENCE: ActionType.ACKNOWLEDGE_PRESENCE,
    OpportunityType.FAREWELL: ActionType.FAREWELL,
    OpportunityType.REMAIN_SILENT: ActionType.REMAIN_SILENT,
}

#: 自発発話の点数の重み。**ここ以外に書かない**（`kernel.WEIGHTS` と同じ方針）。
#: 引く側（コスト）の重みを足す側より大きくしてあるのは、
#: **黙る方へ倒すのが既定**だから。
INITIATIVE_WEIGHTS: dict[str, float] = {
    "expected_value": 1.20,
    "salience": 0.80,
    "novelty": 0.60,
    "goal_relevance": 0.90,
    "relationship_fit": 0.40,
    "timing_score": 0.80,
    "social_cost": -1.30,
    "interruption_risk": -1.50,
    "repetition_risk": -1.10,
    "recent_speech_penalty": -1.00,
}


#: 機会の点数を Action Selector の目盛りへ移すための下駄。
#:
#: `initiative_score` は読みやすさのため −1〜1 に正規化してあるが、
#: `CognitiveKernel._score` の総点は 0.8〜1.6 あたりに出る。**目盛りが
#: 違うまま同じ列へ並べても比べたことにならない**——実測では、正規化した
#: ままだと自発発話の候補が一度も勝てなかった。
#:
#: 値は「沈黙の既定点（約1.09）のすぐ下」に置いてある。つまり
#: **価値がコストを上回った機会だけが沈黙を越えられる。**
INITIATIVE_BASELINE = .85


# ---------------------------------------------------------------------------
# 誰に向けて話すのか
# ---------------------------------------------------------------------------


class TargetScope(StrEnum):
    """発話の宛先。**保持しないと「誰に言ったのか」が残らない。**"""

    #: 特定の人へ。
    DIRECT = "direct"
    #: その場の全員へ。
    GROUP = "group"
    #: 誰に向けるでもない独り言に近いもの。**複数人の場では特に高くつく。**
    AMBIENT = "ambient"


#: 機会の種類 → 宛先。**独り言と呼びかけを分けるのはここ。**
#:
#: 約束の再開や話しかけは特定の人へ、実況は場全体へ、環境への一言は
#: 誰に向けるでもない。全部 `GROUP` にしていると、独り言が呼びかけと
#: 同じ重さで扱われる。
_SCOPE_BY_KIND: dict[str, TargetScope] = {
    "resume_obligation": TargetScope.DIRECT,
    "remind": TargetScope.DIRECT,
    "light_follow_up": TargetScope.DIRECT,
    "share_relevant_memory": TargetScope.DIRECT,
    "continue_shared_topic": TargetScope.DIRECT,
    "comment_on_game": TargetScope.GROUP,
    "react_to_event": TargetScope.GROUP,
    "acknowledge_change": TargetScope.AMBIENT,
    # 挨拶は**その人へ**。場全体への独り言ではない。
    "greet_participant": TargetScope.DIRECT,
    "acknowledge_presence": TargetScope.DIRECT,
    "farewell": TargetScope.DIRECT,
}


def scope_for(kind: OpportunityType | str, *, participant_ids: tuple[str, ...] = ()) -> TargetScope:
    """機会の種類と相手の有無から、宛先を決める。

    相手が特定できていないのに `DIRECT` にはしない——**不明話者を
    特定人物として扱わない**の実体がここ。
    """
    scope = _SCOPE_BY_KIND.get(str(kind), TargetScope.GROUP)
    if scope is TargetScope.DIRECT and not participant_ids:
        return TargetScope.GROUP
    return scope


#: 複数人がいる場での上乗せコスト。
#:
#: **人が増えるほど、割り込みは高くつく。** 2人の会話に入るのと、
#: 3人が話しているところに入るのは重さが違う。
GROUP_COST: dict[str, float] = {
    TargetScope.DIRECT: .20,
    TargetScope.GROUP: .30,
    TargetScope.AMBIENT: .55,
}
#: 他人同士が話している最中の上乗せ。**邪魔をしない。**
CROSSTALK_COST = .45


def social_cost_in_group(
    scope: TargetScope | str, *, participant_count: int = 1,
    others_talking: bool = False, addressed_to_me: bool = False,
    topic_is_shared: bool = True,
) -> float:
    """複数人の場での社会的コスト。**1対1では上乗せしない。**

    `addressed_to_me` が真なら上乗せしない——名指しで聞かれているのに
    「複数人だから」で黙るのはおかしい。
    """
    if addressed_to_me:
        return .0
    if participant_count <= 1:
        return .0
    cost = GROUP_COST.get(str(scope), .3)
    # 人数が増えるほど、割り込みは高くつく（頭打ちあり）。
    cost += min(.25, .08 * (participant_count - 2))
    if others_talking:
        cost += CROSSTALK_COST
    if not topic_is_shared:
        # 自分と相手だけの話題を、全員の前で持ち出さない。
        cost += .20
    return round(min(1.0, cost), 3)


# ---------------------------------------------------------------------------
# いま言うのが良いタイミングか
# ---------------------------------------------------------------------------

#: 相手が話し終えてから、口を開くのが自然になるまでの秒数。
#: **短すぎると食い気味、長すぎると間が持たない。**
_TOO_SOON = 0.8
_COMFORTABLE = 2.5
#: これ以上空くと、いま持ち出しても唐突になる（話題が流れている）。
_STALE_AFTER = 45.0


def timing_score(
    *, seconds_since_user_turn: float = 999.0,
    seconds_since_own_speech: float = 999.0,
    topic_just_closed: bool = False, assistant_speaking: bool = False,
    user_speaking: bool = False,
) -> float:
    """**いま口を開くのが自然か。** 0〜1。

    ここが既定値のままだと「価値はあるが今じゃない」を表現できない。
    間の善し悪しは価値と別軸なので、点数の中で独立して持つ。

    * 相手が話し終えた直後（0.8秒未満）は食い気味 → 低い
    * 1〜3秒あたりがいちばん自然
    * 45秒以上空いたら、いま持ち出しても唐突 → 下げる
    * 自分が直前に話したばかりなら下げる（連投に見える）
    """
    if user_speaking or assistant_speaking:
        return .0
    since_user = max(0.0, float(seconds_since_user_turn))
    if since_user < _TOO_SOON:
        score = .15                     # 食い気味
    elif since_user <= _COMFORTABLE:
        score = .85                     # いちばん自然な間
    elif since_user <= _STALE_AFTER:
        # だんだん唐突になる。
        span = (since_user - _COMFORTABLE) / (_STALE_AFTER - _COMFORTABLE)
        score = .85 - .35 * span
    else:
        score = .35
    if topic_just_closed:
        # 話題が閉じた直後は、間としては空いていても持ち出しにくい。
        score *= .4
    since_own = max(0.0, float(seconds_since_own_speech))
    if since_own < 6.0:
        # 自分が話したばかり。連投に見える。
        score *= .35 + .65 * (since_own / 6.0)
    return round(max(0.0, min(1.0, score)), 3)


def silent_opportunity(reason: str = "nothing_worth_saying") -> InitiativeOpportunity:
    """**沈黙は常に候補に入れる。** 機会があることと話すべきことは別。"""
    return InitiativeOpportunity(
        opportunity_type=OpportunityType.REMAIN_SILENT,
        expected_value=.45, salience=.0, novelty=.0, timing_score=.5,
        confidence=1.0, reasons=(reason,),
    )


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


@dataclass(slots=True)
class InitiativeOpportunity:
    """自分から話す機会の候補。**これ自体は発話ではない。**"""

    opportunity_type: OpportunityType | str
    summary: str = ""
    topic_id: str = ""
    participant_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    salience: float = .5
    novelty: float = .5
    urgency: float = .0
    confidence: float = .8
    expected_value: float = .5
    goal_relevance: float = .0
    relationship_fit: float = .0
    social_cost: float = .0
    interruption_risk: float = .0
    repetition_risk: float = .0
    recent_speech_penalty: float = .0
    timing_score: float = .5
    reasons: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    deduplication_key: str = ""
    #: **誰に向けて話すのか。** 保持しないと「誰に言ったのか」が残らない。
    target_participant_ids: tuple[str, ...] = ()
    target_scope: TargetScope | str = TargetScope.GROUP
    #: 由来の記憶。**記憶をそのまま喋らないための証跡。**
    source_memory_ids: tuple[int, ...] = ()
    #: 種類ごとの但し書き。**専用の型を増やさないための入れ物。**
    #:
    #: 挨拶なら「誰か・どれくらい確からしいか・何人居るか」。
    #: **本文も表示名も入れない**（第12条）——入れる所が増えるほど漏れる。
    metadata: dict[str, Any] = field(default_factory=dict)
    opportunity_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def proposed_action(self) -> ActionType:
        return OPPORTUNITY_ACTION.get(str(self.opportunity_type), ActionType.REMAIN_SILENT)

    @property
    def initiative_score(self) -> float:
        """話す価値からコストを引いたもの。**負なら黙る方が自然。**"""
        parts = {
            "expected_value": _clamp(self.expected_value),
            "salience": _clamp(self.salience),
            "novelty": _clamp(self.novelty),
            "goal_relevance": _clamp(self.goal_relevance),
            "relationship_fit": _clamp(self.relationship_fit, -1.0, 1.0),
            "timing_score": _clamp(self.timing_score),
            "social_cost": _clamp(self.social_cost),
            "interruption_risk": _clamp(self.interruption_risk),
            "repetition_risk": _clamp(self.repetition_risk),
            "recent_speech_penalty": _clamp(self.recent_speech_penalty),
        }
        total = sum(INITIATIVE_WEIGHTS.get(key, 1.0) * value for key, value in parts.items())
        # 重みの絶対値の合計で割り、-1〜1 付近へ寄せる。閾値を人が読める大きさに。
        scale = sum(abs(value) for value in INITIATIVE_WEIGHTS.values())
        return round(total / max(1e-6, scale), 4)

    @property
    def components(self) -> dict[str, float]:
        return {
            "expected_value": _clamp(self.expected_value),
            "salience": _clamp(self.salience),
            "novelty": _clamp(self.novelty),
            "goal_relevance": _clamp(self.goal_relevance),
            "relationship_fit": _clamp(self.relationship_fit, -1.0, 1.0),
            "timing_score": _clamp(self.timing_score),
            "social_cost": -_clamp(self.social_cost),
            "interruption_risk": -_clamp(self.interruption_risk),
            "repetition_risk": -_clamp(self.repetition_risk),
            "recent_speech_penalty": -_clamp(self.recent_speech_penalty),
        }

    def dedup(self) -> str:
        if self.deduplication_key:
            return self.deduplication_key
        return f"{self.opportunity_type}:{self.topic_id}"

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) > self.expires_at

    def to_candidate(self, *, score_offset: float = .0) -> ActionCandidate:
        """Action Selector の列へ並べる形にする。**出どころを必ず付ける。**

        点数は `INITIATIVE_BASELINE` を足して Action Selector の目盛りへ移す。
        **2つの点数系が別の目盛りのままだと、同じ列に並べても比べられない**
        ——実際、正規化した −1〜1 のままだと通常候補（0.8〜1.6）に一度も
        勝てなかった。
        """
        return ActionCandidate(
            action_type=self.proposed_action,
            reasons=self.reasons or (str(self.opportunity_type),),
            score_components=self.components,
            total_score=round(
                INITIATIVE_BASELINE + self.initiative_score + score_offset, 4),
            confidence=_clamp(self.confidence),
            source_type=str(SOURCE_PROACTIVE),
            source_event_ids=tuple(self.source_event_ids),
            topic_id=self.topic_id,
            participant_ids=tuple(self.participant_ids),
            expires_at=self.expires_at,
        )

    def snapshot(self) -> dict[str, Any]:
        """トレース用。**本文は入れない**（第12条）。"""
        return {
            "opportunity_id": self.opportunity_id,
            "type": str(self.opportunity_type),
            "action": str(self.proposed_action),
            "score": self.initiative_score,
            "topic": self.topic_id,
            "confidence": round(float(self.confidence), 2),
            "reasons": list(self.reasons[:4]),
            "events": list(self.source_event_ids[:3]),
            "chars": len(self.summary),
        }


#: 候補の出どころ名。`SpeechSource` と同じ文字列で揃える。
SOURCE_PROACTIVE = "proactive_opportunity"


# ---------------------------------------------------------------------------
# 出入りへの反応 (Phase 6D)
#
# **挨拶は「必ず言うもの」ではない。**
#
# 以前は入室のたびに固定文をTTSへ直接流していた。相手が話している最中でも、
# 危険警告の途中でも、5人が同時に入っても、必ず声が出る。
# ここで候補を作り、**話すかどうかは他の候補と同じ列で決める。**
# ---------------------------------------------------------------------------

#: 挨拶の候補が生きている時間。**入ってから何十秒も経って言うのは変。**
GREETING_TTL = 25.0
FAREWELL_TTL = 15.0
#: これより短い間の出入りは、回線の切れ目とみなす。**挨拶し直さない。**
RECONNECT_WINDOW = 90.0
#: 同じ人へ続けて挨拶しない時間。
GREETING_REPEAT_WINDOW = 1800.0
#: これ以上が一度に入ってきたら、一人ずつ挨拶しない。
MASS_JOIN_THRESHOLD = 3


def greeting_opportunity(
    *, person_id: str, identity_confidence: float = .0, known: bool = False,
    relationship_summary: float = .0, channel_context: str = "",
    group_size: int = 1, recent_interaction_seconds: float = 1e9,
    seconds_since_last_seen: float = 1e9, joined_together: int = 1,
    greeted_recently_seconds: float = 1e9, source_event_id: str = "",
    now: float | None = None,
) -> InitiativeOpportunity | None:
    """入ってきた人への挨拶候補。**作らない方が正しい場面が多い。**

    `None` は「候補にすらしない」。**`greet_on_join: true` は
    「必ず挨拶する」ではなく「挨拶を検討してよい」という意味。**

    ここで落とすのは、Action Selector に並べるまでもないもの:

    * 誰か分からない — **知らない人に名前で挨拶する方が失礼**
    * 短時間の再接続 — 回線が切れただけ。挨拶し直すと鬱陶しい
    * 大人数が同時入室 — 一人ずつ名指しで挨拶すると場が止まる
    * 直前に挨拶済み — 同じ相手に何度も言わない

    場の状況（誰かが話している、危険警告中）はここでは見ない。
    それは `suppression_reason` の仕事で、**判定を2箇所に分けない。**
    """
    moment = time.monotonic() if now is None else now
    if not person_id or not known:
        # **知らない人として扱う。** 名前を当てにいかない。
        return None
    if seconds_since_last_seen <= RECONNECT_WINDOW:
        return None
    if greeted_recently_seconds <= GREETING_REPEAT_WINDOW:
        return None
    if joined_together >= MASS_JOIN_THRESHOLD:
        return None
    # 久しぶりほど言う価値がある。すぐ前まで話していたなら軽く済ませる。
    freshly_talked = recent_interaction_seconds < 600.0
    kind = (OpportunityType.ACKNOWLEDGE_PRESENCE if freshly_talked
            else OpportunityType.GREET_PARTICIPANT)
    return InitiativeOpportunity(
        opportunity_type=kind,
        topic_id=f"greeting:{person_id}",
        participant_ids=(person_id,), target_participant_ids=(person_id,),
        # **その人へ。** 場全体への独り言ではない。
        target_scope=TargetScope.DIRECT,
        source_event_ids=((source_event_id,) if source_event_id else ()),
        expected_value=.55 if freshly_talked else .75,
        salience=.5, novelty=.4 if freshly_talked else .7,
        relationship_fit=_clamp(relationship_summary, -1.0, 1.0),
        # **人が多いほど、名指しの一言は高くつく。**
        social_cost=min(.6, .08 * max(0, int(group_size) - 1)),
        timing_score=.8,
        confidence=_clamp(identity_confidence),
        reasons=("participant_joined",) + (("known_person",) if known else ()),
        created_at=moment, expires_at=moment + GREETING_TTL,
        # **同じ人には1つ。** 入り直しても候補が積み上がらない。
        deduplication_key=f"greet:{person_id}",
        metadata={
            "participant_id": person_id,
            "identity_confidence": round(_clamp(identity_confidence), 3),
            "relationship": round(_clamp(relationship_summary, -1.0, 1.0), 3),
            "channel": str(channel_context)[:40],
            "group_size": int(group_size),
            "recent_interaction_s": round(float(recent_interaction_seconds), 1),
            "away_s": round(float(seconds_since_last_seen), 1),
        },
    )


def farewell_opportunity(
    *, person_id: str, known: bool = False, was_talking_with: bool = False,
    identity_confidence: float = .0, group_size: int = 1,
    abrupt: bool = False, handled_event_ids: tuple[str, ...] = (),
    source_event_id: str = "", now: float | None = None,
) -> InitiativeOpportunity | None:
    """出ていった人への一言。**毎回は要らない。**

    別れの挨拶が自然なのは、**直前までその人と話していた時だけ**。
    黙って抜ける人に毎回声をかけるのは、見送りではなく引き止めになる。

    突然切れた場合も作らない。回線が落ちただけかもしれないし、
    **もう聞いていない相手へ喋っても届かない。**
    """
    moment = time.monotonic() if now is None else now
    if not person_id or not known or not was_talking_with:
        return None
    if abrupt:
        return None
    if source_event_id and source_event_id in set(handled_event_ids):
        return None
    return InitiativeOpportunity(
        opportunity_type=OpportunityType.FAREWELL,
        topic_id=f"farewell:{person_id}",
        participant_ids=(person_id,), target_participant_ids=(person_id,),
        target_scope=TargetScope.DIRECT,
        source_event_ids=((source_event_id,) if source_event_id else ()),
        expected_value=.6, salience=.45, novelty=.5,
        social_cost=min(.5, .08 * max(0, int(group_size) - 1)),
        timing_score=.7, confidence=_clamp(identity_confidence),
        reasons=("participant_left", "was_in_conversation"),
        created_at=moment, expires_at=moment + FAREWELL_TTL,
        deduplication_key=f"farewell:{person_id}",
        metadata={"participant_id": person_id, "group_size": int(group_size)},
    )


# ---------------------------------------------------------------------------
# 発話してはいけない状態
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SpeakingConditions:
    """いま話してよいかの材料。**判定はここには書かない。**

    材料と判定を分けてあるのは、判定を1箇所（`suppression_reason`）に
    まとめるため。散らばると「なぜ黙ったのか」が誰にも説明できなくなる。
    """

    user_speaking: bool = False
    other_participant_speaking: bool = False
    awaiting_end_of_turn: bool = False
    assistant_speaking: bool = False
    handling_interruption: bool = False
    seconds_since_turn_closure: float = 999.0
    last_outcome_status: str = ""
    last_topic_id: str = ""
    focus_mode: bool = False
    only_unknown_speakers: bool = False
    handled_event_ids: tuple[str, ...] = ()
    spoken_dedup_keys: tuple[str, ...] = ()
    topic_closed: bool = False
    #: 危険の警告を出している最中。**挨拶で割り込まない。**
    #:
    #: `WARN` 自体はこの判定を通らないので、警告が止まることはない。
    #: 止まるのは、警告と同時に出ようとした自発発話の方。
    warning_in_progress: bool = False


#: Turn Closure の直後は黙る秒数。
CLOSURE_QUIET_SECONDS = 3.0
#: 自発発話に要る最低限の確からしさ。
MIN_INITIATIVE_CONFIDENCE = .5


def suppression_reason(
    opportunity: InitiativeOpportunity, conditions: SpeakingConditions, *,
    budget_reason: str = "", now: float | None = None,
) -> str:
    """自発発話を止める理由。無ければ空文字。**判定はここ1箇所だけ。**

    `WARN` はここを通らない。危険の警告を社会的コストで止めるのは、
    優先順位を間違えている。
    """
    moment = time.monotonic() if now is None else now
    if opportunity.proposed_action is ActionType.REMAIN_SILENT:
        return ""
    if opportunity.expired(now=moment):
        return "opportunity_expired"
    if float(opportunity.confidence) < MIN_INITIATIVE_CONFIDENCE:
        return "confidence_too_low"
    # --- 相手の番 ---
    if conditions.user_speaking:
        return "user_is_speaking"
    if conditions.other_participant_speaking:
        return "another_participant_is_speaking"
    if conditions.awaiting_end_of_turn:
        return "awaiting_end_of_turn"
    # --- 自分の番 ---
    if conditions.assistant_speaking:
        return "already_speaking"
    if conditions.handling_interruption:
        return "handling_interruption"
    if conditions.warning_in_progress:
        # **危険の最中に挨拶しない。** 警告そのものはここを通らない。
        return "warning_in_progress"
    # --- 直前の決定 ---
    if conditions.seconds_since_turn_closure < CLOSURE_QUIET_SECONDS:
        return "just_closed_the_turn"
    if (conditions.last_outcome_status == "silent_completed"
            and opportunity.topic_id
            and opportunity.topic_id == conditions.last_topic_id):
        # 黙ると決めた話題を、自分から蒸し返さない。
        return "same_topic_after_silence"
    if conditions.topic_closed:
        return "topic_closed"
    # --- 場 ---
    if conditions.focus_mode:
        return "user_is_focused"
    if conditions.only_unknown_speakers:
        return "only_unknown_speakers"
    # --- 重複 ---
    if set(opportunity.source_event_ids) & set(conditions.handled_event_ids):
        return "event_already_handled"
    if opportunity.dedup() in set(conditions.spoken_dedup_keys):
        return "already_said_this"
    if budget_reason:
        return budget_reason
    return ""


# ---------------------------------------------------------------------------
# 予算
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InitiativeBudget:
    """話しすぎを防ぐ。**回数だけでなく、重さも数える。**

    短い相槌10回と長い解説1回では、相手にとっての重さが違う。
    回数だけだと、長い実況を連発しても予算に引っかからない。
    """

    window_seconds: float = 120.0
    max_utterances: int = 4
    max_same_topic_utterances: int = 2
    cooldown_seconds: float = 8.0
    #: (時刻, 話題, 重さ) の履歴。
    history: list[tuple[float, str, float]] = field(default_factory=list)
    #: 窓の中で使ってよい重さの合計。
    max_cost: float = 3.0

    def _live(self, now: float) -> list[tuple[float, str, float]]:
        return [item for item in self.history if 0.0 <= now - item[0] <= self.window_seconds]

    @property
    def recent_cost(self) -> float:
        return round(sum(item[2] for item in self._live(time.monotonic())), 3)

    def check(self, opportunity: InitiativeOpportunity, *, now: float | None = None) -> str:
        """予算に引っかかる理由。無ければ空文字。"""
        moment = time.monotonic() if now is None else now
        live = self._live(moment)
        if live and moment - max(item[0] for item in live) < self.cooldown_seconds:
            return "cooldown"
        if len(live) >= self.max_utterances:
            return "utterance_budget"
        if sum(item[2] for item in live) >= self.max_cost:
            return "cost_budget"
        topic = opportunity.topic_id
        if topic and sum(1 for item in live if item[1] == topic) >= self.max_same_topic_utterances:
            return "same_topic_budget"
        return ""

    def record(
        self, opportunity: InitiativeOpportunity, *, cost: float = 1.0,
        now: float | None = None,
    ) -> None:
        moment = time.monotonic() if now is None else now
        self.history.append((moment, opportunity.topic_id, max(.1, float(cost))))
        del self.history[:-32]

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        moment = time.monotonic() if now is None else now
        live = self._live(moment)
        return {
            "window_seconds": self.window_seconds,
            "used": len(live),
            "max_utterances": self.max_utterances,
            "recent_cost": round(sum(item[2] for item in live), 3),
            "max_cost": self.max_cost,
        }


#: 警告は通常予算の対象外。**ただし同じ警告の連打は抑える。**
WARN_REPEAT_SECONDS = 6.0


class WarnThrottle:
    """同じ危険を何度も叫ばない。**別の危険は止めない。**"""

    def __init__(self, repeat_seconds: float = WARN_REPEAT_SECONDS) -> None:
        self.repeat_seconds = float(repeat_seconds)
        self._last: dict[str, float] = {}

    def allows(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        last = self._last.get(str(key))
        if last is not None and moment - last < self.repeat_seconds:
            return False
        self._last[str(key)] = moment
        return True


# ---------------------------------------------------------------------------
# ゲームイベントの分類
# ---------------------------------------------------------------------------


class GameEventCategory(StrEnum):
    DANGER = "danger"
    MAJOR_PROGRESS = "major_progress"
    UNEXPECTED_EVENT = "unexpected_event"
    FAILURE = "failure"
    SUCCESS = "success"
    DISCOVERY = "discovery"
    REPETITIVE_EVENT = "repetitive_event"
    AMBIENT_CHANGE = "ambient_change"


#: 分類 → (機会の種類, 期待価値, 新しさの下限)。`None` は**原則沈黙**。
#:
#: 画面認識の結果をそのまま文章にしない。**まず分類し、分類ごとに
#: 話すかどうかの方針を持つ。** 逐次反応は実況ではなく読み上げ。
GAME_POLICY: dict[str, tuple[OpportunityType, float, float] | None] = {
    GameEventCategory.DANGER: None,          # WARN 高速経路が扱う
    GameEventCategory.MAJOR_PROGRESS: (OpportunityType.COMMENT_ON_GAME, .80, .0),
    GameEventCategory.UNEXPECTED_EVENT: (OpportunityType.REACT_TO_EVENT, .70, .0),
    GameEventCategory.FAILURE: (OpportunityType.REACT_TO_EVENT, .60, .0),
    GameEventCategory.SUCCESS: (OpportunityType.REACT_TO_EVENT, .60, .0),
    # 新しい発見は、本当に新しい時だけ。
    GameEventCategory.DISCOVERY: (OpportunityType.COMMENT_ON_GAME, .55, .65),
    GameEventCategory.REPETITIVE_EVENT: None,
    GameEventCategory.AMBIENT_CHANGE: None,
}

_DANGER_WORDS = ("危険", "ピンチ", "hp", "体力", "溶岩", "落下", "毒", "敵に囲",
                 "残り時間", "爆発", "死",)
_FAILURE_WORDS = ("失敗", "やられ", "負け", "落ちた", "壊れ", "ミス", "間違")
_SUCCESS_WORDS = ("成功", "倒した", "勝", "完成", "達成", "クリア")
_PROGRESS_WORDS = ("到達", "進ん", "レベル", "解放", "新しいエリア", "ボス")
_DISCOVERY_WORDS = ("発見", "見つけ", "初めて", "レア", "珍し")


def classify_game_event(
    summary: str, *, kind: str = "", priority: float = .5,
    repeat_count: int = 0, novelty: float = .5,
) -> GameEventCategory:
    """ゲームの出来事を8種へ分ける。**逐次反応しないための最初の関門。**

    `repeat_count` を先に見るのは、同じことが何度も起きているなら
    それが何であれ実況の価値が落ちるため（初回だけ反応する）。
    """
    text = f"{kind} {summary}".lower()
    if any(word in text for word in _DANGER_WORDS) or priority >= .85:
        return GameEventCategory.DANGER
    if repeat_count >= 2:
        return GameEventCategory.REPETITIVE_EVENT
    if any(word in text for word in _FAILURE_WORDS):
        return GameEventCategory.FAILURE
    if any(word in text for word in _SUCCESS_WORDS):
        return GameEventCategory.SUCCESS
    if any(word in text for word in _PROGRESS_WORDS):
        return GameEventCategory.MAJOR_PROGRESS
    if any(word in text for word in _DISCOVERY_WORDS):
        return GameEventCategory.DISCOVERY
    if novelty >= .75 and priority >= .5:
        return GameEventCategory.UNEXPECTED_EVENT
    return GameEventCategory.AMBIENT_CHANGE


# ---------------------------------------------------------------------------
# 機会づくり
# ---------------------------------------------------------------------------

#: 機会の既定の寿命（秒）。**状況が変わったら黙って捨てる。**
DEFAULT_TTL = 12.0


def opportunities_from_events(
    events: list[AttentionEvent] | tuple[AttentionEvent, ...], *,
    obligations: tuple[str, ...] = (),
    relationship_fit: float = .0,
    timing: float | None = None,
    now: float | None = None,
) -> list[InitiativeOpportunity]:
    """注意イベントから機会の候補を作る。**沈黙は呼び出し側が足す。**

    **時間だけで機会を作らない。** 沈黙の閾値イベントは、未完了の約束が
    ある時にだけ候補になる。「一定秒数黙ったら話す」を作らないための実体。
    """
    moment = time.monotonic() if now is None else now
    out: list[InitiativeOpportunity] = []
    for event in events:
        if event.expired(now=moment):
            continue
        kind = str(event.event_type)
        topic = event.topic_ids[0] if event.topic_ids else ""
        common = {
            "summary": event.summary[:180],
            "topic_id": topic,
            "participant_ids": event.participant_ids,
            "source_event_ids": (event.event_id,),
            "salience": event.salience,
            "novelty": event.novelty,
            "urgency": event.urgency,
            "confidence": event.confidence,
            "relationship_fit": relationship_fit,
            "expires_at": moment + DEFAULT_TTL,
            "deduplication_key": event.dedup(),
        }
        if timing is not None:
            common["timing_score"] = float(timing)
        if kind == AttentionEventType.GAME_EVENT:
            category = classify_game_event(
                event.summary, priority=event.salience, novelty=event.novelty)
            policy = GAME_POLICY.get(str(category))
            if policy is None:
                continue
            opportunity_type, value, novelty_floor = policy
            if event.novelty < novelty_floor:
                continue
            out.append(InitiativeOpportunity(
                opportunity_type=opportunity_type, expected_value=value,
                reasons=(f"game:{category}",), **common))
        elif kind == AttentionEventType.OBLIGATION_DUE:
            out.append(InitiativeOpportunity(
                opportunity_type=OpportunityType.RESUME_OBLIGATION,
                expected_value=.85, goal_relevance=.8,
                reasons=("unfinished_obligation",), **common))
        elif kind == AttentionEventType.MEMORY_RELEVANCE:
            out.append(InitiativeOpportunity(
                opportunity_type=OpportunityType.SHARE_RELEVANT_MEMORY,
                expected_value=.5, reasons=("relevant_memory",), **common))
        elif kind == AttentionEventType.TASK_PROGRESS:
            out.append(InitiativeOpportunity(
                opportunity_type=OpportunityType.REACT_TO_EVENT,
                expected_value=.6, goal_relevance=.6,
                reasons=("task_progress",), **common))
        elif kind == AttentionEventType.SILENCE_THRESHOLD:
            # **時間そのものは話す理由にならない。**
            # 未完了の約束がある時だけ、その約束の再開として候補になる。
            if not obligations:
                continue
            out.append(InitiativeOpportunity(
                opportunity_type=OpportunityType.RESUME_OBLIGATION,
                expected_value=.6, goal_relevance=.7,
                reasons=("silence_with_open_obligation",), **common))
        elif kind == AttentionEventType.VISUAL_CHANGE:
            # 環境の変化は原則沈黙。よほど目立つ時だけ軽い反応。
            if event.salience < .7 or event.novelty < .7:
                continue
            out.append(InitiativeOpportunity(
                opportunity_type=OpportunityType.ACKNOWLEDGE_CHANGE,
                expected_value=.35, reasons=("notable_change",), **common))
    for item in out:
        # **宛先を種類から決める。** 全部 GROUP のままだと、独り言が
        # 呼びかけと同じ重さで扱われる。
        item.target_scope = scope_for(
            item.opportunity_type, participant_ids=item.participant_ids)
        item.target_participant_ids = item.participant_ids
    return out


# ---------------------------------------------------------------------------
# 内面状態の反映
#
# **強制命令ではなく小さな補正。** 「好奇心が高いから喋る」ではなく、
# 「好奇心が高いから、発見への一言が少し通りやすい」。
# ---------------------------------------------------------------------------

#: 内面が機会1件を動かしてよい上限。行動側の補正（±0.20）より小さくしてある。
#: 二重に効くので、こちらは控えめに。
MAX_INTERNAL_ADJUSTMENT = .12


def apply_internal_state(
    opportunity: InitiativeOpportunity, internal, *, participant_id: str = "",
) -> InitiativeOpportunity:
    """内面から機会へ、小さな補正を掛ける。**破壊的に更新する。**

    ここで足すのは価値とコストだけで、**行動そのものは変えない**。
    「苛立っているから攻撃的な発話を選ぶ」のような経路を作らない。
    """
    if internal is None:
        return opportunity
    try:
        curiosity = internal.value("curiosity", participant_id)
        playfulness = float(internal.social_stance.get("playfulness", .25))
        irritation = internal.value("irritation", participant_id)
        caution = internal.value("caution", participant_id)
        comfort = internal.value("comfort", participant_id)
    except Exception:  # noqa: BLE001 — 内面が読めなくても機会は残す
        return opportunity

    kind = str(opportunity.opportunity_type)
    bonus = .0
    reasons: list[str] = []
    if curiosity > .60 and "game:discovery" in opportunity.reasons:
        # 好奇心は**発見への一言**にだけ効かせる。質問は増やさない。
        bonus += min(MAX_INTERNAL_ADJUSTMENT, .3 * (curiosity - .60))
        reasons.append("curious_about_discovery")
    if playfulness > .45 and comfort > .55 and "game:success" in opportunity.reasons:
        # 遊び心は**安全な成功**への軽口だけ。失敗には効かせない。
        bonus += min(MAX_INTERNAL_ADJUSTMENT, .25 * (playfulness - .45))
        reasons.append("playful_about_success")
    if comfort > .60 and kind == OpportunityType.REACT_TO_EVENT:
        bonus += min(.06, .15 * (comfort - .60))
        reasons.append("comfortable_enough_to_react")

    if irritation > .20:
        # **苛立ちで実況量を増やさない。** 増える方向へは一切効かせない。
        opportunity.social_cost = min(1.0, opportunity.social_cost + min(.35, irritation))
        reasons.append("irritated_speak_less")
        bonus = min(bonus, .0)
    if caution > .20 and opportunity.confidence < .75:
        # 警戒している時に、確信の低い画面認識へ反応しない。
        opportunity.social_cost = min(1.0, opportunity.social_cost + min(.30, caution))
        reasons.append("cautious_about_low_confidence")

    if bonus:
        opportunity.expected_value = min(1.0, opportunity.expected_value + bonus)
    if reasons:
        opportunity.reasons = tuple(dict.fromkeys((*opportunity.reasons, *reasons)))[:6]
    return opportunity


# ---------------------------------------------------------------------------
# 記憶からの機会
#
# **記憶をそのまま突然話さない。** 取得 → 機会 → Action Selector →
# Speech Gate の順に通す。古い記憶を脈絡なく披露するのがいちばん不気味。
# ---------------------------------------------------------------------------

#: 自発発話の材料にしてよい記憶。**この5つだけ。**
MEMORY_INITIATIVE: dict[str, tuple[OpportunityType, float]] = {
    "promise": (OpportunityType.RESUME_OBLIGATION, .85),
    "preference": (OpportunityType.LIGHT_FOLLOW_UP, .45),
    "correction": (OpportunityType.LIGHT_FOLLOW_UP, .45),
    "strong_affect": (OpportunityType.SHARE_RELEVANT_MEMORY, .50),
}
#: 過去の失敗は「思い出して話す」ではなく「**同じ手を打たない**」ために使う。
#: だから機会を作らない——Action Selector 側の減点として既に効いている。
_MEMORY_NEVER_SPEAKS = ("self_failure:",)
#: 現在の状況と結びついていない記憶は候補にしない。
MEMORY_RELEVANCE_FLOOR = .35


def opportunities_from_memories(
    scored, *, topic_id: str = "", participant_ids: tuple[str, ...] = (),
    now: float | None = None, relevance_floor: float = MEMORY_RELEVANCE_FLOOR,
) -> list[InitiativeOpportunity]:
    """想起した記憶から機会を作る。**そのまま喋る経路は作らない。**

    `scored` は `recall.rank()` の結果。関連度が低いものは捨てる——
    **脈絡の無い披露を防ぐのは、ここの閾値**。
    """
    moment = time.monotonic() if now is None else now
    out: list[InitiativeOpportunity] = []
    for item in scored or ():
        memory = getattr(item, "memory", item)
        event_type = str(getattr(memory, "event_type", ""))
        if any(event_type.startswith(prefix) for prefix in _MEMORY_NEVER_SPEAKS):
            continue
        policy = MEMORY_INITIATIVE.get(event_type)
        if policy is None:
            continue
        relevance = float(getattr(item, "score", 0.0))
        if relevance < relevance_floor:
            continue
        opportunity_type, value = policy
        out.append(InitiativeOpportunity(
            opportunity_type=opportunity_type,
            summary=str(getattr(memory, "summary", ""))[:180],
            topic_id=topic_id or (
                memory.topic_ids[0] if getattr(memory, "topic_ids", ()) else ""),
            participant_ids=participant_ids,
            target_participant_ids=participant_ids,
            target_scope=scope_for(opportunity_type, participant_ids=participant_ids),
            source_memory_ids=(int(getattr(memory, "memory_id", 0)),),
            salience=relevance,
            novelty=.4,
            confidence=float(getattr(memory, "confidence", .7)),
            expected_value=value,
            goal_relevance=.7 if event_type == "promise" else .3,
            expires_at=moment + DEFAULT_TTL,
            deduplication_key=f"memory:{getattr(memory, 'memory_id', 0)}",
            reasons=(f"memory:{event_type}",),
        ))
    return out


# ---------------------------------------------------------------------------
# 統合
# ---------------------------------------------------------------------------

#: これだけ近い時間に起きた関連イベントは、1つにまとめる。
MERGE_WINDOW_SECONDS = 6.0


def merge_opportunities(
    opportunities: list[InitiativeOpportunity], *,
    window: float = MERGE_WINDOW_SECONDS, now: float | None = None,
) -> list[InitiativeOpportunity]:
    """近い時間の関連する機会を1つへまとめる。

    「敵が出た」「HPが減った」「避けた」を3回別々に喋らない。
    **新しい総合イベントを優先し、古い候補は吸収する。**
    """
    moment = time.monotonic() if now is None else now
    groups: dict[str, list[InitiativeOpportunity]] = {}
    passthrough: list[InitiativeOpportunity] = []
    for item in opportunities:
        if item.expired(now=moment):
            continue
        # 話題が無いものはまとめようがない。素通しする。
        key = f"{item.opportunity_type}:{item.topic_id}" if item.topic_id else ""
        if not key:
            passthrough.append(item)
            continue
        groups.setdefault(key, []).append(item)

    merged: list[InitiativeOpportunity] = []
    for items in groups.values():
        items.sort(key=lambda x: x.created_at)
        bucket: list[InitiativeOpportunity] = []
        for item in items:
            if bucket and item.created_at - bucket[0].created_at > window:
                merged.append(_fold(bucket))
                bucket = []
            bucket.append(item)
        if bucket:
            merged.append(_fold(bucket))
    return [*passthrough, *merged]


def _fold(items: list[InitiativeOpportunity]) -> InitiativeOpportunity:
    """まとめる。**いちばん新しいものを土台にする。**

    古い方を土台にすると、「さっき敵が出た」で止まって
    「もう避けた」が落ちる。状況は最新が正しい。
    """
    if len(items) == 1:
        return items[0]
    latest = items[-1]
    latest.source_event_ids = tuple(dict.fromkeys(
        eid for item in items for eid in item.source_event_ids))[:8]
    # まとめた分だけ重要度は上がるが、**上限は超えない**。
    latest.salience = min(1.0, max(item.salience for item in items) + .05 * (len(items) - 1))
    latest.expected_value = max(item.expected_value for item in items)
    latest.reasons = tuple(dict.fromkeys(
        (*latest.reasons, f"merged:{len(items)}")))[:5]
    return latest


# ---------------------------------------------------------------------------
# 発話直前の再検証
# ---------------------------------------------------------------------------


def revalidate(
    candidate: ActionCandidate | InitiativeOpportunity,
    conditions: SpeakingConditions, *,
    higher_priority_pending: bool = False, now: float | None = None,
) -> str:
    """TTSへ入れる直前の再確認。**無効なら静かに取り消す。**

    候補を選んでから音声が出るまでの間に、相手が話し始めることも、
    もっと急ぐ出来事が来ることもある。**取り消した発話を後から突然
    再生してはいけない**ので、ここで捨てきる。
    """
    moment = time.monotonic() if now is None else now
    if candidate.expired(now=moment):
        return "expired_before_speaking"
    if conditions.user_speaking:
        return "user_started_speaking"
    if conditions.other_participant_speaking:
        return "another_participant_started_speaking"
    if higher_priority_pending:
        return "higher_priority_event_arrived"
    if conditions.topic_closed:
        return "topic_closed_meanwhile"
    dedup = candidate.dedup() if isinstance(candidate, InitiativeOpportunity) else ""
    if dedup and dedup in set(conditions.spoken_dedup_keys):
        return "already_spoken_by_another_path"
    events = set(getattr(candidate, "source_event_ids", ()) or ())
    if events & set(conditions.handled_event_ids):
        return "event_handled_by_another_path"
    return ""


__all__ = [
    "CLOSURE_QUIET_SECONDS", "DEFAULT_TTL", "FAREWELL_TTL", "GAME_POLICY",
    "GREETING_REPEAT_WINDOW", "GREETING_TTL", "INITIATIVE_WEIGHTS",
    "MASS_JOIN_THRESHOLD", "MIN_INITIATIVE_CONFIDENCE", "OPPORTUNITY_ACTION",
    "RECONNECT_WINDOW", "SOURCE_PROACTIVE",
    "GameEventCategory", "InitiativeBudget", "InitiativeOpportunity",
    "OpportunityType", "SpeakingConditions", "WarnThrottle",
    "classify_game_event", "farewell_opportunity", "greeting_opportunity",
    "merge_opportunities", "opportunities_from_events",
    "revalidate", "silent_opportunity", "suppression_reason",
]
