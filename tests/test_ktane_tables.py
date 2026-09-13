"""表が要る3モジュール。**空でも壊れず、埋めれば解ける。**

キーパッド・心理戦・迷路は解法そのものは単純だが、表が大きい。心理戦は28語ぶんの
順序付きリスト、迷路は9面ぶんの通路。これを記憶から書き起こすと、一箇所間違えた
だけで静かに誤った指示を出す。爆弾では誤りが爆発になるので、表は
`manual/tables/*.json` から読み、未取り込みなら答えない。

ここで確かめるのは2つ。**表が無い時に推測しないこと**と、
**表を入れれば正しく解けること**。
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.games.ktane.modules import solve_keypad, solve_maze, solve_whos_on_first
from neuro_voice.games.ktane.parse import parse_coordinates, parse_symbols, parse_words
from neuro_voice.games.ktane.tables import KtaneTables, load_tables
from neuro_voice.games.profiles import default_profile_root

KTANE_FOLDER = default_profile_root() / "keep_talking_and_nobody_explodes"


# ---------------------------------------------------------------------------
# 未取り込みの時は答えない
# ---------------------------------------------------------------------------


def test_keypad_without_a_table_offers_to_learn_it():
    """**突き放さない。** 実機で同じ拒否が6回続いた。

    キーパッドだけは口で教えられるので、断るのではなく手順を言う。
    心理戦と迷路（下）は表が大きすぎるので、従来どおり断る。
    """
    result = solve_keypad(["星", "稲妻", "逆さのC", "丸"], [])
    assert not result.solved
    assert not result.unknown, "諦めさせない"
    assert "読み上げてくれれば覚える" in result.needs


def test_whos_on_first_without_a_table_refuses():
    result = solve_whos_on_first("YES", [], {}, {})
    assert result.unknown and not result.solved


def test_maze_without_a_table_refuses():
    result = solve_maze([(1, 2), (6, 5)], (1, 1), (6, 6), [])
    assert result.unknown and not result.solved


def test_the_shipped_files_are_valid_but_empty():
    """同梱のJSONは書式として正しく、中身は空。読み込みで落ちないこと。"""
    tables = load_tables(KTANE_FOLDER)
    assert tables.errors == {}, f"同梱のJSONが壊れている: {tables.errors}"
    assert set(tables.missing()) == {"キーパッド", "心理戦", "迷路"}


def test_a_broken_file_does_not_take_the_conversation_down(tmp_path):
    """壊れたJSONで会話ごと落とさない（第17条）。理由だけ残す。"""
    root = tmp_path / "manual" / "tables"
    root.mkdir(parents=True)
    (root / "keypad.json").write_text("{ これはJSONではない", encoding="utf-8")
    tables = load_tables(tmp_path)
    assert "keypad" in tables.errors
    assert not tables.has_keypad


# ---------------------------------------------------------------------------
# キーパッド
# ---------------------------------------------------------------------------

COLUMNS = [
    ["丸", "稲妻", "逆さのC", "しっぽ付きの6", "変なA", "三本線", "水滴"],
    ["星", "稲妻", "しっぽ付きの6", "はてな", "三本線", "変なA", "うずまき"],
]


def test_keypad_orders_by_the_column():
    result = solve_keypad(["三本線", "稲妻", "丸"], COLUMNS)
    assert result.answer.startswith("丸、稲妻、三本線")


def test_keypad_asks_when_two_columns_still_fit():
    result = solve_keypad(["稲妻", "三本線"], COLUMNS)
    assert not result.solved
    assert "2本" in result.because


def test_keypad_admits_when_no_column_matches():
    result = solve_keypad(["丸", "星"], COLUMNS)
    assert not result.solved
    assert "読み取りが違う" in result.because


# ---------------------------------------------------------------------------
# 心理戦
# ---------------------------------------------------------------------------

POSITIONS = {"YES": 3, "FIRST": 2, "": 5}
ORDER = {
    "READY": ["YES", "OKAY", "WHAT", "MIDDLE"],
    "NOTHING": ["UHHH", "RIGHT", "OKAY", "MIDDLE"],
}


def test_whos_on_first_gives_the_position_before_the_buttons_are_known():
    result = solve_whos_on_first("YES", [], POSITIONS, ORDER)
    assert "3番目" in result.answer
    assert result.follow_up


def test_whos_on_first_picks_the_first_available_word():
    buttons = ["MIDDLE", "WHAT", "READY", "OKAY", "NOTHING", "UHHH"]
    result = solve_whos_on_first("YES", buttons, POSITIONS, ORDER)
    # 3番目=READY → 候補順 YES/OKAY/WHAT/MIDDLE。盤面にある最初は OKAY
    assert "OKAY" in result.answer


def test_whos_on_first_normalises_the_wording():
    """THEY'RE と THEYRE を別物として扱わない。"""
    positions = {"THEYRE": 1}
    order = {"READY": ["OKAY"]}
    result = solve_whos_on_first("THEY'RE", [], positions, order)
    assert "1番目" in result.answer


