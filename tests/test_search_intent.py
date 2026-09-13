"""Asking for a search vs. asking what I was doing.

Regression for an observed failure: 「え? 何調べて、何調べてたの?」 — a question
about the assistant's previous turn — matched the substring 「調べて」, was ruled
an explicit search request, went to the Web as
"え? 何調べて、何調べてたの? 対処法", and the resulting Excel pages were then
reported by the assistant as things it had personally been studying.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.kernel import ConversationKernel
from neuro_voice.search.contracts import SearchAnswerMode, SearchDisposition
from neuro_voice.search.deepsearch import build_query, is_contextual_clarification, should_search
from neuro_voice.search.intent import is_search_request, route_search_request


# ---------------------------------------------------------------------------
# The observed failure
# ---------------------------------------------------------------------------


def test_the_utterance_that_triggered_a_bogus_search():
    text = "え? 何調べて、何調べてたの?"
    assert is_search_request(text) is False
    assert should_search(text) is False
    assert ConversationKernel.is_context_reference(text) is True
    decision = ConversationKernel.search_decision_for(text)
    assert decision.allowed is False
    assert decision.explicit_request is False


def test_the_question_is_answered_from_the_conversation():
    kernel = ConversationKernel()
    frame = kernel.build(
        "何調べてたの？", source="local", speaker_id="u1",
        reference_candidates=[{"assistant_text": "ちょっとした調べ物をしてたんだ", "score": .9}],
    )
    assert "resolve_reference" in frame.obligations
    assert frame.search_decision.allowed is False


# ---------------------------------------------------------------------------
# Questions about an action are never search requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "何調べてたの？",
    "さっき何調べてた?",
    "ネットで何調べてたの?",
    "さっき調べてたやつ何だっけ",
    "調べてる?",
    "もう調べた?",
    "ググったの?",
    "さっき検索したでしょ",
    "調べてくれたんだ、ありがとう",
    "何してたの?",
])
def test_questions_about_an_action_do_not_request_a_search(text):
    assert is_search_request(text) is False


@pytest.mark.parametrize("text", [
    "今日の天気調べて",
    "これ調べて",
    "ちょっと調べてみて",
    "調べてくれる？",
    "調べてほしい",
    "調べておいて",
    "VLOOKUPについて検索して",
    "ググってみて",
    "ネットで最新の情報ある？",
])
def test_real_requests_still_reach_the_web(text):
    assert is_search_request(text) is True


def test_an_explicit_request_is_marked_explicit_in_the_kernel():
    decision = ConversationKernel.search_decision_for("VLOOKUPについて検索して")
    assert decision.allowed is True
    assert decision.explicit_request is True
    assert decision.reason_code == "explicit_search_request"


# ---------------------------------------------------------------------------
# The clarification guard is no longer bypassed
# ---------------------------------------------------------------------------


def test_clarification_guard_now_sees_the_question():
    """Previously the substring match made this return False and search ran."""
    assert is_contextual_clarification("何調べてたの？") is True


def test_privacy_and_activity_blocks_still_win_over_an_explicit_request():
    blocked = ConversationKernel.search_decision_for(
        "調べて", privacy_blocked=True,
    )
    assert blocked.allowed is False
    assert blocked.blocked_by_privacy is True
    during_activity = ConversationKernel.search_decision_for(
        "調べて", activity_active=True,
    )
    assert during_activity.allowed is False


def test_ordinary_conversation_is_untouched():
    for text in ("おはよう", "今日は疲れたよ", "VLOOKUPも結構使うよ"):
        assert is_search_request(text) is False


@pytest.mark.parametrize("text", [
    "饅頭こわいを検索して、短く教えて",
    "饅頭こわいを検索して短く教えて",
    "落語の饅頭こわいを調べてから説明して",
])
def test_compound_search_request_keeps_subject_and_reaches_search(text):
    assert is_search_request(text) is True
    assert should_search(text) is True
    query = build_query(text)
    assert "饅頭こわい" in query
    assert "検索して" not in query
    assert "調べて" not in query
    assert "短く" not in query
    assert "説明して" not in query


@pytest.mark.parametrize("text", [
    "何を調べてたの？",
    "検索しないで、知っている範囲で話して",
    "調べてくれてありがとう",
    "「検索して」は依頼の言い方だよ",
])
def test_search_wording_without_a_current_request_does_not_execute(text):
    assert is_search_request(text) is False
    assert route_search_request(text).disposition is SearchDisposition.NOT_REQUESTED


def test_search_route_distinguishes_blocked_clarify_and_deferred():
    blocked = route_search_request("饅頭こわいを検索して", enabled=False)
    assert blocked.disposition is SearchDisposition.BLOCKED
    assert blocked.reason_code == "search_disabled"
    assert blocked.query == "饅頭こわい"

    clarify = route_search_request("それを検索して")
    assert clarify.disposition is SearchDisposition.CLARIFY
    assert clarify.query == ""

    deferred = route_search_request("あとで饅頭こわいを調べておいて")
    assert deferred.disposition is SearchDisposition.DEFERRED


def test_search_route_records_answer_mode_without_polluting_query():
    brief = route_search_request("饅頭こわいを検索して、短く教えて")
    assert brief.disposition is SearchDisposition.EXECUTE
    assert brief.answer_mode is SearchAnswerMode.BRIEF
    assert brief.query == "饅頭こわい"

    explanation = route_search_request("落語の饅頭こわいを調べてから説明して")
    assert explanation.answer_mode is SearchAnswerMode.EXPLANATION
    assert explanation.query == "落語の饅頭こわい"
