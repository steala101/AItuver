"""共有している目標と、未完了の約束。**自分で勝手に目標を作らない。**

ポッポが持ってよい目標は、**ユーザーと結びついたものだけ**。
「もっと賢くなりたい」「話し相手を増やしたい」のような自前の長期欲求は
作らない。作れる仕組みにしておくと、いつか作る。

`GoalRecord` は Phase 5 の `InitiativeOpportunity` を置き換えない。
目標は「何を目指しているか」、機会は「いま話す価値があるか」。
**目標から直接発話しない**——必ず機会にしてから Action Selector を通す。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.types import ActionType


class GoalType(StrEnum):
    """**この4種だけ。** どれもユーザーと結びついている。"""

    USER_GOAL = "user_goal"
    SHARED_GOAL = "shared_goal"
    #: 自分が明示的に引き受けた約束。
    ASSISTANT_COMMITMENT = "assistant_commitment"
    #: ゲームプロフィールが定義している現在の目的。
    GAME_OBJECTIVE = "game_objective"


class GoalStatus(StrEnum):
    #: 提案されたが、まだ承認されていない。**ここから勝手に進まない。**
    PROPOSED = "proposed"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


@dataclass(slots=True)
class GoalRecord:
    """1つの目標。**必ず持ち主がいる。**"""

    description: str
    goal_type: GoalType | str = GoalType.USER_GOAL
    owner_ids: tuple[str, ...] = ()
    status: GoalStatus | str = GoalStatus.PROPOSED
    priority: float = .5
    confidence: float = .6
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    source_event_ids: tuple[str, ...] = ()
    related_entity_ids: tuple[str, ...] = ()
    related_memory_ids: tuple[int, ...] = ()
    #: 完了と判断してよい条件。**満たされない限り COMPLETED にしない。**
    completion_conditions: tuple[str, ...] = ()
    blocked_reason: str = ""
    #: 最後に助言した時刻。同じ助言を繰り返さないため。
    last_advice_at: float = .0
    #: 既存の未完了事項（`WorkingMemory.open_questions`）との橋。
    #: **既存Obligationを置き換えない。** 双方向に辿れるようにするだけ。
    obligation_ids: tuple[str, ...] = ()
    #: どう終わったか。**`COMPLETED` は「終わった」であって「成功した」ではない。**
    #:
    #: 失敗用の状態を足すと、いま状態を見ている全ての場所が
    #: 「COMPLETED だけ見ればよい」から「COMPLETED か FAILED か」へ変わる。
    #: 終わったかどうかは状態、うまくいったかは結果——分けておく方が壊れない。
    #: 空文字は「まだ終わっていない」または「結果が分からない」。
    outcome: str = ""
    goal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def live(self) -> bool:
        return str(self.status) in {str(GoalStatus.ACTIVE), str(GoalStatus.BLOCKED)}

    @property
    def succeeded(self) -> bool | None:
        """うまくいったか。**分からない時は `None`。**"""
        if not self.outcome:
            return None
        return self.outcome not in {"failed", "exploded", "gave_up"}

    def snapshot(self) -> dict[str, Any]:
        """トレース用。**本文は要約のみ**（第12条）。"""
        return {
            "goal_id": self.goal_id,
            "type": str(self.goal_type),
            "status": str(self.status),
            "priority": round(float(self.priority), 2),
            "confidence": round(float(self.confidence), 2),
            "owners": list(self.owner_ids[:3]),
            "blocked": self.blocked_reason[:40],
            "outcome": self.outcome[:20],
            "chars": len(self.description),
        }


# ---------------------------------------------------------------------------
# 登録の判断
# ---------------------------------------------------------------------------


class AdmissionDecision(StrEnum):
    ADMIT = "admit"
    #: 提案として置くが、**承認されるまで動かさない。**
    HOLD_FOR_APPROVAL = "hold_for_approval"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    decision: AdmissionDecision
    status: GoalStatus
    reasons: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {"decision": str(self.decision), "status": str(self.status),
                "reasons": list(self.reasons)}


#: 目標にしてよい出どころ。**ここに無いものは ACTIVE にしない。**
ADMISSIBLE_SOURCES = frozenset({
    "user_stated",          # ユーザーが明示した
    "user_approved",        # こちらの提案をユーザーが承認した
    "explicit_collaboration",  # 明示的な共同作業
    "assistant_promise",    # 自分が明示的に引き受けた約束
    "game_profile",         # ゲームプロフィールが定義している
})
#: 目標にしない出どころ。
_NEVER = frozenset({
    "small_talk", "inferred_life_goal", "psychological_inference",
    "single_visual_change", "self_generated_desire",
})
#: 目標として成立する最低限の確からしさ。
MIN_GOAL_CONFIDENCE = .6


class GoalAdmissionGate:
    """**目標を登録してよいかを決める唯一の場所。**

    散らばると、どこかで「推測した目標」が ACTIVE になる。
    """

    def __init__(self, *, min_confidence: float = MIN_GOAL_CONFIDENCE,
                 max_active: int = 5) -> None:
        self.min_confidence = float(min_confidence)
        self.max_active = int(max_active)

    def evaluate(
        self, goal: GoalRecord, *, source: str = "",
        active_count: int = 0, approved: bool = False,
    ) -> AdmissionVerdict:
        if not str(goal.description or "").strip():
            return AdmissionVerdict(AdmissionDecision.REJECT, GoalStatus.ABANDONED,
                                    ("empty",))
        if source in _NEVER:
            # **推測した人生目標や、AI自身の欲求は目標にしない。**
            return AdmissionVerdict(AdmissionDecision.REJECT, GoalStatus.ABANDONED,
                                    (f"source_not_allowed:{source}",))
        if not goal.owner_ids and str(goal.goal_type) != str(GoalType.GAME_OBJECTIVE):
            # 持ち主のいない目標は、事実上ポッポ自身の目標になる。
            return AdmissionVerdict(AdmissionDecision.REJECT, GoalStatus.ABANDONED,
                                    ("no_owner",))
        if source not in ADMISSIBLE_SOURCES:
            # 出どころが分からないものは、**提案のまま置く。**
            return AdmissionVerdict(
                AdmissionDecision.HOLD_FOR_APPROVAL, GoalStatus.PROPOSED,
                ("source_unverified",))
        if _clamp(goal.confidence) < self.min_confidence and not approved:
            return AdmissionVerdict(
                AdmissionDecision.HOLD_FOR_APPROVAL, GoalStatus.PROPOSED,
                ("low_confidence",))
        if active_count >= self.max_active:
            # 抱えすぎると、どれも進まない。
            return AdmissionVerdict(
                AdmissionDecision.HOLD_FOR_APPROVAL, GoalStatus.PROPOSED,
                ("too_many_active",))
        return AdmissionVerdict(AdmissionDecision.ADMIT, GoalStatus.ACTIVE, (source,))


#: 未完了事項 → 目標の状態。**片方を直したらもう片方へ反映する。**
OBLIGATION_TO_GOAL: dict[str, GoalStatus] = {
    "pending": GoalStatus.ACTIVE,
    "blocked": GoalStatus.BLOCKED,
    "fulfilled": GoalStatus.COMPLETED,
    "resolved": GoalStatus.COMPLETED,
    "cancelled": GoalStatus.ABANDONED,
}
#: 目標 → 未完了事項の状態。逆向き。
GOAL_TO_OBLIGATION: dict[str, str] = {
    GoalStatus.ACTIVE: "pending",
    GoalStatus.PAUSED: "pending",
    GoalStatus.BLOCKED: "blocked",
    GoalStatus.COMPLETED: "fulfilled",
    GoalStatus.ABANDONED: "cancelled",
}


# ---------------------------------------------------------------------------
# 権限
# ---------------------------------------------------------------------------


class Permission(StrEnum):
    """やってよいか。**提案と実行を分ける。**

    `AUTOMATIC` / `NEEDS_APPROVAL` は**分類した後の最終結果**であって、
    分類そのものではない。何でも `AUTOMATIC` にしてよいという意味ではない。
    """

    #: 内部で完結する。勝手にやってよい。
    AUTOMATIC = "automatic"
    #: 提案までは自由。実行にはユーザーの許可が要る。
    NEEDS_APPROVAL = "needs_approval"


class ActionCategory(StrEnum):
    """行動の種類。**取り返しがつくかどうかで分けてある。**"""

    #: 内部状態・記憶検索・状況整理。やり直せる。
    INTERNAL = "internal"
    #: 発話。Action Selector と Speech Gate が決める。
    SPEECH = "speech"
    #: ゲーム内の助言。言うだけで、操作はしない。
    ADVICE_ONLY = "advice_only"
    #: 外部の読み取り。既存の設定に従う。
    EXTERNAL_READ = "external_read"
    #: 外部への書き込み。**明示的な承認が要る。**
    EXTERNAL_WRITE = "external_write"
    #: 取り返しのつかない操作。**承認＋対象の確認が要る。**
    DESTRUCTIVE = "destructive"
    #: 人へ届くもの。**宛先の確認が要る。**
    PERSON_DIRECTED = "person_directed"


class PermissionResult(StrEnum):
    ALLOW_AUTOMATIC = "allow_automatic"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


#: 種類 → 判定。**分からないものは `REQUIRE_CONFIRMATION`。**
#:
#: 「たぶん大丈夫」で自動実行する経路を作らない。判断がつかないなら聞く。
CATEGORY_POLICY: dict[str, PermissionResult] = {
    ActionCategory.INTERNAL: PermissionResult.ALLOW_AUTOMATIC,
    ActionCategory.SPEECH: PermissionResult.ALLOW_AUTOMATIC,
    ActionCategory.ADVICE_ONLY: PermissionResult.ALLOW_AUTOMATIC,
    ActionCategory.EXTERNAL_READ: PermissionResult.ALLOW_AUTOMATIC,
    ActionCategory.EXTERNAL_WRITE: PermissionResult.REQUIRE_CONFIRMATION,
    ActionCategory.DESTRUCTIVE: PermissionResult.REQUIRE_CONFIRMATION,
    ActionCategory.PERSON_DIRECTED: PermissionResult.REQUIRE_CONFIRMATION,
}
#: 対象の確認まで要る種類。**「何を消すのか」を確かめずに消さない。**
NEEDS_TARGET_CHECK = frozenset({
    ActionCategory.DESTRUCTIVE, ActionCategory.PERSON_DIRECTED,
})

#: 行動名 → 種類。ここに無い行動は**分からない**扱い。
ACTION_CATEGORY: dict[str, ActionCategory] = {
    "update_internal_state": ActionCategory.INTERNAL,
    "search_memory": ActionCategory.INTERNAL,
    "organise_situation": ActionCategory.INTERNAL,
    "recall_obligation": ActionCategory.INTERNAL,
    "propose_utterance": ActionCategory.SPEECH,
    "game_advice": ActionCategory.ADVICE_ONLY,
    "suggest_next_step": ActionCategory.ADVICE_ONLY,
    "read_screen": ActionCategory.EXTERNAL_READ,
    "web_search": ActionCategory.EXTERNAL_READ,
    "persist_setting": ActionCategory.EXTERNAL_WRITE,
    "modify_file": ActionCategory.EXTERNAL_WRITE,
    "external_request": ActionCategory.EXTERNAL_WRITE,
    "purchase": ActionCategory.EXTERNAL_WRITE,
    "post": ActionCategory.PERSON_DIRECTED,
    "delete_file": ActionCategory.DESTRUCTIVE,
    "game_input": ActionCategory.DESTRUCTIVE,
    "send_message": ActionCategory.PERSON_DIRECTED,
    "send_email": ActionCategory.PERSON_DIRECTED,
    "contact_other_person": ActionCategory.PERSON_DIRECTED,
}
#: 次の小さな行動 → 種類。
NEXT_ACTION_CATEGORY: dict[str, ActionCategory] = {
    "observe": ActionCategory.INTERNAL,
    "verify": ActionCategory.SPEECH,
    "ask_clarification": ActionCategory.SPEECH,
    "comment": ActionCategory.SPEECH,
    "remind": ActionCategory.SPEECH,
    "resume_task": ActionCategory.SPEECH,
    "suggest_next_step": ActionCategory.ADVICE_ONLY,
    "wait": ActionCategory.INTERNAL,
    "remain_silent": ActionCategory.INTERNAL,
}


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """許可の判定1つ。**Planner の文章ではなく、コードが決める。**"""

    action_id: str
    action_category: ActionCategory | str
    result: PermissionResult | str
    reason: str = ""
    requires_confirmation: bool = False
    requires_target_check: bool = False
    target_ids: tuple[str, ...] = ()
    decided_at: float = field(default_factory=time.monotonic)
    #: 判定そのものに掛かった時間。**安全側の判断が遅いと迂回されたくなる。**
    decided_ms: float = .0

    @property
    def automatic(self) -> bool:
        return str(self.result) == str(PermissionResult.ALLOW_AUTOMATIC)

    @property
    def permission(self) -> Permission:
        return Permission.AUTOMATIC if self.automatic else Permission.NEEDS_APPROVAL

    def snapshot(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "category": str(self.action_category),
            "result": str(self.result),
            "reason": self.reason[:60],
            "requires_confirmation": self.requires_confirmation,
            "requires_target_check": self.requires_target_check,
            "targets": list(self.target_ids[:3]),
        }


def categorise(action: str) -> ActionCategory | None:
    """行動の種類。**知らない行動は `None`**（分からないと言う）。"""
    value = str(action or "").strip()
    return ACTION_CATEGORY.get(value) or NEXT_ACTION_CATEGORY.get(value)


def decide_permission(
    action: str, *, target_ids: tuple[str, ...] = (), action_id: str = "",
    category: ActionCategory | str | None = None,
) -> PermissionDecision:
    """やってよいか。**分からなければ確認を求める。**

    ここが `ALLOW_AUTOMATIC` を既定にしていると、新しい行動を足すたびに
    黙って自動実行されるようになる。既定は確認側。

    `category` を渡せるのは Phase 7 のツール用。ツール側は行動名ではなく
    副作用の分類を持っているので、**同じ判定表をそのまま使う**ため。
    ここを別関数にすると、判定の場所が2つになって必ず食い違う。
    """
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 4)

    if category is None:
        category = categorise(action)
    else:
        try:
            category = ActionCategory(str(category))
        except ValueError:
            category = None
    identifier = action_id or str(action or "unknown")
    if category is None:
        return PermissionDecision(
            action_id=identifier, action_category=ActionCategory.EXTERNAL_WRITE,
            result=PermissionResult.REQUIRE_CONFIRMATION,
            reason="unknown_action", requires_confirmation=True,
            target_ids=tuple(target_ids), decided_ms=elapsed())
    result = CATEGORY_POLICY.get(str(category), PermissionResult.REQUIRE_CONFIRMATION)
    needs_target = category in NEEDS_TARGET_CHECK
    if needs_target and not target_ids:
        # **何に対してやるのか分からないまま実行しない。**
        return PermissionDecision(
            action_id=identifier, action_category=category,
            result=PermissionResult.REQUIRE_CONFIRMATION,
            reason="target_unknown", requires_confirmation=True,
            requires_target_check=True, decided_ms=elapsed())
    return PermissionDecision(
        action_id=identifier, action_category=category, result=result,
        reason=str(category),
        requires_confirmation=result is PermissionResult.REQUIRE_CONFIRMATION,
        requires_target_check=needs_target, target_ids=tuple(target_ids),
        decided_ms=elapsed())


#: 明示的な許可なしに実行してはいけない行動。
#:
#: **取り返しがつくかどうか**で分けてある。内部状態や記憶検索はやり直せるが、
#: 送信・削除・購入・操作はやり直せない。
RESTRICTED_ACTIONS = frozenset({
    "send_message", "send_email", "post", "purchase", "delete_file",
    "game_input", "persist_setting", "contact_other_person",
    "external_request", "modify_file",
})
#: 内部で完結し、いつでもやり直せる行動。
AUTOMATIC_ACTIONS = frozenset({
    "update_internal_state", "search_memory", "organise_situation",
    "propose_utterance", "game_advice", "recall_obligation",
})


def permission_for(action: str) -> Permission:
    """その行動に許可が要るか。**知らない行動は許可が要る側へ倒す。**"""
    value = str(action or "").strip()
    if value in AUTOMATIC_ACTIONS:
        return Permission.AUTOMATIC
    return Permission.NEEDS_APPROVAL


def requires_approval(action: str) -> bool:
    return permission_for(action) is Permission.NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# 次の小さな行動
# ---------------------------------------------------------------------------

#: 目標から出してよい行動。**長い計画は作らない。**
NEXT_ACTIONS: dict[str, ActionType] = {
    "observe": ActionType.COMMENT,
    "comment": ActionType.COMMENT,
    "ask_clarification": ActionType.ASK_CLARIFICATION,
    "verify": ActionType.ASK_CLARIFICATION,
    "remind": ActionType.REMIND,
    "resume_task": ActionType.RESUME_OBLIGATION,
    "suggest_next_step": ActionType.COMMENT,
    "wait": ActionType.REMAIN_SILENT,
    "remain_silent": ActionType.REMAIN_SILENT,
}
#: 一度に扱う行動の数。**長い計画を毎ターン作らない。**
MAX_NEXT_ACTIONS = 3
#: 同じ助言を繰り返さない間隔（秒）。
ADVICE_COOLDOWN = 60.0


@dataclass(frozen=True, slots=True)
class NextAction:
    """目標から出た、次の小さな1手。**これも発話ではない。**"""

    kind: str
    goal_id: str = ""
    action_type: ActionType = ActionType.REMAIN_SILENT
    reason: str = ""
    confidence: float = .6
    #: **固定ではなく、種類から判定した結果。**
    #: 以前はここが常に `AUTOMATIC` で、権限の仕組みが死んでいた。
    decision: PermissionDecision | None = None
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def permission(self) -> Permission:
        if self.decision is None:
            # 判定が無いなら**確認が要る側**へ倒す。
            return Permission.NEEDS_APPROVAL
        return self.decision.permission

    @property
    def category(self) -> ActionCategory | str:
        return self.decision.action_category if self.decision else ActionCategory.EXTERNAL_WRITE

    def snapshot(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind, "goal": self.goal_id,
            "action": str(self.action_type), "reason": self.reason,
            "permission": str(self.permission),
            "decision": self.decision.snapshot() if self.decision else None,
        }


def _with_permission(action: NextAction) -> NextAction:
    """行動へ許可の判定を付ける。**固定の `AUTOMATIC` を作らない。**"""
    import dataclasses

    return dataclasses.replace(
        action, decision=decide_permission(action.kind, action_id=action.action_id))


def next_actions(
    goals: list[GoalRecord] | tuple[GoalRecord, ...], *,
    fact_confidence: float = 1.0, end_signal: float = .0,
    now: float | None = None,
) -> list[NextAction]:
    """いまの目標から、次の1〜3手を出す。**長い計画は作らない。**

    打ち切りの合図が出ていれば、**目標が ACTIVE でも何も出さない**。
    目標は会話を続ける理由にならない。
    """
    if end_signal > .5:
        return []
    moment = time.monotonic() if now is None else now
    out: list[NextAction] = []
    for goal in sorted(goals, key=lambda item: -item.priority):
        status = str(goal.status)
        if status == str(GoalStatus.BLOCKED):
            out.append(_with_permission(NextAction(
                kind="ask_clarification", goal_id=goal.goal_id,
                action_type=ActionType.ASK_CLARIFICATION,
                reason=f"blocked:{goal.blocked_reason}"[:60],
                confidence=goal.confidence)))
        elif status == str(GoalStatus.ACTIVE):
            if moment - goal.last_advice_at < ADVICE_COOLDOWN:
                # **同じ助言を続けない。**
                continue
            out.append(_with_permission(NextAction(
                kind="resume_task", goal_id=goal.goal_id,
                action_type=ActionType.RESUME_OBLIGATION,
                reason="active_goal", confidence=goal.confidence)))
        elif status == str(GoalStatus.PAUSED):
            # **中断中に勝手に再開しない。** 関連状況が来た時だけ
            # 呼び出し側が起こす（`resume_candidates`）。
            continue
        if len(out) >= MAX_NEXT_ACTIONS:
            break
    if fact_confidence < .55:
        # 根拠が薄いなら、断定より確かめる方へ。
        out.insert(0, _with_permission(NextAction(
            kind="verify", action_type=ActionType.ASK_CLARIFICATION,
            reason="low_fact_confidence", confidence=fact_confidence)))
    return out[:MAX_NEXT_ACTIONS]


def resume_candidates(
    goals: list[GoalRecord] | tuple[GoalRecord, ...], *,
    topic_ids: tuple[str, ...] = (), entity_ids: tuple[str, ...] = (),
) -> list[GoalRecord]:
    """中断した目標のうち、**いまの状況に関係するものだけ**。

    関係なく再開すると、話の途中で突然前の作業へ戻る。
    """
    topics = {str(item) for item in topic_ids if item}
    entities = {str(item) for item in entity_ids if item}
    out: list[GoalRecord] = []
    for goal in goals:
        if str(goal.status) != str(GoalStatus.PAUSED):
            continue
        if entities & set(goal.related_entity_ids):
            out.append(goal)
            continue
        if topics and any(topic in goal.description for topic in topics):
            out.append(goal)
    return out


# ---------------------------------------------------------------------------
# 完了の判断
# ---------------------------------------------------------------------------

#: 完了と断定するのに要る確信。**不確実な認識で完了にしない。**
COMPLETION_CONFIDENCE = .8


def mark_completed(
    goal: GoalRecord, *, evidence: str = "", confidence: float = .0,
    now: float | None = None,
) -> bool:
    """完了にしてよいか。**証拠と確信が足りなければ変えない。**

    完了を早まると、まだ終わっていない作業の助言が止まる。
    """
    if _clamp(confidence) < COMPLETION_CONFIDENCE or not evidence:
        return False
    if goal.completion_conditions and not any(
            condition in evidence for condition in goal.completion_conditions):
        return False
    goal.status = GoalStatus.COMPLETED
    goal.updated_at = time.monotonic() if now is None else now
    return True


def pause_for(goal: GoalRecord, reason: str, *, now: float | None = None) -> None:
    """中断する。**破棄はしない。**"""
    if str(goal.status) == str(GoalStatus.ACTIVE):
        goal.status = GoalStatus.PAUSED
        goal.blocked_reason = str(reason)[:80]
        goal.updated_at = time.monotonic() if now is None else now


# ---------------------------------------------------------------------------
# 行動選択への影響
# ---------------------------------------------------------------------------

#: 目標が行動候補を動かしてよい上限。**上書きではない。**
MAX_GOAL_BIAS = .15


def goal_bias(
    goals: list[GoalRecord] | tuple[GoalRecord, ...], *,
    fact_confidence: float = 1.0, end_signal: float = .0,
) -> dict[str, float]:
    """目標から行動候補への**小さな**補正。

    **打ち切りの合図が出ていれば何も足さない。** 目標が ACTIVE でも、
    会話を続ける理由にはならない。
    """
    if end_signal > .5:
        return {}
    adjustments: dict[str, float] = {}

    def add(action: ActionType, amount: float) -> None:
        key = str(action)
        total = adjustments.get(key, .0) + amount
        adjustments[key] = max(-MAX_GOAL_BIAS, min(MAX_GOAL_BIAS, total))

    for goal in goals:
        status = str(goal.status)
        weight = _clamp(goal.priority) * _clamp(goal.confidence)
        if status == str(GoalStatus.ACTIVE):
            add(ActionType.RESUME_OBLIGATION, .10 * weight)
            add(ActionType.CONTINUE_PREVIOUS_TOPIC, .06 * weight)
        elif status == str(GoalStatus.BLOCKED):
            add(ActionType.ASK_CLARIFICATION, .10 * weight)
            add(ActionType.REMAIN_SILENT, .04 * weight)
        elif status == str(GoalStatus.COMPLETED):
            # **終わった話を続けない。**
            add(ActionType.RESUME_OBLIGATION, -.12)
            add(ActionType.CONTINUE_PREVIOUS_TOPIC, -.08)
    if fact_confidence < .55:
        add(ActionType.ANSWER, -.10)
        add(ActionType.ASK_CLARIFICATION, .10)
    return {k: round(v, 4) for k, v in adjustments.items() if abs(v) >= .005}


__all__ = [
    "ADMISSIBLE_SOURCES", "ADVICE_COOLDOWN", "AUTOMATIC_ACTIONS",
    "COMPLETION_CONFIDENCE", "MAX_GOAL_BIAS", "MAX_NEXT_ACTIONS",
    "MIN_GOAL_CONFIDENCE", "NEXT_ACTIONS", "RESTRICTED_ACTIONS",
    "AdmissionDecision", "AdmissionVerdict", "GoalAdmissionGate", "GoalRecord",
    "GoalStatus", "GoalType", "NextAction", "Permission", "goal_bias",
    "mark_completed", "next_actions", "pause_for", "permission_for",
    "requires_approval", "resume_candidates",
]
