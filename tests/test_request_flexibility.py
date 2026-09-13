"""The person's words outrank anything hardcoded.

The continuation system has fixed templates and fixed mode guides for speed and
consistency.  Both are shortcuts, and a shortcut that silently overrules the
request is the assistant refusing to listen:

    「GM兼プレイヤーとしても参加して」

must not be answered with the plain game-master plan whose guide says
「あなたは物語の外にいる。自分用のキャラクターを作らない」.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.dialogue.directive import InteractionMode, directive_from_plan, parse_plan_proposal
from neuro_voice.dialogue.intent_plan import (
    _MODE_GUIDE, build_segment_messages, continuation_candidate,
    directive_prompt_block, plan_from_request,
)


def directive_for(text):
    shape = plan_from_request(text)
    assert shape is not None, f"expected a template plan for {text!r}"
    plan = parse_plan_proposal(json.dumps(shape, ensure_ascii=False))
    return directive_from_plan(plan, conversation_id="local", original_request=text)


# ---------------------------------------------------------------------------
# A qualified request goes to the model, not to a template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "GM兼プレイヤーとしても参加して",
    "GMやりつつポッポも一緒に遊んで",
    "TRPGのGMやって、きみも自分のキャラで参加していいよ",
    "実況しながら一緒に考えて",
    "ラジオやって、でも私も途中で入っていい？",
])
def test_a_combined_or_qualified_request_is_left_to_the_model(text):
    assert plan_from_request(text) is None, text


@pytest.mark.parametrize("text", [
    "TRPGやろう",
    "ポッポがGMやって",
    "ラジオトークやって",
    "3分ラジオして",
    "ゲームが終わるまで実況して",
])
def test_a_plain_request_still_takes_the_fast_path(text):
    assert plan_from_request(text) is not None, text


def test_a_qualified_request_is_still_recognised_as_continuing():
    """It must reach Intent-to-Plan, not fall back to a one-shot reply."""
    for text in ("GM兼プレイヤーとしても参加して", "TRPGのGMやって、きみも参加していいよ"):
        assert continuation_candidate(text)[0] is True, text


# ---------------------------------------------------------------------------
# The guides are defaults, and say so
# ---------------------------------------------------------------------------


def test_the_narration_guide_allows_being_asked_to_join():
    guide = _MODE_GUIDE[InteractionMode.NARRATION]
    assert "既定" in guide
    assert "兼ねて参加" in guide or "そちらに従い" in guide


def test_the_only_hard_rule_is_not_taking_the_players_turn():
    guide = _MODE_GUIDE[InteractionMode.NARRATION]
    assert "どんな依頼でもしない" in guide


def test_the_reply_prompt_marks_the_guide_as_a_default():
    block = directive_prompt_block(directive_for("TRPGやろう"))
    assert "既定" in block
    assert "依頼を優先" in block


def test_the_segment_prompt_marks_the_guide_as_a_default():
    messages = build_segment_messages(directive_for("TRPGやろう"))
    system = messages[0]["content"]
    assert "既定" in system
    assert "ユーザーの依頼のほうを優先" in system


# ---------------------------------------------------------------------------
# The person's literal words travel with the directive
# ---------------------------------------------------------------------------


def test_the_original_wording_reaches_the_reply_prompt():
    """Even on the template path, the model sees what was actually asked."""
    text = "見守って、でも面白い時だけ喋って"
    block = directive_prompt_block(directive_for(text))
    assert text in block


def test_the_original_wording_reaches_the_segment_prompt():
    text = "見守って、でも面白い時だけ喋って"
    messages = build_segment_messages(directive_for(text))
    assert text in messages[-1]["content"]


def test_request_constraints_are_shown_alongside_the_default():
    directive = directive_for("TRPGやろう")
    directive.style_constraints = ["ポッポも自分のキャラクターとして参加する"]
    block = directive_prompt_block(directive)
    assert "ポッポも自分のキャラクターとして参加する" in block


def test_a_revision_can_change_the_role_mid_session():
    """「やっぱりきみも参加して」 must be able to take effect."""
    directive = directive_for("TRPGやろう")
    assert directive.revise({
        "style_constraints": ["GMをやりながら自分のキャラクターでも参加する"],
    }) is True
    assert "GMをやりながら自分のキャラクターでも参加する" in directive.style_constraints
    assert "GMをやりながら自分のキャラクターでも参加する" in directive_prompt_block(directive)
