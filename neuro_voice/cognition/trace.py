"""1ターンの認知処理を、後から追える形で残す。

**長い思考過程もプロンプト全文も会話本文も残さない**（第12条）。
残すのは、状態の数値・候補の点数・選んだ理由・所要時間だけ。
それだけで「なぜこの行動だったか」と「認知層がどれだけ時間を使ったか」は
読める。

既存の `logs/conversation_metrics.jsonl` と同じ書き方に揃えてある
（1行1JSON、追記のみ）。設定で止められる。

あわせて、状態の変化量に上限を掛ける `StateLimit` もここに置く。
記録と制限を別ファイルにすると、片方だけ直して食い違う。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StateLimit:
    """1イベントでどれだけ動かしてよいか。

    上げ幅と下げ幅を分けているのは、**信頼は失うのが速く戻すのが遅い**という
    非対称を持たせたいから。同じ上限にすると、一度の否定的な出来事で
    積み上げた関係が飛ぶ。
    """

    baseline: float = .0
    max_positive: float = .02
    max_negative: float = .02
    #: 何もなければ baseline へ戻る速さ（1ターンあたり）。
    recovery: float = .01

    def apply(self, current: float, delta: float) -> float:
        capped = max(-self.max_negative, min(self.max_positive, float(delta)))
        moved = float(current) + capped
        # 変化が無いターンは、ゆっくり基準値へ戻る。
        if capped == 0 and self.recovery:
            gap = self.baseline - moved
            moved += max(-self.recovery, min(self.recovery, gap))
        return max(0.0, min(1.0, moved))


#: 既定の上限。**ここ以外に書かない。**
#: 安全に関わる判断（拒否・警告）はこれらの値に左右させないこと。
STATE_LIMITS: dict[str, StateLimit] = {
    "trust": StateLimit(baseline=.5, max_positive=.01, max_negative=.02, recovery=.005),
    "comfort": StateLimit(baseline=.5, max_positive=.02, max_negative=.02, recovery=.005),
    "irritation": StateLimit(baseline=.05, max_positive=.03, max_negative=.03, recovery=.02),
    "distress": StateLimit(baseline=.0, max_positive=.05, max_negative=.05, recovery=.03),
}


@dataclass(slots=True)
class StateChange:
    """1つの値の変化。**理由と出どころを必ず添える。**"""

    key: str
    before: float
    after: float
    reason: str
    source_event_id: str = ""

    @property
    def delta(self) -> float:
        return round(self.after - self.before, 4)

    def snapshot(self) -> dict[str, Any]:
        return {
            "key": self.key, "delta": self.delta,
            "after": round(self.after, 3), "reason": self.reason,
            "source_event_id": self.source_event_id,
        }


def limited_change(
    key: str, current: float, delta: float, *, reason: str, source_event_id: str = "",
    limits: dict[str, StateLimit] | None = None,
) -> StateChange:
    """上限を掛けたうえで変化を作る。"""
    limit = (limits or STATE_LIMITS).get(key) or StateLimit()
    return StateChange(
        key=key, before=float(current), after=limit.apply(current, delta),
        reason=reason, source_event_id=source_event_id,
    )


@dataclass(slots=True)
class CognitiveTrace:
    """1ターン分。**本文は入れない。**"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turn_id: str = ""
    started_at: float = field(default_factory=time.time)
    input_event_ids: tuple[str, ...] = ()
    state_snapshot_summary: dict[str, Any] = field(default_factory=dict)
    generated_candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_action: str = ""
    supporting_action: str = ""
    decision_scores: dict[str, float] = field(default_factory=dict)
    decision_reason: str = ""
    planner_input: dict[str, Any] = field(default_factory=dict)
    execution_status: str = ""
    outcome_summary: dict[str, Any] = field(default_factory=dict)
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    latency_breakdown: dict[str, float] = field(default_factory=dict)
    llm_diagnostics: dict[str, Any] = field(default_factory=dict)
    #: 実経路の TurnMetrics。本文なし・合計は speech_end → play_start。
    turn_latency: dict[str, Any] = field(default_factory=dict)
    #: source 別の移動 p50/p90/p95（現在の窓）。
    latency_summary: list[dict[str, Any]] = field(default_factory=list)
    cognition_enabled: bool = False
    rollout_mode: str = "disabled"
    requested_rollout_mode: str = "disabled"
    execution_path: str = "legacy"
    cognition_session_epoch: int = 0
    cognition_activation_source: str = "config"
    cognition_config_fingerprint: str = ""
    transport: str = "LOCAL"
    action_selector_used: bool = False
    speech_gate_active: bool = False
    legacy_response_path_used: bool = True
    speech_request_count: int = 0
    fallback_used: bool = False
    cognitive_fallback_reason: str = ""
    fallback_count: int = 0
    side_effect_state: dict[str, bool] = field(default_factory=dict)

    # -- 記憶 -----------------------------------------------------------
    #
    # **どの記憶がどのスコアを動かしたか**を読めるようにする。
    # 記憶が効いているのかどうかを後から確かめられないと、
    # 「入れたつもり」のまま何も変わっていない状態に気づけない。
    memory_retrieval_trigger: str = ""
    memory_trigger_result: str = ""
    memory_trigger_reason: str = ""
    retrieval_query_feature_hash: str = ""
    memory_store_bound_persona_id: str = ""
    memory_store_path_hash: str = ""
    retrieval_candidate_count: int = 0
    retrieval_after_scope_count: int = 0
    retrieval_after_lexical_count: int = 0
    retrieval_after_semantic_count: int = 0
    retrieval_ranked_count: int = 0
    retrieval_selected_count: int = 0
    retrieval_rejection_reasons: list[str] = field(default_factory=list)
    retrieval_representation_status_counts: dict[str, int] = field(default_factory=dict)
    recall_status: str = "NOT_APPLICABLE"
    recall_llm_call_count: int = 0
    recall_evidence_memory_ids: list[int] = field(default_factory=list)
    retrieval_query_summary: dict[str, Any] = field(default_factory=dict)
    retrieved_memory_ids: list[int] = field(default_factory=list)
    memory_relevance_scores: dict[str, float] = field(default_factory=dict)
    memory_effect_on_actions: dict[str, Any] = field(default_factory=dict)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)
    memory_write_decisions: list[dict[str, Any]] = field(default_factory=list)
    obligation_retrieval_considered: bool = False
    obligation_retrieval_triggered: bool = False
    obligation_retrieval_reason: str = "not_applicable"
    obligation_candidate_count: int = 0
    obligation_relevance_score: float = 0.0
    obligation_effect_on_action: dict[str, Any] = field(default_factory=dict)
    duplicate_resolution: list[str] = field(default_factory=list)
    #: SpeechRequest → TTS → Playback の本文を含まない件数。
    speech_delivery: dict[str, Any] = field(default_factory=dict)
    # Historical response similarity is diagnostic only.  It never proves that
    # this turn has already been delivered; no response text is persisted here.
    echo_guard_checked: bool = False
    echo_match_scope: str = ""
    primary_similarity_score: float = 0.0
    regeneration_attempted: bool = False
    regenerated_similarity_score: float = 0.0
    echo_resolution: str = ""
    final_response_empty: bool = False
    speech_generation_completed: bool = False
    turn_closure_completed: bool = False
    action_outcome_status: str = ""
    delivery_failure_stage: str = ""
    delivery_finalize_called: bool = False
    delivery_terminal_state: str = ""
    playback_started: bool = False
    playback_completed: bool = False
    trace_delivery_finalized: bool = False
    delivery_finalize_error: str = ""
    # A silence is valid only when its decision provenance is complete. These
    # values are reason codes only; they never contain input or response text.
    action_decision_id: str = ""
    decision_source: str = ""
    silence_reason_code: str = ""
    suppression_reason: str = ""
    response_required: bool = False
    response_requirement_source: str = ""
    closure_signal: bool = False
    closure_confidence: float = 0.0
    direct_question: bool = False
    explicit_answer_request: bool = False
    new_task_instruction: bool = False
    safety_warning: bool = False
    response_obligation: str = "OPTIONAL"
    eligible_actions: list[str] = field(default_factory=list)
    candidate_action_scores: dict[str, float] = field(default_factory=dict)
    initially_selected_action: str = ""
    action_constraint_result: str = "NOT_APPLICABLE"
    corrected_action: str = ""
    final_selected_action: str = ""
    closure_response_policy: str = "adaptive"
    action_selection_reason: str = ""
    reflection_ids_used: list[str] = field(default_factory=list)
    reflection_effect_on_state: dict[str, float] = field(default_factory=dict)

    # -- 人格の分離 (Phase 7D ⑤⑥) ---------------------------------------
    #
    # **「Bで0件だった」を目で確かめられるようにする。**
    # 分離が効いているかは、取れた記憶の持ち主を見ないと分からない。
    # 本文は入れない——ID・持ち主・スコープ・件数・理由だけ（第12条）。
    active_persona_id: str = ""
    active_persona_version: str = ""
    persona_epoch: int = 0
    #: 取れた記憶の持ち主。**ここに他人が混ざっていたら漏れている。**
    retrieved_memory_persona_ids: list[str] = field(default_factory=list)
    retrieved_memory_scopes: list[str] = field(default_factory=list)
    #: 他ペルソナのものとして**弾いた**件数。0 なら候補に他人が居なかった。
    cross_persona_rejection_count: int = 0
    cross_persona_rejection_persona_ids: list[str] = field(default_factory=list)
    #: 持ち主不明（旧データ）として弾いた件数。移行漏れの目印。
    legacy_unscoped_rejection_count: int = 0
    reflection_persona_ids: list[str] = field(default_factory=list)
    source_memory_persona_ids: list[str] = field(default_factory=list)
    persona_switch_state: str = ""
    persona_switch_from: str = ""
    persona_switch_to: str = ""
    persona_switch_epoch: int = 0

    # -- 持続する内面 (Phase 4) -----------------------------------------
    #
    # **どの軸がどの行動を動かしたか**を読めるようにする。感情が効いている
    # のかどうかを確かめられないと、「入れたつもり」で何も変わっていない
    # 状態に気づけない——実際 `irritation` は常に 0.0 のままだった。
    internal_state_version: int = 0
    internal_state_summary: dict[str, Any] = field(default_factory=dict)
    state_dimensions_used: list[str] = field(default_factory=list)
    event_appraisal: str = ""
    proposed_state_deltas: list[dict[str, Any]] = field(default_factory=list)
    applied_state_deltas: list[dict[str, Any]] = field(default_factory=list)
    delta_clamp_reasons: list[str] = field(default_factory=list)
    decay_applied: dict[str, float] = field(default_factory=dict)
    relationship_target_id: str = ""
    state_effect_on_actions: dict[str, Any] = field(default_factory=dict)
    state_effect_on_planner: dict[str, Any] = field(default_factory=dict)

    #: 自分から話す判断 (Phase 5)。**なぜ黙ったかも残す。**
    #: 「機会は出ていたが全部抑制された」と「機会が1つも無かった」は違う。
    initiative_summary: dict[str, Any] = field(default_factory=dict)
    speech_gate_result: str = ""
    cancel_reason: str = ""

    #: いまどうなっているか / 何を目指しているか (Phase 6)。
    #: **画面全文も音声全文も内部思考文も入れない**（第12条）。
    world_state_summary: dict[str, Any] = field(default_factory=dict)
    world_state_deltas: list[dict[str, Any]] = field(default_factory=list)
    goal_summary: list[dict[str, Any]] = field(default_factory=list)
    goal_admission_decision: dict[str, Any] = field(default_factory=dict)
    goal_effect_on_actions: dict[str, float] = field(default_factory=dict)
    permission_required: list[str] = field(default_factory=list)
    completion_evidence: str = ""

    # -- 実経路への接続 (Phase 6B) ---------------------------------------
    #
    # **「観測が来た」と「世界状態が動いた」と「保存された」は別々に残す。**
    # 前は3つまとめて「動いている」と言えてしまい、途中で切れていても
    # 気づけなかった。呼ばれたかどうか自体を記録する。
    observation_source: str = ""
    source_event_id: str = ""
    observation_adapter: str = ""
    observe_entity_called: bool = False
    observe_fact_called: bool = False
    observation_dropped: dict[str, int] = field(default_factory=dict)
    world_state_delta_id: str = ""
    #: `None` は「保存を試みていない」。`False` は**試して失敗した**。
    world_state_persisted: bool | None = None
    world_state_persistence_error: str = ""
    goal_id: str = ""
    obligation_id: str = ""
    goal_obligation_sync: str = ""
    next_action_id: str = ""
    permission_category: str = ""
    permission_result: str = ""
    permission_reason: str = ""
    #: どこまで実際に通ったか。止まった所がそのまま残る。
    vertical_slice_stage: str = ""

    # -- 入力元ごとの差 (Phase 6C) ---------------------------------------
    #
    # **どの入力元が動いていて、どれが黙っているか**を1行で読めるようにする。
    # 「世界状態が空」だけ見えても、Discordが繋がっていないのか
    # 映像が来ていないのかが分からない。
    discord_participant_id: str = ""
    participant_presence_delta: str = ""
    vision_scene_type: str = ""
    vision_scene_transition: str = ""
    ktane_event_type: str = ""
    ktane_entity_ids: list[str] = field(default_factory=list)
    ktane_fact_ids: list[str] = field(default_factory=list)
    ktane_goal_id: str = ""
    speech_source_type: str = ""
    suppression_reason: str = ""

    # -- 誰が居るのか / 誰なのか (Phase 6D) -------------------------------
    #
    # **接続が言ったことと、声紋が推したことを、別の欄に残す。**
    # 同じ欄に入れると、後から「なぜこの人だと思ったのか」が読めない。
    presence_event_type: str = ""
    #: 接続元が保証した出来事か。**推測の退出と区別する。**
    presence_authoritative: bool = False
    presence_update_result: str = ""
    greeting_opportunity_id: str = ""
    greeting_action_selected: str = ""
    greeting_suppression_reason: str = ""
    transport_identity: str = ""
    voice_identity: str = ""
    resolved_person_id: str = ""
    identity_resolution_status: str = ""
    identity_link_id: str = ""
    identity_conflict: str = ""
    identity_link_status_change: str = ""
    #: 接続が保証する人数。**言い切ってよい方。**
    authoritative_participant_count: int = 0
    #: 声から推した人数。**言い切ってはいけない方。**
    estimated_participant_count: int = 0
    vision_fact_id: str = ""
    vision_fact_ttl: float = .0

    # -- 関係値の移行 (Phase 6E) ------------------------------------------
    #
    # **どちらを読んで、どちらへ書いたか**を毎ターン残す。
    # 移行の途中で挙動が変わった時、どの段で何が起きたかを
    # 後から言い当てられないと、戻す判断ができない。
    migration_mode: str = ""
    relationship_read_source: str = ""
    relationship_write_targets: list[str] = field(default_factory=list)
    #: legacy と person の差。**軸の数と最大値だけ。** 値そのものは残さない。
    shadow_read_difference: dict[str, float] = field(default_factory=dict)
    migration_record_id: str = ""
    migration_status: str = ""
    migration_conflict: str = ""
    legacy_write_result: str = ""
    person_write_result: str = ""
    fallback_reason: str = ""
    rollback_applied: bool = False

    # -- 移行の操作と、記憶の範囲 (Phase 6F) ------------------------------
    #
    # **どの範囲で記憶を引いたか**を残す。人物単位にすると別人の過去が
    # 混ざりうるので、どの `speaker_id` を束ねたのかが読めないと
    # 混ざっても気づけない。
    migration_ui_operation: str = ""
    migration_transition_from: str = ""
    migration_transition_to: str = ""
    migration_job_id: str = ""
    migration_job_status: str = ""
    #: 昇格を止めた条件。空なら止めていない。
    migration_promotion_gate: list[str] = field(default_factory=list)
    migration_rejection_reason: str = ""
    identity_scope_person_id: str = ""
    identity_scope_speaker_ids: list[str] = field(default_factory=list)
    memory_retrieval_mode: str = ""
    legacy_memory_result_count: int = 0
    person_scope_result_count: int = 0
    deduplicated_memory_count: int = 0
    revoked_ids_excluded: list[str] = field(default_factory=list)
    speaker_status_resolution: str = ""
    speaker_status_ui_source: str = ""
    #: 作業の状態が残ったか。**残らないと、次に何をすべきか分からない。**
    migration_job_persisted: bool = False
    migration_job_recovered: bool = False
    migration_job_resume_from_cursor: str = ""
    migration_job_recovery_action: str = ""
    memory_result_count_before_dedup: int = 0
    memory_id_duplicates_removed: int = 0
    memory_fingerprint_duplicates_removed: int = 0
    memory_result_count_after_dedup: int = 0
    #: 落とした記憶。**IDだけ。本文は入れない**（第12条）。
    duplicate_memory_ids: list[int] = field(default_factory=list)
    possible_near_duplicates: list[int] = field(default_factory=list)

    # -- 道具 (Phase 7) --------------------------------------------------
    #
    # **後から「なぜ実行された／されなかった」を1行で読めるように。**
    # 門が拒んだ理由が残らないと、拒否と「そもそも来なかった」の
    # 区別がつかず、門が効いているのかどうか確かめようがない。
    # 認証情報・引数の値・メッセージ本文は入れない（第30項）。
    plan_goal_id: str = ""
    plan_id: str = ""
    plan_step_id: str = ""
    action_intent_id: str = ""
    requester_person_id: str = ""
    tool_id: str = ""
    operation_id: str = ""
    effect_category: str = ""
    plan_admission_verdict: str = ""
    permission_result: str = ""
    confirmation_id: str = ""
    confirmation_status: str = ""
    confirmation_hash_match: bool = False
    tool_gate_result: str = ""
    tool_gate_rejection_reason: str = ""
    execution_id: str = ""
    idempotency_key: str = ""
    tool_result_status: str = ""
    tool_result_attempts: int = 0
    tool_result_used_by_planner: bool = False
    normalized_result_count: int = 0
    selected_result_ids: list[str] = field(default_factory=list)
    selected_result_domains: list[str] = field(default_factory=list)
    result_presenter_type: str = ""
    final_response_grounded: bool = False
    outcome_verified: bool = False
    outcome_verification_method: str = ""
    tool_output_instruction_like: bool = False
    replan_count: int = 0
    replan_reason: str = ""
    goal_status_change: str = ""
    cancel_state: str = ""

    def note_plan(self, plan: Any, admission: Any = None) -> None:
        if plan is not None:
            self.plan_id = str(getattr(plan, "plan_id", ""))
            self.plan_goal_id = str(getattr(plan, "goal_id", ""))
            self.replan_count = int(getattr(plan, "replan_count", 0) or 0)
            self.requester_person_id = str(
                getattr(plan, "requester_person_id", ""))
            step = getattr(plan, "current", None)
            if step is not None:
                self.plan_step_id = str(getattr(step, "step_id", ""))
                intent = getattr(step, "intent", None)
                if intent is not None:
                    self.action_intent_id = str(getattr(intent, "intent_id", ""))
                    self.tool_id = str(getattr(intent, "tool_id", ""))
                    self.operation_id = str(getattr(intent, "operation_id", ""))
                    self.effect_category = str(
                        getattr(intent, "effect_category", ""))
        if admission is not None:
            self.plan_admission_verdict = str(getattr(admission, "verdict", ""))
            self.mark("plan_admission_ms",
                      float(getattr(admission, "decided_ms", .0) or .0))

    def note_permission(self, decision: Any) -> None:
        if decision is None:
            return
        self.permission_result = str(getattr(decision, "result", ""))
        self.mark("permission_decision_ms",
                  float(getattr(decision, "decided_ms", .0) or .0))

    def note_confirmation(self, request: Any, verdict: Any = None,
                          *, hash_match: bool | None = None) -> None:
        if request is not None:
            self.confirmation_id = str(getattr(request, "confirmation_id", ""))
            self.confirmation_status = str(getattr(request, "status", ""))
        if verdict is not None and not self.confirmation_status:
            self.confirmation_status = (
                "approved" if getattr(verdict, "accepted", False)
                else str(getattr(verdict, "reason", "")))
        if hash_match is not None:
            self.confirmation_hash_match = bool(hash_match)

    def note_tool_gate(self, verdict: Any) -> None:
        if verdict is None:
            return
        allowed = bool(getattr(verdict, "allowed", False))
        self.tool_gate_result = "allow" if allowed else "deny"
        self.tool_gate_rejection_reason = str(
            getattr(verdict, "rejection_reason", ""))[:60]
        if "confirmation_hash_match" in tuple(getattr(verdict, "passed", ())):
            self.confirmation_hash_match = True
        request = getattr(verdict, "request", None)
        if request is not None:
            self.execution_id = str(getattr(request, "execution_id", ""))
            self.idempotency_key = str(getattr(request, "idempotency_key", ""))
        self.mark("tool_gate_ms", float(getattr(verdict, "decided_ms", .0) or .0))

    def note_tool_outcome(self, outcome: Any) -> None:
        """やった結果。**成功したかどうかは、検証まで見て決める。**"""
        if outcome is None:
            return
        result = getattr(outcome, "result", None)
        if result is not None:
            self.execution_id = str(getattr(result, "execution_id", "")) \
                or self.execution_id
            self.tool_result_status = str(getattr(result, "status", ""))
            self.tool_result_attempts = int(getattr(result, "attempts", 0) or 0)
            self.tool_output_instruction_like = bool(
                getattr(result, "instruction_like_output", False))
            self.cancel_state = str(getattr(result, "cancel_state", ""))
            # **外部の待ち時間は内部処理と分けて記録する**（第31項）。
            self.mark("tool_execution_ms",
                      float(getattr(result, "duration_ms", .0) or .0))
        verification = getattr(outcome, "verification", None)
        if verification is not None:
            self.outcome_verified = bool(getattr(verification, "verified", False))
            self.outcome_verification_method = str(
                getattr(verification, "method", ""))
        self.replan_reason = str(getattr(outcome, "replan_reason", ""))
        self.goal_status_change = str(getattr(outcome, "goal_status_change", ""))
        for delta in tuple(getattr(outcome, "world_state_deltas", ()) or ())[:4]:
            self.world_state_deltas.append(dict(delta))

    def note_tool_result_usage(self, *, used_by_planner: bool) -> None:
        """Record only whether the verified result informed the final route."""
        self.tool_result_used_by_planner = bool(used_by_planner)

    def note_search_result_presentation(self, presentation: Any, *,
                                        normalized_count: int,
                                        elapsed_ms: float) -> None:
        """Record result use without recording result titles, URLs or snippets."""
        self.normalized_result_count = max(0, int(normalized_count))
        selected = getattr(presentation, "selected", None)
        self.selected_result_ids = ([str(getattr(selected, "result_id", ""))]
                                    if selected is not None else [])
        self.selected_result_domains = ([str(getattr(selected, "normalized_domain", ""))]
                                        if selected is not None else [])
        self.result_presenter_type = str(getattr(presentation, "presenter_type", ""))
        self.final_response_grounded = bool(getattr(presentation, "grounded", False))
        self.mark("tool_result_to_response_ms", elapsed_ms)

    # -- 結果を会話へ戻す (Phase 7B) --------------------------------------
    #
    # **報告しなかった理由が残らないと、「黙った」と「壊れていた」の
    # 区別がつかない。** Phase 7 で結果が止まっていたことに気づけたのも、
    # 記録があったから。
    tool_result_received: bool = False
    tool_outcome_event_id: str = ""
    result_redaction_applied: bool = False
    instruction_like_detected: bool = False
    plan_state_after_result: str = ""
    goal_state_after_result: str = ""
    tool_result_action_candidates: list[str] = field(default_factory=list)
    selected_tool_result_action: str = ""
    tool_result_speech_source: str = ""
    tool_result_speech_gate_result: str = ""
    tool_result_reported: bool = False
    tool_result_report_suppression_reason: str = ""
    discord_requester_match: bool = False
    discord_channel_match: bool = False
    confirmation_binding_result: str = ""
    replan_requested: bool = False
    replan_of: str = ""

    def note_tool_result_dialogue(
        self, event: Any, *, candidates: Any = (), selected: Any = None,
        gate_result: str = "", reported: bool = False,
        suppression_reason: str = "", plan: Any = None,
    ) -> None:
        """結果が会話まで来たか。**本文も引数も入れない**（第30項）。"""
        if event is None:
            return
        self.tool_result_received = True
        self.tool_outcome_event_id = str(getattr(event, "event_id", ""))
        self.execution_id = str(getattr(event, "execution_id", "")) \
            or self.execution_id
        payload = getattr(event, "payload", None)
        if payload is not None:
            self.result_redaction_applied = bool(
                getattr(payload, "redaction_applied", False))
            self.instruction_like_detected = bool(
                getattr(payload, "diagnostic_fields", {}).get(
                    "instruction_like", False))
        self.tool_result_action_candidates = [
            str(getattr(item, "action_type", "")) for item in (candidates or ())
        ][:6]
        if selected is not None:
            self.selected_tool_result_action = str(selected)
            self.tool_result_speech_source = "cognitive_tool_outcome"
        self.tool_result_speech_gate_result = str(gate_result)[:60]
        self.tool_result_reported = bool(reported)
        self.tool_result_report_suppression_reason = str(suppression_reason)[:60]
        if plan is not None:
            self.plan_state_after_result = str(getattr(plan, "status", ""))
            self.replan_of = str(getattr(plan, "replan_of", ""))
            self.replan_count = int(getattr(plan, "replan_count", 0) or 0)
        self.goal_state_after_result = self.goal_status_change

    def note_delivery_match(self, *, requester_match: bool,
                            channel_match: bool,
                            confirmation_binding: str = "") -> None:
        """**頼まれた場所へ返したか。** ここが false のまま緑にならない。"""
        self.discord_requester_match = bool(requester_match)
        self.discord_channel_match = bool(channel_match)
        self.confirmation_binding_result = str(confirmation_binding)[:40]

    def _dialogue_snapshot(self) -> dict[str, Any]:
        return {
            "received": self.tool_result_received,
            "event_id": self.tool_outcome_event_id,
            "redaction_applied": self.result_redaction_applied,
            "instruction_like": self.instruction_like_detected,
            "plan_state": self.plan_state_after_result,
            "goal_state": self.goal_state_after_result,
            "candidates": list(self.tool_result_action_candidates),
            "selected": self.selected_tool_result_action,
            "speech_source": self.tool_result_speech_source,
            "gate_result": self.tool_result_speech_gate_result,
            "reported": self.tool_result_reported,
            "suppression_reason": self.tool_result_report_suppression_reason,
            "requester_match": self.discord_requester_match,
            "channel_match": self.discord_channel_match,
            "confirmation_binding": self.confirmation_binding_result,
            "replan_requested": self.replan_requested,
            "replan_of": self.replan_of,
        }

    def _tool_snapshot(self) -> dict[str, Any]:
        """道具まわりだけの塊。**引数の値も出力本文も入れない**（第30項）。"""
        return {
            "goal_id": self.plan_goal_id,
            "plan_id": self.plan_id,
            "step_id": self.plan_step_id,
            "intent_id": self.action_intent_id,
            "tool_proposal_id": self.action_intent_id,
            "requester": self.requester_person_id,
            "tool_id": self.tool_id,
            "operation_id": self.operation_id,
            "effect": self.effect_category,
            "admission": self.plan_admission_verdict,
            "permission": self.permission_result,
            "permission_level": self.permission_result,
            "confirmation": {
                "id": self.confirmation_id,
                "status": self.confirmation_status,
                "hash_match": self.confirmation_hash_match,
            },
            "gate": {
                "result": self.tool_gate_result,
                "rejection_reason": self.tool_gate_rejection_reason,
            },
            "execution": {
                "id": self.execution_id,
                "idempotency_key": self.idempotency_key,
                "status": self.tool_result_status,
                "attempts": self.tool_result_attempts,
                "cancel": self.cancel_state,
                "instruction_like_output": self.tool_output_instruction_like,
            },
            "execution_attempt_count": self.tool_result_attempts,
            "execution_accepted_count": 1 if self.execution_id else 0,
            "tool_started": bool(self.execution_id),
            "tool_completed": bool(self.tool_result_status),
            "tool_result_available": bool(self.tool_result_status),
            "tool_result_used_by_planner": self.tool_result_used_by_planner,
            "normalized_result_count": self.normalized_result_count,
            "selected_result_ids": list(self.selected_result_ids),
            "selected_result_domains": list(self.selected_result_domains),
            "result_presenter_type": self.result_presenter_type,
            "final_response_grounded": self.final_response_grounded,
            "side_effect_committed": False,
            "fallback_considered": False,
            "fallback_attempted": False,
            "verified": self.outcome_verified,
            "verification_method": self.outcome_verification_method,
            "replan": {"count": self.replan_count, "reason": self.replan_reason},
            "goal_status_change": self.goal_status_change,
            "dialogue": self._dialogue_snapshot(),
        }

    def note_dedup(self, result: Any) -> None:
        """重複を落とした結果。**IDと件数だけ。**"""
        if result is None:
            return
        self.memory_result_count_before_dedup = int(
            getattr(result, "count_before", 0) or 0)
        self.memory_result_count_after_dedup = int(
            getattr(result, "count_after", 0) or 0)
        self.memory_id_duplicates_removed = int(
            getattr(result, "id_duplicates_removed", 0) or 0)
        self.memory_fingerprint_duplicates_removed = int(
            getattr(result, "fingerprint_duplicates_removed", 0) or 0)
        self.duplicate_memory_ids = [
            int(item) for item in
            (getattr(result, "duplicate_memory_ids", ()) or ())][:8]
        self.possible_near_duplicates = [
            int(item) for item in
            (getattr(result, "possible_near_duplicates", ()) or ())][:6]

    def note_job(self, job: Any, *, persisted: bool = False,
                 recovered: bool = False, action: str = "") -> None:
        """作業の状態。**進んだ位置も残す**——再開はそこから。"""
        if job is None:
            return
        self.migration_job_id = str(getattr(job, "job_id", "") or "")
        self.migration_job_status = str(getattr(job, "status", "") or "")
        self.migration_job_persisted = bool(persisted)
        self.migration_job_recovered = bool(recovered)
        self.migration_job_resume_from_cursor = str(
            getattr(job, "progress_cursor", "") or "")
        self.migration_job_recovery_action = str(
            action or getattr(job, "start_kind", "") or "")

    def note_migration_operation(self, operation: str, result: Any) -> None:
        """UIからの操作と、その結果。**断られた理由も残す。**"""
        self.migration_ui_operation = str(operation or "")
        if not isinstance(result, dict):
            return
        self.migration_transition_from = str(
            (result.get("confirm") or {}).get("from", "") or "")
        self.migration_transition_to = str(
            (result.get("confirm") or {}).get("to", "") or result.get("mode", "") or "")
        job = result.get("job") or {}
        if isinstance(job, dict):
            self.migration_job_id = str(job.get("job_id", "") or "")
            self.migration_job_status = str(job.get("status", "") or "")
        if result.get("ok") is False:
            self.migration_rejection_reason = str(result.get("reason", "") or "")
            self.migration_promotion_gate = [
                str(item) for item in (result.get("blockers") or ())][:6]

    def note_memory_scope(self, result: Any) -> None:
        """記憶をどの範囲で引いたか。**本文は入れない**（第12条）。"""
        if result is None:
            return
        self.memory_retrieval_mode = str(getattr(result, "retrieval_mode", "") or "")
        self.identity_scope_speaker_ids = [
            str(item) for item in (getattr(result, "scope_speaker_ids", ()) or ())][:6]
        self.legacy_memory_result_count = int(
            getattr(result, "legacy_result_count", 0) or 0)
        self.person_scope_result_count = int(
            getattr(result, "person_result_count", 0) or 0)
        self.deduplicated_memory_count = int(
            getattr(result, "deduplicated_count", 0) or 0)
        self.revoked_ids_excluded = [
            str(item) for item in (getattr(result, "excluded_revoked_ids", ()) or ())][:4]

    def _scope_snapshot(self) -> dict[str, Any]:
        return {
            "operation": self.migration_ui_operation,
            "from": self.migration_transition_from,
            "to": self.migration_transition_to,
            "job_id": self.migration_job_id,
            "job_status": self.migration_job_status,
            "job_persisted": self.migration_job_persisted,
            "job_recovered": self.migration_job_recovered,
            "resume_cursor": self.migration_job_resume_from_cursor,
            "recovery_action": self.migration_job_recovery_action,
            "gate": list(self.migration_promotion_gate[:4]),
            "rejected": self.migration_rejection_reason[:60],
            "dedup": {
                "before": self.memory_result_count_before_dedup,
                "after": self.memory_result_count_after_dedup,
                "by_id": self.memory_id_duplicates_removed,
                "by_fingerprint": self.memory_fingerprint_duplicates_removed,
                "duplicate_ids": list(self.duplicate_memory_ids[:8]),
                "near": list(self.possible_near_duplicates[:6]),
            },
            "memory": {
                "mode": self.memory_retrieval_mode,
                "person_id": self.identity_scope_person_id,
                "speakers": list(self.identity_scope_speaker_ids[:6]),
                "legacy_results": self.legacy_memory_result_count,
                "person_results": self.person_scope_result_count,
                "deduplicated": self.deduplicated_memory_count,
                "excluded": list(self.revoked_ids_excluded[:4]),
            },
            "speaker_status": self.speaker_status_resolution,
        }

    def note_relationship_read(self, ref: Any, source: Any, fallback: str = "") -> None:
        """どこから読んだか。**落ちた理由も残す。**"""
        self.relationship_read_source = str(source or "")
        self.fallback_reason = str(fallback or "")
        if ref is not None:
            self.resolved_person_id = str(getattr(ref, "person_id", "") or "")
            self.identity_link_id = str(getattr(ref, "link_id", "") or "")

    def note_relationship_write(self, outcome: Any) -> None:
        """どこへ書いたか。**片方だけ書けたのを成功に見せない。**"""
        if not isinstance(outcome, dict):
            return
        self.relationship_write_targets = [str(x) for x in outcome.get("targets", ())]
        legacy = outcome.get("legacy")
        person = outcome.get("person")
        self.legacy_write_result = "applied" if legacy else "no_change"
        # **失敗を先に見る。** 宛先の一覧だけで判断すると、
        # 「書こうとして落ちた」が「そもそも書かなかった」に見える。
        if outcome.get("error"):
            self.person_write_result = "failed"
        elif "person" not in self.relationship_write_targets:
            self.person_write_result = "skipped"
        else:
            self.person_write_result = "applied" if person else "no_change"
        if outcome.get("error"):
            self.migration_conflict = str(outcome["error"])[:60]

    def note_shadow_difference(self, difference: Any) -> None:
        """差の**大きさだけ**を残す。関係値そのものは出さない（第12条）。"""
        if not isinstance(difference, dict) or not difference:
            return
        self.shadow_read_difference = {
            "axes": float(len(difference)),
            "max": round(max(abs(float(v)) for v in difference.values()), 4),
        }

    def _migration_snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.migration_mode,
            "read_source": self.relationship_read_source,
            "write_targets": list(self.relationship_write_targets),
            "shadow_difference": dict(self.shadow_read_difference),
            "record_id": self.migration_record_id,
            "status": self.migration_status,
            "conflict": self.migration_conflict[:60],
            "legacy_write": self.legacy_write_result,
            "person_write": self.person_write_result,
            "fallback": self.fallback_reason[:40],
            "rollback": self.rollback_applied,
        }

    def note_identity(self, resolution: Any) -> None:
        """人物の解決結果を写す。**声紋ベクトルも音声も入らない**（第12条）。"""
        if resolution is None:
            return
        transport = getattr(resolution, "transport_identity", None)
        voice = getattr(resolution, "voice_identity", None)
        self.transport_identity = str(getattr(transport, "key", "") or "")
        self.voice_identity = str(getattr(voice, "key", "") or "")
        self.resolved_person_id = str(getattr(resolution, "person_id", "") or "")
        self.identity_resolution_status = str(
            getattr(resolution, "resolution_status", "") or "")
        self.identity_link_id = str(getattr(resolution, "link_id", "") or "")
        self.identity_conflict = str(getattr(resolution, "conflict_reason", "") or "")

    def note_presence(self, summary: Any) -> None:
        """**2つの人数を別々に写す。** 片方だけにすると混ざる。"""
        if summary is None:
            return
        self.authoritative_participant_count = int(
            getattr(summary, "authoritative_transport_count", 0) or 0)
        self.estimated_participant_count = int(
            getattr(summary, "estimated_unique_person_count", 0) or 0)

    def _social_snapshot(self) -> dict[str, Any]:
        return {
            "presence": {
                "event": self.presence_event_type,
                "authoritative": self.presence_authoritative,
                "result": self.presence_update_result,
                "authoritative_count": self.authoritative_participant_count,
                "estimated_count": self.estimated_participant_count,
            },
            "greeting": {
                "opportunity_id": self.greeting_opportunity_id,
                "action": self.greeting_action_selected,
                "suppression": self.greeting_suppression_reason[:60],
            },
            "identity": {
                "transport": self.transport_identity,
                "voice": self.voice_identity,
                "person_id": self.resolved_person_id,
                "status": self.identity_resolution_status,
                "link_id": self.identity_link_id,
                "conflict": self.identity_conflict[:60],
                "link_change": self.identity_link_status_change,
            },
            "vision_fact": {
                "fact_id": self.vision_fact_id,
                "ttl": round(float(self.vision_fact_ttl), 1),
            },
        }

    def note_source_detail(self, adapter_result: Any) -> None:
        """入力元ごとの但し書きを写す。**識別子と分類だけ**（第12条）。"""
        detail = getattr(adapter_result, "detail", None)
        if not isinstance(detail, dict):
            return
        source = str(getattr(adapter_result, "source", "") or "")
        if source == "discord":
            self.discord_participant_id = str(detail.get("participant_id", "") or "")
            self.participant_presence_delta = (
                "joined" if detail.get("present") else "left")
        elif source == "vision":
            self.vision_scene_type = str(detail.get("scene", "") or "")
            self.vision_scene_transition = str(detail.get("transition", "") or "")
        elif source == "ktane":
            self.ktane_event_type = str(detail.get("ktane_event", "") or "")

    def stage(self, name: str) -> None:
        """通過点を上書きする。**最後に通った所が残る。**"""
        self.vertical_slice_stage = str(name)

    def note_observation(self, adapter_result: Any) -> None:
        """アダプタの結果をそのまま写す。呼び出し側で組み立てさせない。"""
        if adapter_result is None:
            return
        self.observation_source = str(getattr(adapter_result, "source", "") or "")
        self.source_event_id = str(getattr(adapter_result, "source_event_id", "") or "")
        self.observation_adapter = str(getattr(adapter_result, "adapter", "") or "")
        self.observe_entity_called = bool(getattr(adapter_result, "entity_called", False))
        self.observe_fact_called = bool(getattr(adapter_result, "fact_called", False))
        dropped = getattr(adapter_result, "dropped", None)
        if isinstance(dropped, dict):
            self.observation_dropped = {str(k): int(v) for k, v in dropped.items()}
        self.world_state_delta_id = str(getattr(adapter_result, "delta_id", "") or "")
        self.note_source_detail(adapter_result)

    def _wiring_snapshot(self) -> dict[str, Any]:
        """Phase 6B の接続まわり。**識別子と真偽値のみ**（第12条）。"""
        return {
            "observation": {
                "source": self.observation_source,
                "source_event_id": self.source_event_id,
                "adapter": self.observation_adapter,
                "entity_called": self.observe_entity_called,
                "fact_called": self.observe_fact_called,
                "dropped": dict(self.observation_dropped),
            },
            "persistence": {
                "delta_id": self.world_state_delta_id,
                "persisted": self.world_state_persisted,
                "error": self.world_state_persistence_error[:120],
            },
            "obligation": {
                "goal_id": self.goal_id,
                "obligation_id": self.obligation_id,
                "sync": self.goal_obligation_sync,
            },
            "permission": {
                "next_action_id": self.next_action_id,
                "category": self.permission_category,
                "result": self.permission_result,
                "reason": self.permission_reason,
            },
            "sources": {
                "discord": {
                    "participant_id": self.discord_participant_id,
                    "presence_delta": self.participant_presence_delta,
                },
                "vision": {
                    "scene_type": self.vision_scene_type,
                    "transition": self.vision_scene_transition,
                },
                "ktane": {
                    "event": self.ktane_event_type,
                    "entities": list(self.ktane_entity_ids[:4]),
                    "facts": list(self.ktane_fact_ids[:6]),
                    "goal_id": self.ktane_goal_id,
                },
            },
            "speech": {
                "source_type": self.speech_source_type,
                "gate": self.speech_gate_result,
                "suppression": self.suppression_reason[:60],
            },
            "stage": self.vertical_slice_stage,
        }

    def mark(self, name: str, milliseconds: float) -> None:
        self.latency_breakdown[name] = round(float(milliseconds), 2)

    def _memory_snapshot(self) -> dict[str, Any]:
        """記憶まわりだけの塊。**要約と数値のみ、本文は入れない**（第12条）。"""
        return {
            "trigger": self.memory_retrieval_trigger,
            "diagnostics": {
                "trigger_result": self.memory_trigger_result,
                "trigger_reason": self.memory_trigger_reason,
                "query_feature_hash": self.retrieval_query_feature_hash,
                "store_bound_persona_id": self.memory_store_bound_persona_id,
                "store_path_hash": self.memory_store_path_hash,
                "candidate_count": self.retrieval_candidate_count,
                "after_scope_count": self.retrieval_after_scope_count,
                "after_lexical_count": self.retrieval_after_lexical_count,
                "after_semantic_count": self.retrieval_after_semantic_count,
                "ranked_count": self.retrieval_ranked_count,
                "selected_count": self.retrieval_selected_count,
                "rejection_reasons": list(self.retrieval_rejection_reasons),
                "representation_status_counts": dict(self.retrieval_representation_status_counts),
                "recall_status": self.recall_status,
                "recall_llm_call_count": self.recall_llm_call_count,
                "recall_evidence_memory_ids": list(self.recall_evidence_memory_ids),
                "obligation_retrieval_considered": self.obligation_retrieval_considered,
                "obligation_retrieval_triggered": self.obligation_retrieval_triggered,
                "obligation_retrieval_reason": self.obligation_retrieval_reason,
                "obligation_candidate_count": self.obligation_candidate_count,
                "obligation_relevance_score": round(float(self.obligation_relevance_score), 3),
                "obligation_effect_on_action": dict(self.obligation_effect_on_action),
            },
            "query": self.retrieval_query_summary,
            "retrieved": list(self.retrieved_memory_ids),
            "relevance": {k: round(float(v), 3) for k, v in self.memory_relevance_scores.items()},
            "effect_on_actions": self.memory_effect_on_actions,
            "candidates": self.memory_candidates,
            "write_decisions": self.memory_write_decisions,
            "duplicate_resolution": list(self.duplicate_resolution),
            "reflections_used": list(self.reflection_ids_used),
            "reflection_effect": {
                k: round(float(v), 3) for k, v in self.reflection_effect_on_state.items()
            },
            "persona": self._persona_snapshot(),
        }

    def _persona_snapshot(self) -> dict[str, Any]:
        """分離が効いているかを読む所。**ID と件数だけ。**"""
        return {
            "active": {"id": self.active_persona_id,
                       "version": self.active_persona_version,
                       "epoch": self.persona_epoch},
            "retrieved_persona_ids": sorted(set(self.retrieved_memory_persona_ids)),
            "retrieved_scopes": sorted(set(self.retrieved_memory_scopes)),
            "rejected": {
                "cross_persona": self.cross_persona_rejection_count,
                "cross_persona_ids": sorted(
                    set(self.cross_persona_rejection_persona_ids)),
                "legacy_unscoped": self.legacy_unscoped_rejection_count,
            },
            "reflection_persona_ids": sorted(set(self.reflection_persona_ids)),
            "source_memory_persona_ids": sorted(set(self.source_memory_persona_ids)),
            "switch": {"state": self.persona_switch_state,
                       "from": self.persona_switch_from,
                       "to": self.persona_switch_to,
                       "epoch": self.persona_switch_epoch},
        }

    @property
    def persona_leak(self) -> bool:
        """**他人の記憶が混ざったか。** これが真なら分離は成立していない。"""
        if not self.active_persona_id:
            return False
        return any(owner and owner != self.active_persona_id
                   for owner in self.retrieved_memory_persona_ids)

    def _state_snapshot(self) -> dict[str, Any]:
        """内面まわりだけの塊。**粗い数値とラベルのみ**（第12条）。"""
        return {
            "version": self.internal_state_version,
            "summary": self.internal_state_summary,
            "dimensions_used": list(self.state_dimensions_used),
            "appraisal": self.event_appraisal,
            "proposed_deltas": self.proposed_state_deltas,
            "applied_deltas": self.applied_state_deltas,
            "clamp_reasons": list(self.delta_clamp_reasons),
            "decay": {k: round(float(v), 3) for k, v in self.decay_applied.items()},
            "relationship_target": self.relationship_target_id,
            "effect_on_actions": self.state_effect_on_actions,
            "effect_on_planner": self.state_effect_on_planner,
        }

    def snapshot(self) -> dict[str, Any]:
        total = round(sum(self.latency_breakdown.values()), 2)
        return {
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "at": round(self.started_at, 3),
            "active_persona_id": self.active_persona_id,
            "persona_version": self.active_persona_version,
            "persona_epoch": self.persona_epoch,
            "memory_retrieval_trigger": self.memory_retrieval_trigger,
            "persona_leak": self.persona_leak,
            "persona_switch_state": self.persona_switch_state,
            "events": list(self.input_event_ids),
            "state": self.state_snapshot_summary,
            "cognition": {
                "enabled": self.cognition_enabled,
                "rollout_mode": self.rollout_mode,
                "requested_rollout_mode": self.requested_rollout_mode,
                "execution_path": self.execution_path,
                "session_epoch": self.cognition_session_epoch,
                "activation_source": self.cognition_activation_source,
                "config_fingerprint": self.cognition_config_fingerprint,
                "transport": self.transport,
                "action_selector_used": self.action_selector_used,
                "speech_gate_active": self.speech_gate_active,
                "legacy_response_path_used": self.legacy_response_path_used,
                "speech_request_count": self.speech_request_count,
                "fallback_used": self.fallback_used,
                "fallback_reason": self.cognitive_fallback_reason,
                "fallback_count": self.fallback_count,
                "side_effect_state": self.side_effect_state,
            },
            "candidates": self.generated_candidates,
            "selected": self.selected_action,
            "supporting": self.supporting_action,
            "scores": {k: round(v, 3) for k, v in self.decision_scores.items()},
            "reason": self.decision_reason,
            "planner_input": self.planner_input,
            "status": self.execution_status,
            "outcome": self.outcome_summary,
            "state_changes": self.state_changes,
            "memory": self._memory_snapshot(),
            "speech_delivery": self.speech_delivery,
            "response_delivery": {
                "echo_guard_checked": self.echo_guard_checked,
                "echo_match_scope": self.echo_match_scope,
                "primary_similarity_score": round(float(self.primary_similarity_score), 3),
                "regeneration_attempted": self.regeneration_attempted,
                "regenerated_similarity_score": round(float(self.regenerated_similarity_score), 3),
                "echo_resolution": self.echo_resolution,
                "final_response_empty": self.final_response_empty,
                "speech_generation_completed": self.speech_generation_completed,
                "turn_closure_completed": self.turn_closure_completed,
                "action_outcome_status": self.action_outcome_status,
                "delivery_failure_stage": self.delivery_failure_stage,
                "delivery_finalize_called": self.delivery_finalize_called,
                "delivery_terminal_state": self.delivery_terminal_state,
                "playback_started": self.playback_started,
                "playback_completed": self.playback_completed,
                "trace_delivery_finalized": self.trace_delivery_finalized,
                "delivery_finalize_error": self.delivery_finalize_error,
            },
            "silence": {
                "action_decision_id": self.action_decision_id,
                "decision_source": self.decision_source,
                "reason_code": self.silence_reason_code,
                "suppression_reason": self.suppression_reason,
                "response_required": self.response_required,
                "response_requirement_source": self.response_requirement_source,
            },
            "action_constraints": {
                "closure_signal": self.closure_signal,
                "closure_confidence": round(float(self.closure_confidence), 3),
                "direct_question": self.direct_question,
                "explicit_answer_request": self.explicit_answer_request,
                "new_task_instruction": self.new_task_instruction,
                "safety_warning": self.safety_warning,
                "response_obligation": self.response_obligation,
                "eligible_actions": list(self.eligible_actions),
                "candidate_action_scores": dict(self.candidate_action_scores),
                "initially_selected_action": self.initially_selected_action,
                "action_constraint_result": self.action_constraint_result,
                "corrected_action": self.corrected_action,
                "final_selected_action": self.final_selected_action,
                "closure_response_policy": self.closure_response_policy,
                "action_selection_reason": self.action_selection_reason,
            },
            "internal_state": self._state_snapshot(),
            "initiative": {
                **self.initiative_summary,
                "speech_gate": self.speech_gate_result,
                "cancel_reason": self.cancel_reason,
            },
            "world": {
                "summary": self.world_state_summary,
                "deltas": self.world_state_deltas[-6:],
                "goals": self.goal_summary[:6],
                "admission": self.goal_admission_decision,
                "effect_on_actions": {
                    k: round(float(v), 3) for k, v in self.goal_effect_on_actions.items()
                },
                "permission_required": list(self.permission_required[:4]),
                "completion_evidence": self.completion_evidence[:80],
            },
            "wiring": self._wiring_snapshot(),
            "social": self._social_snapshot(),
            "migration": self._migration_snapshot(),
            "scope": self._scope_snapshot(),
            "tool": self._tool_snapshot(),
            "latency_ms": {**self.latency_breakdown, "total": total},
            "llm_diagnostics": self.llm_diagnostics,
            "turn_latency": self.turn_latency,
            "latency_summary": self.latency_summary,
        }


class TraceWriter:
    """1行1JSONで追記する。既定では**何も書かない**。"""

    def __init__(self, path: str | Path | None, *, enabled: bool = False) -> None:
        self.enabled = bool(enabled and path)
        self._path = Path(path) if path else None

    def write(self, trace: CognitiveTrace) -> None:
        if not self.enabled or self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace.snapshot(), ensure_ascii=False) + "\n")
        except OSError:
            # 記録に失敗しても会話は続ける（第17条）。
            logger.debug("認知トレースの書き出しに失敗", exc_info=True)
