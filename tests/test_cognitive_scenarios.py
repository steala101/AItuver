"""**同じ発話でも、内部状態が違えば行動が変わること。**

Phase 2 の目的は機能追加ではなく証明である。音声にもDiscordにも繋がず、
`CognitiveEvent` の列を投入して結果を比べる。汎用フレームワークは作らない
——ここにある小さな `Scenario` で足りる。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from neuro_voice.cognition import (
    ActionOutcome, ActionType, CognitiveEvent, CognitiveKernel, EventType, build_state,
)
from neuro_voice.cognition.trace import STATE_LIMITS, CognitiveTrace, limited_change


@dataclass(slots=True)
class Scenario:
    name: str
    initial_state: dict[str, Any] = field(default_factory=dict)
    events: tuple[CognitiveEvent, ...] = ()
    expected_actions: tuple[ActionType, ...] = ()
    forbidden_actions: tuple[ActionType, ...] = ()

    def run(self) -> tuple[ActionType, list, CognitiveTrace]:
        kernel = CognitiveKernel()
        state = build_state(**self.initial_state)
        event = self.events[0]
        trace = CognitiveTrace()
        started = time.perf_counter()
        candidates = kernel.propose(event, state)
        decision = kernel.decide(event, state, candidates)
        trace.mark("action_selection_ms", (time.perf_counter() - started) * 1000)
        trace.selected_action = str(decision.selected_action)
        trace.decision_scores = {
            str(item.action_type): item.total_score for item in candidates
        }
        trace.state_snapshot_summary = state.summary()
        return decision.selected_action, candidates, trace


def utterance(text: str, confidence: float = 1.0) -> CognitiveEvent:
    return CognitiveEvent(
        event_type=EventType.USER_UTTERANCE, content=text, confidence=confidence,
    )


def check(scenario: Scenario) -> None:
    action, candidates, trace = scenario.run()
    if scenario.expected_actions:
        assert action in scenario.expected_actions, (
            f"{scenario.name}: {action} / 期待 {scenario.expected_actions}"
        )
    for forbidden in scenario.forbidden_actions:
        assert action is not forbidden, f"{scenario.name}: {forbidden} を選んだ"
        proposed = {item.action_type for item in candidates}
        assert forbidden not in proposed, f"{scenario.name}: {forbidden} を候補に出した"
    assert trace.decision_scores, "候補の点数がトレースに残ること"
    assert trace.latency_breakdown["action_selection_ms"] < 50, "選択に時間をかけない"


# ---------------------------------------------------------------------------
# ケースA: 「もういいよ」
# ---------------------------------------------------------------------------

ENDING = {"end_signal": .9, "engagement": .1}


def test_case_a1_ordinary_small_talk_stops():
    check(Scenario(
        "A1 雑談中の打ち切り",
        initial_state={"user_state": ENDING},
        events=(utterance("もういいよ"),),
        expected_actions=(ActionType.REMAIN_SILENT, ActionType.ANSWER),
        forbidden_actions=(ActionType.CONTINUE_PREVIOUS_TOPIC, ActionType.MAKE_LIGHT_JOKE),
    ))


def test_case_a2_does_not_resume_a_lecture_the_user_ended():
    """説明の途中で止められた。**勝手に再開しない。**"""
    action, candidates, _ = Scenario(
        "A2 説明中断後の打ち切り",
        initial_state={
            "user_state": ENDING,
            "world_state": {"interrupted_response": "さっきの説明の続きだけど"},
            "obligations": ("finish_interrupted_response",),
        },
        events=(utterance("もういいよ"),),
    ).run()
    assert action is not ActionType.RESUME_INTERRUPTED_RESPONSE
    assert action is not ActionType.CONTINUE_PREVIOUS_TOPIC
    # 打ち切りの合図が、再開の点数を実際に下げていること（絶対値ではなく差で見る）。
    kernel = CognitiveKernel()
    keep_going = {
        "world_state": {"interrupted_response": "続き"},
        "obligations": ("finish_interrupted_response",),
    }
    with_end = kernel._score(
        ActionType.CONTINUE_PREVIOUS_TOPIC,
        build_state(**keep_going, user_state=ENDING), utterance("もういいよ"),
    ).score_components["conversation_coherence"]
    without_end = kernel._score(
        ActionType.CONTINUE_PREVIOUS_TOPIC,
        build_state(**keep_going), utterance("それで？"),
    ).score_components["conversation_coherence"]
    assert with_end < without_end
    del candidates


def test_case_a3_high_irritation_never_jokes():
    check(Scenario(
        "A3 苛立ちが高い",
        initial_state={
            "user_state": {**ENDING, "frustration": .8},
            "affect": {"irritation": .8}, "relationship": {"comfort": .9},
        },
        events=(utterance("もういいよ"),),
        expected_actions=(
            ActionType.REMAIN_SILENT, ActionType.ACKNOWLEDGE_EMOTION,
        ),
        forbidden_actions=(ActionType.MAKE_LIGHT_JOKE,),
    ))


# ---------------------------------------------------------------------------
# ケースB: 「どう思う？」
# ---------------------------------------------------------------------------


def test_case_b1_a_clear_topic_is_answered():
    check(Scenario(
        "B1 直前の話題が明確",
        initial_state={"working_memory": {"active_theme": "配信構成", "ambiguity": .0}},
        events=(utterance("どう思う？"),),
        expected_actions=(ActionType.ANSWER,),
    ))


def test_case_b2_several_candidates_ask_first():
    check(Scenario(
        "B2 話題候補が複数",
        initial_state={"working_memory": {"ambiguity": .9}},
        events=(utterance("どう思う？"),),
        expected_actions=(ActionType.ASK_CLARIFICATION,),
    ))


def test_case_b3_low_confidence_never_guesses():
    check(Scenario(
        "B3 聞き取りが怪しい",
        initial_state={"uncertainty": {"input": .3}},
        events=(utterance("どう思う？", confidence=.3),),
        expected_actions=(ActionType.ASK_CLARIFICATION, ActionType.REMAIN_SILENT),
    ))


# ---------------------------------------------------------------------------
# ケースC: 「失敗したかもしれない」
# ---------------------------------------------------------------------------


def test_case_c1_a_light_game_slip_stays_light():
    action, candidates, _ = Scenario(
        "C1 ゲーム内の軽い失敗",
        initial_state={"relationship": {"comfort": .8}, "affect": {"irritation": .0}},
        events=(utterance("失敗したかもしれない"),),
    ).run()
    assert ActionType.MAKE_LIGHT_JOKE in {c.action_type for c in candidates}


def test_case_c2_high_anxiety_prioritises_the_feeling():
    check(Scenario(
        "C2 不安が高い",
        initial_state={
            "user_state": {"distress": .85}, "relationship": {"comfort": .8},
        },
        events=(utterance("失敗したかもしれない"),),
        expected_actions=(ActionType.ACKNOWLEDGE_EMOTION,),
        forbidden_actions=(ActionType.MAKE_LIGHT_JOKE,),
    ))


def test_case_c3_a_serious_task_does_not_reassure_without_grounds():
    """根拠のない安心を与えない。まず状況を確かめる側へ寄せる。"""
    action, candidates, _ = Scenario(
        "C3 重大な現実タスク",
        initial_state={
            "user_state": {"distress": .6}, "working_memory": {"ambiguity": .8},
            "uncertainty": {"input": .9},
        },
        events=(utterance("失敗したかもしれない"),),
    ).run()
    assert action in {ActionType.ASK_CLARIFICATION, ActionType.ACKNOWLEDGE_EMOTION}
    assert ActionType.MAKE_LIGHT_JOKE not in {c.action_type for c in candidates}


# ---------------------------------------------------------------------------
# 同じ発話・違う状態で行動が変わる（これがPhase 2の主張そのもの）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,states", [
    ("もういいよ", ({"user_state": ENDING}, {"user_state": {"distress": .8}})),
    ("どう思う？", ({"working_memory": {"ambiguity": .9}}, {"uncertainty": {"input": 1.0}})),
])
def test_the_same_words_lead_to_different_actions(text, states):
    kernel = CognitiveKernel()
    chosen = {
        kernel.decide(utterance(text), build_state(**state)).selected_action
        for state in states
    }
    assert len(chosen) > 1, f"状態が違うのに同じ行動: {chosen}"


# ---------------------------------------------------------------------------
# 3種類の状態が、点数成分として実際に効いている
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,component,low,high", [
    (ActionType.ACKNOWLEDGE_EMOTION, "affect_fit",
     {"user_state": {"distress": .0}}, {"user_state": {"distress": .9}}),
    (ActionType.MAKE_LIGHT_JOKE, "relationship_fit",
     {"relationship": {"comfort": .1}}, {"relationship": {"comfort": .9}}),
    (ActionType.CONTINUE_PREVIOUS_TOPIC, "conversation_coherence",
     {"user_state": ENDING}, {"obligations": ("前の話",)}),
])
def test_state_moves_a_named_score_component(action, component, low, high):
    kernel = CognitiveKernel()
    a = kernel._score(action, build_state(**low), utterance("x")).score_components[component]
    b = kernel._score(action, build_state(**high), utterance("x")).score_components[component]
    assert b > a, f"{action}.{component}: {a} -> {b}"


# ---------------------------------------------------------------------------
# 変化量の上限
# ---------------------------------------------------------------------------


def test_a_single_event_cannot_swing_a_value():
    change = limited_change("trust", .5, -.9, reason="test", source_event_id="e1")
    assert change.delta >= -STATE_LIMITS["trust"].max_negative
    assert change.reason and change.source_event_id


def test_trust_is_lost_faster_than_it_is_regained():
    """積み上げた関係が一度の出来事で飛ばないように、上下を非対称にする。"""
    limit = STATE_LIMITS["trust"]
    assert limit.max_negative > limit.max_positive


def test_values_drift_back_towards_the_baseline():
    limit = STATE_LIMITS["irritation"]
    assert limit.apply(.5, .0) < .5


def test_outcome_deltas_are_capped_and_explained():
    kernel = CognitiveKernel()
    state = build_state(user_state={"distress": .9})
    decision = kernel.decide(utterance("つらい"), state)
    updates = CognitiveKernel.apply_outcome(
        decision, ActionOutcome(action_id=decision.action_id, status="completed"), state,
    )
    for change in updates.get("state_changes", []):
        assert abs(change["delta"]) <= .03, change
        assert change["reason"], change


# ---------------------------------------------------------------------------
# 補助行動
# ---------------------------------------------------------------------------


def test_a_supporting_action_is_at_most_one():
    kernel = CognitiveKernel()
    decision = kernel.decide(
        utterance("設計が全部間違ってる気がする"),
        build_state(user_state={"distress": .9}),
    )
    assert decision.supporting_action in {ActionType.ANSWER, None}
    assert decision.snapshot()["supporting"] in {"answer", ""}


# ---------------------------------------------------------------------------
# トレース
# ---------------------------------------------------------------------------


def test_the_trace_keeps_no_conversation(tmp_path):
    from neuro_voice.cognition.trace import TraceWriter

    trace = CognitiveTrace(state_snapshot_summary={"distress": .7})
    trace.selected_action = "acknowledge_emotion"
    trace.mark("action_selection_ms", 1.2)
    path = tmp_path / "trace.jsonl"
    TraceWriter(path, enabled=True).write(trace)
    written = path.read_text(encoding="utf-8")
    assert "acknowledge_emotion" in written
    assert "total" in written
    for forbidden in ("prompt", "content", "text"):
        assert f'"{forbidden}"' not in written


def test_the_trace_is_off_by_default(tmp_path):
    from neuro_voice.cognition.trace import TraceWriter

    path = tmp_path / "trace.jsonl"
    TraceWriter(path).write(CognitiveTrace())
    assert not path.exists(), "既定では1バイトも書かない"


def test_distress_is_recorded_as_an_inference_not_a_statement():
    """推定を本人の申告として保存しない。"""
    summary = build_state(user_state={"distress": .8}).summary()
    assert summary["distress_source"] == "inference"
