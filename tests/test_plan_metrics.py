"""Measuring whether the plan reached the reply.

`ConversationPlanner` is 537 lines and decides something every turn.  Nobody
has checked whether the decision survives into what gets said.  Two very
different faults produce the same complaint ("毎回似た構成"):

* the planner keeps choosing the same thing;
* the planner varies and the reply ignores it.

The second one cannot be fixed by adding another planner, so the measurement
has to come before the change.  These tests hold down the measurement itself —
including the part that keeps it honest, which is refusing to score what it
cannot actually check.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.plan_metrics import (
    MET, MISSED, UNVERIFIABLE, Adherence, Check, DiversityWindow,
    count_questions, ending_key, evaluate, opening_key, record_for,
)


class Plan:
    """Only the fields the measurement reads."""

    def __init__(self, **fields):
        defaults = dict(
            ask_follow_up=None, target_length="", minimum_moves=0,
            response_shape="", primary_style="", selected_features=(),
        )
        defaults.update(fields)
        for key, value in defaults.items():
            setattr(self, key, value)


def verdict_of(adherence, name):
    return next(c.verdict for c in adherence.checks if c.name == name)


# ---------------------------------------------------------------------------
# Honesty: what cannot be checked is not scored
# ---------------------------------------------------------------------------


def test_an_unverifiable_shape_is_marked_not_assumed_met():
    result = evaluate(Plan(response_shape="proposal_path"), "そうしてみようか。")
    assert verdict_of(result, "response_shape") == UNVERIFIABLE
    assert result.checked == 0


def test_both_readings_are_published():
    """A high score over two checks must not read as a high score overall."""
    result = Adherence(checks=[
        Check("a", MET), Check("b", MET),
        Check("c", UNVERIFIABLE), Check("d", UNVERIFIABLE),
    ])
    assert result.score == 1.0            # 検証できた範囲では満点
    assert result.strict_score == 0.5     # 判定不能を未達に倒すと半分
    assert result.checkable_ratio == 0.5  # そもそも半分しか測れていない


def test_nothing_checkable_scores_zero_rather_than_one():
    result = Adherence(checks=[Check("a", UNVERIFIABLE)])
    assert result.score == 0.0
    assert result.checkable_ratio == 0.0


def test_an_empty_reply_is_unverifiable():
    assert evaluate(Plan(ask_follow_up=True), "").checked == 0


# ---------------------------------------------------------------------------
# ask_follow_up — the one the user complained about
# ---------------------------------------------------------------------------


def test_a_plan_not_to_ask_is_kept():
    result = evaluate(Plan(ask_follow_up=False), "たぶんそれで合ってると思う。")
    assert verdict_of(result, "ask_follow_up") == MET


def test_a_plan_not_to_ask_is_broken_by_a_question():
    result = evaluate(Plan(ask_follow_up=False), "そうなんだ。どうしてそう思ったの？")
    assert verdict_of(result, "ask_follow_up") == MISSED
    assert "ask_follow_up" in result.violations


def test_a_plan_to_ask_is_broken_by_silence_on_the_matter():
    result = evaluate(Plan(ask_follow_up=True), "なるほどね。")
    assert verdict_of(result, "ask_follow_up") == MISSED


# ---------------------------------------------------------------------------
# 長さ・文数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length,reply,expected", [
    ("short", "うん、いいと思う。", MET),
    ("short", "そうだね。" * 40, MISSED),
    ("long", "詳しく説明すると。" * 20, MET),
    ("long", "短い。", MISSED),
])
def test_the_length_band_is_checked(length, reply, expected):
    assert verdict_of(evaluate(Plan(target_length=length), reply), "target_length") == expected


@pytest.mark.parametrize("length", ["short", "medium"])
def test_the_bands_overlap_so_a_boundary_reply_satisfies_either_plan(length):
    """帯の境目に落ちた返答は、どちらの計画でも違反にしない。

    境界ちょうどを狙って外すのは不服従ではないし、狼少年の計測は無いより悪い。
    """
    reply = "あ" * 90 + "。"
    assert verdict_of(evaluate(Plan(target_length=length), reply), "target_length") == MET


def test_an_unknown_band_is_unverifiable():
    assert verdict_of(
        evaluate(Plan(target_length="とても長い"), "本文"), "target_length",
    ) == UNVERIFIABLE


@pytest.mark.parametrize("length,size", [("short", 71), ("long", 106)])
def test_the_lengths_the_first_run_wrongly_called_violations(length, size):
    """実測 71文字/106文字。旧帯（short<=70, long>=110）は数文字の差で
    「計画を無視した」と判定していた。物差しの側の誤りだった。"""
    reply = "あ" * size + "。"
    assert verdict_of(evaluate(Plan(target_length=length), reply), "target_length") == MET


def test_the_planned_length_is_recorded_so_a_violation_can_be_read_later():
    """違反だけ残しても事後に読めない。初回の計測はこれを欠いていた。"""
    record = record_for(Plan(target_length="short", ask_follow_up=True), "どう思う？")
    snapshot = record.snapshot()
    assert snapshot["planned_length"] == "short"
    assert snapshot["planned_question"] is True


def test_the_window_reports_when_length_planning_is_pinned():
    window = DiversityWindow(size=4)
    for turn, length in enumerate(("long", "long", "medium", "short"), 1):
        window.record(record_for(
            Plan(target_length=length), "返答です。",
            turn=turn,
        ))
    assert window.metrics()["planned_length_distribution"] == {
        "long": 2, "medium": 1, "short": 1,
    }


def test_an_absent_question_plan_is_recorded_as_unknown_not_as_no():
    snapshot = record_for(Plan(), "本文。").snapshot()
    assert snapshot["planned_question"] is None
    assert snapshot["planned_length"] == ""


def test_the_minimum_number_of_moves_is_approximated_by_sentences():
    result = evaluate(Plan(minimum_moves=2), "そうだね。私はこう思う。")
    assert verdict_of(result, "minimum_moves") == MET
    result = evaluate(Plan(minimum_moves=3), "うん。")
    assert verdict_of(result, "minimum_moves") == MISSED


# ---------------------------------------------------------------------------
# shape / style のうち、文面に残るもの
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape,reply,expected", [
    ("concise_ping", "それいいね。", MET),
    ("concise_ping", "そうだね。" * 30, MISSED),
    ("question_hypothesis", "たぶんこうかな。どう思う？", MET),
    ("question_hypothesis", "たぶんこうだと思う。", MISSED),
    ("direct_take", "答えは三つあると思う。", MET),
    ("direct_take", "なるほど、それは大事だね。", MISSED),
    ("memory_bridge", "そういえば前に似た話をしたよね。", MET),
    ("memory_bridge", "面白い話だね。", MISSED),
    ("self_reconsider", "こうかな……いや待って、逆かも。", MET),
    ("self_reconsider", "こうだと思う。", MISSED),
    ("gentle_support", "無理しなくていいよ。", MET),
    ("gentle_support", "大丈夫？", MISSED),
])
def test_the_verifiable_shapes(shape, reply, expected):
    assert verdict_of(evaluate(Plan(response_shape=shape), reply), "response_shape") == expected


@pytest.mark.parametrize("style,reply,expected", [
    ("concise", "うん、それでいい。", MET),
    ("concise", "説明すると。" * 30, MISSED),
    ("curious", "それどうやったの？", MET),
    ("opinionated", "そこは違うと思う。", MET),
    ("opinionated", "そうなんだ。", MISSED),
])
def test_the_verifiable_styles(style, reply, expected):
    assert verdict_of(evaluate(Plan(primary_style=style), reply), "primary_style") == expected


def test_selected_features_are_admitted_to_be_uncheckable():
    result = evaluate(Plan(selected_features=("a", "b")), "本文。")
    assert verdict_of(result, "selected_features") == UNVERIFIABLE


# ---------------------------------------------------------------------------
# 記録に本文を残さない（第12条）
# ---------------------------------------------------------------------------


def test_the_record_keeps_no_words():
    record = record_for(Plan(ask_follow_up=False), "秘密の内容をここに書いた。", turn=7)
    dumped = str(record.snapshot())
    assert "秘密" not in dumped
    assert record.opening and record.opening != "秘密の内容をここ"


def test_the_same_opening_produces_the_same_key():
    assert opening_key("なるほどね。それで？") == opening_key("なるほどね。だから？")
    assert opening_key("なるほどね。") != opening_key("いや違うと思う。")


def test_punctuation_does_not_change_the_key():
    assert ending_key("そうだね。") == ending_key("そうだね")


@pytest.mark.parametrize("reply,count", [
    ("どう思う？", 1), ("なんで？どうして?", 2), ("そうだね。", 0),
])
def test_counting_questions(reply, count):
    assert count_questions(reply) == count


# ---------------------------------------------------------------------------
# 直近ターンの偏り
# ---------------------------------------------------------------------------


def window_of(*replies, shape="direct_take", style="concise"):
    window = DiversityWindow(size=8)
    for index, reply in enumerate(replies):
        window.record(record_for(
            Plan(response_shape=shape, primary_style=style), reply, turn=index,
        ))
    return window


def test_consecutive_questions_are_counted():
    window = window_of("どう？", "なんで？", "そうかな？")
    assert window.metrics()["consecutive_questions"] == 3


def test_a_turn_without_a_question_breaks_the_streak():
    window = window_of("どう？", "なんで？", "そうだね。")
    assert window.metrics()["consecutive_questions"] == 0


def test_repeated_openings_are_visible():
    window = window_of("なるほどね。あ。", "なるほどね。い。", "なるほどね。う。")
    assert window.metrics()["opening_repeat_ratio"] > 0.5


def test_varied_openings_are_not_flagged():
    window = window_of("なるほどね。", "いや違うと思う。", "それ面白いね。")
    assert window.metrics()["opening_repeat_ratio"] == 0.0


def test_the_same_shape_in_a_row_is_counted():
    assert window_of("あ。", "い。", "う。", shape="direct_take").metrics()["shape_streak"] == 3


def test_the_distribution_shows_the_bias():
    window = DiversityWindow(size=8)
    for shape in ("direct_take", "direct_take", "concise_ping"):
        window.record(record_for(Plan(response_shape=shape), "本文。"))
    assert window.metrics()["shape_distribution"] == {"direct_take": 2, "concise_ping": 1}


def test_length_variation_is_reported():
    metrics = window_of("短い。", "とても長い説明。" * 20, "普通の長さの返事だね。").metrics()
    assert metrics["length_stdev"] > 0


def test_the_window_forgets_old_turns():
    window = DiversityWindow(size=3)
    for index in range(10):
        window.record(record_for(Plan(), f"発話{index}。", turn=index))
    assert window.metrics()["window"] == 3


def test_an_empty_window_does_not_divide_by_zero():
    metrics = DiversityWindow().metrics()
    assert metrics["window"] == 0
    assert metrics["adherence_mean"] is None


def test_the_aggregate_publishes_both_readings():
    window = window_of("うん、それでいい。", "そうだね。")
    metrics = window.metrics()
    assert metrics["adherence_mean"] is not None
    assert metrics["strict_mean"] is not None
    assert metrics["checkable_ratio_mean"] is not None


def test_planner_on_and_off_are_distinguishable():
    """The A/B comparison depends on this flag surviving into the record."""
    on = record_for(Plan(), "本文。", planner_enabled=True)
    off = record_for(Plan(), "本文。", planner_enabled=False)
    assert on.snapshot()["planner"] is True
    assert off.snapshot()["planner"] is False
