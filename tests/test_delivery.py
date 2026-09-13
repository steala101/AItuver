"""A written pause has to become a heard one.

Style-Bert-VITS2 reads 「うーん……そうだなあ」 at the same pace as the rest of the
sentence, so writing the hesitation is not enough — the silence has to be put
into the audio stream.  The carrier is 「……」 rather than a tag, because the
same string is shown on screen (第1条 1.4).
"""
from __future__ import annotations

import pytest

from neuro_voice.tts.delivery import (
    SpokenPiece, count_gestures, has_hesitation, plan_delivery,
)


def texts(pieces):
    return [piece.text for piece in pieces]


def pauses(pieces):
    return [piece.pause_after_ms for piece in pieces]


# ---------------------------------------------------------------------------
# Ordinary speech is untouched
# ---------------------------------------------------------------------------


def test_a_fluent_sentence_stays_one_piece():
    pieces = plan_delivery("今日は天気がよかったから、少し歩いてきたよ。")
    assert len(pieces) == 1
    assert pieces[0].pause_after_ms == 0
    assert pieces[0].speed_scale == 1.0


def test_empty_text_produces_nothing():
    assert plan_delivery("") == []
    assert plan_delivery("   ") == []


def test_a_comma_is_not_a_pause():
    """Normal punctuation is the synthesiser's job, not ours."""
    pieces = plan_delivery("そうだね、それは面白い。")
    assert len(pieces) == 1


# ---------------------------------------------------------------------------
# Written hesitation becomes silence
# ---------------------------------------------------------------------------


def test_an_ellipsis_splits_the_sentence():
    pieces = plan_delivery("うーん……ちょっと自信ないな。")
    assert texts(pieces) == ["うーん", "ちょっと自信ないな。"]
    assert pieces[0].pause_after_ms > 0


def test_a_longer_ellipsis_reads_as_a_longer_beat():
    short = plan_delivery("うーん…そうかも。")[0].pause_after_ms
    long = plan_delivery("うーん………そうかも。")[0].pause_after_ms
    assert long > short


def test_the_pause_has_a_ceiling():
    pieces = plan_delivery("うーん…………そうかも。", max_pause_ms=700)
    assert pieces[0].pause_after_ms == 700


def test_a_filler_opening_is_spoken_more_slowly():
    pieces = plan_delivery("えっと……たしか去年の話だったと思う。")
    assert pieces[0].speed_scale < 1.0
    assert pieces[1].speed_scale == 1.0, "迷っていない部分まで遅くしない"


def test_only_the_hesitating_part_slows_down():
    pieces = plan_delivery("うーん……いや、それははっきり違うと思う。")
    assert pieces[0].speed_scale < 1.0
    assert pieces[-1].speed_scale == 1.0


def test_a_sentence_ending_in_an_ellipsis_keeps_the_silence():
    pieces = plan_delivery("なんて言えばいいかな……")
    assert len(pieces) == 1
    assert pieces[0].pause_after_ms > 0


def test_a_sentence_opening_with_an_ellipsis_does_not_emit_an_empty_piece():
    pieces = plan_delivery("……そうだね。")
    assert all(piece.speaks for piece in pieces)
    assert texts(pieces) == ["そうだね。"]


def test_an_open_ending_gets_a_short_beat():
    """「〜かな」 left hanging sounds clipped without a breath after it."""
    pieces = plan_delivery("たぶんそうだと思うけど")
    assert pieces[-1].pause_after_ms > 0


def test_a_closed_ending_gets_none():
    pieces = plan_delivery("それは確かに正しいよ。")
    assert pieces[-1].pause_after_ms == 0


def test_the_number_of_pauses_is_bounded():
    text = "あ……えっと……うーん……そうだね……たぶん……ね。"
    pieces = plan_delivery(text, max_pauses=2)
    assert sum(1 for piece in pieces if piece.pause_after_ms > 0) <= 3


# ---------------------------------------------------------------------------
# Nothing is invented
# ---------------------------------------------------------------------------


def test_no_pause_is_added_to_text_that_did_not_ask_for_one():
    text = "昨日はカレーを作ったよ。玉ねぎを焦がしちゃったけどね"
    pieces = plan_delivery(text)
    # The open ending earns a beat; nothing is inserted mid-sentence.
    assert len(pieces) == 1


def test_the_spoken_text_is_never_changed():
    text = "うーん……たしかそうだった。"
    joined = "".join(texts(plan_delivery(text)))
    assert joined == "うーんたしかそうだった。"     # only the ellipsis is consumed


@pytest.mark.parametrize("text,expected", [
    ("うーん……そうだね", True),
    ("えっと、たしかね", True),
    ("そうだね、それは面白い。", False),
    ("", False),
])
def test_detecting_whether_a_reply_hesitates(text, expected):
    assert has_hesitation(text) is expected


@pytest.mark.parametrize("text,count", [
    ("うーん……そうだね", 2),
    ("……あれ……いや……", 3),
    ("はっきりそう思う。", 0),
])
def test_counting_gestures_for_the_damping_input(text, count):
    assert count_gestures(text) == count
