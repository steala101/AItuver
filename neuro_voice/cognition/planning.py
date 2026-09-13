"""短い計画と、やろうとしていることの宣言。**長い計画は作らない。**

`GoalRecord` は「何を目指しているか」で、`BoundedPlan` は
「そのために次の1〜3手で何をするか」。ここを無制限にすると、
黙って動き続ける存在になる——**3手で終わらない用事は、
そもそも一度ユーザーへ返した方がいい。**

`ActionIntent` と `ToolExecutionRequest` を分けてあるのが要点。
意図は「こうしたい」であって、実行の許可ではない。混ぜると、
意図を作った時点で実行が確定してしまい、間に確認を挟めなくなる。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.goals import (
    GoalStatus, PermissionResult, decide_permission,
)
from neuro_voice.cognition.tools import (
    EffectCategory, NO_SIDE_EFFECT, ToolOperation, ToolRegistry,
    canonical, normalise_parameters,
)
from neuro_voice.cognition.types import ActionCandidate, ActionType

#: **4手以上の計画は作らない**（第33項）。
MAX_PLAN_STEPS = 3
#: 作り直してよい回数。0 だと1回の失敗で諦め、多いと粘り続ける。
MAX_REPLANS = 1
#: 身元が確かでないと外部副作用を許さない解決状態。
_KNOWN_IDENTITY = frozenset({"authoritative", "confirmed"})


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# 意図
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """やろうとしていること1つ。**これ自体は何も起こさない。**"""

    tool_id: str
    operation_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    effect_category: EffectCategory | str = EffectCategory.READ_ONLY
    goal_id: str = ""
    plan_id: str = ""
    step_id: str = ""
    #: 誰のために。**「誰か」ではなく、解決済みの `person_id`。**
    requester_person_id: str = ""
    requester_resolution: str = "unknown"
    # -- どこでの話か (Phase 7B) ------------------------------------------
    #
    # **Local の結果を Discord へ返さない。別チャンネルへも返さない。**
    # ここを持たせないと、確認と報告の宛先が「いま繋がっている方」に
    # なってしまい、頼んでいない人のところへ結果が出る。
    conversation_id: str = ""
    channel_id: str = ""
    #: `local` / `discord`。出口のアダプタを選ぶためだけに使う。
    origin: str = "local"
    #: なぜそうしたいか。**短い要約だけ。会話本文は入れない**（第12条）。
    rationale_summary: str = ""
    target_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=_now)
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def requester_known(self) -> bool:
        return str(self.requester_resolution) in _KNOWN_IDENTITY

    @property
    def side_effect_free(self) -> bool:
        return str(self.effect_category) in {
            str(item) for item in NO_SIDE_EFFECT}

    def fingerprint(self, parameters: dict[str, Any] | None = None) -> str:
        """確認と冪等キーの素。**引数まで含める**（第11項）。

        **依頼者と会話も含める**（Phase 7B）。同じ引数でも、別の人・別の
        チャンネルからの依頼は別の操作。含めないと、片方で取った確認が
        もう片方へ流用できてしまう。
        """
        payload = self.parameters if parameters is None else parameters
        return "|".join((str(self.tool_id), str(self.operation_id),
                         str(self.effect_category), canonical(payload),
                         ",".join(sorted(self.target_ids)),
                         str(self.requester_person_id),
                         str(self.conversation_id), str(self.channel_id)))

    def snapshot(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id, "tool_id": self.tool_id,
            "operation_id": self.operation_id,
            "effect": str(self.effect_category),
            "requester": self.requester_person_id,
            "resolution": self.requester_resolution,
            "goal_id": self.goal_id, "step_id": self.step_id,
        }


# ---------------------------------------------------------------------------
# 計画
# ---------------------------------------------------------------------------


class StepStatus(StrEnum):
    PENDING = "pending"
    #: 確認待ち。**ここで止まっているのは失敗ではない。**
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    #: やってみたが、効いたかどうか分からない。**完了にしない。**
    UNVERIFIED = "unverified"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ADMITTED = "admitted"
    RUNNING = "running"
    #: 中断。再開できる。
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_PLAN = frozenset({
    PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED})


@dataclass(slots=True)
class PlanStep:
    intent: ActionIntent
    index: int = 0
    status: StepStatus | str = StepStatus.PENDING
    #: 直前の Step が終わってから実行するか。既定は順番どおり。
    depends_on: str = ""
    execution_id: str = ""
    confirmation_id: str = ""
    failure_reason: str = ""
    step_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    @property
    def done(self) -> bool:
        return str(self.status) in {
            str(StepStatus.COMPLETED), str(StepStatus.CANCELLED)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id, "index": self.index,
            "status": str(self.status), "tool_id": self.intent.tool_id,
            "operation_id": self.intent.operation_id,
            "effect": str(self.intent.effect_category),
            "failure": self.failure_reason[:60],
        }


@dataclass(slots=True)
class BoundedPlan:
    """1〜3手。**それ以上は計画ではなく自律行動になる。**"""

    goal_id: str
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus | str = PlanStatus.DRAFT
    requester_person_id: str = ""
    replan_count: int = 0
    max_replans: int = MAX_REPLANS
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    closed_reason: str = ""
    #: 作り直した元の計画。**元の user goal を保つための紐**（Phase 7B）。
    replan_of: str = ""
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def active(self) -> bool:
        return str(self.status) not in {str(item) for item in _TERMINAL_PLAN}

    @property
    def current(self) -> PlanStep | None:
        """次にやる1手。**先の Step を並列で走らせない。**"""
        for step in self.steps:
            if step.done:
                continue
            return step
        return None

    def step(self, step_id: str) -> PlanStep | None:
        for item in self.steps:
            if item.step_id == str(step_id):
                return item
        return None

    def close(self, status: PlanStatus, reason: str = "") -> None:
        self.status = status
        self.closed_reason = str(reason)[:80]
        self.updated_at = _now()
        for step in self.steps:
            if not step.done and str(step.status) != str(StepStatus.FAILED):
                step.status = StepStatus.CANCELLED

    def snapshot(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, "goal_id": self.goal_id,
            "status": str(self.status), "replans": self.replan_count,
            "requester": self.requester_person_id,
            "steps": [item.snapshot() for item in self.steps],
            "closed_reason": self.closed_reason,
        }


def build_plan(
    *, goal_id: str, intents: list[ActionIntent] | tuple[ActionIntent, ...],
    requester_person_id: str = "", max_replans: int = MAX_REPLANS,
) -> BoundedPlan:
    """意図の並びから計画を1つ。**上限は切り捨てず、後で拒否させる。**

    ここで黙って3手へ詰めると、「4手目を落とした」ことが誰にも
    伝わらない。長すぎる計画は Admission Gate で断る。
    """
    plan = BoundedPlan(goal_id=str(goal_id),
                       requester_person_id=str(requester_person_id),
                       max_replans=int(max_replans))
    for index, intent in enumerate(intents):
        step = PlanStep(intent=intent, index=index)
        step.intent = replace(intent, plan_id=plan.plan_id,
                              step_id=step.step_id, goal_id=str(goal_id))
        plan.steps.append(step)
    return plan


# ---------------------------------------------------------------------------
# 受け入れ判定
# ---------------------------------------------------------------------------


class AdmissionVerdict(StrEnum):
    ACCEPT = "accept"
    REQUIRE_CLARIFICATION = "require_clarification"
    REQUIRE_CONFIRMATION = "require_confirmation"
    REJECT = "reject"
    #: いまは通せないが、条件が変われば通る。**捨てない。**
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class PlanAdmission:
    verdict: AdmissionVerdict | str
    reason: str = ""
    blockers: tuple[str, ...] = ()
    #: 確認が要る Step。ここが空でない限り実行しない。
    confirmation_steps: tuple[str, ...] = ()
    decided_ms: float = .0

    @property
    def accepted(self) -> bool:
        return str(self.verdict) == str(AdmissionVerdict.ACCEPT)

    def snapshot(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict), "reason": self.reason[:80],
            "blockers": list(self.blockers[:6]),
            "confirmation_steps": list(self.confirmation_steps[:3]),
        }


class PlanAdmissionGate:
    """計画を通してよいか。**判断は Planner の文章ではなくここが決める。**

    順番に意味がある。**「作れない計画」を先に落とす**——身元や確認の
    話は、そもそも実行できる形になってからでないと意味がない。
    """

    def __init__(
        self, registry: ToolRegistry, *, enabled: bool = False,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        self.registry = registry
        self.enabled = bool(enabled)
        self.max_steps = int(max_steps)

    def admit(
        self, plan: BoundedPlan, *, goal_status: Any = GoalStatus.ACTIVE,
        pending_confirmations: int = 0,
    ) -> PlanAdmission:
        started = time.perf_counter()

        def done(verdict: AdmissionVerdict, reason: str,
                 blockers: tuple[str, ...] = (),
                 confirmation_steps: tuple[str, ...] = ()) -> PlanAdmission:
            return PlanAdmission(
                verdict=verdict, reason=reason, blockers=blockers,
                confirmation_steps=confirmation_steps,
                decided_ms=round((time.perf_counter() - started) * 1000, 4))

        if not self.enabled:
            return done(AdmissionVerdict.REJECT, "planning_disabled")
        if not plan.steps:
            return done(AdmissionVerdict.REJECT, "empty_plan")
        if len(plan.steps) > self.max_steps:
            return done(AdmissionVerdict.REJECT, "too_many_steps",
                        (f"steps:{len(plan.steps)}>{self.max_steps}",))
        if str(goal_status) not in {str(GoalStatus.ACTIVE),
                                    str(GoalStatus.BLOCKED)}:
            return done(AdmissionVerdict.REJECT, "goal_not_active",
                        (f"goal_status:{goal_status}",))

        blockers: list[str] = []
        clarify: list[str] = []
        confirm: list[str] = []

        for step in plan.steps:
            intent = step.intent
            tool, operation = self.registry.resolve(
                intent.tool_id, intent.operation_id)
            if tool is None:
                blockers.append(f"tool_not_registered:{intent.tool_id}")
                continue
            if operation is None:
                blockers.append(f"operation_not_allowed:{intent.operation_id}")
                continue
            if not tool.enabled:
                blockers.append(f"tool_disabled:{intent.tool_id}")
                continue
            # **宣言と実物が食い違ったら通さない。** 意図の側で
            # `read_only` と名乗れば読み取りになる、という形にしない。
            if str(intent.effect_category) != str(operation.effect_category):
                blockers.append(f"effect_mismatch:{step.step_id}")
                continue
            normalised = normalise_parameters(operation.parameters,
                                              intent.parameters)
            if not normalised.valid:
                if normalised.missing_only:
                    clarify.append(f"{step.step_id}:{normalised.errors[0]}")
                else:
                    blockers.append(f"parameter_invalid:{normalised.errors[0]}")
                continue
            if operation.needs_target and not (
                    normalised.target_ids or intent.target_ids):
                blockers.append(f"target_unknown:{step.step_id}")
                continue
            decision = decide_permission(
                intent.operation_id,
                category=operation.action_category,
                target_ids=normalised.target_ids or intent.target_ids,
                action_id=step.step_id)
            if str(decision.result) == str(PermissionResult.DENY):
                blockers.append(f"permission_denied:{step.step_id}")
                continue
            if decision.requires_confirmation:
                # **身元が分からない相手のために外部副作用を起こさない。**
                if not intent.requester_known:
                    blockers.append(f"requester_unknown:{step.step_id}")
                    continue
                confirm.append(step.step_id)

        if blockers:
            return done(AdmissionVerdict.REJECT, blockers[0], tuple(blockers))
        if clarify:
            return done(AdmissionVerdict.REQUIRE_CLARIFICATION, clarify[0],
                        tuple(clarify))
        if confirm:
            if pending_confirmations:
                # **同時に複数の確認を並べない**（第13項）。
                # 「はい」がどれを指すか決められなくなる。
                return done(AdmissionVerdict.WAIT, "confirmation_in_flight",
                            (f"pending:{pending_confirmations}",))
            return done(AdmissionVerdict.REQUIRE_CONFIRMATION,
                        "confirmation_required", (), tuple(confirm))
        return done(AdmissionVerdict.ACCEPT, "admitted")


# ---------------------------------------------------------------------------
# 作り直し
# ---------------------------------------------------------------------------

#: 作り直してよい理由。**「うまくいかなかった」だけでは足りない。**
REPLANNABLE = frozenset({
    "tool_temporarily_unavailable", "missing_information",
    "world_state_changed", "assumption_refuted",
    # Phase 7B: 読み取り対象が無い／代替の読み取りツールがある。
    "target_not_found", "read_only_alternative_available",
})
#: ここへ来たら、もう作り直さない。
REPLAN_STOP = frozenset({
    "same_failure_repeated", "unknown_outcome", "permission_denied",
    "requester_unknown", "user_cancelled", "goal_closed",
    # Phase 7B: 副作用が起きたかもしれない／安全境界を破った。
    "side_effect_possible", "confirmation_expired", "safety_boundary",
    "destructive_operation",
})
#: 作り直してよい副作用の分類。**読むだけ。**
#:
#: 失敗した書き込みを別の書き込みへ切り替える経路を作らない。
#: 「1通目が届かなかったから別の宛先へ」は、こちらが決めてよいことではない。
REPLANNABLE_EFFECTS = frozenset({
    EffectCategory.INTERNAL, EffectCategory.READ_ONLY,
})


@dataclass(frozen=True, slots=True)
class ReplanVerdict:
    allowed: bool
    reason: str = ""


def can_replan(plan: BoundedPlan, reason: str,
               *, previous_reasons: tuple[str, ...] = (),
               effect_category: Any = None,
               side_effect_confirmed: bool = False) -> ReplanVerdict:
    key = str(reason or "").strip()
    if key in REPLAN_STOP:
        return ReplanVerdict(False, key)
    if side_effect_confirmed:
        # **外部へ何か起きた後は作り直さない。** 何が起きたか
        # 分かっていない状態で次の手を打つのが一番危ない。
        return ReplanVerdict(False, "side_effect_possible")
    if effect_category is not None and str(effect_category) not in {
            str(item) for item in REPLANNABLE_EFFECTS}:
        return ReplanVerdict(False, f"effect_not_replannable:{effect_category}")
    if not plan.active:
        return ReplanVerdict(False, "plan_closed")
    if plan.replan_count >= plan.max_replans:
        return ReplanVerdict(False, "replan_limit_reached")
    if key in previous_reasons:
        # **同じ失敗を繰り返したら止める。** 3回粘っても結果は同じ。
        return ReplanVerdict(False, "same_failure_repeated")
    if key not in REPLANNABLE:
        return ReplanVerdict(False, f"not_replannable:{key or 'unknown'}")
    return ReplanVerdict(True, key)


# ---------------------------------------------------------------------------
# Action Selector への接続
# ---------------------------------------------------------------------------

#: 受け入れ判定 → 行動の種類。**Goal から直接ツールを呼ばない**（第33項）。
_VERDICT_ACTION: dict[str, ActionType] = {
    AdmissionVerdict.ACCEPT: ActionType.EXECUTE_TOOL,
    AdmissionVerdict.REQUIRE_CONFIRMATION: ActionType.ASK_CONFIRMATION,
    AdmissionVerdict.REQUIRE_CLARIFICATION: ActionType.ASK_CLARIFICATION,
    AdmissionVerdict.WAIT: ActionType.WAIT,
    AdmissionVerdict.REJECT: ActionType.CANCEL_PLAN,
}

#: 副作用の重さ → 基礎点。**重いものほど低い。**
#:
#: 黙って外部へ書き込む方が、聞き返すより点数が高い状態にしない。
_EFFECT_SCORE: dict[str, float] = {
    EffectCategory.INTERNAL: .62,
    EffectCategory.READ_ONLY: .58,
    EffectCategory.LOCAL_REVERSIBLE: .48,
    EffectCategory.EXTERNAL_WRITE: .40,
    EffectCategory.PERSON_DIRECTED: .36,
    EffectCategory.DESTRUCTIVE: .30,
}


def plan_candidates(
    plan: BoundedPlan, admission: PlanAdmission, *,
    now: float | None = None, ttl: float = 90.0,
) -> list[ActionCandidate]:
    """計画を**候補の列へ載せる**。ここから先は他の行動と同じ扱い。

    ここで `ActionCandidate` を返すだけにしてあるのが要点。実行するかは
    Action Selector が他の候補（危険警告・相手の発話・自発発話）と
    比べて決める。**計画があるという理由だけで実行しない。**
    """
    step = plan.current
    if step is None or not plan.active:
        return []
    action = _VERDICT_ACTION.get(str(admission.verdict), ActionType.WAIT)
    if action is ActionType.CANCEL_PLAN and str(
            admission.verdict) != str(AdmissionVerdict.REJECT):
        action = ActionType.WAIT
    effect = str(step.intent.effect_category)
    base = _EFFECT_SCORE.get(effect, .30)
    if action is ActionType.ASK_CONFIRMATION:
        # **聞く方を高くする。** 迷ったら聞く側へ倒れてほしい。
        base = max(base, .55)
    reference = time.monotonic() if now is None else now
    return [ActionCandidate(
        action_type=action,
        target=step.intent.requester_person_id or step.intent.tool_id,
        parameters={
            "plan_id": plan.plan_id, "step_id": step.step_id,
            "intent_id": step.intent.intent_id,
            "tool_id": step.intent.tool_id,
            "operation_id": step.intent.operation_id,
            "effect_category": effect,
            "verdict": str(admission.verdict),
        },
        reasons=(f"plan:{plan.plan_id}", f"verdict:{admission.verdict}",
                 f"effect:{effect}"),
        score_components={"effect": base,
                          "admission": .1 if admission.accepted else .0},
        total_score=base + (.1 if admission.accepted else .0),
        confidence=.9 if admission.accepted else .6,
        blocking_conditions=admission.blockers[:3],
        source_type="bounded_plan",
        participant_ids=((step.intent.requester_person_id,)
                         if step.intent.requester_person_id else ()),
        expires_at=reference + float(ttl),
    )]


def operation_for(registry: ToolRegistry,
                  intent: ActionIntent) -> ToolOperation | None:
    _tool, operation = registry.resolve(intent.tool_id, intent.operation_id)
    return operation


__all__ = [
    "MAX_PLAN_STEPS", "MAX_REPLANS", "REPLANNABLE", "REPLAN_STOP",
    "ActionIntent", "AdmissionVerdict", "BoundedPlan", "PlanAdmission",
    "PlanAdmissionGate", "PlanStatus", "PlanStep", "ReplanVerdict",
    "StepStatus", "build_plan", "can_replan", "operation_for",
    "plan_candidates",
]
