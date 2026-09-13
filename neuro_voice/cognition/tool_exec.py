"""道具を実際に使う所。**門を通らない実行経路をここに作らない。**

Phase 6 まで、副作用のある操作は**一つも繋がっていなかった**。
だからここは「危ない経路を塞ぐ」作業ではなく、**まだ何も無い所に、
最初から門を付けて繋ぐ**作業になる。塞ぐ対象が無いぶん素直だが、
逆に**この門が唯一の防壁**になる。

守りたいのは5つ:

* **確認は、確認した操作にだけ効く。** 引数を変えたら取り直す。
* **同じ実行を2回しない。**
* **結果が分からないものを、勝手にやり直さない。**
* **ツールの出力は命令ではない。**
* **失敗を成功として話さない。**
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as _Timeout
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from neuro_voice.cognition.goals import PermissionResult
from neuro_voice.cognition.planning import (
    ActionIntent, BoundedPlan, PlanStep, PlanStatus, StepStatus,
)
from neuro_voice.cognition.tools import (
    EffectCategory, NO_SIDE_EFFECT, ToolOperation, ToolRegistry,
    VerificationMethod, canonical, normalise_parameters,
)

#: 確認が生きている時間。**長すぎると状況が変わってから実行される。**
CONFIRMATION_TTL = 120.0
#: 声で承認する時に要る一致の強さ。**曖昧な一致で外部操作を通さない。**
MIN_APPROVER_CONFIDENCE = .75
#: 身元が確かだと言える解決状態。
_KNOWN = frozenset({"authoritative", "confirmed"})
#: このプロセスの起動ID。再起動をまたいだ実行中を見分けるため。
PROCESS_ID = uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 確認
# ---------------------------------------------------------------------------


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    #: 一度の実行で使い切った。**二度目は無い**（第11項）。
    CONSUMED = "consumed"


def confirmation_hash(intent: ActionIntent,
                      parameters: dict[str, Any] | None = None) -> str:
    """**何を確認したかの指紋。**

    ツール・操作・対象・宛先・引数・副作用分類のどれが変わっても
    別の値になる。実行の直前にこれを照合するので、確認の後で
    引数だけ差し替える経路は通らない。
    """
    return _digest(intent.fingerprint(parameters))[:16]


@dataclass(slots=True)
class ConfirmationRequest:
    """1つの操作についての「やっていい?」。"""

    intent_id: str
    plan_id: str
    step_id: str
    tool_id: str
    operation_id: str
    effect_category: str
    confirmation_hash: str
    requester_person_id: str
    #: 記録してよい形の引数。**機密値は `<sensitive>` で伏せてある。**
    parameter_summary: dict[str, Any] = field(default_factory=dict)
    target_ids: tuple[str, ...] = ()
    #: どこでの話か（Phase 7B）。**別チャンネルの「はい」を受けない。**
    conversation_id: str = ""
    channel_id: str = ""
    status: ConfirmationStatus | str = ConfirmationStatus.PENDING
    created_at: float = field(default_factory=_now)
    expires_at: float = 0.0
    approver_person_id: str = ""
    decided_at: float = 0.0
    consumed_at: float = 0.0
    denial_reason: str = ""
    process_id: str = PROCESS_ID
    confirmation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def pending(self) -> bool:
        return str(self.status) == str(ConfirmationStatus.PENDING)

    @property
    def approved(self) -> bool:
        return str(self.status) == str(ConfirmationStatus.APPROVED)

    def expired(self, *, now: float | None = None) -> bool:
        return (now or _now()) > self.expires_at

    def snapshot(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "status": str(self.status), "tool_id": self.tool_id,
            "operation_id": self.operation_id,
            "effect": self.effect_category,
            "requester": self.requester_person_id,
            "approver": self.approver_person_id,
            "hash": self.confirmation_hash,
            "targets": list(self.target_ids[:3]),
        }


@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    accepted: bool
    reason: str = ""
    request: ConfirmationRequest | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason,
                "confirmation_id": (self.request.confirmation_id
                                    if self.request else "")}


class ConfirmationStore:
    """確認の台帳。**「はい」を、どの操作の「はい」かへ結びつける。**"""

    def __init__(self, *, ttl: float = CONFIRMATION_TTL,
                 journal: Any = None) -> None:
        self.ttl = float(ttl)
        self.journal = journal
        self._items: dict[str, ConfirmationRequest] = {}
        self._lock = threading.RLock()

    # -- 作る -----------------------------------------------------------

    def open(self, intent: ActionIntent, *,
             parameters: dict[str, Any] | None = None,
             loggable: dict[str, Any] | None = None,
             target_ids: tuple[str, ...] = (),
             now: float | None = None) -> ConfirmationRequest:
        reference = now or _now()
        request = ConfirmationRequest(
            intent_id=intent.intent_id, plan_id=intent.plan_id,
            step_id=intent.step_id, tool_id=intent.tool_id,
            operation_id=intent.operation_id,
            effect_category=str(intent.effect_category),
            confirmation_hash=confirmation_hash(intent, parameters),
            requester_person_id=intent.requester_person_id,
            parameter_summary=dict(loggable or {}),
            target_ids=tuple(target_ids or intent.target_ids),
            conversation_id=str(getattr(intent, "conversation_id", "")),
            channel_id=str(getattr(intent, "channel_id", "")),
            created_at=reference, expires_at=reference + self.ttl)
        with self._lock:
            self._items[request.confirmation_id] = request
        self._persist(request)
        return request

    # -- 読む -----------------------------------------------------------

    def get(self, confirmation_id: str) -> ConfirmationRequest | None:
        with self._lock:
            return self._items.get(str(confirmation_id))

    def pending_for(self, person_id: str = "", *,
                    now: float | None = None) -> list[ConfirmationRequest]:
        self.sweep(now=now)
        with self._lock:
            return [item for item in self._items.values()
                    if item.pending and (not person_id
                                         or item.requester_person_id == person_id)]

    @property
    def pending_count(self) -> int:
        return len(self.pending_for())

    # -- 決める ---------------------------------------------------------

    def approve(self, *, person_id: str, resolution: str = "unknown",
                confidence: float = 1.0, confirmation_id: str = "",
                conversation_id: str = "", channel_id: str = "",
                ambiguous_utterance: bool = False,
                now: float | None = None) -> ApprovalVerdict:
        """承認を受け付ける。**受け付けない理由の方が多い。**

        「はい」という声は、それだけでは誰の何への同意か分からない。
        ここは、その3つ（誰が・どの操作へ・まだ有効か）を全部
        埋められた時にだけ通す。
        """
        reference = now or _now()
        self.sweep(now=reference)
        if str(resolution) not in _KNOWN:
            # **不明・競合の話者は外部副作用を承認できない**（第12項）。
            return ApprovalVerdict(False, f"approver_identity_{resolution}")
        if float(confidence) < MIN_APPROVER_CONFIDENCE:
            return ApprovalVerdict(False, "low_approver_confidence")
        if ambiguous_utterance:
            # **聞き取れたか怪しい「はい」を承認にしない**（第10項）。
            # 書き込みは戻せないので、聞き直す方が安い。
            return ApprovalVerdict(False, "ambiguous_utterance")

        with self._lock:
            if confirmation_id:
                request = self._items.get(str(confirmation_id))
                if request is None:
                    return ApprovalVerdict(False, "confirmation_not_found")
                candidates = [request]
            else:
                candidates = [item for item in self._items.values()
                              if item.pending]
            if not candidates:
                return ApprovalVerdict(False, "no_pending_confirmation")
            if len(candidates) > 1:
                # **どれへの「はい」か決められない**（第13項）。
                # ここで先頭を選ぶと、送るつもりの無い方が送られる。
                return ApprovalVerdict(False, "ambiguous_multiple_pending")
            request = candidates[0]
            if str(request.status) == str(ConfirmationStatus.CONSUMED):
                return ApprovalVerdict(False, "already_consumed", request)
            if not request.pending:
                return ApprovalVerdict(False, f"not_pending:{request.status}",
                                       request)
            if request.expired(now=reference):
                request.status = ConfirmationStatus.EXPIRED
                self._persist(request)
                return ApprovalVerdict(False, "expired", request)
            if request.requester_person_id and (
                    request.requester_person_id != str(person_id)):
                # **別人の了承を流用しない**（第33項）。
                return ApprovalVerdict(False, "approver_not_requester", request)
            # **別チャンネル・別会話の返事を受理しない**（Phase 7B）。
            # Discord では、同じ人が別のチャンネルで「はい」と言うことが
            # 普通に起きる。それをこちらの確認への同意にしない。
            if conversation_id and request.conversation_id and (
                    conversation_id != request.conversation_id):
                return ApprovalVerdict(False, "conversation_mismatch", request)
            if channel_id and request.channel_id and (
                    channel_id != request.channel_id):
                return ApprovalVerdict(False, "channel_mismatch", request)
            request.status = ConfirmationStatus.APPROVED
            request.approver_person_id = str(person_id)
            request.decided_at = reference
        self._persist(request)
        return ApprovalVerdict(True, "approved", request)

    def deny(self, confirmation_id: str, reason: str = "user_denied",
             ) -> ConfirmationRequest | None:
        return self._close(confirmation_id, ConfirmationStatus.DENIED, reason)

    def cancel(self, confirmation_id: str, reason: str = "cancelled",
               ) -> ConfirmationRequest | None:
        return self._close(confirmation_id, ConfirmationStatus.CANCELLED, reason)

    def cancel_all(self, reason: str = "turn_closed") -> int:
        """会話が閉じた時。**保留のまま持ち越して後で実行しない**（第26項）。"""
        count = 0
        for item in list(self.pending_for()):
            self.cancel(item.confirmation_id, reason)
            count += 1
        return count

    def consume(self, confirmation_id: str) -> ConfirmationRequest | None:
        """一度の実行で使い切る。**同じ「はい」で2回実行しない。**"""
        with self._lock:
            request = self._items.get(str(confirmation_id))
            if request is None or not request.approved:
                return None
            request.status = ConfirmationStatus.CONSUMED
            request.consumed_at = _now()
        self._persist(request)
        return request

    def sweep(self, *, now: float | None = None) -> int:
        reference = now or _now()
        expired: list[ConfirmationRequest] = []
        with self._lock:
            for item in self._items.values():
                if item.pending and item.expired(now=reference):
                    item.status = ConfirmationStatus.EXPIRED
                    expired.append(item)
        for item in expired:
            self._persist(item)
        return len(expired)

    def restore(self, rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
                *, now: float | None = None) -> int:
        """再起動後。**保留は期限を見て、切れていれば期限切れにする**（第24項）。"""
        reference = now or _now()
        restored = 0
        for row in rows or ():
            request = ConfirmationRequest(
                confirmation_id=str(row.get("confirmation_id", "")),
                intent_id=str(row.get("intent_id", "")),
                plan_id=str(row.get("plan_id", "")),
                step_id=str(row.get("step_id", "")),
                tool_id=str(row.get("tool_id", "")),
                operation_id=str(row.get("operation_id", "")),
                effect_category=str(row.get("effect_category", "")),
                confirmation_hash=str(row.get("confirmation_hash", "")),
                requester_person_id=str(row.get("requester_person_id", "")),
                target_ids=tuple(
                    item for item in str(row.get("target_ids", "")).split(",")
                    if item),
                status=str(row.get("status", "pending")),
                created_at=float(row.get("created_at", reference)),
                expires_at=float(row.get("expires_at", 0.0)),
                approver_person_id=str(row.get("approver_person_id", "")),
                process_id=str(row.get("process_id", "")))
            if request.pending and request.expired(now=reference):
                request.status = ConfirmationStatus.EXPIRED
                self._persist(request)
            elif request.approved:
                # **承認済みのまま再起動をまたがせない。**
                # 状況が変わっているかもしれないのに、起動しただけで
                # 外部操作が走る経路になる。
                request.status = ConfirmationStatus.EXPIRED
                request.denial_reason = "restart_invalidated"
                self._persist(request)
            with self._lock:
                self._items[request.confirmation_id] = request
            restored += 1
        return restored

    # -- 内部 -----------------------------------------------------------

    def _close(self, confirmation_id: str, status: ConfirmationStatus,
               reason: str) -> ConfirmationRequest | None:
        with self._lock:
            request = self._items.get(str(confirmation_id))
            if request is None:
                return None
            request.status = status
            request.denial_reason = str(reason)[:60]
            request.decided_at = _now()
        self._persist(request)
        return request

    def _persist(self, request: ConfirmationRequest) -> None:
        if self.journal is None:
            return
        saver = getattr(self.journal, "save_confirmation", None)
        if saver is None:
            return
        saver({
            "confirmation_id": request.confirmation_id,
            "intent_id": request.intent_id, "plan_id": request.plan_id,
            "step_id": request.step_id, "tool_id": request.tool_id,
            "operation_id": request.operation_id,
            "effect_category": request.effect_category,
            "confirmation_hash": request.confirmation_hash,
            "requester_person_id": request.requester_person_id,
            "approver_person_id": request.approver_person_id,
            "target_ids": ",".join(request.target_ids[:4]),
            "conversation_id": request.conversation_id,
            "channel_id": request.channel_id,
            "status": str(request.status), "created_at": request.created_at,
            "expires_at": request.expires_at,
            "decided_at": request.decided_at,
            "consumed_at": request.consumed_at,
            "process_id": request.process_id,
        })


# ---------------------------------------------------------------------------
# 実行要求
# ---------------------------------------------------------------------------


def idempotency_key(intent: ActionIntent,
                    parameters: dict[str, Any] | None = None) -> str:
    """同じ操作を2回しないための鍵。

    **計画・手順・ツール・操作・引数**から決まる。ここに時刻や
    実行IDを混ぜると毎回違う値になり、二重送信が素通りする。
    """
    return _digest(str(intent.plan_id), str(intent.step_id),
                   str(intent.tool_id), str(intent.operation_id),
                   canonical(parameters if parameters is not None
                             else intent.parameters))[:24]


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    """門を通った後にだけ作られる。**これがある＝許可済み。**"""

    tool_id: str
    operation_id: str
    #: ツールへ実際に渡す値。**会話履歴も人格情報も入れない**（第14項）。
    normalized_parameters: dict[str, Any] = field(default_factory=dict)
    #: 記録用。機密値は伏せてある。
    loggable_parameters: dict[str, Any] = field(default_factory=dict)
    plan_id: str = ""
    step_id: str = ""
    action_intent_id: str = ""
    requester_person_id: str = ""
    permission_decision_id: str = ""
    confirmation_id: str = ""
    idempotency_key: str = ""
    timeout_seconds: float = 20.0
    effect_category: str = str(EffectCategory.READ_ONLY)
    dry_run: bool = False
    created_at: float = field(default_factory=_now)
    process_id: str = PROCESS_ID
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def snapshot(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id, "tool_id": self.tool_id,
            "operation_id": self.operation_id,
            "parameters": dict(self.loggable_parameters),
            "effect": self.effect_category,
            "idempotency_key": self.idempotency_key,
            "confirmation_id": self.confirmation_id,
            "requester": self.requester_person_id,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# 門
# ---------------------------------------------------------------------------

#: 門の検査項目。**順番に意味がある**（作れない物を先に落とす）。
GATE_CHECKS: tuple[str, ...] = (
    "tool_registered", "tool_enabled", "operation_allowed",
    "parameter_schema", "effect_category_match", "permission_valid",
    "confirmation_approved", "approver_matches_requester",
    "confirmation_not_expired", "confirmation_hash_match",
    "plan_still_active", "not_already_executed",
)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    allowed: bool
    rejection_reason: str = ""
    passed: tuple[str, ...] = ()
    request: ToolExecutionRequest | None = None
    decided_ms: float = .0

    def snapshot(self) -> dict[str, Any]:
        return {
            "result": "allow" if self.allowed else "deny",
            "rejection_reason": self.rejection_reason,
            "passed": list(self.passed),
            "execution_id": self.request.execution_id if self.request else "",
        }


class ToolGate:
    """**全ての実行がここを通る。** 迂回する経路を作らない。

    一つでも欠けたら拒否。「たぶん大丈夫」で通す枝を作らない——
    そういう枝は、後から条件が増えた時に必ず更新から漏れる。
    """

    def __init__(self, registry: ToolRegistry, *,
                 confirmations: ConfirmationStore | None = None,
                 ledger: "IdempotencyLedger | None" = None,
                 enabled: bool = False) -> None:
        self.registry = registry
        self.confirmations = confirmations or ConfirmationStore()
        self.ledger = ledger or IdempotencyLedger()
        self.enabled = bool(enabled)

    def check(
        self, intent: ActionIntent, *, permission: Any = None,
        confirmation_id: str = "", plan: BoundedPlan | None = None,
        approver_person_id: str = "", now: float | None = None,
    ) -> GateVerdict:
        started = time.perf_counter()
        passed: list[str] = []
        reference = now or _now()

        def deny(reason: str) -> GateVerdict:
            return GateVerdict(
                False, reason, tuple(passed),
                decided_ms=round((time.perf_counter() - started) * 1000, 4))

        if not self.enabled:
            return deny("tool_execution_disabled")

        tool, operation = self.registry.resolve(intent.tool_id,
                                                intent.operation_id)
        if tool is None:
            return deny("tool_not_registered")
        passed.append("tool_registered")
        if not tool.enabled:
            return deny("tool_disabled")
        passed.append("tool_enabled")
        if operation is None:
            return deny("operation_not_allowed")
        passed.append("operation_allowed")

        normalised = normalise_parameters(operation.parameters,
                                          intent.parameters)
        if not normalised.valid:
            return deny(f"parameter_schema:{normalised.errors[0]}")
        passed.append("parameter_schema")

        if str(intent.effect_category) != str(operation.effect_category):
            return deny("effect_category_mismatch")
        passed.append("effect_category_match")

        if permission is None:
            return deny("permission_missing")
        if str(getattr(permission, "result", "")) == str(PermissionResult.DENY):
            return deny("permission_denied")
        if str(getattr(permission, "action_id", "")) != str(intent.step_id):
            # **別の行動のために出た判定を流用しない。**
            return deny("permission_action_mismatch")
        passed.append("permission_valid")

        requires_confirmation = bool(
            getattr(permission, "requires_confirmation", False))
        if requires_confirmation:
            request = self.confirmations.get(confirmation_id)
            if request is None:
                return deny("confirmation_missing")
            if not request.approved:
                return deny(f"confirmation_not_approved:{request.status}")
            passed.append("confirmation_approved")
            approver = approver_person_id or request.approver_person_id
            if (not approver
                    or approver != request.requester_person_id
                    or approver != intent.requester_person_id):
                return deny("approver_mismatch")
            passed.append("approver_matches_requester")
            if request.expired(now=reference):
                return deny("confirmation_expired")
            passed.append("confirmation_not_expired")
            if request.confirmation_hash != confirmation_hash(
                    intent, normalised.values):
                # **確認の後で引数が変わった**（第11項）。取り直す。
                return deny("confirmation_hash_mismatch")
            passed.append("confirmation_hash_match")
        else:
            passed.extend(("confirmation_approved",
                           "approver_matches_requester",
                           "confirmation_not_expired",
                           "confirmation_hash_match"))

        if plan is not None:
            if not plan.active:
                return deny("plan_not_active")
            step = plan.step(intent.step_id)
            if step is None:
                return deny("step_not_in_plan")
            if step.done:
                return deny("step_already_closed")
        passed.append("plan_still_active")

        key = idempotency_key(intent, normalised.values)
        existing = self.ledger.lookup(key)
        if existing is not None and existing.terminal:
            return deny("already_executed")
        passed.append("not_already_executed")

        request_obj = ToolExecutionRequest(
            tool_id=intent.tool_id, operation_id=intent.operation_id,
            normalized_parameters=dict(normalised.values),
            loggable_parameters=dict(normalised.loggable),
            plan_id=intent.plan_id, step_id=intent.step_id,
            action_intent_id=intent.intent_id,
            requester_person_id=intent.requester_person_id,
            permission_decision_id=str(getattr(permission, "action_id", "")),
            confirmation_id=str(confirmation_id or ""),
            idempotency_key=key, timeout_seconds=operation.timeout_seconds,
            effect_category=str(operation.effect_category),
            dry_run=operation.dry_run_only)
        return GateVerdict(
            True, "", tuple(passed), request_obj,
            round((time.perf_counter() - started) * 1000, 4))


# ---------------------------------------------------------------------------
# 結果
# ---------------------------------------------------------------------------


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    #: **やったかどうか分からない。** 一番危ない状態。自動でやり直さない。
    UNKNOWN_OUTCOME = "unknown_outcome"
    PARTIAL_SUCCESS = "partial_success"


_TERMINAL_TOOL = frozenset({
    ToolStatus.SUCCEEDED, ToolStatus.FAILED, ToolStatus.CANCELLED,
    ToolStatus.PARTIAL_SUCCESS, ToolStatus.UNKNOWN_OUTCOME,
})


class CancelState(StrEnum):
    """止めてくれと言われた時、実際どうなったか。

    **「止めました」と言えるのは `CANCELLED` だけ。**
    止まっていないものを止まったことにすると、ユーザーは
    起きていない前提で次の行動を決める。
    """

    NONE = "none"
    REQUESTED = "cancel_requested"
    RUNNING = "still_running"
    CANCELLED = "cancelled"
    NOT_CANCELLABLE = "not_cancellable"
    COMPLETED = "already_completed"


@dataclass(frozen=True, slots=True)
class ToolResult:
    execution_id: str
    tool_id: str
    operation_id: str
    status: ToolStatus | str
    #: 短い要約。**ツールの出力全文をここへ入れない**（第12条）。
    output_summary: str = ""
    structured_output: dict[str, Any] = field(default_factory=dict)
    #: 副作用が実際に起きたと**確かめられた**か。予想では立てない。
    side_effect_confirmed: bool = False
    external_reference: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    error_type: str = ""
    error_summary: str = ""
    attempts: int = 1
    cancel_state: CancelState | str = CancelState.NONE
    #: ツールの出力に「これを実行せよ」と読める部分があったか（第20項）。
    instruction_like_output: bool = False

    @property
    def succeeded(self) -> bool:
        return str(self.status) == str(ToolStatus.SUCCEEDED)

    @property
    def terminal(self) -> bool:
        return str(self.status) in {str(item) for item in _TERMINAL_TOOL}

    @property
    def duration_ms(self) -> float:
        if not (self.started_at and self.completed_at):
            return .0
        return round((self.completed_at - self.started_at) * 1000, 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id, "tool_id": self.tool_id,
            "operation_id": self.operation_id, "status": str(self.status),
            "summary": self.output_summary[:80],
            "side_effect_confirmed": self.side_effect_confirmed,
            "error": f"{self.error_type}:{self.error_summary[:60]}".strip(":"),
            "attempts": self.attempts, "cancel": str(self.cancel_state),
            "instruction_like": self.instruction_like_output,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# ツール出力を信用しすぎない
# ---------------------------------------------------------------------------

#: 出力の中で「命令」に見える言い回し。**見つけても実行しない。印を付けるだけ。**
_INSTRUCTION_MARKERS: tuple[str, ...] = (
    "system:", "system prompt", "ignore previous", "ignore all previous",
    "disregard the above", "new instructions", "you must now",
    "以下を実行", "次のツールを実行", "システム命令", "命令を無視",
    "これまでの指示を無視", "至急実行してください",
)


def instruction_like(value: Any) -> bool:
    """ツールの出力が命令のふりをしていないか。

    **見つけたからといって落とさない。** 検索結果に「無視しろ」と
    書いてあること自体は、ユーザーが知りたい情報かもしれない。
    やるのは印を付けることだけで、**実行しないのは常に既定**。
    """
    text = str(value or "").lower()
    return any(marker.lower() in text for marker in _INSTRUCTION_MARKERS)


def summarise_output(value: Any, *, limit: int = 240) -> str:
    """出力を短い要約へ。**そのままプロンプトへ流さない前提の形。**"""
    text = " ".join(str(value or "").split())
    return text[:limit]


# ---------------------------------------------------------------------------
# 冪等台帳
# ---------------------------------------------------------------------------


class IdempotencyLedger:
    """同じ実行を2回しないための記録。"""

    def __init__(self, *, journal: Any = None) -> None:
        self.journal = journal
        self._results: dict[str, ToolResult] = {}
        self._running: dict[str, str] = {}
        self._lock = threading.RLock()

    def lookup(self, key: str) -> ToolResult | None:
        with self._lock:
            return self._results.get(str(key))

    def running(self, key: str) -> str:
        with self._lock:
            return self._running.get(str(key), "")

    def mark_running(self, key: str, execution_id: str) -> bool:
        """走り始めた印。**既に走っていれば `False`**（二重起動しない）。"""
        with self._lock:
            if str(key) in self._running:
                return False
            if str(key) in self._results:
                return False
            self._running[str(key)] = str(execution_id)
            return True

    def record(self, key: str, result: ToolResult) -> None:
        with self._lock:
            self._running.pop(str(key), None)
            if result.terminal:
                self._results[str(key)] = result

    def release(self, key: str) -> None:
        with self._lock:
            self._running.pop(str(key), None)

    def restore(self, rows: Any) -> int:
        """再起動後。**実行中だったものを結果として持ち込まない。**"""
        count = 0
        for row in rows or ():
            status = str(row.get("status", ""))
            key = str(row.get("idempotency_key", ""))
            if not key or status not in {str(item) for item in _TERMINAL_TOOL}:
                continue
            with self._lock:
                self._results[key] = ToolResult(
                    execution_id=str(row.get("execution_id", "")),
                    tool_id=str(row.get("tool_id", "")),
                    operation_id=str(row.get("operation_id", "")),
                    status=status,
                    side_effect_confirmed=bool(
                        row.get("side_effect_confirmed", 0)),
                    started_at=float(row.get("started_at", 0.0)),
                    completed_at=float(row.get("completed_at", 0.0)))
            count += 1
        return count


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

ToolCallable = Callable[..., Any]


@dataclass(slots=True)
class ExecutionHandle:
    execution_id: str
    idempotency_key: str
    future: Future | None = None
    cancel_event: threading.Event | None = None
    cancellable: bool = True
    result: ToolResult | None = None

    @property
    def done(self) -> bool:
        return self.result is not None or (
            self.future is not None and self.future.done())


class ToolExecutor:
    """実際に呼ぶ所。**会話を止めない**（第18項）。

    音声会話の途中で外部の応答を同期的に待つと、ポッポは数秒間
    何も聞こえなくなる。実行は別スレッドへ出して、会話とWARNは
    そのまま動かす。
    """

    def __init__(self, registry: ToolRegistry, *,
                 ledger: IdempotencyLedger | None = None,
                 journal: Any = None, max_workers: int = 2) -> None:
        self.registry = registry
        self.ledger = ledger or IdempotencyLedger()
        self.journal = journal
        self._bindings: dict[tuple[str, str], ToolCallable] = {}
        self._pool = ThreadPoolExecutor(max_workers=int(max_workers),
                                        thread_name_prefix="tool")
        self._handles: dict[str, ExecutionHandle] = {}
        self._lock = threading.RLock()

    # -- 束ねる ---------------------------------------------------------

    def bind(self, tool_id: str, operation_id: str,
             handler: ToolCallable) -> None:
        """実物を繋ぐ。**台帳に無いものは繋げない。**"""
        _tool, operation = self.registry.resolve(tool_id, operation_id)
        if operation is None:
            raise ValueError(f"未登録の操作: {tool_id}.{operation_id}")
        self._bindings[(str(tool_id), str(operation_id))] = handler

    def bound(self, tool_id: str, operation_id: str) -> bool:
        return (str(tool_id), str(operation_id)) in self._bindings

    # -- 走らせる -------------------------------------------------------

    def submit(self, request: ToolExecutionRequest, *,
               max_retries_override: int | None = None) -> ExecutionHandle:
        _tool, operation = self.registry.resolve(request.tool_id,
                                                 request.operation_id)
        handle = ExecutionHandle(
            execution_id=request.execution_id,
            idempotency_key=request.idempotency_key,
            cancellable=bool(operation.cancellable) if operation else False)

        existing = self.ledger.lookup(request.idempotency_key)
        if existing is not None:
            # **同じ要求が来た。やり直さず、前の結果を返す**（第16項）。
            handle.result = existing
            return handle
        if not self.ledger.mark_running(request.idempotency_key,
                                        request.execution_id):
            handle.result = self._result(
                request, ToolStatus.FAILED, error_type="duplicate_in_flight",
                error_summary="同じ実行が走っている")
            return handle
        if operation is None:
            self.ledger.release(request.idempotency_key)
            handle.result = self._result(
                request, ToolStatus.FAILED, error_type="operation_missing")
            return handle
        handler = self._bindings.get((request.tool_id, request.operation_id))
        if handler is None:
            self.ledger.release(request.idempotency_key)
            handle.result = self._result(
                request, ToolStatus.FAILED, error_type="not_bound",
                error_summary="実物が繋がっていない")
            return handle

        handle.cancel_event = threading.Event()
        handle.future = self._pool.submit(
            self._run, request, operation, handler, handle.cancel_event,
            max_retries_override)
        with self._lock:
            self._handles[request.execution_id] = handle
        return handle

    def wait(self, handle: ExecutionHandle,
             timeout: float | None = None) -> ToolResult:
        """結果を取りに行く。**会話側が呼ぶのは、暇になってから。**"""
        if handle.result is not None:
            return handle.result
        if handle.future is None:
            return self._unknown(handle)
        limit = timeout
        try:
            result = handle.future.result(timeout=limit)
        except _Timeout:
            return self._unknown(handle)
        handle.result = result
        return result

    def cancel(self, execution_id: str) -> CancelState:
        """止めてくれ、と言われた時。**止まらないものは正直に言う。**"""
        with self._lock:
            handle = self._handles.get(str(execution_id))
        if handle is None:
            return CancelState.NONE
        if handle.result is not None or (
                handle.future is not None and handle.future.done()):
            return CancelState.COMPLETED
        if not handle.cancellable:
            return CancelState.NOT_CANCELLABLE
        if handle.cancel_event is not None:
            handle.cancel_event.set()
        if handle.future is not None and handle.future.cancel():
            handle.result = ToolResult(
                execution_id=handle.execution_id, tool_id="", operation_id="",
                status=ToolStatus.CANCELLED, cancel_state=CancelState.CANCELLED)
            self.ledger.release(handle.idempotency_key)
            return CancelState.CANCELLED
        return CancelState.REQUESTED

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- 内部 -----------------------------------------------------------

    def _run(self, request: ToolExecutionRequest, operation: ToolOperation,
             handler: ToolCallable, cancel: threading.Event,
             max_retries_override: int | None = None) -> ToolResult:
        started = _now()
        attempts = 0
        # **やり直してよいのは副作用の無いものだけ**（第17項）。
        limit = (max(0, int(max_retries_override))
                 if max_retries_override is not None
                 else (operation.max_retries if operation.auto_retryable else 0))
        last_error = ""
        error_type = ""
        while attempts <= limit:
            attempts += 1
            if cancel.is_set():
                return self._finish(request, self._result(
                    request, ToolStatus.CANCELLED, started_at=started,
                    attempts=attempts, cancel_state=CancelState.CANCELLED))
            try:
                raw = handler(**dict(request.normalized_parameters))
            except TimeoutError as error:
                error_type, last_error = "timeout", str(error)[:120]
            except Exception as error:                  # noqa: BLE001
                error_type = type(error).__name__
                last_error = str(error)[:120]
            else:
                summary = summarise_output(raw)
                structured = raw if isinstance(raw, dict) else {}
                return self._finish(request, self._result(
                    request, ToolStatus.SUCCEEDED, output_summary=summary,
                    structured_output=structured, started_at=started,
                    attempts=attempts,
                    side_effect_confirmed=(
                        not operation.side_effect_free and bool(raw)),
                    external_reference=str(structured.get("reference", ""))[:60],
                    instruction_like_output=instruction_like(summary)))
            if attempts > limit:
                break
            time.sleep(min(.4 * attempts, 1.2))

        if error_type == "timeout" and not operation.side_effect_free:
            # **書き込みの途中で切れた。起きたかどうか分からない**（第17項）。
            #
            # 最初は `IRREVERSIBLE` の3種だけを見ていたが、
            # `LOCAL_REVERSIBLE` が漏れていた——**ファイルは作られたかも
            # しれないのに `TIMED_OUT`（何も起きていない）** になる。
            # 戻せるかどうかと、起きたかどうかは別の話。
            status = ToolStatus.UNKNOWN_OUTCOME
        elif error_type == "timeout":
            status = ToolStatus.TIMED_OUT
        else:
            status = ToolStatus.FAILED
        return self._finish(request, self._result(
            request, status, started_at=started, attempts=attempts,
            error_type=error_type or "error", error_summary=last_error))

    def _finish(self, request: ToolExecutionRequest,
                result: ToolResult) -> ToolResult:
        self.ledger.record(request.idempotency_key, result)
        self._persist(request, result)
        return result

    def _unknown(self, handle: ExecutionHandle) -> ToolResult:
        """待ちきれなかった。**成功とも失敗とも言わない。**"""
        return ToolResult(
            execution_id=handle.execution_id, tool_id="", operation_id="",
            status=ToolStatus.UNKNOWN_OUTCOME,
            cancel_state=CancelState.RUNNING,
            error_type="wait_timeout")

    def _result(self, request: ToolExecutionRequest, status: ToolStatus,
                **kwargs: Any) -> ToolResult:
        started = kwargs.pop("started_at", _now())
        return ToolResult(
            execution_id=request.execution_id, tool_id=request.tool_id,
            operation_id=request.operation_id, status=status,
            started_at=started, completed_at=_now(), **kwargs)

    def _persist(self, request: ToolExecutionRequest,
                 result: ToolResult) -> None:
        if self.journal is None:
            return
        saver = getattr(self.journal, "save_tool_execution", None)
        if saver is None:
            return
        saver({
            "execution_id": request.execution_id,
            "plan_id": request.plan_id, "step_id": request.step_id,
            "action_intent_id": request.action_intent_id,
            "tool_id": request.tool_id, "operation_id": request.operation_id,
            "effect_category": request.effect_category,
            "idempotency_key": request.idempotency_key,
            "requester_person_id": request.requester_person_id,
            "confirmation_id": request.confirmation_id,
            "status": str(result.status),
            "side_effect_confirmed": int(result.side_effect_confirmed),
            "attempts": result.attempts,
            "error_type": result.error_type,
            "process_id": request.process_id,
            "created_at": request.created_at,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
        })


# ---------------------------------------------------------------------------
# 結果の検証
# ---------------------------------------------------------------------------


class VerificationOutcome(StrEnum):
    VERIFIED = "verified"
    #: **確かめられなかった。** 失敗ではないが、成功でもない。
    UNVERIFIED = "unverified"
    REFUTED = "refuted"


@dataclass(frozen=True, slots=True)
class Verification:
    outcome: VerificationOutcome | str
    method: VerificationMethod | str = VerificationMethod.NONE
    reason: str = ""

    @property
    def verified(self) -> bool:
        return str(self.outcome) == str(VerificationOutcome.VERIFIED)

    def snapshot(self) -> dict[str, Any]:
        return {"outcome": str(self.outcome), "method": str(self.method),
                "reason": self.reason[:60]}


def verify_result(operation: ToolOperation, result: ToolResult, *,
                  readback: Callable[[], Any] | None = None) -> Verification:
    """**「成功が返った」は「効いた」ではない**（第21項）。

    ツールが `SUCCEEDED` を返しても、ファイルが無い・設定が変わって
    いない・送信されていない、はいくらでもある。確かめられない時は
    `UNVERIFIED` にして、**Goal を完了にしない。**
    """
    if not result.succeeded:
        return Verification(VerificationOutcome.REFUTED,
                            operation.verification, f"status:{result.status}")
    method = operation.verification
    if method is VerificationMethod.OUTPUT_PRESENT:
        if result.output_summary or result.structured_output:
            return Verification(VerificationOutcome.VERIFIED, method,
                                "output_present")
        return Verification(VerificationOutcome.UNVERIFIED, method,
                            "empty_output")
    if method in {VerificationMethod.READBACK, VerificationMethod.FILE_EXISTS}:
        if readback is None:
            return Verification(VerificationOutcome.UNVERIFIED, method,
                                "no_readback_available")
        try:
            value = readback()
        except Exception as error:                      # noqa: BLE001
            return Verification(VerificationOutcome.UNVERIFIED, method,
                                type(error).__name__)
        if value:
            return Verification(VerificationOutcome.VERIFIED, method,
                                "readback_ok")
        return Verification(VerificationOutcome.REFUTED, method,
                            "readback_empty")
    if method is VerificationMethod.PROVIDER_ACK:
        if result.side_effect_confirmed and result.external_reference:
            return Verification(VerificationOutcome.VERIFIED, method,
                                "provider_ack")
        return Verification(VerificationOutcome.UNVERIFIED, method,
                            "no_provider_ack")
    return Verification(VerificationOutcome.UNVERIFIED, method,
                        "no_verification_defined")


# ---------------------------------------------------------------------------
# 結果を戻す
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolActionOutcome:
    execution_id: str
    plan_id: str
    step_id: str
    action: str
    result: ToolResult
    verification: Verification
    world_state_deltas: tuple[dict[str, Any], ...] = ()
    goal_status_change: str = ""
    obligation_status_change: str = ""
    memory_candidate: dict[str, Any] | None = None
    replan_reason: str = ""

    @property
    def verified(self) -> bool:
        return self.verification.verified

    def snapshot(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id, "plan_id": self.plan_id,
            "step_id": self.step_id, "action": self.action,
            "result": self.result.snapshot(),
            "verification": self.verification.snapshot(),
            "world_deltas": list(self.world_state_deltas[:4]),
            "goal_status_change": self.goal_status_change,
            "replan_reason": self.replan_reason,
        }


#: 結果 → 作り直してよい理由。**「不明」はここに入れない**（第23項）。
_RESULT_TO_REPLAN: dict[str, str] = {
    str(ToolStatus.TIMED_OUT): "tool_temporarily_unavailable",
    str(ToolStatus.FAILED): "tool_temporarily_unavailable",
}


def apply_tool_outcome(plan: BoundedPlan, step: PlanStep, result: ToolResult,
                       verification: Verification) -> ToolActionOutcome:
    """結果を計画と世界へ戻す。**成功したことにしない。**"""
    deltas: list[dict[str, Any]] = []
    goal_change = ""
    replan = ""

    if result.succeeded and verification.verified:
        step.status = StepStatus.COMPLETED
        deltas.append({
            "predicate": "tool_outcome", "entity": step.intent.tool_id,
            "value": "succeeded", "confidence": .9,
        })
        if plan.current is None:
            plan.status = PlanStatus.COMPLETED
            goal_change = "completed"
        else:
            plan.status = PlanStatus.RUNNING
    elif result.succeeded:
        # **返事は良かったが、効いたか確かめられない。**
        step.status = StepStatus.UNVERIFIED
        step.failure_reason = verification.reason
        plan.status = PlanStatus.PAUSED
        deltas.append({
            "predicate": "tool_outcome", "entity": step.intent.tool_id,
            "value": "unverified", "confidence": .5,
        })
    elif str(result.status) == str(ToolStatus.UNKNOWN_OUTCOME):
        # **やったかどうか分からない。止まる**（第17項・第23項）。
        step.status = StepStatus.BLOCKED
        step.failure_reason = "unknown_outcome"
        plan.status = PlanStatus.PAUSED
        replan = "unknown_outcome"
        deltas.append({
            "predicate": "tool_outcome", "entity": step.intent.tool_id,
            "value": "unknown", "confidence": .4,
        })
    elif str(result.status) == str(ToolStatus.CANCELLED):
        step.status = StepStatus.CANCELLED
        plan.close(PlanStatus.CANCELLED, "user_cancelled")
        goal_change = "cancelled"
    else:
        step.status = StepStatus.FAILED
        step.failure_reason = f"{result.error_type}"[:60]
        plan.status = PlanStatus.PAUSED
        replan = _RESULT_TO_REPLAN.get(str(result.status), "")
        deltas.append({
            "predicate": "tool_outcome", "entity": step.intent.tool_id,
            "value": "failed", "confidence": .8,
        })

    step.execution_id = result.execution_id
    plan.updated_at = _now()
    return ToolActionOutcome(
        execution_id=result.execution_id, plan_id=plan.plan_id,
        step_id=step.step_id, action="execute_tool", result=result,
        verification=verification, world_state_deltas=tuple(deltas),
        goal_status_change=goal_change, replan_reason=replan,
        memory_candidate=({
            "information_type": "system_fact",
            "summary": f"{step.intent.tool_id}:{result.status}",
        } if result.terminal else None))


def restart_disposition(row: dict[str, Any]) -> str:
    """再起動時、走っていた実行をどう扱うか（第24項）。

    **再起動だけを理由に外部操作をやり直さない。**
    """
    status = str(row.get("status", ""))
    effect = str(row.get("effect_category", ""))
    if status in {str(item) for item in _TERMINAL_TOOL}:
        return "keep_result"
    if effect in {str(item) for item in NO_SIDE_EFFECT}:
        return "resume_candidate"
    return "unknown_outcome_no_retry"


__all__ = [
    "CONFIRMATION_TTL", "GATE_CHECKS", "MIN_APPROVER_CONFIDENCE", "PROCESS_ID",
    "ApprovalVerdict", "CancelState", "ConfirmationRequest",
    "ConfirmationStatus", "ConfirmationStore", "ExecutionHandle",
    "GateVerdict", "IdempotencyLedger", "ToolActionOutcome",
    "ToolExecutionRequest", "ToolExecutor", "ToolGate", "ToolResult",
    "ToolStatus", "Verification", "VerificationOutcome",
    "apply_tool_outcome", "confirmation_hash", "idempotency_key",
    "instruction_like", "restart_disposition", "summarise_output",
    "verify_result",
]
