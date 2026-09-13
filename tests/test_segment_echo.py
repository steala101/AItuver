"""A radio segment must not open by repeating how the last one ended.

Observed: two consecutive chat boxes during a radio talk.

    …お互いの安全や尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。
    お互いの尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。だからこそ…

The monologue guide asks the next segment to continue "from the last thought
of the previous one", and the previous segment is also shown to the model, so
a local 12B obliges by restating that closing sentence.  Note the two lines are
not identical — 「安全や」 is dropped — so an equality check does not see it.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.dialogue.directive import sentences, trim_echoed_opening
from tests.test_behavior_directive import (
    RADIO_PLAN, execute, make_controller, segment, start,
)

CLOSING = (
    "ニュースを見ていると、多様性を認めるっていうのは、ただ「混ぜる」だけじゃなくて、"
    "お互いの安全や尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。"
)
ECHO = "お互いの尊厳を守り合うことがすごく大事なんだなって改めて感じるよ。"
NEW = "だからこそ、誰かが傷つくことを目的とした行為には絶対に反対だし、"


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def test_a_reworded_echo_is_detected():
    """Not a string match: 「安全や」 was dropped in the repeat."""
    assert trim_echoed_opening(ECHO + NEW, CLOSING) == NEW


def test_a_verbatim_echo_is_detected():
    assert trim_echoed_opening(CLOSING + NEW, CLOSING) == NEW


def test_an_all_echo_segment_becomes_empty():
    assert trim_echoed_opening(ECHO, CLOSING) == ""


def test_genuinely_new_text_is_untouched():
    text = "さて、次は全然ちがう話。昨日ためした料理がひどい出来だったんだ。"
    assert trim_echoed_opening(text, CLOSING) == text


def test_a_shared_topic_word_is_not_an_echo():
    """Talking about the same subject is the point; repeating a line is not."""
    text = "尊厳って言葉、普段はあまり使わないけど重たいよね。"
    assert trim_echoed_opening(text, CLOSING) == text


def test_a_short_opening_is_never_trimmed():
    """「そうだね。」 after a similar line is phrasing, not a repeat."""
    assert trim_echoed_opening("うん。" + NEW, "うん。") == "うん。" + NEW


def test_at_most_two_sentences_are_dropped():
    previous = "あ。い。う。え。"
    text = "あ、そうだよね、確かにそう思う。い、そうだよね、確かにそう思う。本題はここから。"
    kept = trim_echoed_opening(text, text, max_dropped=2)
    assert len(sentences(kept)) == 1


def test_no_previous_segment_means_nothing_to_compare():
    assert trim_echoed_opening(ECHO, "") == ECHO


def test_empty_input_is_safe():
    assert trim_echoed_opening("", CLOSING) == ""


@pytest.mark.parametrize("text,count", [
    ("一つ。二つ！三つ？", 3),
    ("句点なし", 1),
    ("", 0),
])
def test_sentence_splitting(text, count):
    assert len(sentences(text)) == count


# ---------------------------------------------------------------------------
# Through the segment loop
# ---------------------------------------------------------------------------


def running_show():
    controller = make_controller()
    directive = start(controller)
    execute(controller, directive, segment(spoken_content=CLOSING, topic="ニュース"))
    return controller, directive


def test_the_echo_never_reaches_the_speaker():
    controller, directive = running_show()
    _outcome, said = execute(
        controller, directive, segment(spoken_content=ECHO + NEW, topic="ニュース"),
    )
    assert said[-1] == NEW
    assert controller.metrics["segment_echo_trimmed"] == 1


def test_a_segment_that_is_only_an_echo_is_treated_as_a_loop():
    controller, directive = running_show()
    outcome, said = execute(
        controller, directive, segment(spoken_content=ECHO, topic="ニュース"),
    )
    assert outcome.status == "waited"
    assert outcome.reason == "echoed_previous_segment"
    assert said == []                     # nothing was spoken at all
    assert controller.metrics["directive_loops_detected"] >= 1


def test_a_normal_continuation_is_spoken_in_full():
    controller, directive = running_show()
    follow_up = "ところで、昨日ためした料理がひどい出来だったんだ。焦がしちゃってさ。"
    _outcome, said = execute(
        controller, directive, segment(spoken_content=follow_up, topic="料理"),
    )
    assert said[-1] == follow_up
    assert controller.metrics["segment_echo_trimmed"] == 0


def test_the_first_segment_has_nothing_to_echo():
    controller = make_controller()
    directive = start(controller)
    _outcome, said = execute(
        controller, directive, segment(spoken_content=CLOSING, topic="ニュース"),
    )
    assert said == [CLOSING]
