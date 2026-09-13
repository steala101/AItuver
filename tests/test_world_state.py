"""いまどうなっているか、何を目指しているか。

Phase 6 の必須11シナリオ。守りたいのは3つ。

1. **過去（記憶）と現在（状況）を混ぜない。** 記憶は増えていくが、
   現在状況は古くなって消える
2. **不確かなものを断定しない。** 見失っただけで「無い」と言わない。
   低い確信で既知の人物へ寄せない
3. **勝手に目標を作らない。** ポッポ自身の長期欲求は登録できない

3が特に大事。作れる仕組みにしておくと、いつか作る。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition.goals import (
    ADVICE_COOLDOWN, AUTOMATIC_ACTIONS, RESTRICTED_ACTIONS, AdmissionDecision,
    GoalAdmissionGate, GoalRecord, GoalStatus, GoalType, Permission, goal_bias,
    mark_completed, next_actions, pause_for, permission_for, requires_approval,
    resume_candidates,
)
from neuro_voice.cognition.types import ActionType, InformationType
from neuro_voice.cognition.world import (
    CONTRADICTION_MARGIN, DeltaOperation, EntityType, GroundedWorldState,
    ItemStatus, Presence, WorldStateGate, resolve_entity,
)


def world() -> tuple[GroundedWorldState, WorldStateGate]:
    return GroundedWorldState(), WorldStateGate()


def goal(**kwargs) -> GoalRecord:
    kwargs.setdefault("description", "設定を確認する")
    kwargs.setdefault("owner_ids", ("chibi",))
    kwargs.setdefault("confidence", .9)
    return GoalRecord(**kwargs)


# ---------------------------------------------------------------------------
# ケース1: 状況更新
# ---------------------------------------------------------------------------


def test_a_change_becomes_a_delta_and_updates_the_state():
    state, gate = world()
    delta = gate.observe_fact(state, "player", "hp", "low",
                              confidence=.9, event_ids=("e1",), now=1000.0)
    assert str(delta.operation) == str(DeltaOperation.ADD)
    assert delta.applied
    fact = state.fact("player", "hp")
    assert fact is not None and fact.value == "low"


def test_the_original_event_is_traceable():
    """**どのイベントから来たかを辿れる。**"""
    state, gate = world()
    gate.observe_fact(state, "player", "hp", "low", event_ids=("e1", "e2"),
                      confidence=.9, now=1000.0)
    assert state.fact("player", "hp").source_event_ids == ("e1", "e2")
    assert state.recent_changes[-1].source_event_ids == ("e1", "e2")


def test_the_summary_keeps_no_screen_or_speech_text():
    """**画面全文も音声全文も残さない**（第12条）。"""
    import json

    state, gate = world()
    gate.observe_entity(state, "口座番号1234と書かれた看板",
                        confidence=.9, now=1000.0)
    text = json.dumps(state.summary(now=1000.0), ensure_ascii=False)
    assert "1234" not in text


# ---------------------------------------------------------------------------
# ケース2: 古い事実
# ---------------------------------------------------------------------------


def test_a_transient_fact_goes_stale():
    """**すべてを永久に現在状態として残さない。**"""
    state, gate = world()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    assert state.fact("player", "hp").usable(now=1002.0)
    assert not state.fact("player", "hp").usable(now=1100.0)


def test_a_session_long_fact_survives():
    """HPは数秒で古くなるが、「いまMinecraftをやっている」はもつ。"""
    state, gate = world()
    gate.observe_fact(state, "session", "playing", "minecraft",
                      confidence=.9, now=1000.0)
    assert state.fact("session", "playing").usable(now=2000.0)


def test_sweeping_marks_stale_facts_without_deleting_them():
    """**消さずに落とす。**"""
    state, gate = world()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    marked = state.sweep(now=1100.0)
    assert marked
    assert str(state.fact("player", "hp").status) == str(ItemStatus.STALE)
    assert state.fact("player", "hp") is not None, "消してはいけない"


def test_a_stale_fact_is_not_used_as_evidence():
    state, gate = world()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    assert not state.usable_facts(now=1100.0)


def test_seeing_it_again_refreshes_the_fact():
    state, gate = world()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    state.sweep(now=1100.0)
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1100.0)
    assert str(state.fact("player", "hp").status) == str(ItemStatus.ACTIVE)


# ---------------------------------------------------------------------------
# ケース3: 矛盾
# ---------------------------------------------------------------------------


def test_a_confident_contradiction_replaces_the_fact():
    state, gate = world()
    gate.observe_fact(state, "player", "holding", "none", confidence=.6, now=1000.0)
    delta = gate.observe_fact(state, "player", "holding", "pickaxe",
                              confidence=.95, now=1001.0)
    assert str(delta.operation) == str(DeltaOperation.CONTRADICT)
    assert state.fact("player", "holding").value == "pickaxe"


def test_a_narrow_contradiction_does_not_flip_it():
    """**僅差では覆さない。** 代わりに不確かにする。"""
    state, gate = world()
    gate.observe_fact(state, "player", "holding", "none", confidence=.8, now=1000.0)
    delta = gate.observe_fact(state, "player", "holding", "pickaxe",
                              confidence=.8 + CONTRADICTION_MARGIN / 2, now=1001.0)
    assert not delta.applied
    assert state.fact("player", "holding").value == "none"
    assert str(state.fact("player", "holding").status) == str(ItemStatus.UNCERTAIN)


def test_the_old_value_is_recorded_in_the_delta():
    state, gate = world()
    gate.observe_fact(state, "player", "holding", "none", confidence=.6, now=1000.0)
    delta = gate.observe_fact(state, "player", "holding", "pickaxe",
                              confidence=.95, now=1001.0)
    assert delta.previous_value == "none"


# ---------------------------------------------------------------------------
# ケース4: 不明対象
# ---------------------------------------------------------------------------


def test_a_low_confidence_sighting_is_not_merged():
    """**「たぶんあの人」で統合しない。** 混ざったら分離できない。"""
    state, gate = world()
    gate.observe_entity(state, "チビ", entity_type=EntityType.PARTICIPANT,
                        confidence=.95, now=1000.0)
    entity, _ = gate.observe_entity(state, "チビ", confidence=.4, now=1001.0)
    assert len(state.entities) == 2
    assert str(entity.entity_type) == str(EntityType.UNKNOWN)


def test_a_confident_sighting_is_merged():
    state, gate = world()
    first, _ = gate.observe_entity(state, "チビ", entity_type=EntityType.PARTICIPANT,
                                   confidence=.95, now=1000.0)
    again, delta = gate.observe_entity(state, "チビ",
                                       entity_type=EntityType.PARTICIPANT,
                                       confidence=.9, now=1001.0)
    assert again.entity_id == first.entity_id
    assert str(delta.operation) == str(DeltaOperation.CONFIRM)


def test_different_kinds_are_never_merged():
    """人と場所を同じ名前でまとめない。"""
    state, gate = world()
    gate.observe_entity(state, "ネザー", entity_type=EntityType.LOCATION,
                        confidence=.95, now=1000.0)
    entity, _ = gate.observe_entity(state, "ネザー",
                                    entity_type=EntityType.PARTICIPANT,
                                    confidence=.95, now=1001.0)
    assert len(state.entities) == 2


def test_resolution_refuses_below_the_threshold():
    assert resolve_entity("チビ", [], confidence=.9) is None


# ---------------------------------------------------------------------------
# 見失っただけで「無い」と言わない
# ---------------------------------------------------------------------------


def test_losing_sight_is_not_absence():
    """**画面から外れただけで不存在を断定しない。**"""
    state, gate = world()
    entity, _ = gate.observe_entity(state, "作業台", confidence=.9, now=1000.0)
    gate.lost_sight_of(state, entity.entity_id, now=1001.0)
    assert str(entity.presence) == str(Presence.RECENTLY_SEEN)
    assert str(entity.status) != str(ItemStatus.REMOVED)


def test_confirmed_absence_is_different():
    state, gate = world()
    entity, _ = gate.observe_entity(state, "作業台", confidence=.9, now=1000.0)
    gate.lost_sight_of(state, entity.entity_id, confirmed_absent=True, now=1001.0)
    assert str(entity.presence) == str(Presence.CONFIRMED_ABSENT)


def test_presence_degrades_gradually():
    state, gate = world()
    entity, _ = gate.observe_entity(state, "作業台", confidence=.9, now=1000.0)
    state.sweep(now=1030.0)
    assert str(entity.presence) == str(Presence.RECENTLY_SEEN)
    state.sweep(now=1200.0)
    assert str(entity.presence) == str(Presence.INFERRED)


def test_entities_do_not_accumulate_forever():
    """**全画面のオブジェクトを永久に溜めない。**"""
    state, gate = world()
    for index in range(60):
        gate.observe_entity(state, f"石{index}", confidence=.5, now=1000.0 + index)
    state.sweep(now=1100.0, max_entities=40)
    assert len(state.entities) == 40


# ---------------------------------------------------------------------------
# ケース5: 共有目標
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "user_stated", "user_approved", "explicit_collaboration",
    "assistant_promise", "game_profile",
])
def test_a_goal_from_an_allowed_source_becomes_active(source):
    verdict = GoalAdmissionGate().evaluate(goal(), source=source)
    assert verdict.decision is AdmissionDecision.ADMIT
    assert verdict.status is GoalStatus.ACTIVE


def test_the_goal_types_stay_limited():
    assert {str(item) for item in GoalType} == {
        "user_goal", "shared_goal", "assistant_commitment", "game_objective"}


# ---------------------------------------------------------------------------
# ケース6: 勝手な目標
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "small_talk", "inferred_life_goal", "psychological_inference",
    "single_visual_change", "self_generated_desire",
])
def test_a_self_generated_goal_is_rejected(source):
    """**ポッポ自身の長期欲求を作れる仕組みを残さない。**"""
    verdict = GoalAdmissionGate().evaluate(goal(), source=source)
    assert verdict.decision is AdmissionDecision.REJECT


def test_an_unverified_source_only_gets_proposed():
    verdict = GoalAdmissionGate().evaluate(goal(), source="llm_suggestion")
    assert verdict.decision is AdmissionDecision.HOLD_FOR_APPROVAL
    assert verdict.status is GoalStatus.PROPOSED


def test_a_goal_without_an_owner_is_rejected():
    """持ち主のいない目標は、事実上ポッポ自身の目標になる。"""
    verdict = GoalAdmissionGate().evaluate(
        goal(owner_ids=()), source="user_stated")
    assert verdict.decision is AdmissionDecision.REJECT


def test_a_game_objective_may_have_no_owner():
    verdict = GoalAdmissionGate().evaluate(
        goal(owner_ids=(), goal_type=GoalType.GAME_OBJECTIVE), source="game_profile")
    assert verdict.decision is AdmissionDecision.ADMIT


def test_a_low_confidence_goal_waits_for_approval():
    verdict = GoalAdmissionGate().evaluate(
        goal(confidence=.3), source="user_stated")
    assert verdict.decision is AdmissionDecision.HOLD_FOR_APPROVAL
    assert GoalAdmissionGate().evaluate(
        goal(confidence=.3), source="user_stated",
        approved=True).decision is AdmissionDecision.ADMIT


def test_too_many_active_goals_are_held():
    verdict = GoalAdmissionGate(max_active=2).evaluate(
        goal(), source="user_stated", active_count=2)
    assert verdict.decision is AdmissionDecision.HOLD_FOR_APPROVAL


# ---------------------------------------------------------------------------
# ケース7: 中断と再開
# ---------------------------------------------------------------------------


def test_an_interrupted_goal_is_paused_not_abandoned():
    item = goal(status=GoalStatus.ACTIVE)
    pause_for(item, "別の話題", now=1000.0)
    assert str(item.status) == str(GoalStatus.PAUSED)


def test_a_paused_goal_is_not_resumed_on_its_own():
    """**中断中に勝手に再開しない。**"""
    item = goal(status=GoalStatus.PAUSED)
    assert next_actions([item]) == []


def test_a_paused_goal_resumes_only_on_a_related_situation():
    item = goal(status=GoalStatus.PAUSED, description="設定を確認する",
                related_entity_ids=("entity1",))
    assert resume_candidates([item], topic_ids=("夕飯",)) == []
    assert resume_candidates([item], topic_ids=("設定",)) == [item]
    assert resume_candidates([item], entity_ids=("entity1",)) == [item]


# ---------------------------------------------------------------------------
# ケース8: 完了
# ---------------------------------------------------------------------------


def test_completion_needs_confidence_and_evidence():
    """**不確実な認識で完了断定しない。**"""
    item = goal(status=GoalStatus.ACTIVE)
    assert not mark_completed(item, evidence="", confidence=.99)
    assert not mark_completed(item, evidence="作れた", confidence=.5)
    assert mark_completed(item, evidence="作れた", confidence=.9)
    assert str(item.status) == str(GoalStatus.COMPLETED)


def test_completion_conditions_are_checked():
    item = goal(status=GoalStatus.ACTIVE, completion_conditions=("ダイヤ",))
    assert not mark_completed(item, evidence="石が採れた", confidence=.95)
    assert mark_completed(item, evidence="ダイヤが採れた", confidence=.95)


def test_a_completed_goal_stops_the_same_advice():
    """**完了後に同じ助言を続けない。**"""
    done = goal(status=GoalStatus.COMPLETED)
    bias = goal_bias([done])
    assert bias.get(str(ActionType.RESUME_OBLIGATION), 0) < 0
    assert next_actions([done]) == []


def test_the_same_advice_is_not_repeated_within_the_cooldown():
    item = goal(status=GoalStatus.ACTIVE, last_advice_at=1000.0)
    assert next_actions([item], now=1000.0 + ADVICE_COOLDOWN / 2) == []
    assert next_actions([item], now=1000.0 + ADVICE_COOLDOWN + 1)


# ---------------------------------------------------------------------------
# ケース9: Closure
# ---------------------------------------------------------------------------


def test_an_active_goal_does_not_override_a_closure():
    """**「もういいよ」に、目標を理由として食い下がらない。**"""
    item = goal(status=GoalStatus.ACTIVE, priority=1.0)
    assert next_actions([item], end_signal=.9) == []
    assert goal_bias([item], end_signal=.9) == {}


def test_the_goal_is_kept_across_a_closure():
    item = goal(status=GoalStatus.ACTIVE)
    next_actions([item], end_signal=.9)
    assert str(item.status) == str(GoalStatus.ACTIVE), "破棄しない"


# ---------------------------------------------------------------------------
# ケース10: 複数人
# ---------------------------------------------------------------------------


def test_a_goal_belongs_to_the_person_who_stated_it():
    item = goal(owner_ids=("speaker_a",))
    assert "speaker_b" not in item.owner_ids
    verdict = GoalAdmissionGate().evaluate(item, source="user_stated")
    assert verdict.decision is AdmissionDecision.ADMIT


def test_a_shared_goal_can_have_several_owners():
    item = goal(goal_type=GoalType.SHARED_GOAL, owner_ids=("speaker_a", "poppo"))
    assert GoalAdmissionGate().evaluate(
        item, source="explicit_collaboration").decision is AdmissionDecision.ADMIT


# ---------------------------------------------------------------------------
# ケース11: 外部行動
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(RESTRICTED_ACTIONS))
def test_external_actions_need_approval(action):
    """**無許可で実行しない。** 提案と実行を分ける。"""
    assert requires_approval(action)
    assert permission_for(action) is Permission.NEEDS_APPROVAL


@pytest.mark.parametrize("action", sorted(AUTOMATIC_ACTIONS))
def test_internal_actions_run_freely(action):
    assert not requires_approval(action)


def test_an_unknown_action_needs_approval():
    """**知らない行動は許可が要る側へ倒す。**"""
    assert requires_approval("do_something_new")
    assert requires_approval("")


# ---------------------------------------------------------------------------
# 行動選択への影響
# ---------------------------------------------------------------------------


def test_an_active_goal_nudges_resuming():
    bias = goal_bias([goal(status=GoalStatus.ACTIVE, priority=1.0)])
    assert bias.get(str(ActionType.RESUME_OBLIGATION), 0) > 0


def test_a_blocked_goal_nudges_asking():
    bias = goal_bias([goal(status=GoalStatus.BLOCKED, priority=1.0,
                           blocked_reason="材料が足りない")])
    assert bias.get(str(ActionType.ASK_CLARIFICATION), 0) > 0


def test_low_confidence_facts_favour_checking():
    bias = goal_bias([], fact_confidence=.3)
    assert bias.get(str(ActionType.ASK_CLARIFICATION), 0) > 0
    assert bias.get(str(ActionType.ANSWER), 0) < 0


def test_the_goal_effect_stays_small():
    """**目標は命令ではない。** 上書きにしない。"""
    from neuro_voice.cognition.goals import MAX_GOAL_BIAS

    many = [goal(status=GoalStatus.ACTIVE, priority=1.0) for _ in range(8)]
    for value in goal_bias(many).values():
        assert abs(value) <= MAX_GOAL_BIAS


def test_a_goal_cannot_displace_a_warning():
    from neuro_voice.cognition import (
        CognitiveEvent, CognitiveKernel, EventType, build_state,
    )

    decision = CognitiveKernel(
        goal_bias=goal_bias([goal(status=GoalStatus.ACTIVE, priority=1.0)]),
    ).decide(
        CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.95),
        build_state(world_state={"danger": .9}))
    assert decision.selected_action is ActionType.WARN


def test_next_actions_stay_short():
    """**長い計画を毎ターン作らない。**"""
    from neuro_voice.cognition.goals import MAX_NEXT_ACTIONS

    many = [goal(status=GoalStatus.ACTIVE, priority=1.0 - i * .1) for i in range(8)]
    assert len(next_actions(many)) <= MAX_NEXT_ACTIONS


# ---------------------------------------------------------------------------
# 記憶との分離・機能フラグ
# ---------------------------------------------------------------------------


def test_the_world_state_is_not_an_episodic_memory():
    """**同じレコードへ押し込まない。** 性質が違う。"""
    from neuro_voice.cognition.episodic import EpisodicMemory
    from neuro_voice.cognition.world import WorldFact

    fact_fields = set(WorldFact.__dataclass_fields__)
    memory_fields = set(EpisodicMemory.__dataclass_fields__)
    # 現在状況にしかないもの（鮮度）と、記憶にしかないもの（重要度）。
    assert "expires_at" in fact_fields and "expires_at" not in memory_fields
    assert "importance" in memory_fields and "importance" not in fact_fields


def test_a_fact_records_where_it_came_from():
    from neuro_voice.cognition.world import WorldFact

    fact = WorldFact(subject_id="player", predicate="hp",
                     source_type=InformationType.OBSERVATION)
    assert str(fact.source_type) == str(InformationType.OBSERVATION)
    assert fact.observed_at and fact.last_verified_at


class Cfg:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def runtime(**flags):
    from neuro_voice.cognition.runtime import InitiativeRuntime

    return InitiativeRuntime(Cfg(**flags))


def test_the_defaults_are_off():
    live = runtime()
    for flag in ("world_enabled", "entity_tracking_enabled", "fact_tracking_enabled",
                 "goals_enabled", "obligation_integration_enabled",
                 "next_action_enabled"):
        assert not getattr(live, flag), flag


def test_nothing_is_tracked_while_off():
    live = runtime()
    assert live.observe_entity("チビ") == (None, None)
    assert live.observe_fact("player", "hp", "low") is None
    assert live.propose_goal(goal(), source="user_stated") is None
    assert live.next_actions() == []
    assert live.goal_bias() == {}


def test_each_stage_turns_on_independently():
    live = runtime(**{"world_state.enabled": True,
                      "world_state.entity_tracking_enabled": True})
    entity, _ = live.observe_entity("チビ", confidence=.9, now=1000.0)
    assert entity is not None
    # 事実の追跡はまだ上げていない。
    assert live.observe_fact("player", "hp", "low") is None


def test_the_runtime_status_shows_the_stages():
    live = runtime(**{"world_state.enabled": True, "goals.enabled": True})
    status = live.status()
    assert status["world"]["enabled"] is True
    assert status["goals"]["enabled"] is True
    assert "latency_ms" in status


def test_no_llm_is_needed_for_world_updates():
    """**通常イベントごとに追加LLMを呼ばない。**"""
    live = runtime(**{"world_state.enabled": True,
                      "world_state.entity_tracking_enabled": True,
                      "world_state.fact_tracking_enabled": True,
                      "goals.enabled": True, "goals.next_action_enabled": True})
    live.observe_entity("チビ", confidence=.9, now=1000.0)
    live.observe_fact("player", "hp", "low", confidence=.9, now=1000.0)
    live.propose_goal(goal(), source="user_stated")
    assert live.next_actions() is not None
    for key in ("entity_resolution_ms", "world_state_delta_ms",
                "goal_matching_ms", "next_action_generation_ms"):
        assert key in live.status()["latency_ms"], key
