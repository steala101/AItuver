"""Interpreting a request that already says what it wants.

Observed in the log: 「ちょっと3分間ぐらいでラジオトークやって。」 produced

    LLM生成: 初トークン=1750 ms      <- the reply
    LLM生成: 初トークン=12087 ms     <- the planning call, queued behind it

A local backend serves one request at a time, so spending a model round-trip
to rediscover "radio, three minutes" costs twelve seconds of silence before
the first word.  Requests this explicit are read directly.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.directive import ExecutionMode, InteractionMode, parse_plan_proposal
from neuro_voice.dialogue.intent_plan import plan_from_request, requested_duration_seconds


def plan(text, **kwargs):
    shape = plan_from_request(text, **kwargs)
    return None if shape is None else shape


# ---------------------------------------------------------------------------
# Reading the requested length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("ちょっと3分間ぐらいでラジオトークやって。", 180),
    ("5分ラジオして", 300),
    ("30秒だけ話して", 30),
    ("1時間実況して", 3600),
])
def test_an_explicit_length_is_read(text, expected):
    assert requested_duration_seconds(text) == pytest.approx(expected)


def test_no_stated_length_returns_nothing():
    assert requested_duration_seconds("ラジオトークやって") is None
    assert requested_duration_seconds("") is None


def test_an_absurd_length_is_capped():
    assert requested_duration_seconds("100時間話して") == pytest.approx(3600)


# ---------------------------------------------------------------------------
# The shapes that need no interpretation
# ---------------------------------------------------------------------------


def test_the_observed_request_is_read_without_a_model():
    shape = plan("ちょっと3分間ぐらいでラジオトークやって。")
    assert shape is not None
    assert shape["execution_mode"] == "STREAMED_LONG_RESPONSE"
    assert shape["interaction_mode"] == "MONOLOGUE"
    assert shape["duration_hint_seconds"] == pytest.approx(180)


def test_a_radio_request_without_a_length_gets_the_default():
    shape = plan("面白い話題でラジオトークやってみて", default_duration_seconds=300)
    assert shape["duration_hint_seconds"] == pytest.approx(300)


@pytest.mark.parametrize("text,mode,interaction", [
    ("ゲームが終わるまで実況して", "ONGOING_DIRECTIVE", "ACTIVITY_COMMENTARY"),
    ("今日は静かに見守って", "OBSERVATION_DIRECTIVE", "AMBIENT_WATCH"),
    ("この企画を一緒に考えて", "COLLABORATIVE_SESSION", "CO_THINKING"),
    ("しばらく話し続けて", "STREAMED_LONG_RESPONSE", "MONOLOGUE"),
])
def test_each_familiar_shape_is_recognised(text, mode, interaction):
    shape = plan(text)
    assert shape["execution_mode"] == mode
    assert shape["interaction_mode"] == interaction


@pytest.mark.parametrize("text", [
    "日本の首都は？",
    "この文章を短くして",
    "必要そうなことを調べながら進めて",   # ambiguous: the model should decide
    "",
])
def test_an_unclear_request_is_left_to_the_model(text):
    assert plan_from_request(text) is None


@pytest.mark.parametrize("text", [
    "前にTRPGやったの覚えてる？",
    "TRPGって何？",
    "ラジオはどう思う？",
    "実況の話をしてたよ",
])
def test_activity_topic_mentions_never_use_a_start_template(text):
    assert plan_from_request(text) is None


def test_the_produced_shape_validates_into_a_real_plan():
    shape = plan("ちょっと3分間ぐらいでラジオトークやって。")
    import json

    proposal = parse_plan_proposal(json.dumps(shape, ensure_ascii=False))
    assert proposal is not None
    assert proposal.execution_mode is ExecutionMode.STREAMED_LONG_RESPONSE
    assert proposal.interaction_mode is InteractionMode.MONOLOGUE
    assert proposal.duration_hint_seconds == pytest.approx(180)
    assert proposal.search_policy.value == "NO_SEARCH"
    assert proposal.stop_conditions


def test_a_commentary_shape_keeps_its_open_ended_duration():
    import json

    proposal = parse_plan_proposal(
        json.dumps(plan("ゲームが終わるまで実況して"), ensure_ascii=False),
    )
    assert proposal.duration_hint_seconds is None
    assert proposal.needs_environment_events is True
