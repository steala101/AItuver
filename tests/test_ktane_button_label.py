"""ボタンに書いてある文字を、日本語で受け取る。

実機で「長押しとか押すとかの表現が通じず、push / hold と英語表記でないと
伝わらない場面が結構あった」と言われた。原因は2つ。

1. こちらが「Abort / Detonate / Hold / Press」と英語で聞き返していた
2. 「押す」を語彙から外していた（動詞と同じ形なので誤読を恐れた）

2番目の直し方が間違っていた。**衝突する語を捨てるのではなく、文字を
読み上げている場面かどうかで分ける。** 捨てると、いちばん自然な言い方だけが
通らないという歪んだ挙動になる。
"""
from __future__ import annotations

import pytest

from neuro_voice.games.ktane import KtaneExpert
from neuro_voice.games.ktane.parse import parse_button_label


# ---------------------------------------------------------------------------
# 日本語で言われた文字が届く
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spoken,expected", [
    ("長押しって書いてある", "hold"),
    ("押すって書いてある", "press"),
    ("文字は長押し", "hold"),
    ("ラベルは押す", "press"),
    ("中止って表示されてる", "abort"),
    ("爆破って書いてる", "detonate"),
])
def test_japanese_labels_are_understood(spoken, expected):
    assert parse_button_label(spoken) == expected


@pytest.mark.parametrize("spoken,expected", [
    ("hold", "hold"), ("HOLD", "hold"), ("ホールド", "hold"),
    ("push", "press"), ("プッシュ", "press"), ("press", "press"),
    ("abort", "abort"), ("アボート", "abort"), ("中止", "abort"),
    ("detonate", "detonate"), ("起爆", "detonate"), ("爆破", "detonate"),
])
def test_the_unambiguous_wordings_still_work(spoken, expected):
    """英語で言う人を締め出さない。増やしただけで、減らしていない。"""
    assert parse_button_label(spoken) == expected


# ---------------------------------------------------------------------------
# 動詞と文字を混ぜない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spoken", [
    "ボタンを押すね",
    "じゃあ押すよ",
    "押していい？",
])
def test_a_bare_verb_is_not_read_as_the_label(spoken):
    """**尋ねてもいないのに「押す」で label を確定させない。**

    確定すると、色だけ聞いた状態で誤った指示が出て即ミスになる。
    """
    assert parse_button_label(spoken) == ""


@pytest.mark.parametrize("spoken", [
    "押したよ", "押しました", "長押ししてみた", "押しといた",
])
def test_a_report_of_having_pressed_is_not_a_label(spoken):
    """尋ねた直後でも、押した報告は文字ではない。"""
    assert parse_button_label(spoken, expected=True) == ""


def test_asking_first_makes_a_bare_word_an_answer():
    """聞いた直後なら、発話まるごとが答え。「長押し」の一言で通る。"""
    assert parse_button_label("長押し") == ""
    assert parse_button_label("長押し", expected=True) == "hold"


# ---------------------------------------------------------------------------
# 会話の中で
# ---------------------------------------------------------------------------


def test_the_question_is_asked_in_japanese():
    """**聞き方が答え方を決める。** 英語で列挙すると英語で返ってくる。"""
    expert = KtaneExpert()
    solution = expert.solve("ボタンの色は白")
    assert "長押し" in solution.needs
    assert "押す" in solution.needs


@pytest.mark.parametrize("answer", ["長押し", "押す", "ホールド", "hold"])
def test_answering_the_question_in_any_wording_solves_it(answer):
    expert = KtaneExpert()
    expert.edge.batteries = 1
    expert.solve("ボタンの色は赤")
    solution = expert.solve(answer)
    assert solution.solved, (answer, solution)


def test_the_label_is_not_taken_before_it_was_asked_for():
    """色も文字も無いうちの「押すね」で決めつけない。"""
    expert = KtaneExpert()
    expert.solve("次はボタンのモジュール")
    expert.solve("ボタンを押すね")
    assert "label" not in expert.slots


def test_a_japanese_label_reaches_the_right_rule():
    """「赤で長押し」は規則5。押しっぱなしではなく、すぐ離す。"""
    expert = KtaneExpert()
    solution = expert.solve("ボタンは赤で、長押しって書いてある")
    assert solution.solved, solution
    assert "すぐ" in solution.answer, solution