def test_whos_on_first_admits_an_unknown_display_word():
    result = solve_whos_on_first("ZZZZ", [], POSITIONS, ORDER)
    assert not result.solved
    assert "表に無い" in result.because


def test_the_blank_display_is_a_real_entry():
    """空欄の画面も表の項目。空文字を『未入力』と混同しない。"""
    result = solve_whos_on_first("", [], POSITIONS, ORDER)
    assert "5番目" in result.answer


# ---------------------------------------------------------------------------
# 迷路
# ---------------------------------------------------------------------------

MAZE = [{
    "name": "テスト用",
    "circles": [[1, 2], [6, 5]],
    # (1,1)→(2,1)→(2,2) だけが通れる細い道
    "passages": [[[1, 1], [2, 1]], [[2, 1], [2, 2]]],
}]


def test_maze_returns_the_shortest_path():
    result = solve_maze([(1, 2), (6, 5)], (1, 1), (2, 2), MAZE)
    assert result.answer.startswith("右、下")


def test_maze_walks_both_directions_even_though_only_one_is_written():
    """通路は片方向だけ書けばよい。逆向きはコードが補う。"""
    result = solve_maze([(1, 2), (6, 5)], (2, 2), (1, 1), MAZE)
    assert result.answer.startswith("上、左")


def test_maze_says_when_there_is_no_route():
    result = solve_maze([(1, 2), (6, 5)], (1, 1), (6, 6), MAZE)
    assert not result.solved
    assert "繋がる道が" in result.because


def test_maze_needs_the_circles_to_choose_the_board():
    result = solve_maze([(1, 2)], (1, 1), (2, 2), MAZE)
    assert not result.solved and "丸2つ" in result.needs


def test_maze_rejects_an_unknown_circle_layout():
    result = solve_maze([(3, 3), (4, 4)], (1, 1), (2, 2), MAZE)
    assert not result.solved
    assert "合う迷路が表に無い" in result.because


def test_maze_notices_when_you_are_already_there():
    assert "出口にいる" in solve_maze([(1, 2), (6, 5)], (2, 2), (2, 2), MAZE).answer


# ---------------------------------------------------------------------------
# 聞き取り
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("記号は 丸、稲妻、三本線、水滴", ["丸", "稲妻", "三本線", "水滴"]),
    ("シンボルは しっぽ付きの6 と 逆さのC", ["しっぽ付きの6", "逆さのC"]),
    ("今日はいい天気だね", []),
])
def test_symbols_are_taken_as_the_user_named_them(text, expected):
    """記号に公式の読み方は無い。利用者が付けた呼び名をそのまま受ける。"""
    assert parse_symbols(text) == expected


def test_button_words_are_picked_up():
    words = parse_words("ボタンは MIDDLE, WHAT, READY, OKAY, NOTHING, UHHH")
    assert "MIDDLE" in words and "UHHH" in words


@pytest.mark.parametrize("text,expected", [
    ("左から2、上から3", [(2, 3)]),
    ("丸は左から1番目の上から2番目と、左から6番目の上から5番目", [(1, 2), (6, 5)]),
    ("2,3", [(2, 3)]),
    ("左から9、上から9", []),          # 6×6の外は捨てる
])
def test_coordinates_are_read_both_ways(text, expected):
    assert parse_coordinates(text) == expected


# ---------------------------------------------------------------------------
# 取り込めば動く
# ---------------------------------------------------------------------------


def test_filling_the_files_makes_the_modules_available(tmp_path):
    root = tmp_path / "manual" / "tables"
    root.mkdir(parents=True)
    (root / "keypad.json").write_text(
        json.dumps({"columns": COLUMNS}, ensure_ascii=False), encoding="utf-8")
    (root / "whos_on_first.json").write_text(json.dumps(
        {"display_to_position": POSITIONS, "label_to_order": ORDER}, ensure_ascii=False,
    ), encoding="utf-8")
    (root / "maze.json").write_text(
        json.dumps({"mazes": MAZE}, ensure_ascii=False), encoding="utf-8")
    tables = load_tables(tmp_path)
    assert tables.missing() == []
    assert tables.has_keypad and tables.has_whos_on_first and tables.has_mazes


def test_partial_data_only_enables_what_was_filled(tmp_path):
    root = tmp_path / "manual" / "tables"
    root.mkdir(parents=True)
    (root / "keypad.json").write_text(
        json.dumps({"columns": COLUMNS}, ensure_ascii=False), encoding="utf-8")
    tables = load_tables(tmp_path)
    assert tables.has_keypad
    assert tables.missing() == ["心理戦", "迷路"]


def test_an_empty_registry_is_not_a_table():
    """列が1本しかない＝書きかけ。中途半端な表で答えない。"""
    assert not KtaneTables(keypad_columns=[["丸"]]).has_keypad
