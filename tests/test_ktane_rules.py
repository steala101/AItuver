"""爆弾解除の規則。**間違えたら爆発するので、分岐を全部踏む。**

ポッポは「マニュアル担当」を名乗っていたのに、プロファイルに入っていたのは
役割の説明と版番号と検証コードだけで、規則が1行も無かった。だから何を
説明されても指示が出せなかった。

判断をコードへ置いた以上、正しさの保証はここにある。各モジュールの分岐を
条件ごとに踏み、**規則が無いモジュールを勝手に答えないこと**も確かめる。
間違った指示は「分からない」よりはるかに悪い。
"""
from __future__ import annotations

import pytest

from neuro_voice.games.ktane.edgework import Edgework
from neuro_voice.games.ktane.modules import (
    MemoryPress, decode_morse, release_on, solve_button, solve_complicated_wire,
    solve_memory, solve_morse, solve_password, solve_simon, solve_wire_sequence,
    solve_wires, unknown_module,
)


def edge(serial="A7B3C4", batteries=2, lit=(), ports=(), strikes=0):
    return Edgework(
        serial=serial, batteries=batteries, lit_indicators=set(lit),
        ports=set(ports), strikes=strikes,
    )


# ---------------------------------------------------------------------------
# 配線
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("colors,expected", [
    # 3本
    (["blue", "white", "black"], "2本目"),          # 赤が無い
    (["red", "blue", "white"], "3本目"),            # 最後が白
    (["blue", "blue", "red"], "2本目"),             # 青が2本以上 → 最後の青
    (["red", "black", "black"], "3本目"),           # どれにも当たらない
])
def test_three_wires(colors, expected):
    assert expected in solve_wires(colors, edge()).answer


def test_four_wires_uses_the_serial_when_red_repeats():
    colors = ["red", "blue", "red", "black"]
    assert "3本目" in solve_wires(colors, edge(serial="A1B2C3")).answer   # 末尾3=奇数
    # 末尾が偶数なら次の規則へ落ちる（青がちょうど1本 → 1本目）
    assert "1本目" in solve_wires(colors, edge(serial="A1B2C4")).answer


@pytest.mark.parametrize("colors,expected", [
    (["blue", "black", "black", "yellow"], "1本目"),   # 最後が黄で赤が無い
    (["black", "blue", "black", "black"], "1本目"),    # 青がちょうど1本
    (["yellow", "black", "yellow", "black"], "4本目"),  # 黄が2本以上
    (["black", "black", "black", "black"], "2本目"),   # どれにも当たらない
])
def test_four_wires(colors, expected):
    assert expected in solve_wires(colors, edge(serial="A1B2C4")).answer


@pytest.mark.parametrize("colors,serial,expected", [
    (["red", "red", "red", "red", "black"], "A1B2C3", "4本目"),   # 最後が黒・奇数
    (["red", "yellow", "yellow", "blue", "white"], "A1B2C4", "1本目"),  # 赤1・黄2以上
    (["blue", "blue", "white", "white", "red"], "A1B2C4", "2本目"),     # 黒が無い
    (["black", "black", "red", "red", "white"], "A1B2C4", "1本目"),     # その他
])
def test_five_wires(colors, serial, expected):
    assert expected in solve_wires(colors, edge(serial=serial)).answer


@pytest.mark.parametrize("colors,serial,expected", [
    (["red", "blue", "black", "white", "red", "blue"], "A1B2C3", "3本目"),  # 黄無し・奇数
    (["yellow", "white", "white", "red", "black", "blue"], "A1B2C4", "4本目"),  # 黄1・白2以上
    (["blue", "black", "white", "yellow", "yellow", "blue"], "A1B2C4", "6本目"),  # 赤無し
    (["red", "yellow", "yellow", "black", "blue", "white"], "A1B2C4", "4本目"),  # その他
])
def test_six_wires(colors, serial, expected):
    assert expected in solve_wires(colors, edge(serial=serial)).answer


def test_wires_asks_for_the_serial_instead_of_guessing():
    """シリアルを知らないまま偶奇を決めつけない。"""
    result = solve_wires(["red", "blue", "red", "black"], edge(serial=""))
    assert not result.solved
    assert "シリアル" in result.needs


