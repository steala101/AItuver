"""Answering 「マジでそう。」 with the previous answer, word for word.

Observed three times in a row on screen:

    ポッポ  本当にね！外に出るだけで体力が削られる感じがするよ。…
    チビ    マジでそう。
    ポッポ  本当にね！外に出るだけで体力が削られる感じがするよ。…   ← 同一
    チビ    マジでそう。
    ポッポ  本当にね！外に出るだけで体力が削られる感じがするよ。…   ← 同一

The turn added nothing, so the most salient text in front of the model was its
own previous answer.  Adding 「マジで」 to a vocabulary list would patch this
one phrase; comparing the produced text catches the whole class, and repeating
yourself verbatim is a mechanical property that code can check (第2条).

The reply is streamed to the screen and the speaker token by token, so the
check has to happen before it is out.  Buffering the whole reply would add its
generation time to every turn — the opposite of what the tempo work is for —
so only the opening sentence is held.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.echo import (
    EchoSource, ReplyEchoGuard, containment, is_echo, resolve_historical_echo,
)
from neuro_voice.utils.textseg import SentenceSegmenter

PREVIOUS = "本当にね！外に出るだけで体力が削られる感じがするよ。今日は特に、冷たい飲み物とかで自分をクールダウンさせないとダメなレベルじゃない？"


def segmenter():
    return SentenceSegmenter(max_chars=120, min_chars=8)


def run(previous, reply, *, chunk=4):
    guard = ReplyEchoGuard(previous, segmenter())
    verdict, ready, shown = "holding", [], ""
    for index in range(0, len(reply), chunk):
        verdict, ready, shown = guard.feed(reply[index:index + chunk])
        if verdict != "holding":
            return verdict, ready, shown
    return guard.flush()


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def test_a_verbatim_repeat_is_an_echo():
    assert is_echo(PREVIOUS, PREVIOUS)


def test_a_reworded_repeat_is_still_an_echo():
    reworded = "本当にね！外に出るだけで体力が削られる感じがする。"
    assert is_echo(reworded, PREVIOUS)


def test_a_new_answer_is_not_an_echo():
    assert not is_echo("じゃあ今日は早めに切り上げようか。", PREVIOUS)


def test_sharing_a_topic_word_is_not_an_echo():
    assert not is_echo("体力って、歳とると減るよね。", PREVIOUS)


def test_a_short_agreement_is_not_an_echo():
    """「うん。」 twice is phrasing, not repetition."""
    assert not is_echo("うん。", PREVIOUS)
    assert containment("そうだね", PREVIOUS) == 0.0


# ---------------------------------------------------------------------------
# Guarding the stream
# ---------------------------------------------------------------------------


def test_the_repeat_never_reaches_the_screen_or_the_speaker():
    verdict, ready, shown = run(PREVIOUS, PREVIOUS)
    assert verdict == "repeat"
    assert ready == []
    assert shown == ""


def test_a_normal_reply_is_released_intact():
    reply = "じゃあ今日は早めに切り上げようか。無理して倒れたら元も子もないし。"
    verdict, ready, shown = run(PREVIOUS, reply)
    assert verdict == "clear"
    assert shown.startswith("じゃあ")
    assert ready, "最初の文は読み上げへ渡される"


def test_only_the_opening_is_held():
    """The rest streams as before; holding everything would cost latency."""
    reply = "じゃあ今日は早めに切り上げようか。無理して倒れたら元も子もないし。"
    guard = ReplyEchoGuard(PREVIOUS, segmenter())
    verdicts = []
    for index in range(0, len(reply), 4):
        verdicts.append(guard.feed(reply[index:index + 4])[0])
    assert "clear" in verdicts
    assert verdicts.index("clear") < len(verdicts) - 1, "文末まで待たずに解除される"


def test_a_changed_tiny_interjection_does_not_hide_the_repeated_body():
    previous = (
        "あはは、確かに！仕事があるってわかってて粘っちゃうの、"
        "ちょっとずるいよね。でも、集中しちゃうと時間が溶けちゃうから危ないよ。"
    )
    repeated = (
        "あはは、やっぱり！「仕事がある」ってわかってて粘っちゃうの、"
        "ちょっとずるいよね。でも、集中しちゃうと時間が溶けちゃうから危ないよ。"
    )

    verdict, ready, shown = run(previous, repeated)

    assert verdict == "repeat"
    assert ready == []
    assert shown == ""


def test_a_tiny_interjection_plus_new_content_is_released():
    previous = "明日は仕事なら、今日は早めに切り上げた方がよさそう。"
    reply = "あはは、そうだね！でも今やってるところだけ終わらせたいな。"

    verdict, ready, shown = run(previous, reply)

    assert verdict == "clear"
    assert shown == reply
    assert ready


def test_a_short_recurring_opener_is_not_mistaken_for_the_whole_old_reply():
    """Observed false positive: 「えっ、なに？」だけが古い返答と重なった。"""
    guard = ReplyEchoGuard(
        "えっ、なに？それってどういうこと？もう少し教えて。",
        segmenter(),
    )
    verdict = "holding"
    for chunk in ("えっ、なに？", "「くだす」って……"):
        verdict, _ready, _shown = guard.feed(chunk)
    if verdict == "holding":
        verdict, _ready, _shown = guard.flush()
    assert verdict == "clear"


def test_a_reply_with_no_sentence_end_is_decided_on_flush():
    guard = ReplyEchoGuard(PREVIOUS, segmenter())
    for chunk in ("本当にね", "！外に出る", "だけで体力が削られる感じ"):
        guard.feed(chunk)
    assert guard.flush()[0] == "repeat"


def test_a_short_new_reply_without_punctuation_still_gets_through():
    guard = ReplyEchoGuard(PREVIOUS, segmenter())
    guard.feed("そうしよう")
    assert guard.flush()[0] == "clear"


def test_an_empty_reply_is_not_called_a_repeat():
    guard = ReplyEchoGuard(PREVIOUS, segmenter())
    assert guard.flush()[0] == "clear"


def test_no_previous_reply_means_no_guarding():
    assert ReplyEchoGuard("", segmenter()).active is False
    assert ReplyEchoGuard("　\n ", segmenter()).active is False


def test_the_first_reply_of_a_session_is_never_blocked():
    guard = ReplyEchoGuard("", segmenter())
    verdict, _ready, _shown = guard.feed("はじめまして。")
    assert verdict != "repeat"


@pytest.mark.parametrize("chunk", [1, 3, 7, 40])
def test_the_verdict_does_not_depend_on_token_boundaries(chunk):
    assert run(PREVIOUS, PREVIOUS, chunk=chunk)[0] == "repeat"
    assert run(PREVIOUS, "全然ちがう話をしようか。", chunk=chunk)[0] == "clear"


# ---------------------------------------------------------------------------
# ユーザーの言葉のオウム返し（persona の一文から移設）
# ---------------------------------------------------------------------------

USER_SAID = "今のAIって話しててもAI感が強いんだよね。返事は返ってくるけど、中身が薄い気がする。"


def test_parroting_the_user_is_a_repeat_too():
    """persona の「オウム返しはしない」を、規則ではなく比較で担う。"""
    verdict, _ready, _shown = run([""], USER_SAID)
    assert verdict == "clear"          # 相手が空なら止めない
    verdict, _ready, _shown = run(["", USER_SAID], USER_SAID)
    assert verdict == "repeat"


def test_answering_the_user_in_different_words_is_not_a_repeat():
    verdict, _ready, _shown = run(
        [PREVIOUS, USER_SAID],
        "分かる。定型文で埋めてる感じがすると、聞いてる側は冷めちゃうよね。",
    )
    assert verdict == "clear"


def test_both_sources_are_checked_not_only_the_first():
    assert run([PREVIOUS, USER_SAID], PREVIOUS)[0] == "repeat"
    assert run([PREVIOUS, USER_SAID], USER_SAID)[0] == "repeat"


def test_labelled_source_reports_which_kind_triggered_the_guard():
    guard = ReplyEchoGuard(
        [
            EchoSource(PREVIOUS, "assistant"),
            EchoSource(USER_SAID, "user"),
        ],
        segmenter(),
    )
    guard.feed(USER_SAID)
    verdict, _ready, _shown = guard.flush()
    assert verdict == "repeat"
    assert guard.match is not None
    assert guard.match.kind == "user"


def test_short_user_word_may_be_used_inside_a_real_clarifying_question():
    """A 3-character ASR result must not suppress a useful clarification."""
    guard = ReplyEchoGuard(
        [EchoSource("前の長い返答です。", "assistant"), EchoSource("くだす。", "user")],
        segmenter(),
    )
    verdict, _ready, _shown = guard.feed("えっ、なに？「くだす」って……")
    if verdict == "holding":
        verdict, _ready, _shown = guard.flush()
    assert verdict == "clear"


def test_a_list_of_empty_sources_disables_the_guard():
    assert ReplyEchoGuard(["", "  "], segmenter()).active is False
    assert ReplyEchoGuard([], segmenter()).active is False


def test_a_plain_string_still_works():
    """既存の呼び出しを壊さない。"""
    assert ReplyEchoGuard(PREVIOUS, segmenter()).active is True
    assert run(PREVIOUS, PREVIOUS)[0] == "repeat"


def test_historical_similarity_never_discards_an_undelivered_primary():
    result = resolve_historical_echo(
        PREVIOUS, PREVIOUS, regenerated_matches_history=True,
    )
    assert result.text == PREVIOUS
    assert result.resolution == "ALLOW_HISTORICAL_SIMILARITY"


def test_empty_regeneration_keeps_the_valid_primary():
    result = resolve_historical_echo(PREVIOUS, "")
    assert result.text == PREVIOUS
    assert result.resolution == "PRIMARY"


def test_valid_regeneration_wins_over_the_primary():
    regenerated = "別の表現で、内容を短く答える。"
    result = resolve_historical_echo(PREVIOUS, regenerated)
    assert result.text == regenerated
    assert result.resolution == "REGENERATED"


def test_only_a_true_empty_pair_needs_a_safe_fallback():
    result = resolve_historical_echo("", "")
    assert result.text == ""
    assert result.resolution == ""
