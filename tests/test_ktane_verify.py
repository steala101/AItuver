"""ポッポが自分で考え、コードが検算する。

これまでは判断をすべてコードが決め、確定した一文をそのまま返していた。
正しいが、**ポッポのLLMを一度も通っていなかった**——声だけポッポで、
言葉は表引きの結果だった。

いまは規則をプロンプトへ渡してポッポが自分で考える。その答えを発話の前に
突き合わせ、食い違ったら差し替える（第2条）。

ここで一番気をつけるのは3つ目の状態である。**結論を取り出せない時に
否定しない。** 読めなかっただけで正しいかもしれないのに差し替えるのは、
それ自体が誤りになる。
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.games.ktane.modules import Solution
from neuro_voice.games.ktane.rulebook import RULE_TEXT, rule_text
from neuro_voice.games.ktane.verify import AGREE, DISAGREE, UNCLEAR, check, correction
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
        "display_name_ja": "完全爆弾解除マニュアル", "aliases": ["ktane", "爆弾解除"],
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
# 一致・不一致・判定不能
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer,reply,verdict", [
    # 配線
    ("2本目を切って", "上から2本目、そこを切って", AGREE),
    ("2本目を切って", "3本目を切ればいいと思う", DISAGREE),
    ("2本目を切って", "うーん、たぶん切れば大丈夫だよ", UNCLEAR),
    # 切る / 切らない
    ("切って", "それは切っていいよ", AGREE),
    ("切って", "それは切らないで", DISAGREE),
    ("切らないで", "そこは切らずに残して", AGREE),
    # ボタン
    ("押しっぱなしにして。帯の色を教えて", "長押しして。帯の色は？", AGREE),
    ("押しっぱなしにして。帯の色を教えて", "すぐ押してすぐ離して", DISAGREE),
    ("すぐ押して、すぐ離して", "ぱっと押してすぐ離すの", AGREE),
    # サイモン
    ("青、赤 の順に押して", "青、赤の順ね", AGREE),
    ("青、赤 の順に押して", "赤、青の順で", DISAGREE),
    # 記憶
    ("左から3番目のボタンを押して", "左から3番目だよ", AGREE),
    ("左から3番目のボタンを押して", "左から1番目を押して", DISAGREE),
    ("「4」と書いてあるボタンを押して", "「4」って書いてあるやつ", AGREE),
    ("「4」と書いてあるボタンを押して", "「2」のボタンね", DISAGREE),
    # モールス
    ("周波数を 3.505 に合わせて送信して", "3.505に合わせて", AGREE),
    ("周波数を 3.505 に合わせて送信して", "3.515だね", DISAGREE),
    # パスワード
    ("「about」で送信して", "「about」で送信", AGREE),
    ("「about」で送信して", "「after」かな", DISAGREE),
])
def test_the_verdict(answer, reply, verdict):
    assert check(reply, Solution(answer=answer)) == verdict


def test_two_different_numbers_cannot_be_judged():
    """「2本目か3本目」と迷っている発話を、どちらとも決めつけない。"""
    assert check("2本目か3本目だと思う", Solution(answer="2本目を切って")) == UNCLEAR


def test_saying_both_cut_and_keep_cannot_be_judged():
    assert check("切ってもいいけど切らない方がいいかも", Solution(answer="切って")) == UNCLEAR


def test_an_empty_reply_is_not_a_disagreement():
    assert check("", Solution(answer="2本目を切って")) == UNCLEAR


def test_nothing_to_compare_against_is_unclear():
    assert check("2本目を切って", Solution()) == UNCLEAR


# ---------------------------------------------------------------------------
# 差し替える文
# ---------------------------------------------------------------------------


def test_the_correction_carries_the_answer_and_the_reason():
    text = correction(Solution(answer="2本目を切って", because="3本で赤が無いとき"))
    assert "2本目" in text and "3本で赤が無いとき" in text


def test_the_correction_reads_as_poppo_catching_herself():
    """訂正であることが分かる言い方にする。黙って別のことを言わない。"""
    assert correction(Solution(answer="切って")).startswith("んー")


# ---------------------------------------------------------------------------
# 会話の中での動き
# ---------------------------------------------------------------------------


def test_a_solvable_turn_is_left_to_poppo(manager):
    """答えが出せるターンでも、固定文を返さずポッポに喋らせる。"""
    say(manager, "シリアルはA7B3C1")
    outcome = say(manager, "配線が3本、上から青、白、黒")
    assert not outcome.handled, "コードが答えを喋ってしまわない"
    assert manager.check_pending(), "検算する答えは抱えている"


def test_the_rules_are_handed_to_poppo(manager):
    """扱っているモジュールの規則がプロンプトへ載ること。"""
    say(manager, "配線が3本、上から青、白、黒")
    context = manager.grounded_context()
    assert "【配線の規則】" in context
    assert "自分で読んで" in context


def test_only_the_current_module_is_loaded(manager):
    """全モジュールぶんを常時載せない。文脈長を食い潰す。"""
    say(manager, "配線が3本、上から青、白、黒")
    context = manager.grounded_context()
    assert "【ボタンの規則】" not in context
    assert "【サイモンの規則】" not in context


def test_a_matching_reply_is_spoken_as_it_is(manager):
    say(manager, "シリアルはA7B3C1")
    say(manager, "配線が3本、上から青、白、黒")
    verdict, replacement = manager.verify_reply("それなら上から2本目を切っちゃって")
    assert verdict == AGREE
    assert replacement == "", "一致したらポッポの言葉のまま"


def test_a_wrong_reply_is_replaced_before_it_is_spoken(manager):
    say(manager, "シリアルはA7B3C1")
    say(manager, "配線が3本、上から青、白、黒")
    verdict, replacement = manager.verify_reply("よし、3本目を切って！")
    assert verdict == DISAGREE
    assert "2本目" in replacement, "正しい答えへ差し替える"
    assert "3本目" not in replacement, "間違った指示を残さない"


def test_an_unreadable_reply_is_left_alone(manager):
    """読めなかっただけかもしれないのに差し替えない。"""
    say(manager, "シリアルはA7B3C1")
    say(manager, "配線が3本、上から青、白、黒")
    verdict, replacement = manager.verify_reply("えーっと、ちょっと待ってね")
    assert verdict == UNCLEAR
    assert replacement == ""


def test_the_check_is_consumed_once(manager):
    """同じ答えで次のターンまで検算し続けない。"""
    say(manager, "シリアルはA7B3C1")
    say(manager, "配線が3本、上から青、白、黒")
    manager.verify_reply("2本目を切って")
    assert not manager.check_pending()
    assert manager.verify_reply("何か別の話") == (UNCLEAR, "")


def test_nothing_to_verify_outside_a_session(tmp_path):
    cfg = FakeConfig({"video.game_profile": "", "game_profiles.root": str(tmp_path)})
    manager = GameProfileSessionManager(cfg, tmp_path / "state.json")
    assert manager.verify_reply("2本目を切って") == (UNCLEAR, "")
    assert not manager.check_pending()


def test_the_agreement_rate_is_counted(manager):
    """分担が機能しているかを、印象ではなく数で見られるようにする。"""
    say(manager, "シリアルはA7B3C1")
    say(manager, "配線が3本、上から青、白、黒")
    manager.verify_reply("2本目を切って")
    assert manager.expert.agreement_counts["agree"] == 1
    say(manager, "配線が3本、上から青、白、黒")
    manager.verify_reply("3本目を切って")
    assert manager.expert.agreement_counts["disagree"] == 1


# ---------------------------------------------------------------------------
# 規則そのもの
# ---------------------------------------------------------------------------


def test_every_implemented_module_has_rule_text():
    """コードで解けるのに、ポッポには何も渡さない状態を作らない。"""
    from neuro_voice.games.ktane.expert import IMPLEMENTED, NEEDS_TABLE

    for module in IMPLEMENTED - NEEDS_TABLE:
        assert rule_text(module), f"{module} の規則テキストが無い"


def test_the_rule_text_stays_small_enough_to_send():
    """1モジュール400トークン以内。文脈長は既に苦しい。"""
    from neuro_voice.llm.context_budget import estimate_tokens

    for module, text in RULE_TEXT.items():
        assert estimate_tokens(text) <= 400, f"{module} の規則が長すぎる"


def test_an_unknown_module_has_no_rule_text():
    assert rule_text("maze") == ""
    assert rule_text("") == ""


# ---------------------------------------------------------------------------
# 根拠の無い操作指示を止める（実機で爆発した件）
#
# ログ: 表が空のキーパッドに対して「一番上の『プサイ』のボタンを押して」と
# 言い、爆発した。コードは何も答えていない。**ポッポが自分で作った。**
# 検算は「コードの答えがある時」しか働かないので、答えが無い時に指示させない
# 網が別に要る。
# ---------------------------------------------------------------------------

from neuro_voice.games.ktane import KtaneExpert  # noqa: E402
from neuro_voice.games.ktane.verify import (  # noqa: E402
    contains_instruction, ungrounded_refusal,
)


@pytest.mark.parametrize("reply", [
    "一番上の「プサイ」のボタンを押して",
    "一番下の白をカットしてね",
    "2本目を切って",
    "そのまま押しっぱなしにして",
    "タイマーに4が出た瞬間に離して",
    "周波数を 3.505 に合わせて",
    "「about」で送信して",
    "赤、青の順に押して",
])
def test_operational_instructions_are_detected(reply):
    assert contains_instruction(reply)


@pytest.mark.parametrize("reply", [
    "どの線を切ればいいか、まだ分からない",
    "切ってもいいのかな？",
    "何本目を切るか教えて",
    "うわー緊張するね",
    "シリアル番号を教えて",
    "それ、押したらどうなると思う？",
])
def test_questions_and_musings_are_not_instructions(reply):
    """仮定や質問まで止めると、会話にならない。"""
    assert not contains_instruction(reply)


def test_an_instruction_without_a_computed_answer_is_replaced():
    """**コードが何も答えていないのに操作を指示させない。**"""
    expert = KtaneExpert()
    expert.solve("キーパッドのモジュール")
    verdict, replacement = expert.verify("一番上の「プサイ」のボタンを押して")
    assert verdict == DISAGREE
    assert "まだ判断できてない" in replacement
    assert "プサイ" not in replacement


def test_the_replacement_says_what_is_missing():
    expert = KtaneExpert()
    expert.solve("キーパッドのモジュール")
    _verdict, replacement = expert.verify("一番上のボタンを押して")
    assert "キーパッドの表" in replacement


def test_ordinary_talk_without_an_answer_is_left_alone():
    """指示していない発話まで差し替えない。"""
    expert = KtaneExpert()
    verdict, replacement = expert.verify("うわー、これ難しそうだね")
    assert verdict == UNCLEAR
    assert replacement == ""


def test_a_grounded_instruction_still_passes():
    """コードが同じ答えを持っているなら、そのまま話してよい。"""
    expert = KtaneExpert()
    expert.edge.serial = "A1B2C4"
    # `pending_check` を置くのは session 側（`_defusal_outcome`）。
    # ここは expert 単体なので、同じ形を手で作る。
    expert.pending_check = expert.solve("配線が3本、上から青、白、黒")
    assert expert.pending_check is not None
    verdict, replacement = expert.verify("2本目を切って")
    assert verdict == AGREE
    assert replacement == ""


def test_the_prompt_forbids_inventing_for_unlisted_modules(tmp_path):
    """規則が載っていないモジュールについて、操作を言わせない。"""
    import json
    from pathlib import Path

    from neuro_voice.games.session import GameProfileSessionManager

    root = Path(__file__).resolve().parents[1] / "game_profiles"
    cfg = FakeConfig({
        "game_profiles.root": str(root),
        "video.game_profile": KTANE,
    })
    manager = GameProfileSessionManager(cfg, tmp_path / "ktane_state.json")
    manager.handle_final_input("爆弾解除を始めよう", actor_id="a", source="local")
    context = manager.grounded_context()
    assert "規則が載っていないモジュール" in context
    assert "推測で操作を指示すると爆発する" in context
    del json