def test_wires_rejects_an_impossible_count():
    assert not solve_wires(["red", "blue"], edge()).solved


# ---------------------------------------------------------------------------
# ボタン
# ---------------------------------------------------------------------------


def test_button_blue_abort_is_held():
    assert "押しっぱなし" in solve_button("blue", "Abort", edge()).answer


def test_button_detonate_with_batteries_is_tapped():
    assert "すぐ離して" in solve_button("red", "Detonate", edge(batteries=2)).answer


def test_button_white_with_car_lit_is_held():
    assert "押しっぱなし" in solve_button("white", "Press", edge(lit=["CAR"])).answer


def test_button_frk_with_three_batteries_is_tapped():
    assert "すぐ離して" in solve_button("yellow", "Press", edge(batteries=3, lit=["FRK"])).answer


def test_button_red_hold_is_tapped():
    assert "すぐ離して" in solve_button("red", "Hold", edge(batteries=1)).answer


def test_button_falls_through_to_hold():
    assert "押しっぱなし" in solve_button("yellow", "Press", edge(batteries=1, lit=["SND"])).answer


def test_button_asks_for_batteries_rather_than_assuming():
    result = solve_button("red", "Detonate", edge(batteries=None))
    assert not result.solved and "電池" in result.needs


@pytest.mark.parametrize("strip,digit", [
    ("blue", "4"), ("white", "1"), ("yellow", "5"), ("red", "1"),
])
def test_the_release_digit_comes_from_the_strip(strip, digit):
    assert digit in release_on(strip).answer


# ---------------------------------------------------------------------------
# サイモン
# ---------------------------------------------------------------------------


def test_simon_with_a_vowel_and_no_strikes():
    result = solve_simon(["red", "blue"], edge(serial="A1B2C3", strikes=0))
    assert result.answer.startswith("青、赤")


def test_simon_without_a_vowel_changes_the_table():
    result = solve_simon(["red", "blue"], edge(serial="X1Y2Z3", strikes=0))
    assert result.answer.startswith("青、黄")


def test_simon_uses_the_strike_count():
    a = solve_simon(["red"], edge(serial="A1B2C3", strikes=0)).answer
    b = solve_simon(["red"], edge(serial="A1B2C3", strikes=1)).answer
    c = solve_simon(["red"], edge(serial="A1B2C3", strikes=2)).answer
    assert len({a, b, c}) == 3, "ミスの数で押す色が変わる"


def test_simon_beyond_two_strikes_uses_the_last_row():
    """3回ミスすると爆発するので、表は2回までしかない。落ちないこと。"""
    assert solve_simon(["red"], edge(serial="A1B2C3", strikes=5)).solved


def test_simon_asks_for_the_serial():
    result = solve_simon(["red"], edge(serial=""))
    assert not result.solved and "シリアル" in result.needs


# ---------------------------------------------------------------------------
# 記憶
# ---------------------------------------------------------------------------


def test_memory_first_stage_is_positional():
    assert "2番目" in solve_memory(1, 1, []).answer
    assert "4番目" in solve_memory(1, 4, []).answer


def test_memory_later_stages_need_the_history():
    history = [MemoryPress(position=3, label=2)]
    assert "3番目" in solve_memory(2, 2, history).answer
    assert "「2」" in solve_memory(3, 2, history).answer


def test_memory_says_what_it_is_missing():
    result = solve_memory(3, 1, [])
    assert not result.solved
    assert "2段目" in result.needs


def test_memory_fifth_stage_reads_back_a_label():
    history = [
        MemoryPress(1, 4), MemoryPress(2, 1), MemoryPress(3, 3), MemoryPress(4, 2),
    ]
    assert "「2」" in solve_memory(5, 3, history).answer   # 4段目の数字


# ---------------------------------------------------------------------------
# モールス信号
# ---------------------------------------------------------------------------


def test_morse_decodes_letters():
    assert decode_morse("... .... . .-.. .-..") == "shell"


def test_morse_rejects_an_unreadable_symbol():
    assert decode_morse("... ....... .") == ""


def test_morse_answers_once_one_word_is_left():
    assert "3.505" in solve_morse("shel").answer


