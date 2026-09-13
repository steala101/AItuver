"""Internal values must not reach the speaker, and real speech must not be cut.

The prompt forbade this in seven places (~300 tokens, one enumeration
duplicated word for word) and it still happened.  The check moved into code,
so these tests carry the whole burden of "is it safe to say".

Half of them are false-positive tests.  A leak that reaches the ear is
embarrassing; a guard that silences a real sentence is worse, because nobody
can tell it happened.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.leakage import (
    has_internal_leak, leak_markers, strip_internal_leak,
)


@pytest.mark.parametrize("text", [
    "response_shape=direct_take: 直接の答え",
    "primary_style=playful なので軽く返す",
    "- 候補1: 今日の話 / 直接返す",
    "候補2：別の見方",
    "score=0.96 が最上位",
    "[thought] これは内部の考え",
    "【Conversation Planner・内部方針】",
    "【Surface Realizer・最終発話規則】",
    "The user is asking about the weather.",
    "Drafting a response now",
    "Final version: こんにちは",
    "ASK_FOLLOW_UP=no",
    "engine_variant=b で行く",
    "target_length=long",
])
def test_internal_markers_are_caught(text):
    assert has_internal_leak(text)


@pytest.mark.parametrize("text", [
    "そうだね、それ面白いと思う。",
    "スコアは3対1だったよ。",
    "候補が多すぎて選べないね。",          # 「候補」だけでは内部値ではない
    "ゲームのFPSが60出てる。",
    "URLを送っておくね。",
    "『なるほど』って言われると照れる。",
    "AIって結局なんなんだろうね。",
    "1: まずこれ、2: つぎにこれ",           # 番号付けは内部値ではない
    "点数＝高いほどいいってこと？",
    "",
])
def test_ordinary_speech_is_not_touched(text):
    assert not has_internal_leak(text)
    assert strip_internal_leak(text) == text


def test_only_the_leaking_sentence_is_dropped():
    """A stray internal line must not cost the answer that came with it."""
    text = "それは面白いね。response_shape=direct_take。私はこう思うよ。"
    kept = strip_internal_leak(text)
    assert "response_shape" not in kept
    assert "それは面白いね。" in kept
    assert "私はこう思うよ。" in kept


def test_a_reply_that_is_only_a_leak_becomes_empty():
    assert strip_internal_leak("候補1: 話題A / 直接返す") == ""


def test_markers_are_reported_for_logging_without_the_sentence():
    """第12条: ログへ残せるのは印であって、会話ではない。"""
    markers = leak_markers("秘密の話をした。score=0.9。")
    assert markers == ["score="]
    assert all("秘密" not in item for item in markers)


def test_the_check_is_case_insensitive_for_english_markers():
    assert has_internal_leak("the user is asking about it")
