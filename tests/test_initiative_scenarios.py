"""Phase 5 の必須12シナリオ。

一本の経路を端から端まで通す形で書いてある。個々の部品は
`test_attention.py` / `test_initiative.py` / `test_initiative_runtime.py` に
あるので、ここは**組み合わせた時に壊れないか**だけを見る。

いちばん守りたいのは「話す」側ではなく**「話さない」側**。
うるさいAIは、静かなAIより直すのが難しい。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition import (
    ActionType, CognitiveEvent, CognitiveKernel, EventType, build_state,
)
from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.cognition.initiative import (
    SOURCE_PROACTIVE, InitiativeOpportunity, OpportunityType, SpeakingConditions,
    TargetScope, WarnThrottle, apply_internal_state,
    opportunities_from_memories, social_cost_in_group,
)
from neuro_voice.cognition.internal_state import (
    AFFECT_BASELINE, UnifiedInternalState,
)
from neuro_voice.cognition.rollout import (
    SpeechRequest, SpeechSource, check_speech,
)
from neuro_voice.cognition.runtime import InitiativeRuntime


class Cfg:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


STAGE_ALL = {
    "initiative.enabled": True, "initiative.speech_enabled": True,
    "initiative.game_commentary_enabled": True,
    "initiative.memory_initiative_enabled": True,
    "initiative.idle_initiative_enabled": True,
    "initiative.budget.cooldown_s": 0.0,
}


def runtime(**overrides) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**{**STAGE_ALL, **overrides}))


def game_event(summary="ボスエリアに到達した", **kwargs) -> AttentionEvent:
    kwargs.setdefault("salience", .8)
    kwargs.setdefault("novelty", .8)
    kwargs.setdefault("confidence", .9)
    kwargs.setdefault("occurred_at", 1000.0)
    kwargs.setdefault("topic_ids", ("ボス",))
    return AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, summary=summary, **kwargs)


def speaking_opportunities(result):
    return [item for item in result.opportunities
            if item.proposed_action is not ActionType.REMAIN_SILENT]


# ---------------------------------------------------------------------------
# ケース1: 無意味な沈黙
# ---------------------------------------------------------------------------


def test_case1_time_alone_never_makes_it_speak():
    """**時間だけを理由に発話しない。**"""
    live = runtime()
    live.observe(AttentionEvent(
        event_type=AttentionEventType.SILENCE_THRESHOLD,
        occurred_at=1000.0, expected_duration=900.0, salience=.9), now=1000.0)
    result = live.evaluate(SpeakingConditions(), obligations=(), now=1001.0)
    assert speaking_opportunities(result) == []


def test_case1_silence_with_an_open_promise_is_different():
    live = runtime()
    live.observe(AttentionEvent(
        event_type=AttentionEventType.SILENCE_THRESHOLD,
        occurred_at=1000.0, expected_duration=900.0, salience=.9), now=1000.0)
    result = live.evaluate(
        SpeakingConditions(), obligations=("設定の確認",), now=1001.0)
    assert speaking_opportunities(result)


# ---------------------------------------------------------------------------
# ケース2: 有意味なゲームイベント（最初の垂直スライス）
# ---------------------------------------------------------------------------


def test_case2_a_meaningful_game_event_reaches_a_decision():
    """イベント → 機会 → Action Selector → 発話。**一回だけ。**"""
    live = runtime()
    assert live.observe(game_event(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    chances = speaking_opportunities(result)
    assert chances and chances[0].proposed_action in {
        ActionType.COMMENT, ActionType.REACT}

    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(),
        None, result.opportunities, now=1001.0)
    assert decision.source_type == SOURCE_PROACTIVE
    assert decision.selected_action in {ActionType.COMMENT, ActionType.REACT}


def test_case2_the_expiry_check_uses_the_callers_clock():
    """**期限の判定に別の時計を使わない。**

    ここは `time.monotonic()` を直接読んでいて、注入した時計で作られた
    機会が**全部「期限切れ」として静かに消えていた**。
    """
    chance = InitiativeOpportunity(
        opportunity_type=OpportunityType.COMMENT_ON_GAME,
        topic_id="ボス", expected_value=.8, salience=.8, novelty=.8,
        expires_at=1013.0)
    candidates = CognitiveKernel().propose(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(),
        [chance], now=1001.0)
    assert any(c.source_type == SOURCE_PROACTIVE for c in candidates)
    # 同じ機会でも、時計を進めれば消える。
    later = CognitiveKernel().propose(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(),
        [chance], now=1020.0)
    assert not any(c.source_type == SOURCE_PROACTIVE for c in later)


def test_case2_silence_is_still_compared_against_it():
    live = runtime()
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    assert any(item.proposed_action is ActionType.REMAIN_SILENT
               for item in result.opportunities)


def test_case2_the_stage_flag_holds_it_back():
    """段階を上げていなければ、同じイベントでも候補にならない。"""
    live = runtime(**{"initiative.game_commentary_enabled": False})
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    assert speaking_opportunities(result) == []
    assert any(reason == "stage_disabled" for _id, reason in result.suppressed)


# ---------------------------------------------------------------------------
# ケース3: 反復イベント
# ---------------------------------------------------------------------------


def test_case3_the_same_event_does_not_produce_repeated_commentary():
    """**実況を連打しない。**"""
    live = runtime()
    accepted = sum(
        live.observe(game_event(dedup_key="boss") if False else AttentionEvent(
            event_type=AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した",
            topic_ids=("ボス",), salience=.8, novelty=.8, confidence=.9,
            occurred_at=1000.0 + i * .2, deduplication_key="boss"),
            now=1000.0 + i * .2)
        for i in range(6)
    )
    assert accepted == 1, accepted


def test_case3_related_events_merge_into_one():
    live = runtime()
    for index, summary in enumerate(("敵にやられた", "敵にやられた", "敵にやられた")):
        live.observe(AttentionEvent(
            event_type=AttentionEventType.GAME_EVENT, summary=summary,
            topic_ids=("戦闘",), salience=.8, novelty=.8, confidence=.9,
            occurred_at=1000.0 + index * 5, deduplication_key=f"k{index}"),
            now=1000.0 + index * 5)
    result = live.evaluate(SpeakingConditions(), now=1011.0)
    assert len(speaking_opportunities(result)) == 1


def test_case3_after_speaking_the_same_thing_is_not_said_again():
    live = runtime()
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    chosen = speaking_opportunities(result)[0]
    live.record_spoken(chosen, now=1001.0)
    live.observe(game_event(deduplication_key="other"), now=1010.0)
    again = live.evaluate(SpeakingConditions(), now=1011.0)
    assert all(item.dedup() != chosen.dedup() for item in speaking_opportunities(again))


# ---------------------------------------------------------------------------
# ケース4: 候補生成後のユーザー発話
# ---------------------------------------------------------------------------


def test_case4_speech_is_cancelled_when_the_user_starts_talking():
    """**後から遅れて再生しない。** ここで捨てきる。"""
    live = runtime()
    live.observe(game_event(), now=1000.0)
    chosen = speaking_opportunities(live.evaluate(SpeakingConditions(), now=1001.0))[0]
    # ここでユーザーが話し始めた。
    cancelled = live.confirm(chosen, SpeakingConditions(user_speaking=True), now=1002.0)
    assert cancelled == "user_started_speaking"
    assert live.counters["revalidation_cancelled"] == 1


def test_case4_the_gate_also_refuses_it():
    """再検証と Gate の**二重**で止める。入口で1度見るだけでは足りない。"""
    result = check_speech(
        SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                      source_action=ActionType.COMMENT, confidence=.8,
                      user_speaking=True),
        cognition_enabled=True, has_valid_decision=True, decision_allows_speech=True)
    assert not result.allowed


# ---------------------------------------------------------------------------
# ケース5: Turn Closure 直後
# ---------------------------------------------------------------------------


def test_case5_the_closed_topic_is_not_reopened():
    """「もういいよ」の直後に、**同じ話題**を自分から蒸し返さない。"""
    live = runtime()
    live.observe(game_event(topic_ids=("ボス",)), now=1000.0)
    conditions = SpeakingConditions(
        last_outcome_status="silent_completed", last_topic_id="ボス")
    result = live.evaluate(conditions, now=1001.0)
    assert speaking_opportunities(result) == []
    assert any(reason == "same_topic_after_silence" for _id, reason in result.suppressed)


def test_case5_the_quiet_window_right_after_closing_applies_too():
    live = runtime()
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(
        SpeakingConditions(seconds_since_turn_closure=1.0), now=1001.0)
    assert speaking_opportunities(result) == []


# ---------------------------------------------------------------------------
# ケース6: Closure 直後の別話題
# ---------------------------------------------------------------------------


def test_case6_closure_is_not_applied_to_every_topic():
    """**Closure を無条件に全イベントへ適用しない。** 別件は普通に評価する。"""
    live = runtime()
    live.observe(game_event(topic_ids=("宝箱",)), now=1000.0)
    conditions = SpeakingConditions(
        last_outcome_status="silent_completed", last_topic_id="ボス")
    assert speaking_opportunities(live.evaluate(conditions, now=1001.0))


def test_case6_danger_after_closure_still_warns():
    """危険は Closure でも止まらない。**別の経路の仕事。**"""
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.95),
        build_state(world_state={"danger": .9},
                    user_state={"end_signal": .9, "engagement": .05}))
    assert decision.selected_action is ActionType.WARN


# ---------------------------------------------------------------------------
# ケース7: WARN 高速経路
# ---------------------------------------------------------------------------


def test_case7_a_confident_danger_warns_immediately():
    result = check_speech(
        SpeechRequest(source_type=SpeechSource.GAME_WARNING_FAST_PATH,
                      source_action=ActionType.WARN, confidence=.95,
                      bypass_reason="realtime_danger"),
        cognition_enabled=True)
    assert result.allowed


def test_case7_the_commentary_budget_does_not_block_a_warning():
    """**警告は通常の実況予算に阻害されない。**"""
    live = runtime()
    for index in range(5):
        live.record_spoken(
            InitiativeOpportunity(opportunity_type=OpportunityType.COMMENT_ON_GAME,
                                  topic_id="ボス"),
            now=1000.0 + index)
    assert live.budget.check(
        InitiativeOpportunity(opportunity_type=OpportunityType.COMMENT_ON_GAME),
        now=1006.0)
    # 予算を使い切っていても、警告は別枠。
    assert live.allows_warning("lava", now=1006.0)


def test_case7_the_same_warning_is_still_throttled():
    throttle = WarnThrottle(repeat_seconds=6.0)
    assert throttle.allows("lava", now=1000.0)
    assert not throttle.allows("lava", now=1002.0)
    assert throttle.allows("creeper", now=1002.0)


# ---------------------------------------------------------------------------
# ケース8: WARN 以外の高速経路
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [
    ActionType.COMMENT, ActionType.REACT, ActionType.ANSWER,
    ActionType.SHARE_MEMORY, ActionType.LIGHT_FOLLOW_UP,
])
def test_case8_only_warnings_use_the_fast_path(action):
    result = check_speech(
        SpeechRequest(source_type=SpeechSource.GAME_WARNING_FAST_PATH,
                      source_action=action, confidence=.95),
        cognition_enabled=True)
    assert not result.allowed
    assert result.reason == "fast_path_action_not_allowlisted"


# ---------------------------------------------------------------------------
# ケース9: 発話予算
# ---------------------------------------------------------------------------


def test_case9_only_a_few_of_many_candidates_get_spoken():
    """短時間に多数の候補 → **重要な少数だけ。**"""
    live = runtime(**{"initiative.budget.max_utterances": 2,
                      "initiative.budget.max_same_topic": 99})
    spoken = 0
    for index in range(8):
        live.observe(AttentionEvent(
            event_type=AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した",
            topic_ids=(f"t{index}",), salience=.8, novelty=.8, confidence=.9,
            occurred_at=1000.0 + index * 10, deduplication_key=f"k{index}"),
            now=1000.0 + index * 10)
        result = live.evaluate(SpeakingConditions(), now=1000.0 + index * 10 + 1)
        chances = speaking_opportunities(result)
        if chances:
            live.record_spoken(chances[0], now=1000.0 + index * 10 + 1)
            spoken += 1
    assert spoken <= 3, spoken


def test_case9_the_cooldown_spaces_them_out():
    live = runtime(**{"initiative.budget.cooldown_s": 8.0})
    live.record_spoken(
        InitiativeOpportunity(opportunity_type=OpportunityType.COMMENT_ON_GAME),
        now=1000.0)
    live.observe(game_event(occurred_at=1002.0), now=1002.0)
    result = live.evaluate(SpeakingConditions(), now=1003.0)
    assert any(reason == "cooldown" for _id, reason in result.suppressed)


# ---------------------------------------------------------------------------
# ケース10: 複数人会話
# ---------------------------------------------------------------------------


def test_case10_others_talking_makes_it_expensive_to_interrupt():
    """**他人同士の会話を邪魔しない。**"""
    alone = social_cost_in_group(TargetScope.GROUP, participant_count=1)
    crowd = social_cost_in_group(TargetScope.GROUP, participant_count=3)
    crosstalk = social_cost_in_group(
        TargetScope.GROUP, participant_count=3, others_talking=True)
    assert alone == 0.0
    assert 0 < crowd < crosstalk


def test_case10_being_addressed_removes_the_group_cost():
    """名指しで聞かれているのに「複数人だから」で黙るのはおかしい。"""
    assert social_cost_in_group(
        TargetScope.GROUP, participant_count=4, others_talking=True,
        addressed_to_me=True) == 0.0


def test_case10_ambient_muttering_costs_the_most():
    direct = social_cost_in_group(TargetScope.DIRECT, participant_count=3)
    ambient = social_cost_in_group(TargetScope.AMBIENT, participant_count=3)
    assert ambient > direct


def test_case10_the_group_cost_actually_suppresses_commentary():
    live = runtime()
    live.observe(game_event(), now=1000.0)
    quiet = live.evaluate(SpeakingConditions(), now=1001.0)
    live_group = runtime()
    live_group.observe(game_event(), now=1000.0)
    crowded = live_group.evaluate(
        SpeakingConditions(), now=1001.0,
        participant_count=3, others_talking=True)
    assert speaking_opportunities(quiet)
    best_quiet = speaking_opportunities(quiet)[0].initiative_score
    crowded_chances = speaking_opportunities(crowded)
    if crowded_chances:
        assert crowded_chances[0].initiative_score < best_quiet
    assert True


def test_case10_an_unknown_speaker_is_not_treated_as_a_person():
    """**不明話者を特定人物として扱わない。**"""
    conditions = SpeakingConditions(only_unknown_speakers=True)
    live = runtime()
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(conditions, now=1001.0)
    assert speaking_opportunities(result) == []


# ---------------------------------------------------------------------------
# ケース11: 未完了の約束（記憶起点）
# ---------------------------------------------------------------------------


class FakeMemory:
    def __init__(self, memory_id, event_type, summary, confidence=.9):
        self.memory_id = memory_id
        self.event_type = event_type
        self.summary = summary
        self.confidence = confidence
        self.topic_ids = ("設定",)


class FakeScored:
    def __init__(self, memory, score):
        self.memory = memory
        self.score = score


def test_case11_a_promise_becomes_a_candidate_not_a_sentence():
    """**記憶をそのまま突然話さない。** 機会にしてから列へ並べる。"""
    out = opportunities_from_memories(
        [FakeScored(FakeMemory(7, "promise", "後で設定を確認しておく"), .8)])
    assert len(out) == 1
    assert out[0].proposed_action is ActionType.RESUME_OBLIGATION
    assert out[0].source_memory_ids == (7,)
    assert out[0].reasons == ("memory:promise",)


def test_case11_it_still_goes_through_the_action_selector():
    chance = opportunities_from_memories(
        [FakeScored(FakeMemory(7, "promise", "後で設定を確認しておく"), .9)])[0]
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.SILENCE_TIMEOUT), build_state(),
        None, [chance])
    assert decision.selected_action in {
        ActionType.RESUME_OBLIGATION, ActionType.REMAIN_SILENT}


def test_case11_an_irrelevant_memory_is_not_paraded():
    """**古い記憶を脈絡なく披露しない。** 関連度の下限で落とす。"""
    assert opportunities_from_memories(
        [FakeScored(FakeMemory(9, "promise", "むかしの約束"), .05)]) == []


def test_case11_past_failures_never_become_something_to_say():
    """失敗は「思い出して話す」ためではなく「**同じ手を打たない**」ため。"""
    assert opportunities_from_memories([FakeScored(
        FakeMemory(3, "self_failure:kept_talking_after_end_signal", "x"), .9)]) == []


@pytest.mark.parametrize("event_type", ["conversation", "strong_affect_unknown", ""])
def test_case11_only_the_listed_memory_kinds_are_used(event_type):
    assert opportunities_from_memories(
        [FakeScored(FakeMemory(1, event_type, "何か"), .9)]) == []


def test_case11_the_memory_stage_flag_holds_it_back():
    live = runtime(**{"initiative.memory_initiative_enabled": False})
    result = live.evaluate(
        SpeakingConditions(), now=1001.0,
        memories=[FakeScored(FakeMemory(7, "promise", "後で設定を確認"), .9)])
    assert speaking_opportunities(result) == []


# ---------------------------------------------------------------------------
# ケース12: 期限切れ
# ---------------------------------------------------------------------------


def test_case12_an_expired_candidate_is_never_spoken():
    live = runtime()
    stale = InitiativeOpportunity(
        opportunity_type=OpportunityType.COMMENT_ON_GAME,
        topic_id="ボス", expires_at=1000.0)
    assert live.confirm(stale, SpeakingConditions(), now=1005.0) == "expired_before_speaking"


def test_case12_the_gate_refuses_an_expired_request():
    result = check_speech(
        SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                      source_action=ActionType.COMMENT, confidence=.8,
                      expires_at=1.0),
        cognition_enabled=True, has_valid_decision=True, decision_allows_speech=True)
    assert not result.allowed
    assert result.reason == "opportunity_expired"


def test_case12_expired_events_never_become_candidates():
    live = runtime()
    live.observe(AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した",
        topic_ids=("ボス",), salience=.8, novelty=.8, confidence=.9,
        occurred_at=1000.0, expires_at=1001.0), now=1000.0)
    assert speaking_opportunities(live.evaluate(SpeakingConditions(), now=1010.0)) == []


# ---------------------------------------------------------------------------
# 内面状態の反映（§16）
# ---------------------------------------------------------------------------


def chance(**kwargs) -> InitiativeOpportunity:
    kwargs.setdefault("opportunity_type", OpportunityType.COMMENT_ON_GAME)
    kwargs.setdefault("topic_id", "ボス")
    return InitiativeOpportunity(**kwargs)


def internal(**affect) -> UnifiedInternalState:
    stance = {"playfulness": affect.pop("playfulness", .25)}
    return UnifiedInternalState(
        affect={**AFFECT_BASELINE, **affect}, social_stance=stance)


def test_curiosity_only_helps_a_discovery():
    curious = internal(curiosity=.9)
    discovery = apply_internal_state(chance(reasons=("game:discovery",)), curious)
    other = apply_internal_state(chance(reasons=("game:success",)), curious)
    assert discovery.expected_value > .5
    assert other.expected_value == .5


def test_irritation_never_increases_commentary():
    """**苛立ちで実況量を増やさない。**"""
    grumpy = internal(irritation=.5, curiosity=.9, comfort=.9, playfulness=.9)
    before = chance(reasons=("game:discovery",))
    after = apply_internal_state(chance(reasons=("game:discovery",)), grumpy)
    assert after.expected_value <= before.expected_value
    assert after.social_cost > before.social_cost
    assert "irritated_speak_less" in after.reasons


def test_caution_suppresses_reactions_to_uncertain_vision():
    wary = internal(caution=.6)
    unsure = apply_internal_state(chance(confidence=.5), wary)
    certain = apply_internal_state(chance(confidence=.95), wary)
    assert unsure.social_cost > certain.social_cost


def test_playfulness_only_applies_to_a_safe_success():
    playful = internal(playfulness=.8, comfort=.8)
    success = apply_internal_state(chance(reasons=("game:success",)), playful)
    failure = apply_internal_state(chance(reasons=("game:failure",)), playful)
    assert success.expected_value > failure.expected_value


def test_the_internal_effect_stays_small():
    """**強制命令ではなく小さな補正。**"""
    extreme = internal(curiosity=1.0, comfort=1.0, playfulness=1.0)
    boosted = apply_internal_state(chance(reasons=("game:discovery",)), extreme)
    assert boosted.expected_value - .5 <= .2, boosted.expected_value


def test_a_missing_internal_state_changes_nothing():
    assert apply_internal_state(chance(), None).expected_value == .5


# ---------------------------------------------------------------------------
# レイテンシとトレース
# ---------------------------------------------------------------------------


def test_no_llm_is_needed_for_an_ordinary_event():
    """**通常イベントごとにLLMを呼ばない。**

    LLMを一切渡していない状態で、イベントから機会まで通ること。
    """
    live = runtime()
    live.observe(game_event(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    assert result.opportunities
    for key in ("focus_ms", "opportunity_generation_ms", "deduplication_ms",
                "initiative_scoring_ms", "suppression_ms"):
        assert key in result.latency_ms, key


def test_the_whole_evaluation_is_fast():
    live = runtime()
    for index in range(12):
        live.observe(AttentionEvent(
            event_type=AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した",
            topic_ids=(f"t{index}",), salience=.8, novelty=.8, confidence=.9,
            occurred_at=1000.0 + index, deduplication_key=f"k{index}"),
            now=1000.0 + index)
    import time as _time

    started = _time.perf_counter()
    live.evaluate(SpeakingConditions(), now=1020.0)
    assert (_time.perf_counter() - started) * 1000 < 50


def test_the_trace_keeps_no_screen_or_speech_text():
    """**画面全文・音声全文・内部思考文を通常ログへ残さない。**"""
    import json

    live = runtime()
    live.observe(game_event(summary="口座番号は1234と画面に出ている"), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    text = json.dumps(result.snapshot(), ensure_ascii=False)
    assert "1234" not in text and "口座番号" not in text


def test_the_trace_records_why_it_stayed_quiet():
    """「機会は出ていたが抑制された」と「機会が無かった」は違う。"""
    live = runtime()
    live.observe(game_event(), now=1000.0)
    payload = live.evaluate(
        SpeakingConditions(user_speaking=True), now=1001.0).snapshot()
    assert payload["suppressed"]
    assert payload["suppressed"][0][1] == "user_is_speaking"


def test_every_stage_can_be_turned_off_independently():
    live = runtime(**{
        "initiative.game_commentary_enabled": False,
        "initiative.memory_initiative_enabled": False,
        "initiative.idle_initiative_enabled": False,
    })
    stages = live.status()["stages"]
    assert not stages["game_commentary"]
    assert not stages["memory_initiative"]
    assert not stages["idle_initiative"]
    assert stages["pre_speech_revalidation"], "再検証は既定で入っている"