def test_morse_asks_for_one_more_letter_while_ambiguous():
    result = solve_morse("b")
    assert not result.solved
    assert "次の1文字" in result.needs


def test_morse_admits_when_nothing_matches():
    result = solve_morse("zzz")
    assert not result.solved
    assert "一覧に無い" in result.because


# ---------------------------------------------------------------------------
# 複雑な配線
# ---------------------------------------------------------------------------


def test_complicated_plain_wire_is_cut():
    assert solve_complicated_wire(
        red=False, blue=False, star=False, led=False, edge=edge(),
    ).answer == "切って"


def test_complicated_blue_with_star_is_never_cut():
    assert solve_complicated_wire(
        red=False, blue=True, star=True, led=False, edge=edge(),
    ).answer == "切らないで"


def test_complicated_red_depends_on_the_serial():
    assert "切って" == solve_complicated_wire(
        red=True, blue=False, star=False, led=False, edge=edge(serial="A1B2C4"),
    ).answer
    assert "切らないで" == solve_complicated_wire(
        red=True, blue=False, star=False, led=False, edge=edge(serial="A1B2C3"),
    ).answer


def test_complicated_blue_led_depends_on_the_parallel_port():
    assert "切って" == solve_complicated_wire(
        red=False, blue=True, star=False, led=True, edge=edge(ports=["Parallel"]),
    ).answer
    assert "切らないで" == solve_complicated_wire(
        red=False, blue=True, star=False, led=True, edge=edge(ports=["Serial"]),
    ).answer


def test_complicated_red_led_depends_on_the_batteries():
    assert "切って" == solve_complicated_wire(
        red=True, blue=False, star=False, led=True, edge=edge(batteries=2),
    ).answer
    assert "切らないで" == solve_complicated_wire(
        red=True, blue=False, star=False, led=True, edge=edge(batteries=1),
    ).answer


def test_complicated_asks_before_assuming_ports():
    result = solve_complicated_wire(
        red=False, blue=True, star=False, led=True, edge=edge(ports=()),
    )
    assert not result.solved and "ポート" in result.needs


def test_every_complicated_combination_is_covered():
    """16通りすべてが答えか質問になること。落ちる組み合わせを残さない。"""
    for red in (True, False):
        for blue in (True, False):
            for star in (True, False):
                for led in (True, False):
                    result = solve_complicated_wire(
                        red=red, blue=blue, star=star, led=led,
                        edge=edge(serial="A1B2C4", batteries=2, ports=["Parallel"]),
                    )
                    assert result.solved, (red, blue, star, led)


# ---------------------------------------------------------------------------
# 順番に配線
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("color,occurrence,target,cut", [
    ("red", 1, "C", True), ("red", 1, "A", False),
    ("blue", 2, "A", True), ("blue", 2, "B", False),
    ("black", 1, "B", True), ("black", 8, "C", True), ("black", 8, "A", False),
])
def test_wire_sequences(color, occurrence, target, cut):
    result = solve_wire_sequence(color, occurrence, target)
    assert result.answer == ("切って" if cut else "切らないで")


def test_wire_sequence_rejects_an_unknown_colour():
    assert not solve_wire_sequence("white", 1, "A").solved


# ---------------------------------------------------------------------------
# パスワード
# ---------------------------------------------------------------------------


def test_password_narrows_to_one_word():
    result = solve_password([["a"], ["b"], ["o"], ["u"], ["t"]])
    assert "about" in result.answer


def test_password_asks_for_the_next_column_while_ambiguous():
    result = solve_password([["t"], ["h"]])
    assert not result.solved
    assert "3列目" in result.needs


def test_password_admits_an_impossible_combination():
    result = solve_password([["z"], ["z"]])
    assert not result.solved
    assert "一覧に無い" in result.because


# ---------------------------------------------------------------------------
# 実装していないモジュール
# ---------------------------------------------------------------------------


def test_a_missing_rule_is_admitted_not_invented():
    """キーパッド・心理戦・迷路は規則を持っていない。推測で答えない。"""
    result = unknown_module("迷路")
    assert result.unknown
    assert not result.solved
    assert "手元のマニュアルに入っていない" in result.because
