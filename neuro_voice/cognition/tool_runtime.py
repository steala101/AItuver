"""台帳・門・実行を1つに束ねて、実際の経路へ繋ぐ所。

ここが無いと、`tools.py` / `planning.py` / `tool_exec.py` は
**部品として正しいのに誰も呼んでいない**状態のままになる。Phase 6E で
`attach_identity_runtime()` に呼び出し側が無かったのと同じ形——
自分の引き継ぎ文を書いている最中に気づいた種類の抜けなので、
最初から束ねる側を用意しておく。

**架空のツールを実経路へ載せない**（第27項）。ここが繋ぐのは、監査で
実在を確かめた3つの読み取り専用ツールだけ。書き込みは試験用のみ。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from neuro_voice.cognition.goals import GoalStatus, decide_permission
from neuro_voice.cognition.planning import (
    ActionIntent, AdmissionVerdict, BoundedPlan, PlanAdmission,
    PlanAdmissionGate, PlanStatus, build_plan, plan_candidates,
)
from neuro_voice.cognition.tool_exec import (
    ConfirmationRequest, ConfirmationStore, IdempotencyLedger, ToolExecutor,
    ToolGate, ToolResult, ToolStatus, Verification, apply_tool_outcome,
    verify_result,
)
from neuro_voice.cognition.tool_report import (
    ReportLedger, build_outcome_event, report_candidates,
)
from neuro_voice.cognition.tools import (
    EffectCategory, ToolRegistry, default_registry, normalise_parameters,
)


def _flag(cfg: Any, path: str, default: bool = False) -> bool:
    if cfg is None:
        return default
    try:
        return bool(cfg.get(path, default))
    except Exception:                                   # noqa: BLE001
        return default


@dataclass(slots=True)
class ToolTurn:
    """1回分の顛末。**発話へ渡してよいのはここに入っているものだけ。**"""

    stage: str
    reason: str = ""
    plan: BoundedPlan | None = None
    admission: PlanAdmission | None = None
    confirmation: ConfirmationRequest | None = None
    result: ToolResult | None = None
    permission: Any = None
    gate_verdict: Any = None
    verification: Verification | None = None
    outcome: Any = None
    candidates: list[Any] = field(default_factory=list)
    #: 結果の出来事（Phase 7B）。**ここから先が会話経路。**
    event: Any = None
    #: 結果を伝える候補。**発話ではなく候補。**
    report_candidates: list[Any] = field(default_factory=list)
    latency_ms: dict[str, float] = field(default_factory=dict)

    @property
    def spoke_safely(self) -> bool:
        """**結果として話してよいか。** 検証が通った時だけ。"""
        return bool(self.result and self.result.succeeded
                    and self.verification and self.verification.verified)

    def planner_payload(self) -> dict[str, Any] | None:
        """Conversation Planner へ渡す全部。**これ以上は渡さない。**"""
        if self.event is None:
            return None
        from neuro_voice.cognition.tool_report import planner_payload

        return planner_payload(self.event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "reason": self.reason[:80],
            "plan": self.plan.snapshot() if self.plan else None,
            "admission": self.admission.snapshot() if self.admission else None,
            "confirmation": (self.confirmation.snapshot()
                             if self.confirmation else None),
            "result": self.result.snapshot() if self.result else None,
            "verification": (self.verification.snapshot()
                             if self.verification else None),
            "event": self.event.snapshot() if self.event else None,
            "latency_ms": dict(self.latency_ms),
        }


class ToolRuntime:
    """設定を読んで、部品を繋いだ状態にする。**既定は全部止まっている。**"""

    def __init__(self, cfg: Any = None, *, journal: Any = None) -> None:
        self.cfg = cfg
        self.registry: ToolRegistry = default_registry(cfg)
        self.confirmations = ConfirmationStore(
            ttl=float((cfg.get("confirmation.ttl_seconds", 120)
                       if cfg else 120) or 120),
            journal=journal if _flag(cfg, "tool_execution.persistence_enabled")
            else None)
        self.ledger = IdempotencyLedger()
        self.journal = journal
        self.admission_gate = PlanAdmissionGate(
            self.registry,
            enabled=_flag(cfg, "planning.enabled"),
            max_steps=int((cfg.get("planning.max_steps", 3) if cfg else 3) or 3))
        self.gate = ToolGate(
            self.registry, confirmations=self.confirmations,
            ledger=self.ledger, enabled=_flag(cfg, "tool_execution.enabled"))
        self.executor = ToolExecutor(
            self.registry, ledger=self.ledger,
            journal=journal if _flag(cfg, "tool_execution.persistence_enabled")
            else None)
        self._readbacks: dict[tuple[str, str], Callable[[], Any]] = {}
        #: どの実行をもう報告したか。**同じ結果を二度言わない**（Phase 7B）。
        self.reports = ReportLedger()
        self.scratch: Any = None
        self._read_only_session_enabled = False

    def set_read_only_session(self, enabled: bool) -> None:
        """Enable only the bound web-search capability for this process session."""
        self._read_only_session_enabled = bool(enabled)
        configured = _flag(self.cfg, "tool_execution.tools.web_search_enabled")
        self.registry.set_enabled("web_search", configured or self._read_only_session_enabled)
        self.admission_gate.enabled = bool(
            _flag(self.cfg, "planning.enabled") or self._read_only_session_enabled)
        self.gate.enabled = bool(
            _flag(self.cfg, "tool_execution.enabled") or self._read_only_session_enabled)

    # -- 実物を繋ぐ -----------------------------------------------------

    def bind_search(self, handler: Callable[..., Any]) -> None:
        """検索を繋ぐ。**最初の垂直スライスはこれ1つ**（第27項）。"""
        self.executor.bind("web_search", "search", handler)

    def bind_game_state(self, handler: Callable[..., Any]) -> None:
        self.executor.bind("game_state_read_only", "read", handler)

    def bind_vision(self, handler: Callable[..., Any]) -> None:
        self.executor.bind("vision_read_only", "describe", handler)

    def bind_readback(self, tool_id: str, operation_id: str,
                      handler: Callable[[], Any]) -> None:
        """やった後に確かめる手。**無ければ `UNVERIFIED` のまま。**"""
        self._readbacks[(str(tool_id), str(operation_id))] = handler

    def bind_scratch(self, root: Any = None) -> bool:
        """試験用の実書き込みを繋ぐ（Phase 7B）。

        **`test_scratch_root` が設定されていなければ繋がない。**
        既定の書き先を勝手に決めると、「試したつもり」が本物の
        プロジェクトファイルの隣に落ちる。
        """
        from neuro_voice.cognition.scratch_tool import (
            ScratchWriter, scratch_handler,
        )

        configured = root if root is not None else (
            self.cfg.get("tools.test_scratch_root", "") if self.cfg else "")
        writer = ScratchWriter(configured or None)
        if not writer.enabled:
            return False
        self.scratch = writer
        self.executor.bind("test_scratch_create_text", "create",
                           scratch_handler(writer))
        return True

    # -- 使う -----------------------------------------------------------

    def propose(self, intent: ActionIntent, *, goal_id: str = "",
                goal_status: Any = GoalStatus.ACTIVE) -> ToolTurn:
        """計画を作って受け入れ判定まで。**まだ実行しない。**"""
        plan = build_plan(
            goal_id=goal_id or intent.goal_id or "goal:adhoc",
            intents=[intent],
            requester_person_id=intent.requester_person_id,
            max_replans=int((self.cfg.get("planning.max_replans", 1)
                             if self.cfg else 1) or 1))
        admission = self.admission_gate.admit(
            plan, goal_status=goal_status,
            pending_confirmations=self.confirmations.pending_count)
        turn = ToolTurn(stage=str(admission.verdict), reason=admission.reason,
                        plan=plan, admission=admission)
        if _flag(self.cfg, "planning.bounded_plans_enabled"):
            # **候補の列へ載せる。** ここから先は他の行動と同じ扱い。
            turn.candidates = plan_candidates(plan, admission)
        if admission.verdict is AdmissionVerdict.REQUIRE_CONFIRMATION:
            turn.confirmation = self._open_confirmation(plan)
        self._persist_plan(plan)
        return turn

    def execute(self, turn: ToolTurn, *, approver_person_id: str = "",
                wait: float | None = 20.0, user_awaiting: bool = True) -> ToolTurn:
        """門を通して実行する。**ここが唯一の入口。**"""
        plan = turn.plan
        if plan is None or plan.current is None:
            turn.stage, turn.reason = "no_step", "実行する手が無い"
            return turn
        step = plan.current
        intent = step.intent
        _tool, operation = self.registry.resolve(intent.tool_id,
                                                 intent.operation_id)
        if operation is None:
            turn.stage, turn.reason = "denied", "operation_not_allowed"
            return turn
        if not self._effect_allowed(operation.effect_category):
            turn.stage, turn.reason = "denied", "effect_category_disabled"
            return turn

        normalised = normalise_parameters(operation.parameters,
                                          intent.parameters)
        permission = decide_permission(
            intent.operation_id, category=operation.action_category,
            target_ids=normalised.target_ids or intent.target_ids,
            action_id=intent.step_id)
        turn.permission = permission
        confirmation_id = (turn.confirmation.confirmation_id
                           if turn.confirmation else "")
        verdict = self.gate.check(
            intent, permission=permission, confirmation_id=confirmation_id,
            plan=plan, approver_person_id=approver_person_id)
        turn.gate_verdict = verdict
        if not verdict.allowed:
            turn.stage, turn.reason = "denied", verdict.rejection_reason
            return turn

        if confirmation_id:
            # **一度の実行で使い切る。** 実行の前に印を付ける——
            # 後だと、途中で落ちた時に「はい」が生き残る。
            self.confirmations.consume(confirmation_id)

        single_attempt = (
            self._read_only_session_enabled
            and intent.tool_id == "web_search"
            and intent.operation_id == "search"
        )
        handle = self.executor.submit(
            verdict.request, max_retries_override=0 if single_attempt else None)
        result = self.executor.wait(handle, timeout=wait)

        # **この順番を崩さない**（第2項）。
        # 1. 結果を保存 → 2. 検証 → 3. Step → 4. Goal/World →
        # 5. 出来事 → 6. Action Selector（呼び出し側）
        # 発話してから成功状態へ更新する形にすると、途中で落ちた時に
        # 「言ったのに記録が無い」が残る。
        mark = time.perf_counter()
        verification = verify_result(
            operation, result, readback=self._readback_for(intent, result))
        turn.latency_ms["outcome_verification_ms"] = round(
            (time.perf_counter() - mark) * 1000, 2)

        outcome = apply_tool_outcome(plan, step, result, verification)
        turn.result, turn.verification, turn.outcome = (
            result, verification, outcome)

        mark = time.perf_counter()
        turn.event = build_outcome_event(
            result=result, verification=verification, intent=intent,
            plan_id=plan.plan_id, step_id=step.step_id, operation=operation,
            reversible=(str(operation.effect_category)
                        == str(EffectCategory.LOCAL_REVERSIBLE)),
            rollback_reference=str(
                (result.structured_output or {}).get("created_path", ""))[:200])
        turn.latency_ms["tool_outcome_event_ms"] = round(
            (time.perf_counter() - mark) * 1000, 2)
        turn.latency_ms["tool_execution_ms"] = result.duration_ms

        if _flag(self.cfg, "tools.result_dialogue_enabled"):
            mark = time.perf_counter()
            turn.report_candidates = report_candidates(
                turn.event, user_awaiting=user_awaiting,
                recovery_available=self._recovery_available(
                    plan, operation, result),
                reported=self.reports.reported(result.execution_id))
            turn.latency_ms["tool_result_action_selection_ms"] = round(
                (time.perf_counter() - mark) * 1000, 2)
        else:
            self.reports.suppress(result.execution_id,
                                  "result_dialogue_disabled")

        turn.stage = ("succeeded" if turn.spoke_safely
                      else str(result.status))
        turn.reason = verification.reason
        self._persist_plan(plan)
        self._mark_result_reportable(result)
        return turn

    # -- 結果を伝える ---------------------------------------------------

    def speech_request(self, turn: ToolTurn, action: Any, *,
                       user_speaking: bool = False,
                       closure_suppressed: str = "") -> Any:
        """発話要求を1つ作る。**`ToolResult` からは作れない形にしてある。**

        引数に `ToolTurn` ではなく `ToolResult` を渡す入口を用意しない。
        出来事へ変換され、候補になり、決定されたものだけがここへ来る。
        """
        from neuro_voice.cognition.rollout import SpeechRequest, SpeechSource

        event = turn.event
        if event is None:
            return None
        return SpeechRequest(
            source_type=SpeechSource.COGNITIVE_TOOL_OUTCOME,
            source_action=action,
            confidence=.9 if event.verified else .65,
            execution_id=event.execution_id,
            tool_outcome_event_id=event.event_id,
            conversation_id=event.conversation_id,
            channel_id=event.channel_id,
            outcome_verified=event.verified,
            already_reported=self.reports.reported(event.execution_id),
            participant_ids=((event.requester_person_id,)
                             if event.requester_person_id else ()),
            user_speaking=bool(user_speaking),
            closure_suppressed=str(closure_suppressed or ""))

    def claim_report(self, turn: ToolTurn) -> bool:
        """報告してよいか。**取れるのは1回だけ**（二重報告防止）。"""
        if turn.event is None:
            return False
        return self.reports.claim(turn.event.execution_id)

    def _readback_for(self, intent: ActionIntent,
                      result: ToolResult) -> Callable[[], Any] | None:
        """**「作った」を、作った物を見て確かめる。**

        ツールが返した `SUCCEEDED` を根拠にしない。書いたと言っている
        場所を、こちらで読み直す。
        """
        registered = self._readbacks.get((intent.tool_id, intent.operation_id))
        if registered is not None:
            return registered
        if self.scratch is None:
            return None
        relative = str((result.structured_output or {}).get(
            "relative_path", "") or "")
        if not relative:
            return None
        return lambda: self.scratch.exists(relative)

    def _recovery_available(self, plan: BoundedPlan, operation: Any,
                            result: ToolResult) -> bool:
        """やり直しを聞いてよいか。**読むだけの失敗に限る。**"""
        if not _flag(self.cfg, "tools.bounded_replan_enabled"):
            return False
        from neuro_voice.cognition.planning import can_replan

        reason = ("target_not_found"
                  if str(result.status) == str(ToolStatus.FAILED)
                  else "tool_temporarily_unavailable")
        return can_replan(
            plan, reason, effect_category=operation.effect_category,
            side_effect_confirmed=result.side_effect_confirmed).allowed

    def _mark_result_reportable(self, result: ToolResult) -> None:
        if self.journal is None or not _flag(
                self.cfg, "tool_execution.persistence_enabled"):
            return
        marker = getattr(self.journal, "mark_execution_reported", None)
        if marker is None or not self.reports.reported(result.execution_id):
            return
        marker(result.execution_id)

    def cancel(self, turn: ToolTurn, reason: str = "user_cancelled") -> str:
        """止める。**止まらないものを「止めた」と言わない**（第18項）。"""
        if turn.confirmation is not None:
            self.confirmations.cancel(turn.confirmation.confirmation_id, reason)
        state = "no_execution"
        if turn.result is not None:
            state = str(self.executor.cancel(turn.result.execution_id))
        if turn.plan is not None and turn.plan.active:
            turn.plan.close(PlanStatus.CANCELLED, reason)
            self._persist_plan(turn.plan)
        turn.stage = "cancelled"
        return state

    def close_turn(self, reason: str = "turn_closed") -> int:
        """会話が閉じた。**未承認を持ち越さない**（第26項）。"""
        return self.confirmations.cancel_all(reason)

    def recover(self) -> dict[str, int]:
        """起動時。**副作用操作を勝手に再開しない**（第24項）。"""
        out = {"executions_unknown": 0, "confirmations_restored": 0}
        if self.journal is None:
            return out
        from neuro_voice.cognition.tool_exec import PROCESS_ID

        marker = getattr(self.journal, "mark_executions_unknown", None)
        if marker is not None:
            out["executions_unknown"] = int(marker(PROCESS_ID) or 0)
        reader = getattr(self.journal, "confirmations", None)
        if reader is not None:
            out["confirmations_restored"] = self.confirmations.restore(
                reader(pending_only=True))
        ledger_rows = getattr(self.journal, "tool_executions", None)
        if ledger_rows is not None:
            self.ledger.restore(ledger_rows())
        return out

    def shutdown(self) -> None:
        self.executor.shutdown()

    # -- 内部 -----------------------------------------------------------

    def _effect_allowed(self, effect: Any) -> bool:
        """副作用の重さごとの許可。**上げていないものは通さない。**"""
        key = str(effect)
        if key in {str(EffectCategory.INTERNAL), str(EffectCategory.READ_ONLY)}:
            return (_flag(self.cfg, "tool_execution.read_only_enabled")
                    or (self._read_only_session_enabled
                        and key == str(EffectCategory.READ_ONLY)))
        if key == str(EffectCategory.DESTRUCTIVE):
            return _flag(self.cfg, "tool_execution.destructive_enabled")
        return _flag(self.cfg, "tool_execution.external_write_enabled")

    def _open_confirmation(self, plan: BoundedPlan) -> ConfirmationRequest | None:
        if not _flag(self.cfg, "confirmation.enabled"):
            return None
        step = plan.current
        if step is None:
            return None
        _tool, operation = self.registry.resolve(step.intent.tool_id,
                                                 step.intent.operation_id)
        if operation is None:
            return None
        normalised = normalise_parameters(operation.parameters,
                                          step.intent.parameters)
        request = self.confirmations.open(
            step.intent, parameters=normalised.values,
            loggable=normalised.loggable,
            target_ids=normalised.target_ids or step.intent.target_ids)
        step.confirmation_id = request.confirmation_id
        return request

    def _persist_plan(self, plan: BoundedPlan) -> None:
        if self.journal is None or not _flag(
                self.cfg, "tool_execution.persistence_enabled"):
            return
        saver = getattr(self.journal, "save_plan", None)
        if saver is None:
            return
        import json

        saver({
            "plan_id": plan.plan_id, "goal_id": plan.goal_id,
            "status": str(plan.status),
            "requester_person_id": plan.requester_person_id,
            "replan_count": plan.replan_count,
            "step_count": len(plan.steps),
            # **機密値は入っていない形だけ**（`loggable` と同じ扱い）。
            "steps_json": json.dumps([item.snapshot() for item in plan.steps],
                                     ensure_ascii=False),
            "closed_reason": plan.closed_reason,
            "created_at": plan.created_at, "updated_at": plan.updated_at,
        })


def search_intent(query: str, *, person_id: str, resolution: str,
                  rationale: str = "") -> ActionIntent:
    """**最初に繋ぐ実ツール**（第27項）。読むだけ・副作用なし。"""
    from neuro_voice.search.intent import route_search_request

    value = str(query)
    route = route_search_request(value)
    clean_query = route.query if route.query else value.strip()
    return ActionIntent(
        tool_id="web_search", operation_id="search",
        parameters={"query": clean_query[:200]},
        effect_category=EffectCategory.READ_ONLY,
        requester_person_id=str(person_id),
        requester_resolution=str(resolution),
        rationale_summary=str(rationale)[:80])


__all__ = ["ToolRuntime", "ToolTurn", "search_intent"]
