"""持続する内面。**毎ターン初期化された人格に戻らないこと。**

Phase 4 の必須テスト。守りたいのは2つ。

1. 内面が**行動を実際に変える**こと（プロンプトへ書くだけにしない）
2. 内面が**変えてはいけないものを変えない**こと——安全警告、終了制御、
   Speech Gate、そして人そのものの評価

2 の方が大事。感情で判断が変わる仕組みは、入れた瞬間に
「機嫌の悪い日は警告が遅い」を許してしまう。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition import (
    ActionType, CognitiveEvent, CognitiveKernel, EventType, build_state,
)
from neuro_voice.cognition.internal_state import (
    AFFECT_BASELINE, MAX_STATE_BIAS, DeltaTarget, DurationClass, EventAppraisal,
    PreferenceState, StateDelta, StateDeltaGate, UnifiedInternalState, action_bias,
    appraise, band, decay, expression_constraints, propose_affect_deltas,
)
from neuro_voice.cognition.types import InformationType


def gate() -> StateDeltaGate:
    return StateDeltaGate()


def affect_delta(dimension: str, amount: float, **kwargs) -> StateDelta:
    kwargs.setdefault("target", DeltaTarget.GLOBAL_AFFECT)
    kwargs.setdefault("duration_class", DurationClass.MOMENTARY)
    kwargs.setdefault("source_event_ids", ("evt1",))
    return StateDelta(dimension=dimension, proposed_delta=amount, **kwargs)


def state_with(**affect) -> UnifiedInternalState:
    values = dict(AFFECT_BASELINE)
    values.update(affect)
    return UnifiedInternalState(affect=values)


# ---------------------------------------------------------------------------
# 監査で見つかった、切れていた配線
# ---------------------------------------------------------------------------


def test_irritation_is_actually_readable_now():
    """**ここは長らく常に 0.0 を返していた。**

    `TemporalSelf.snapshot()` は感情を `"affect"` の下に入れて返し、
    `AffectState` の軸名は `frustration`。名前も階層も食い違っていたので、
    苛立ち時の抑制も冗談の罰も一度も効いていなかった。
    """
    nested = build_state(affect={"calendar_age_seconds": 1, "affect": {"frustration": .8}})
    assert nested.irritation == pytest.approx(.8)
    flat = build_state(affect={"irritation": .6})
    assert flat.irritation == pytest.approx(.6)
    assert build_state().irritation == 0.0


@pytest.mark.parametrize("name", ["caution", "tension", "familiarity", "playfulness"])
def test_the_remaining_relationship_axes_reach_action_selection(name):
    """17軸のうち行動選択が読めるのは trust と comfort だけだった。"""
    state = build_state(relationship={name: .7})
    assert getattr(state, name) == pytest.approx(.7)
    assert name in state.summary()


# ---------------------------------------------------------------------------
# StateDelta と制約
# ---------------------------------------------------------------------------


def test_the_llm_cannot_write_a_state_value_directly():
    """**確信の低い提案は捨てる。** 型と confidence を先に見る。"""
    assert not gate().evaluate(affect_delta("irritation", .5, confidence=.2), .05).applied
    assert gate().evaluate(affect_delta("irritation", .03, confidence=.9), .05).applied
    assert gate().evaluate(affect_delta("irritation", float("nan")), .05).clamp_reason == "not_finite"
    assert gate().evaluate(affect_delta("irritation", "たくさん"), .05).clamp_reason == "not_a_number"


def test_a_short_term_event_cannot_move_a_long_term_value():
    """**一瞬の苛立ちで信頼を削らない。** 機嫌で人を評価することになる。"""
    delta = StateDelta(
        target=DeltaTarget.PARTICIPANT_RELATIONSHIP, dimension="trust",
        proposed_delta=-.2, duration_class=DurationClass.MOMENTARY,
        source_event_ids=("evt1",), participant_id="chibi",
    )
    checked = gate().evaluate(delta, .5)
    assert not checked.applied
    assert checked.clamp_reason == "short_term_cannot_move_long_term"


def test_a_long_term_change_needs_an_evidence_id():
    """根拠のIDを持たない長期変化は残さない。後から何も辿れない。"""
    delta = StateDelta(
        target=DeltaTarget.PARTICIPANT_RELATIONSHIP, dimension="trust",
        proposed_delta=.01, duration_class=DurationClass.LONG_TERM,
        source_type=InformationType.OBSERVATION, participant_id="chibi",
    )
    assert gate().evaluate(delta, .5).clamp_reason == "long_term_needs_evidence_id"


def test_an_inference_cannot_move_a_long_term_value():
    delta = StateDelta(
        target=DeltaTarget.PARTICIPANT_RELATIONSHIP, dimension="trust",
        proposed_delta=.01, duration_class=DurationClass.LONG_TERM,
        source_type=InformationType.INFERENCE, source_memory_ids=(4,),
        participant_id="chibi",
    )
    assert gate().evaluate(delta, .5).clamp_reason == "long_term_needs_grounded_source"


def test_the_same_event_is_not_applied_twice():
    checker = gate()
    assert checker.evaluate(affect_delta("irritation", .03), .05).applied
    second = checker.evaluate(affect_delta("irritation", .03), .08)
    assert not second.applied
    assert second.clamp_reason == "duplicate_event"


def test_one_event_cannot_cross_two_bands():
    """**慣性。** 安心からいきなり敵対へは行かない。"""
    assert band(.1) == "neutral" and band(.4) == "slight" and band(.8) == "high"
    checked = gate().evaluate(affect_delta("irritation", .9), .05)
    assert band(.05 + checked.applied_delta) != "high"


def test_a_single_event_moves_a_value_only_a_little():
    checked = gate().evaluate(affect_delta("irritation", 1.0), .05)
    assert 0 < checked.applied_delta <= .05, checked.snapshot()
    assert checked.clamp_reason


# ---------------------------------------------------------------------------
# 意味評価
# ---------------------------------------------------------------------------


def test_an_end_signal_is_a_boundary_not_hostility():
    """**打ち切りは敵意ではない。** 境界の表明として扱う。"""
    assert appraise(end_signal=.9, interrupted=True) is EventAppraisal.BOUNDARY_SIGNAL


@pytest.mark.parametrize("kwargs,expected", [
    ({"danger": .9}, EventAppraisal.GAME_EVENT),
    ({"correction": True}, EventAppraisal.CORRECTION),
    ({"outcome_status": "failed"}, EventAppraisal.FAILURE),
    ({"outcome_status": "spoken_completed"}, EventAppraisal.SUCCESS),
    ({"playful": True}, EventAppraisal.PLAYFUL_INTERACTION),
    ({"user_frustration": .8}, EventAppraisal.RELATIONSHIP_SIGNAL),
    ({}, EventAppraisal.NEUTRAL_EVENT),
])
def test_appraisal_uses_the_outcome_not_a_keyword(kwargs, expected):
    assert appraise(**kwargs) is expected


def test_hostility_raises_caution_not_irritation():
    """**腹を立てるのと距離を取るのは違う。**

    苛立ちで応じると、そこから先の判断が全部それに引きずられる。
    """
    deltas = {d.dimension: d.proposed_delta
              for d in propose_affect_deltas(EventAppraisal.HOSTILITY)}
    assert deltas.get("caution", 0) > 0
    assert "irritation" not in deltas


# ---------------------------------------------------------------------------
# 必須シナリオ: 単発の否定 / 繰り返し / 訂正 / 良い経験 / 修復
# ---------------------------------------------------------------------------


def test_a_single_negative_turn_does_not_touch_trust():
    """短期の苛立ちだけが動く。長期の信頼・親しさは大きく変わらない。"""
    deltas = propose_affect_deltas(EventAppraisal.FAILURE, event_id="e1")
    targets = {str(d.target) for d in deltas}
    assert targets == {str(DeltaTarget.GLOBAL_AFFECT)}
    assert all(str(d.duration_class) == str(DurationClass.MOMENTARY) for d in deltas)


def test_a_single_negative_turn_does_not_make_it_hostile():
    """敵対反応をしない。valence も comfort も落とさない。"""
    dimensions = {d.dimension for d in propose_affect_deltas(EventAppraisal.FAILURE)}
    assert "comfort" not in dimensions and "valence" not in dimensions


def test_repeated_end_signals_penalise_continuing_step_by_step():
    """段階的に。**一度で最大にはならない。**"""
    checker, current, applied = gate(), .05, []
    for index in range(4):
        delta = affect_delta("irritation", .03, source_event_ids=(f"e{index}",))
        checker.evaluate(delta, current)
        current += delta.applied_delta
        applied.append(action_bias(state_with(irritation=current)).for_action(
            ActionType.CONTINUE_PREVIOUS_TOPIC))
    assert applied[0] >= applied[-1], applied
    assert applied[-1] < 0
    assert abs(applied[-1]) <= MAX_STATE_BIAS


def test_irritation_does_not_follow_into_a_new_topic():
    """**別の話題まで不機嫌を引きずらない。** 時間で戻る。"""
    heated = {**AFFECT_BASELINE, "irritation": .4}
    cooled = decay(heated, elapsed_seconds=1200)
    assert cooled["irritation"] < .15
    assert action_bias(state_with(**cooled)).empty


def test_a_correction_lowers_confidence_and_favours_checking():
    deltas = {d.dimension: d.proposed_delta
              for d in propose_affect_deltas(EventAppraisal.CORRECTION)}
    assert deltas["confidence"] < 0
    bias = action_bias(state_with(confidence=.45))
    assert bias.for_action(ActionType.ASK_CLARIFICATION) > 0
    assert bias.for_action(ActionType.ANSWER) < 0


def test_a_correction_does_not_lower_trust_in_the_person():
    """訂正されたのは情報であって、相手ではない。"""
    targets = {str(d.target) for d in propose_affect_deltas(EventAppraisal.CORRECTION)}
    assert str(DeltaTarget.PARTICIPANT_RELATIONSHIP) not in targets


def test_one_good_experience_does_not_create_a_favourite():
    """**一度楽しかったで大好きにならない。**"""
    preference = PreferenceState(subject="ktane")
    preference.observe(1.0)
    assert preference.label == "unclear", preference.snapshot()
    for _ in range(4):
        preference.observe(1.0)
    assert preference.label == "leans_positive"
    assert preference.confidence <= .9


def test_one_bad_experience_does_not_flip_a_liking():
    """**一度の不快で嫌いにならない。** 符号がすぐには反転しない。"""
    preference = PreferenceState(subject="ktane")
    for _ in range(4):
        preference.observe(1.0)
    before = preference.valence
    preference.observe(-1.0)
    assert preference.valence > 0, (before, preference.valence)
    assert preference.contradiction_count == 1
    assert preference.confidence < .9


def test_a_relationship_can_recover():
    """**否定的な状態を永久固定しない。**"""
    tense = state_with(irritation=.35, tension=.4, comfort=.4)
    assert action_bias(tense).for_action(ActionType.CONTINUE_PREVIOUS_TOPIC) < 0
    recovered = decay(tense.affect, elapsed_seconds=1800)
    assert recovered["irritation"] < .15
    assert action_bias(state_with(**recovered)).for_action(
        ActionType.CONTINUE_PREVIOUS_TOPIC) == 0


# ---------------------------------------------------------------------------
# 安全側 — ここが今回いちばん大事
# ---------------------------------------------------------------------------


def test_emotion_never_touches_a_warning():
    """**嫌いな相手への警告を弱めない。** 触らせない側で防ぐ。"""
    hostile = state_with(irritation=.9, caution=.9, comfort=.0, amusement=.0)
    bias = action_bias(hostile)
    assert bias.for_action(ActionType.WARN) == 0

    decision = CognitiveKernel(state_bias=bias).decide(
        CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.95),
        build_state(world_state={"danger": .9}),
    )
    assert decision.selected_action is ActionType.WARN


def test_silence_is_never_an_emotional_punishment():
    """**沈黙は会話上の選択であって、不機嫌の表明ではない。**"""
    for irritation in (.05, .35, .6, .95):
        bias = action_bias(state_with(irritation=irritation))
        assert bias.for_action(ActionType.REMAIN_SILENT) == 0, irritation


def test_the_closure_path_still_wins_regardless_of_mood():
    """終了シグナルは感情状態に関係なく優先される。"""
    playful = state_with(amusement=.9, comfort=.9, valence=.9)
    ending = build_state(user_state={"end_signal": .9, "engagement": .05})
    decision = CognitiveKernel(state_bias=action_bias(playful)).decide(
        CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ"), ending,
    )
    assert decision.selected_action in {
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE,
    }


def test_a_good_mood_cannot_unblock_a_low_confidence_answer():
    """Speech Gate と入力確信の下限を、機嫌で越えさせない。"""
    happy = state_with(valence=.95, comfort=.95, confidence=.95, amusement=.9)
    candidates = CognitiveKernel(state_bias=action_bias(happy)).propose(
        CognitiveEvent(event_type=EventType.ASR_UNCERTAIN, confidence=.2),
        build_state(uncertainty={"input": .2}),
    )
    answer = [c for c in candidates if c.action_type is ActionType.ANSWER]
    assert answer and answer[0].blocked


def test_humour_needs_more_than_a_good_mood():
    """遊び心・安心・面白さの3つがそろった時だけ。"""
    assert action_bias(state_with(amusement=.8)).for_action(ActionType.MAKE_LIGHT_JOKE) == 0
    safe = UnifiedInternalState(
        affect={**AFFECT_BASELINE, "amusement": .8, "comfort": .7},
        social_stance={"playfulness": .6},
    )
    assert action_bias(safe).for_action(ActionType.MAKE_LIGHT_JOKE) > 0


def test_irritation_suppresses_humour():
    grumpy = UnifiedInternalState(
        affect={**AFFECT_BASELINE, "amusement": .8, "comfort": .7, "irritation": .35},
        social_stance={"playfulness": .6},
    )
    assert action_bias(grumpy).for_action(ActionType.MAKE_LIGHT_JOKE) < 0


def test_curiosity_does_not_increase_questions():
    """**好奇心で質問を増やさない。** 増やすのは自分から述べる側。"""
    curious = state_with(curiosity=.95)
    assert action_bias(curious).for_action(ActionType.ASK_CLARIFICATION) == 0
    assert action_bias(curious).for_action(ActionType.CONTINUE_PREVIOUS_TOPIC) > 0


def test_every_bias_stays_small():
    extreme = UnifiedInternalState(
        affect={name: 1.0 for name in AFFECT_BASELINE},
        social_stance={"playfulness": 1.0},
    )
    for value in action_bias(extreme).adjustments.values():
        assert abs(value) <= MAX_STATE_BIAS


# ---------------------------------------------------------------------------
# Planner への接続
# ---------------------------------------------------------------------------


def test_the_planner_gets_coarse_labels_not_numbers():
    """**生の状態も履歴もプロンプトへ投入しない。**"""
    import json

    view = expression_constraints(state_with(irritation=.4, tension=.5))
    text = json.dumps(view, ensure_ascii=False)
    assert "0.4" not in text and "0.5" not in text
    assert set(view) == {"affect", "stance", "relationship", "expression_constraints"}
    assert view["stance"]["mode"] == "guarded"


def test_humour_is_closed_off_while_winding_down():
    view = expression_constraints(
        state_with(amusement=.9, comfort=.9), end_signal=.9,
    )
    assert view["expression_constraints"]["humor_allowed"] is False
    assert view["expression_constraints"]["verbosity"] == "short"


def test_low_confidence_makes_the_wording_hedged():
    view = expression_constraints(state_with(confidence=.3, caution=.4))
    assert view["expression_constraints"]["assertiveness"] == "hedged"


# ---------------------------------------------------------------------------
# 相手ごと・不明話者・永続化
# ---------------------------------------------------------------------------


def service(tmp_path, **flags):
    from neuro_voice.mind.internal import InternalStateService
    from neuro_voice.mind.relationship import RelationshipStore

    class Cfg:
        def get(self, key, default=None):
            return flags.get(key, default)

    cfg = Cfg()
    return InternalStateService(
        cfg, relationships=RelationshipStore(tmp_path / "rel.json", cfg),
    )


ALL_ON = {
    "internal_state.enabled": True, "internal_state.affect_enabled": True,
    "internal_state.relationship_updates_enabled": True,
    "internal_state.preference_updates_enabled": True,
    "internal_state.planner_expression_enabled": True,
    "mind.relationship.enabled": True,
}


def long_term(dimension: str, amount: float, participant: str) -> StateDelta:
    return StateDelta(
        target=DeltaTarget.PARTICIPANT_RELATIONSHIP, dimension=dimension,
        proposed_delta=amount, duration_class=DurationClass.LONG_TERM,
        source_type=InformationType.USER_STATEMENT, source_memory_ids=(1,),
        source_event_ids=("e1",), participant_id=participant,
    )


def test_one_participant_does_not_affect_another(tmp_path):
    """**Aの出来事をBへ波及させない。**"""
    internal = service(tmp_path, **ALL_ON)
    internal.apply([long_term("comfort", .02, "speaker_a")])
    state = internal.snapshot("speaker_b")
    assert state.relationship("speaker_b").get("comfort", .5) == pytest.approx(.5, abs=.01)
    assert internal.snapshot("speaker_a").relationship("speaker_a")["comfort"] > .5


def test_an_unknown_speaker_does_not_move_a_long_term_value(tmp_path):
    """誰のものか分からない出来事で、特定の人との関係を変えない。"""
    internal = service(tmp_path, **ALL_ON)
    applied = internal.apply([long_term("trust", .02, "unknown")])
    assert not applied[0].applied
    assert applied[0].clamp_reason == "unknown_participant"


def test_short_term_affect_resets_but_long_term_survives(tmp_path):
    """**再起動で短期感情は消えてよい。長期の関係は残る。**"""
    internal = service(tmp_path, **ALL_ON)
    internal.apply([affect_delta("irritation", .04)])
    internal.apply([long_term("comfort", .02, "chibi")])
    assert internal.snapshot().affect["irritation"] > AFFECT_BASELINE["irritation"]

    internal.reset_short_term()
    assert internal.snapshot().affect["irritation"] == AFFECT_BASELINE["irritation"]
    assert internal.snapshot("chibi").relationship("chibi")["comfort"] > .5


def test_the_long_term_state_survives_a_new_service(tmp_path):
    """**LLMを交換しても長期の関係は維持される**（第5条）。"""
    first = service(tmp_path, **ALL_ON)
    first.apply([long_term("comfort", .02, "chibi")])
    before = first.snapshot("chibi").relationship("chibi")["comfort"]

    second = service(tmp_path, **ALL_ON)
    assert second.snapshot("chibi").relationship("chibi")["comfort"] == pytest.approx(
        before, abs=.001)
    assert second.snapshot().affect["irritation"] == AFFECT_BASELINE["irritation"]


# ---------------------------------------------------------------------------
# 機能フラグ・トレース・レイテンシ
# ---------------------------------------------------------------------------


def test_the_defaults_are_all_off(tmp_path):
    internal = service(tmp_path)
    for flag in ("enabled", "affect_enabled", "relationship_updates_enabled",
                 "preference_updates_enabled", "planner_expression_enabled"):
        assert not getattr(internal, flag), flag


def test_nothing_moves_while_the_flag_is_off(tmp_path):
    internal = service(tmp_path)
    assert internal.observe_outcome(outcome=type("O", (), {"status": "failed"})()) == []
    assert internal.action_bias(internal.snapshot()).empty
    assert internal.planner_view(internal.snapshot()) == {}


def test_the_planner_view_needs_its_own_flag(tmp_path):
    internal = service(
        tmp_path, **{"internal_state.enabled": True, "internal_state.affect_enabled": True},
    )
    assert internal.planner_view(internal.snapshot()) == {}


def test_the_trace_keeps_no_raw_history():
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    trace.internal_state_summary = state_with(irritation=.3).summary()
    trace.event_appraisal = str(EventAppraisal.BOUNDARY_SIGNAL)
    trace.state_effect_on_actions = action_bias(state_with(irritation=.3)).snapshot()
    payload = trace.snapshot()["internal_state"]
    assert payload["appraisal"] == "boundary_signal"
    assert payload["effect_on_actions"]["adjustments"]["continue_previous_topic"] < 0
    assert "prompt" not in json.dumps(payload)


def test_a_turn_needs_no_extra_llm_call(tmp_path):
    """**通常ターンで追加のLLM呼び出しを必須にしない。**

    サービスにLLMを一切渡していない状態で、一連が最後まで通ること。
    """
    internal = service(tmp_path, **ALL_ON)
    outcome = type("O", (), {"status": "interrupted", "interrupted": True})()
    cognitive = build_state(user_state={"end_signal": .9})
    assert internal.observe_outcome(
        outcome=outcome, cognitive_state=cognitive, event_id="e1",
    )
    state = internal.snapshot()
    assert internal.action_bias(state) is not None
    assert internal.planner_view(state)
    for key in ("state_snapshot_ms", "event_appraisal_ms", "state_delta_ms",
                "state_apply_ms", "state_to_action_bias_ms", "state_to_planner_ms"):
        assert key in internal.latency_ms, key


# ---------------------------------------------------------------------------
# 垂直スライス（§16）
# ---------------------------------------------------------------------------


def test_the_vertical_slice_end_to_end(tmp_path):
    """説明を続ける → 終了合図 → 苛立ちが少し上がる → CONTINUE へ罰
    → 沈黙か短い了承 → 時間で戻る。**この一連が動くのが Phase 4 の最小形。**
    """
    internal = service(tmp_path, **ALL_ON)
    ending = build_state(user_state={"end_signal": .9, "engagement": .05},
                         obligations=("前の説明の続き",))
    outcome = type("O", (), {"status": "interrupted", "interrupted": True})()
    decision = type("D", (), {"selected_action": ActionType.CONTINUE_PREVIOUS_TOPIC})()

    for index in range(3):
        internal.observe_outcome(
            decision=decision, outcome=outcome, cognitive_state=ending,
            event_id=f"turn{index}", participant_id="chibi",
        )
    state = internal.snapshot("chibi")
    assert state.affect["caution"] > AFFECT_BASELINE["caution"]

    bias = internal.action_bias(state, participant_id="chibi")
    chosen = CognitiveKernel(state_bias=bias if not bias.empty else None).decide(
        CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ"), ending,
    )
    assert chosen.selected_action in {
        ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE,
    }
    # そして戻る。**否定的な状態を永久固定しない。**
    cooled = decay(state.affect, elapsed_seconds=3600)
    assert cooled["caution"] < state.affect["caution"]
    assert cooled["irritation"] <= AFFECT_BASELINE["irritation"] + .01
