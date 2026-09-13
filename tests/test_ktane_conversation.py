"""話し言葉から解けること。そして解けない時に作り話をしないこと。

音声で伝わるので書式は期待できない。「赤、青、赤の3本」「シリアルはA7B3C1」
「電池2個」——どれも1発話で来るとは限らず、順番もばらばらである。

**答えが確定した時はLLMを通さない。** 12Bのモデルに多条件の分岐を解かせると
間違えるし、爆弾では間違いが爆発になる。ここで検証しているのは、
`ActivityOutcome.reply` として確定した指示がそのまま返ることである。
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.games.ktane import KtaneExpert
from neuro_voice.games.ktane.parse import (
    parse_colors, parse_module, parse_ports, parse_serial,
)
from neuro_voice.games.session import GameProfileSessionManager

KTANE = "keep_talking_and_nobody_explodes"


class FakeConfig:
    def __init__(self, values):
        self._values = dict(values)
        self.persisted = []

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def persist(self, key, value):
        self._values[key] = value
        self.persisted.append((key, value))
        return True


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "game_profiles"
    (root / KTANE).mkdir(parents=True)
    (root / KTANE / "profile.json").write_text(json.dumps({
        "id": KTANE, "display_name": "Keep Talking and Nobody Explodes",
        "display_name_ja": "完全爆弾解除マニュアル",
        "aliases": ["ktane", "爆弾解除"],
        "assistant_role": "マニュアル担当", "interaction_mode": "manual_expert",
        "vision_policy": "manual_expert_no_bomb_view",
        "conversation_instruction": "画面は見ない",
        "manual": {"version": "1-ja", "verification_code": "122"},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = FakeConfig({"game_profiles.root": str(root), "video.game_profile": KTANE})
    manager = GameProfileSessionManager(cfg, tmp_path / "state.json")
    manager.handle_final_input("爆弾解除を始めよう", actor_id="a", source="local")
    return manager


def say(manager, text):
    return manager.handle_final_input(text, actor_id="a", source="local")


# ---------------------------------------------------------------------------
# 聞き取り
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("赤、青、赤の3本だよ", ["red", "blue", "red"]),
    ("上から白、黒、白、黄色", ["white", "black", "white", "yellow"]),
    ("あか あお きいろ", ["red", "blue", "yellow"]),
    ("レッド、ブルー、ブラック", ["red", "blue", "black"]),
])
def test_colours_are_heard_in_order(text, expected):
    assert parse_colors(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("シリアルはA7B3C1", "A7B3C1"),
    ("しりあるは えー ぜろ ...", ""),          # 読めない断片は捨てる
    ("シリアル番号 XY 4 Z 89", "XY4Z89"),
])
def test_the_serial_is_only_taken_when_it_looks_like_one(text, expected):
    assert parse_serial(text) == expected


def test_ports_are_recognised_by_their_japanese_names():
    assert "Parallel" in parse_ports("パラレルポートがあるよ")
    assert "Parallel" in parse_ports("並列ポートが1個")


@pytest.mark.parametrize("text,module", [
    ("配線のモジュールから", "wires"),
    ("複雑な配線なんだけど", "complicated_wires"),      # 「配線」より先に当たること
    ("順番に配線するやつ", "wire_sequences"),
    ("でかいボタンがある", "button"),
    ("サイモンが光ってる", "simon"),
    ("モールス信号のやつ", "morse"),
    ("迷路のモジュール", "maze"),
])
def test_the_module_is_identified(text, module):
    assert parse_module(text) == module


# ---------------------------------------------------------------------------
# 会話の流れ
# ---------------------------------------------------------------------------


def test_a_solvable_wire_module_goes_to_poppo_with_the_answer_held(manager):
    """答えが出せるターンは、固定文ではなくポッポに喋らせる。

    コードの答えは `pending_check` として抱えておき、発話の前に突き合わせる
    （`tests/test_ktane_verify.py`）。以前はここで固定文を返しており、
    ポッポのLLMを一度も通っていなかった。
    """
    say(manager, "シリアルはA7B3C1だよ")
    outcome = say(manager, "配線が3本、上から青、白、黒")
    assert not outcome.handled
    assert manager.check_pending()
    assert "2本目" in manager.expert.pending_check.answer   # 赤が無い → 2本目
    assert "赤が無い" in manager.expert.pending_check.because


def test_the_bomb_facts_are_remembered_across_turns(manager):
    say(manager, "シリアルはA1B2C3、電池は2個")
    say(manager, "配線4本、赤、青、赤、黒")
    assert manager.check_pending()
    answer = manager.expert.pending_check.answer
    assert "3本目" in answer, "前のターンで聞いたシリアルを使うこと"


def test_a_missing_fact_is_asked_for_instead_of_assumed(manager):
    """シリアルを知らないまま偶奇を決めつけない。"""
    outcome = say(manager, "配線4本、赤、青、赤、黒")
    assert outcome.reason == "ktane_needs_more"
    assert "シリアル" in outcome.reply


def test_a_module_without_its_table_is_refused_up_front(manager):
    """座標を聞き出してから「やっぱり分からない」と言わない。

    迷路の解法はコードにあるが、9面ぶんの通路は `manual/tables/maze.json` から
    読む。同梱のファイルは空なので、名前が出た時点で断る。
    """
    outcome = say(manager, "迷路のモジュールなんだけど")
    assert outcome.reason == "ktane_rule_missing"
    assert "取り込んでいない" in outcome.reply
    assert "切" not in outcome.reply, "表が無いのに操作を指示しない"


def test_edgework_alone_is_acknowledged(manager):
    outcome = say(manager, "電池は3個あるよ")
    assert outcome.reason == "ktane_edgework_recorded"
    assert "電池" in outcome.reply


def test_small_talk_during_a_session_is_left_to_the_conversation(manager):
    """雑談まで規則で握らない。"""
    outcome = say(manager, "うわー緊張してきた")
    assert outcome.reason != "ktane_solved"
    assert not outcome.reply or "切" not in outcome.reply


def test_a_new_session_forgets_the_previous_bomb(manager):
    say(manager, "シリアルはA1B2C3、電池は2個")
    assert manager.expert.edge.serial == "A1B2C3"
    say(manager, "もうやめよう")
    say(manager, "爆弾解除を始めよう")
    assert manager.expert.edge.serial == "", "前の爆弾のシリアルを持ち越さない"
    assert manager.expert.edge.batteries is None


# ---------------------------------------------------------------------------
# プロンプトへ渡す文脈
# ---------------------------------------------------------------------------


def test_the_context_lists_which_modules_have_rules(manager):
    context = manager.grounded_context()
    assert "配線" in context and "パスワード" in context
    assert "表が未取り込み" in context
    assert "迷路" in context and "思いつきで指示しない" in context


def test_the_context_shows_what_is_still_unknown(manager):
    assert "未確認" in manager.grounded_context()
    say(manager, "シリアルはA1B2C3")
    assert "A1B2C3" in manager.grounded_context()


def test_the_context_tells_poppo_to_read_the_rules_herself(manager):
    """規則を渡して自分で選ばせる。答えを渡して読み上げさせるのではない。"""
    context = manager.grounded_context()
    assert "自分で読んで" in context
    assert "断定せず一つだけ尋ねる" in context


# ---------------------------------------------------------------------------
# 段階のあるモジュール
# ---------------------------------------------------------------------------


def test_memory_history_is_kept_by_the_expert():
    expert = KtaneExpert()
    expert.record_memory_press(position=3, label=2)
    solution = expert.solve("記憶モジュール、2段目、画面は2")
    assert "3番目" in solution.answer


def test_morse_letters_accumulate_across_turns():
    expert = KtaneExpert()
    assert not expert.solve("モールスで b が読めた").solved
    assert "3.565" in expert.solve("モールス bombs まで読めた").answer


# ---------------------------------------------------------------------------
# 記憶モジュール: 押した結果を受け取る
#
# 4段目以降は「1段目でどこを押したか」「2段目で何が書いてあったか」の両方が要る。
# 指示した側は片方を知っているので、聞き返すのは片方だけでよい。
# ---------------------------------------------------------------------------


def test_the_position_is_known_so_only_the_label_is_asked():
    expert = KtaneExpert()
    first = expert.solve("記憶モジュール、画面は3")
    assert "3番目" in first.answer
    assert "何て書いてあった" in first.follow_up


def test_reporting_the_label_completes_the_press():
    expert = KtaneExpert()
    expert.solve("記憶モジュール、画面は3")        # 左から3番目を押して
    outcome = expert.solve("2だった")
    assert "3番目" in outcome.answer and "「2」" in outcome.answer
    assert expert.memory_history[0].position == 3
    assert expert.memory_history[0].label == 2


def test_the_label_is_known_so_the_position_is_asked():
    expert = KtaneExpert()
    first = expert.solve("記憶の2段目、画面は1")
    assert "「4」" in first.answer
    assert "何番目" in first.follow_up
    expert.solve("左から2番目")
    assert expert.memory_history[-1].position == 2
    assert expert.memory_history[-1].label == 4


def test_a_full_five_stage_run_works_from_speech_alone():
    """1段目から5段目まで、押下結果の申告だけで通ること。"""
    expert = KtaneExpert()
    for display, reported in ((1, 3), (3, 1), (3, 2), (2, 4)):
        expert.solve(f"記憶モジュール、画面は{display}")
        expert.solve(f"{reported}だった")
    assert len(expert.memory_history) == 4
    final = expert.solve("記憶モジュール、画面は1")
    assert final.solved, "5段目まで履歴がそろえば答えが出る"


def test_the_next_stage_report_is_not_mistaken_for_a_press_result():
    """「画面は2」を押下結果として食べない。"""
    expert = KtaneExpert()
    expert.solve("記憶モジュール、画面は3")
    outcome = expert.solve("次は画面が2")
    assert not expert.memory_history, "画面の申告を押下結果と取り違えない"
    assert outcome is not None and outcome.solved


def test_an_unrelated_number_during_memory_is_ignored():
    expert = KtaneExpert()
    expert.solve("記憶モジュール、画面は3")
    expert.solve("記憶モジュール、9個くらいありそう")
    assert not expert.memory_history, "1〜4以外は押下結果にしない"


# ---------------------------------------------------------------------------
# 同じ質問を返し続けない
#
# 実機で「ボタンの色と、書いてある文字を教えて。」ばかり返る状態になった。
# `current_module` は言い直すまで残るので、一度「ボタン」と言われると以後の
# 発話がすべてボタンの判定へ入り、そこが必ず質問を返していたため。
# ---------------------------------------------------------------------------


def test_a_button_turn_without_any_clue_is_left_to_the_conversation(manager):
    say(manager, "でかいボタンがあるよ")
    outcome = say(manager, "シリアルはA1B2C3")
    assert outcome.reason == "ktane_edgework_recorded", "色も文字も無い発話に質問を返さない"
    assert "ボタンの色" not in (outcome.reply or "")


def test_the_same_question_is_not_asked_twice_in_a_row(manager):
    first = say(manager, "青いボタンなんだけど")
    assert first.reason == "ktane_needs_more"
    second = say(manager, "うーん、なんだっけ")
    assert second.reason != "ktane_needs_more", "同じ一文を返し続けない"


def test_only_the_missing_half_is_asked_for(manager):
    outcome = say(manager, "青いボタンなんだけど")
    assert "書いてある文字" in outcome.reply
    assert "ボタンの色と" not in outcome.reply, "分かっている方まで聞き直さない"


def test_pressing_as_a_verb_is_not_read_as_the_label(manager):
    """「ボタンを押すね」の『押す』を Press の文字と取り違えない。

    取り違えると、条件を1つ飛ばして間違った指示を確定させる。
    """
    outcome = say(manager, "青いボタンを押すね")
    assert outcome.reason != "ktane_solved"
    assert "書いてある文字" in (outcome.reply or "")


def test_the_button_is_solved_once_both_halves_are_known(manager):
    say(manager, "電池は2個")
    outcome = say(manager, "青いボタンで、Abortって書いてある")
    assert not outcome.handled, "答えが出たターンはポッポが喋る"
    assert "押しっぱなし" in manager.expert.pending_check.answer
