"""自分から話す。**機会を見つけたことと、話すべきことは別。**

Phase 5 第二弾。守りたいのは3つ。

1. **候補を作った時点では話さない。** 必ず Action Selector を通る
2. **沈黙は常に候補にある。** 「機会＝発話」にしない
3. **時間は発話の理由にならない。** 「一定秒数黙ったら話す」を作らない

3を落とすと、静かにしていたいだけの時に話しかけてくるAIになる。
実装としては動いて見えるので、テストが無いと気づけない。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition import (
    ActionType, CognitiveEvent, CognitiveKernel, EventType, build_state,
)
from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.cognition.initiative import (
    CLOSURE_QUIET_SECONDS, SOURCE_PROACTIVE, GameEventCategory, InitiativeBudget,
    InitiativeOpportunity, OpportunityType, SpeakingConditions, WarnThrottle,
    classify_game_event, merge_opportunities, opportunities_from_events,
    revalidate, silent_opportunity, suppression_reason,
)
from neuro_voice.cognition.rollout import (
    PROACTIVE_ALLOWLIST, SpeechRequest, SpeechSource, check_speech,
)


def chance(kind=OpportunityType.COMMENT_ON_GAME, **kwargs) -> InitiativeOpportunity:
    kwargs.setdefault("topic_id", "配線")
    kwargs.setdefault("created_at", 1000.0)
    kwargs.setdefault("source_event_ids", ("e1",))
    return InitiativeOpportunity(opportunity_type=kind, **kwargs)


def attention(kind, **kwargs) -> AttentionEvent:
    kwargs.setdefault("occurred_at", 1000.0)
    kwargs.setdefault("salience", .6)
    return AttentionEvent(event_type=kind, **kwargs)


def free() -> SpeakingConditions:
    """話してよい状態。ここから1つずつ潰していく。"""
    return SpeakingConditions()


# ---------------------------------------------------------------------------
# 機会は発話ではない
# ---------------------------------------------------------------------------


def test_an_opportunity_is_not_speech():
    """**作った時点では話さない。** 行動の提案でしかない。"""
    opportunity = chance()
    assert opportunity.proposed_action is ActionType.COMMENT
    candidate = opportunity.to_candidate()
    assert candidate.source_type == SOURCE_PROACTIVE
    assert candidate.source_event_ids == ("e1",)
    assert candidate.topic_id == "配線"


def test_the_candidate_carries_its_provenance():
    """**出どころを候補自身が持つ。** 後から辿れないと Gate が検証できない。"""
    candidate = chance(
        participant_ids=("chibi",), expires_at=2000.0, confidence=.8).to_candidate()
    for field in ("source_type", "source_event_ids", "topic_id",
                  "participant_ids", "expires_at"):
        assert getattr(candidate, field), field
    assert candidate.snapshot()["source"] == SOURCE_PROACTIVE


def test_cost_outweighs_value_in_the_score():
    """**黙る方へ倒すのが既定。** 引く側の重みを大きくしてある。"""
    cheap = chance(expected_value=.8, salience=.8)
    costly = chance(expected_value=.8, salience=.8, social_cost=.8,
                    interruption_risk=.8)
    assert costly.initiative_score < cheap.initiative_score
    # コストが全部立っていれば、価値があっても負になる。
    worst = chance(expected_value=.8, salience=.8, social_cost=1.0,
                   interruption_risk=1.0, repetition_risk=1.0,
                   recent_speech_penalty=1.0)
    assert worst.initiative_score < 0, worst.initiative_score


def test_silence_is_always_available_as_an_opportunity():
    silent = silent_opportunity()
    assert silent.proposed_action is ActionType.REMAIN_SILENT
    assert suppression_reason(silent, free()) == ""


# ---------------------------------------------------------------------------
# 発話してはいけない状態
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value,expected", [
    ("user_speaking", True, "user_is_speaking"),
    ("other_participant_speaking", True, "another_participant_is_speaking"),
    ("awaiting_end_of_turn", True, "awaiting_end_of_turn"),
    ("assistant_speaking", True, "already_speaking"),
    ("handling_interruption", True, "handling_interruption"),
    ("focus_mode", True, "user_is_focused"),
    ("only_unknown_speakers", True, "only_unknown_speakers"),
    ("topic_closed", True, "topic_closed"),
])
def test_each_forbidden_state_suppresses_speech(field, value, expected):
    conditions = free()
    setattr(conditions, field, value)
    assert suppression_reason(chance(), conditions) == expected


def test_speech_is_suppressed_right_after_closing_a_turn():
    conditions = free()
    conditions.seconds_since_turn_closure = CLOSURE_QUIET_SECONDS - .5
    assert suppression_reason(chance(), conditions) == "just_closed_the_turn"


def test_the_topic_we_chose_to_be_silent_about_is_not_reopened():
    """**黙ると決めた話題を自分から蒸し返さない。**"""
    conditions = free()
    conditions.last_outcome_status = "silent_completed"
    conditions.last_topic_id = "配線"
    assert suppression_reason(chance(topic_id="配線"), conditions) == "same_topic_after_silence"
    assert suppression_reason(chance(topic_id="夕飯"), conditions) == ""


def test_an_expired_opportunity_is_refused():
    assert suppression_reason(
        chance(expires_at=999.0), free(), now=1000.0) == "opportunity_expired"


def test_a_low_confidence_opportunity_is_refused():
    assert suppression_reason(chance(confidence=.2), free()) == "confidence_too_low"


def test_an_event_already_handled_is_not_spoken_about_again():
    conditions = free()
    conditions.handled_event_ids = ("e1",)
    assert suppression_reason(chance(), conditions) == "event_already_handled"


def test_the_same_thing_is_not_said_twice():
    conditions = free()
    conditions.spoken_dedup_keys = (chance().dedup(),)
    assert suppression_reason(chance(), conditions) == "already_said_this"


def test_silence_is_never_suppressed():
    """**沈黙を止める理由は無い。** どんな状態でも選べる。"""
    conditions = SpeakingConditions(
        user_speaking=True, assistant_speaking=True, focus_mode=True,
        topic_closed=True)
    assert suppression_reason(silent_opportunity(), conditions) == ""


# ---------------------------------------------------------------------------
# 時間は理由にならない
# ---------------------------------------------------------------------------


def test_silence_alone_never_creates_an_opportunity():
    """**「一定秒数黙ったら必ず話す」を作らない。**

    静かにしていたいだけの時に話しかけてくるAIになる。
    """
    events = [attention(AttentionEventType.SILENCE_THRESHOLD, expected_duration=300.0)]
    assert opportunities_from_events(events, obligations=(), now=1000.0) == []


def test_silence_with_an_open_promise_does_create_one():
    """未完了の約束と結びついて初めて候補になる。"""
    events = [attention(AttentionEventType.SILENCE_THRESHOLD, expected_duration=300.0)]
    out = opportunities_from_events(events, obligations=("設定の確認",), now=1000.0)
    assert len(out) == 1
    assert out[0].proposed_action is ActionType.RESUME_OBLIGATION
    assert "silence_with_open_obligation" in out[0].reasons


def test_an_ambient_change_stays_silent():
    """環境の変化は原則沈黙。"""
    events = [attention(AttentionEventType.VISUAL_CHANGE, salience=.4, novelty=.4)]
    assert opportunities_from_events(events, now=1000.0) == []


# ---------------------------------------------------------------------------
# ゲームイベントの分類
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("summary,expected", [
    ("HPが残りわずか", GameEventCategory.DANGER),
    ("溶岩が近い", GameEventCategory.DANGER),
    ("ボスエリアに到達した", GameEventCategory.MAJOR_PROGRESS),
    ("敵にやられた", GameEventCategory.FAILURE),
    ("ボスを倒した", GameEventCategory.SUCCESS),
    ("レアな鉱石を発見", GameEventCategory.DISCOVERY),
    ("草が揺れている", GameEventCategory.AMBIENT_CHANGE),
])
def test_game_events_are_classified_before_reacting(summary, expected):
    """**画面認識の結果をそのまま文章にしない。** まず分類する。"""
    assert classify_game_event(summary) is expected


def test_a_repeated_event_becomes_repetitive_whatever_it_is():
    assert classify_game_event("ボスを倒した", repeat_count=3) is GameEventCategory.REPETITIVE_EVENT


def test_repetitive_and_ambient_events_produce_no_opportunity():
    """**逐次反応は実況ではなく読み上げ。**"""
    for summary in ("草が揺れている", "雲が動いた"):
        assert opportunities_from_events(
            [attention(AttentionEventType.GAME_EVENT, summary=summary)], now=1000.0) == []


def test_danger_is_left_to_the_warning_path():
    """危険を自発発話の予算と条件で扱わない。**別の経路の仕事。**"""
    out = opportunities_from_events(
        [attention(AttentionEventType.GAME_EVENT, summary="HPが残りわずか")], now=1000.0)
    assert out == []


def test_a_discovery_needs_real_novelty():
    low = opportunities_from_events(
        [attention(AttentionEventType.GAME_EVENT, summary="レアな鉱石を発見", novelty=.3)],
        now=1000.0)
    high = opportunities_from_events(
        [attention(AttentionEventType.GAME_EVENT, summary="レアな鉱石を発見", novelty=.9)],
        now=1000.0)
    assert low == [] and len(high) == 1


def test_major_progress_becomes_a_commentary_candidate():
    out = opportunities_from_events(
        [attention(AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した")], now=1000.0)
    assert len(out) == 1
    assert out[0].proposed_action is ActionType.COMMENT
    assert out[0].reasons == ("game:major_progress",)


# ---------------------------------------------------------------------------
# 予算
# ---------------------------------------------------------------------------


def test_speaking_too_often_hits_the_budget():
    budget = InitiativeBudget(cooldown_seconds=0.0, max_utterances=3, max_cost=99,
                              max_same_topic_utterances=99)
    for index in range(3):
        assert budget.check(chance(), now=1000.0 + index) == ""
        budget.record(chance(), now=1000.0 + index)
    assert budget.check(chance(), now=1003.0) == "utterance_budget"


def test_long_utterances_cost_more_than_short_ones():
    """**回数だけだと、長い実況を連発しても引っかからない。**"""
    budget = InitiativeBudget(cooldown_seconds=0.0, max_utterances=99, max_cost=3.0)
    budget.record(chance(), cost=2.5, now=1000.0)
    assert budget.check(chance(), now=1001.0) == ""
    budget.record(chance(), cost=1.0, now=1001.0)
    assert budget.check(chance(), now=1002.0) == "cost_budget"


def test_the_same_topic_has_its_own_ceiling():
    budget = InitiativeBudget(cooldown_seconds=0.0, max_utterances=99,
                              max_same_topic_utterances=2, max_cost=99)
    for index in range(2):
        budget.record(chance(topic_id="配線"), now=1000.0 + index)
    assert budget.check(chance(topic_id="配線"), now=1002.0) == "same_topic_budget"
    assert budget.check(chance(topic_id="夕飯"), now=1002.0) == ""


def test_a_cooldown_follows_every_utterance():
    budget = InitiativeBudget(cooldown_seconds=8.0)
    budget.record(chance(), now=1000.0)
    assert budget.check(chance(), now=1003.0) == "cooldown"
    assert budget.check(chance(), now=1009.0) == ""


def test_the_budget_window_slides():
    budget = InitiativeBudget(window_seconds=10.0, cooldown_seconds=0.0, max_utterances=1)
    budget.record(chance(), now=1000.0)
    assert budget.check(chance(), now=1005.0) == "utterance_budget"
    assert budget.check(chance(), now=1020.0) == ""


def test_the_same_warning_is_not_shouted_twice():
    """警告は予算の対象外だが、**同じ警告の連打は抑える。**"""
    throttle = WarnThrottle(repeat_seconds=6.0)
    assert throttle.allows("lava", now=1000.0)
    assert not throttle.allows("lava", now=1003.0)
    assert throttle.allows("lava", now=1007.0)


def test_a_different_warning_is_never_throttled():
    throttle = WarnThrottle(repeat_seconds=60.0)
    assert throttle.allows("lava", now=1000.0)
    assert throttle.allows("creeper", now=1000.1)


# ---------------------------------------------------------------------------
# 統合
# ---------------------------------------------------------------------------


def test_three_related_events_become_one_utterance():
    """「敵が出た」「HPが減った」「避けた」を3回喋らない。"""
    items = [
        chance(topic_id="戦闘", source_event_ids=(f"e{i}",), created_at=1000.0 + i)
        for i in range(3)
    ]
    merged = merge_opportunities(items, now=1010.0)
    assert len(merged) == 1
    assert set(merged[0].source_event_ids) == {"e0", "e1", "e2"}
    assert "merged:3" in merged[0].reasons


def test_the_newest_event_wins_when_merging():
    """**状況は最新が正しい。** 古い方を土台にすると「もう避けた」が落ちる。"""
    old = chance(topic_id="戦闘", summary="敵が出た", created_at=1000.0,
                 source_event_ids=("e0",))
    new = chance(topic_id="戦闘", summary="避けた", created_at=1002.0,
                 source_event_ids=("e1",))
    merged = merge_opportunities([old, new], now=1010.0)
    assert merged[0].summary == "避けた"


def test_distant_events_are_not_merged():
    items = [
        chance(topic_id="戦闘", created_at=1000.0, source_event_ids=("a",)),
        chance(topic_id="戦闘", created_at=1030.0, source_event_ids=("b",)),
    ]
    assert len(merge_opportunities(items, window=6.0, now=1040.0)) == 2


def test_merging_does_not_inflate_salience_past_the_ceiling():
    items = [chance(topic_id="戦闘", salience=1.0, source_event_ids=(f"e{i}",),
                    created_at=1000.0 + i) for i in range(5)]
    assert merge_opportunities(items, now=1010.0)[0].salience <= 1.0


def test_expired_opportunities_drop_out_when_merging():
    items = [chance(topic_id="戦闘", expires_at=999.0), chance(topic_id="戦闘")]
    assert len(merge_opportunities(items, now=1000.0)) == 1


# ---------------------------------------------------------------------------
# Action Selector への統合
# ---------------------------------------------------------------------------


def test_silence_is_always_in_the_candidate_list():
    """**機会があっても、沈黙は必ず並ぶ。**"""
    candidates = CognitiveKernel().propose(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE),
        build_state(), [chance(expected_value=.9, salience=.9)])
    assert any(c.action_type is ActionType.REMAIN_SILENT for c in candidates)


def test_a_weak_opportunity_loses_to_silence():
    """面白いが重要でない変化 → 沈黙が勝つ。"""
    weak = chance(expected_value=.3, salience=.3, novelty=.3,
                  social_cost=.6, interruption_risk=.5)
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(), None, [weak])
    assert decision.selected_action is ActionType.REMAIN_SILENT


def test_a_strong_opportunity_can_win():
    strong = chance(OpportunityType.RESUME_OBLIGATION, expected_value=.95,
                    salience=.9, novelty=.8, goal_relevance=.9, timing_score=.9)
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.SILENCE_TIMEOUT), build_state(), None, [strong])
    assert decision.selected_action is ActionType.RESUME_OBLIGATION
    assert decision.source_type == SOURCE_PROACTIVE
    assert decision.source_event_ids == ("e1",)


def test_an_end_signal_pushes_proactive_candidates_down():
    """**打ち切りの合図に、自分から話を広げない。**"""
    strong = chance(expected_value=.95, salience=.9, novelty=.9, timing_score=.9)
    ending = build_state(user_state={"end_signal": .9, "engagement": .05})
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ"),
        ending, None, [strong])
    assert decision.selected_action in {
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE}


def test_memory_penalties_apply_to_proactive_candidates_too():
    """**自発発話だけ補正を免れない。**

    免れると、過去に嫌がられた話題を自分からは平気で持ち出すことになる。
    """
    from neuro_voice.cognition import EpisodicMemory, build_query, influence_from, rank

    failures = [
        EpisodicMemory(memory_id=i + 1, summary=f"x{i}", importance=.8, confidence=.9,
                       occurred_at=1e9,
                       event_type="self_failure:kept_talking_after_end_signal")
        for i in range(3)
    ]
    influence = influence_from(rank(failures, build_query(
        "もういいよ", trigger="similar_to_past_failure",
        failure_patterns=("kept_talking_after_end_signal",))))
    opportunity = chance(OpportunityType.CONTINUE_SHARED_TOPIC,
                         expected_value=.8, salience=.8)
    event = CognitiveEvent(event_type=EventType.SILENCE_TIMEOUT)
    plain = CognitiveKernel().propose(event, build_state(), [opportunity])
    biased = CognitiveKernel(memory_influence=influence).propose(
        event, build_state(), [opportunity])

    def score(items):
        return next(c.total_score for c in items
                    if c.action_type is ActionType.CONTINUE_PREVIOUS_TOPIC
                    and c.source_type == SOURCE_PROACTIVE)

    assert score(biased) < score(plain)


def test_an_expired_opportunity_never_reaches_the_candidate_list():
    candidates = CognitiveKernel().propose(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(),
        [chance(expires_at=1.0)])
    assert not any(c.source_type == SOURCE_PROACTIVE for c in candidates)


def test_a_danger_event_is_not_displaced_by_an_opportunity():
    """**どれだけ良い機会でも、危険の警告を押しのけない。**"""
    decision = CognitiveKernel().decide(
        CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.95),
        build_state(world_state={"danger": .9}), None,
        [chance(expected_value=1.0, salience=1.0, novelty=1.0, timing_score=1.0)])
    assert decision.selected_action is ActionType.WARN


# ---------------------------------------------------------------------------
# Speech Gate
# ---------------------------------------------------------------------------


def proactive(**kwargs) -> SpeechRequest:
    kwargs.setdefault("source_action", ActionType.COMMENT)
    kwargs.setdefault("confidence", .8)
    return SpeechRequest(source_type=SpeechSource.PROACTIVE_OPPORTUNITY, **kwargs)


def gate(request, **kwargs):
    kwargs.setdefault("has_valid_decision", True)
    kwargs.setdefault("decision_allows_speech", True)
    return check_speech(request, cognition_enabled=True, **kwargs)


def test_a_valid_proactive_request_passes():
    assert gate(proactive()).allowed


def test_proactive_speech_without_a_decision_is_refused():
    """**発話元不明の自発TTS要求を禁止する。**"""
    result = gate(proactive(), has_valid_decision=False)
    assert not result.allowed
    assert result.reason == "proactive_speech_without_decision"


def test_an_ordinary_answer_cannot_leave_through_the_proactive_path():
    result = gate(proactive(source_action=ActionType.ANSWER))
    assert not result.allowed
    assert result.reason == "proactive_action_not_allowlisted"


def test_a_warning_does_not_leave_through_the_proactive_path():
    """危険警告を自発発話の条件と予算で扱わない。"""
    assert ActionType.WARN not in PROACTIVE_ALLOWLIST
    assert not gate(proactive(source_action=ActionType.WARN)).allowed


def test_a_comment_cannot_leave_through_the_warning_path():
    """逆向きも塞ぐ。"""
    result = check_speech(
        SpeechRequest(source_type=SpeechSource.GAME_WARNING_FAST_PATH,
                      source_action=ActionType.COMMENT, confidence=.9),
        cognition_enabled=True)
    assert not result.allowed
    assert result.reason == "fast_path_action_not_allowlisted"


def test_proactive_speech_stops_when_the_user_starts_talking():
    result = gate(proactive(user_speaking=True))
    assert not result.allowed
    assert result.reason == "user_is_speaking"


def test_proactive_speech_stops_when_the_candidate_expired():
    assert not gate(proactive(expires_at=1.0)).allowed


def test_proactive_speech_stops_under_closure_suppression():
    result = gate(proactive(closure_suppressed="same_topic_after_silence"))
    assert not result.allowed
    assert "closure:" in result.reason


def test_a_decision_that_does_not_speak_blocks_the_request():
    assert not gate(proactive(), decision_allows_speech=False).allowed


def test_low_confidence_proactive_speech_is_refused():
    assert not gate(proactive(confidence=.2)).allowed


# ---------------------------------------------------------------------------
# 発話直前の再検証
# ---------------------------------------------------------------------------


def test_a_candidate_is_cancelled_if_the_user_starts_speaking():
    """**取り消した発話を後から突然再生しない。** ここで捨てきる。"""
    conditions = free()
    conditions.user_speaking = True
    assert revalidate(chance(), conditions) == "user_started_speaking"


def test_a_candidate_is_cancelled_by_a_higher_priority_event():
    assert revalidate(chance(), free(),
                      higher_priority_pending=True) == "higher_priority_event_arrived"


def test_a_candidate_is_cancelled_when_it_expires_in_the_meantime():
    assert revalidate(chance(expires_at=999.0), free(),
                      now=1000.0) == "expired_before_speaking"


def test_a_candidate_is_cancelled_if_another_path_already_said_it():
    conditions = free()
    conditions.spoken_dedup_keys = (chance().dedup(),)
    assert revalidate(chance(), conditions) == "already_spoken_by_another_path"


def test_a_candidate_is_cancelled_when_the_topic_closed():
    conditions = free()
    conditions.topic_closed = True
    assert revalidate(chance(), conditions) == "topic_closed_meanwhile"


def test_a_still_valid_candidate_passes_revalidation():
    assert revalidate(chance(), free(), now=1000.0) == ""


def test_revalidation_also_works_on_a_plain_candidate():
    """候補（`ActionCandidate`）でも機会でも同じ関数で確かめられること。"""
    conditions = free()
    conditions.user_speaking = True
    assert revalidate(chance().to_candidate(), conditions) == "user_started_speaking"
