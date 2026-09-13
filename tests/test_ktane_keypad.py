"""キーパッドの記号を、声で言える名前で扱う。

実機で「ごめん、キーパッドの表をまだ取り込んでいない」が**6回続けて**
返っていた。足りなかったのは表だけではなく、**記号を言葉で受け取る
仕組み**そのものだった。

キーパッドの記号には公式の読み方が無い。プレイヤーはその場で呼び名を
作るので、こちらが正解の名前を持てない。だから「利用者の呼び名を受け取り、
ゆるく照合し、紛らわしければ聞き返す」が要る。
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.games.ktane import KtaneExpert
from neuro_voice.games.ktane.symbols import (
    known_symbols, match_symbol, normalize, resolve_symbols,
)
from neuro_voice.games.ktane.tables import KtaneTables

COLUMNS = [
    ["丸に線", "稲妻", "逆さのC", "しっぽ付きの6", "変なA", "三本線", "水滴"],
    ["星", "稲妻", "しっぽ付きの6", "はてな", "三本線", "変なA", "うずまき"],
]


# ---------------------------------------------------------------------------
# 呼び名のゆらぎ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spoken,expected", [
    ("しっぽ付きの6", "しっぽ付き6"),
    ("しっぽ付きの6みたいなやつ", "しっぽ付き6"),
    ("逆さのＣ", "逆さc"),
    ("  三 本 線  ", "三本線"),
])
def test_decoration_is_stripped_but_the_word_is_kept(spoken, expected):
    assert normalize(spoken) == expected


@pytest.mark.parametrize("spoken", [
    # 語順まで入れ替わった言い方（「6にしっぽ」）は**狙わない**。
    # 拾おうと閾値を緩めると、別の記号を掴んで即ミスになる。
    "しっぽ付きの6", "しっぽ付き6", "しっぽ付きの6みたいなやつ",
])
def test_the_same_symbol_survives_different_wordings(spoken):
    match = match_symbol(spoken, known_symbols(COLUMNS))
    assert match is not None and match.name == "しっぽ付きの6", spoken


def test_an_unknown_nickname_is_not_forced_onto_something():
    """知らない呼び名を一番近いものへ寄せない。押すボタンが変わる。"""
    assert match_symbol("台形みたいな図形", known_symbols(COLUMNS)) is None


def test_two_close_candidates_are_reported_as_ambiguous():
    """似た呼び名が2つあるなら、勝手に選ばず聞き返す。"""
    match = match_symbol("変な", ["変なA", "変なB"])
    assert match is not None and match.ambiguous


def test_the_three_outcomes_are_separated():
    """返す言葉が変わるので、分からないと紛らわしいを混ぜない。"""
    resolved, unknown, ambiguous = resolve_symbols(
        ["稲妻", "台形みたいな図形"], known_symbols(COLUMNS),
    )
    assert resolved == ["稲妻"]
    assert unknown == ["台形みたいな図形"]
    assert ambiguous == []


# ---------------------------------------------------------------------------
# 会話の中で
# ---------------------------------------------------------------------------


def expert_with_table() -> KtaneExpert:
    return KtaneExpert(tables=KtaneTables(keypad_columns=[list(c) for c in COLUMNS]))


def test_symbols_accumulate_across_turns():
    """4つを1発話で言い切れるとは限らない。"""
    expert = expert_with_table()
    expert.solve("キーパッドの記号は 丸に線、稲妻")
    solution = expert.solve("記号は 三本線")
    assert solution is not None
    assert "丸に線" in solution.answer, solution


def test_an_unknown_nickname_asks_for_features_not_for_the_table():
    expert = expert_with_table()
    solution = expert.solve("記号は 台形みたいな図形")
    assert not solution.solved
    assert "どれか分からない" in solution.because
    assert "どんな形か" in solution.needs


def test_the_table_can_be_taught_by_voice():
    """**JSONを手で書かなくても、読み上げれば覚える。**"""
    expert = KtaneExpert()
    solution = expert.solve("キーパッドの1列目は 丸に線、稲妻、逆さのC、しっぽ付きの6")
    assert solution.solved
    assert "1列目を覚えた" in solution.answer
    assert expert.tables.keypad_columns[0][:2] == ["丸に線", "稲妻"]


def test_teaching_reports_how_far_it_has_got():
    expert = KtaneExpert()
    expert.solve("キーパッドの1列目は 丸に線、稲妻、逆さのC")
    solution = expert.solve("キーパッドの2列目は 星、稲妻、はてな")
    assert "2/6列" in solution.because
    assert "次の列" in solution.follow_up


def test_an_empty_table_explains_how_to_fill_it_instead_of_refusing():
    """突き放して6回同じことを言うのをやめる。"""
    expert = KtaneExpert()
    solution = expert.solve("キーパッドの記号は 丸に線、稲妻")
    assert not solution.solved
    assert "読み上げてくれれば覚える" in solution.needs


def test_a_taught_table_then_solves():
    expert = KtaneExpert()
    expert.solve("キーパッドの1列目は 丸に線、稲妻、逆さのC、しっぽ付きの6、変なA")
    solution = expert.solve("記号は 稲妻、丸に線、変なA")
    assert solution.solved
    assert solution.answer.startswith("丸に線、稲妻、変なA")


def test_switching_module_forgets_the_symbols():
    """前のモジュールで聞いた記号を持ち越さない。"""
    expert = expert_with_table()
    expert.solve("キーパッドの記号は 丸に線")
    expert.solve("次は配線のモジュール")
    assert not expert.slots


# ---------------------------------------------------------------------------
# 同じ質問を繰り返していた件（実機のログ）
# ---------------------------------------------------------------------------


def test_the_button_remembers_what_it_already_heard():
    """「色は白」の次のターンでまた色を聞かない。"""
    expert = KtaneExpert()
    expert.edge.batteries = 1
    first = expert.solve("ボタンの色は赤")
    assert "文字" in first.needs
    second = expert.solve("ホールドって書いてある")
    assert second.solved, second


def test_wire_colours_accumulate_when_given_in_pieces():
    """「赤と青」「あと赤と黒」と分けて言われても組み立てる。"""
    expert = KtaneExpert()
    expert.edge.serial = "A1B2C4"
    expert.solve("配線は4本")
    expert.solve("赤と青")
    solution = expert.solve("あと赤と黒")
    assert solution.solved, solution


def test_the_remaining_count_is_asked_for_not_the_whole_list_again():
    expert = KtaneExpert()
    expert.solve("配線は4本")
    solution = expert.solve("赤と青")
    assert "残り2本" in solution.needs


# ---------------------------------------------------------------------------
# 決まった呼び名を要求しない
#
# 「毎回同じ言い方をして」は、こちらの都合でしかない。人が変われば、
# その日の気分でも言い方は変わる。**形の特徴**で寄せる。
# ---------------------------------------------------------------------------

from neuro_voice.games.ktane.symbols import (  # noqa: E402
    SymbolVocabulary, features,
)


@pytest.mark.parametrize("spoken", [
    "丸に線", "円の中に横棒", "まるにぼう", "丸くて線が入ってるやつ",
])
def test_the_same_shape_is_found_from_different_descriptions(spoken):
    """文字列は全部違うが、形は同じ。"""
    match = match_symbol(spoken, known_symbols(COLUMNS))
    assert match is not None and match.name == "丸に線", spoken


@pytest.mark.parametrize("spoken,expected", [
    ("ぎざぎざの線", "稲妻"),
    ("雷みたいなの", "稲妻"),
    ("Cが裏返ってる", "逆さのC"),
    ("反対向きのシー", "逆さのC"),
    ("しずくみたいな形", "水滴"),
    ("涙のかたち", "水滴"),
])
def test_free_descriptions_reach_the_right_symbol(spoken, expected):
    match = match_symbol(spoken, known_symbols(COLUMNS))
    assert match is not None and match.name == expected, (spoken, match)


def test_features_ignore_words_it_does_not_know():
    """知らない言葉で勝手に近い形へ寄せない。"""
    assert features("なんかすごい形") == set()


def test_a_description_with_no_known_feature_is_still_refused():
    assert match_symbol("なんかすごい形", known_symbols(COLUMNS)) is None


# ---------------------------------------------------------------------------
# 言われ方を覚える
# ---------------------------------------------------------------------------


def test_a_learned_wording_works_next_time():
    vocabulary = SymbolVocabulary()
    assert match_symbol("ポッポ印", known_symbols(COLUMNS)) is None
    vocabulary.learn("稲妻", "ポッポ印")
    match = match_symbol(
        "ポッポ印", known_symbols(COLUMNS), aliases=vocabulary.table(),
    )
    assert match is not None and match.name == "稲妻"


def test_the_same_wording_is_not_stored_twice():
    vocabulary = SymbolVocabulary()
    assert vocabulary.learn("稲妻", "ポッポ印")
    assert not vocabulary.learn("稲妻", "ポッポ印")


@pytest.mark.parametrize("spoken", ["稲妻", "いなずま", "雷"])
def test_a_wording_that_already_matches_is_not_stored(spoken):
    """既に特徴で照合できるものを溜めない。表が膨らむだけ。"""
    assert not SymbolVocabulary().learn("稲妻", spoken)


def test_asking_back_then_being_told_teaches_the_wording():
    """聞き返して分かったら、その言い方を次から使う。"""
    expert = expert_with_table()
    # 形の言葉が1つも入っていない呼び名。これは寄せようがない。
    asked = expert.solve("記号は ポッポ印")
    assert not asked.solved
    assert "どれか分からない" in asked.because
    expert.solve("記号は うずまき")           # 答えてもらう
    assert "ポッポ印" in expert.vocabulary.for_symbol("うずまき")
