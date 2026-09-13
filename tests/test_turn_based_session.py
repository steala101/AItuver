"""A turn-based session must not poll, and must not forget its own story.

Observed on 2026-07-26 23:31-23:37 (a TRPG run):

* After a backchannel the directive went ACTIVE, the model answered every
  segment request with WAIT because it was the player's move, and the tick
  loop asked again — 20 generations in 90 seconds, none of them spoken, all
  queued ahead of the person on a serial local LLM.

* ``文脈長のため古い会話を17件除外`` by turn twelve.  The opening scene was
  gone from the history, so the game master stopped narrating and started
  making friendly small talk about the player's answers.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.dialogue.directive import (
    DirectiveStatus, SegmentActionType, parse_plan_proposal,
)
from neuro_voice.dialogue.intent_plan import directive_prompt_block
from tests.test_behavior_directive import RADIO_PLAN, make_controller, run

NARRATION_PLAN = json.dumps({
    "understanding": "TRPGのGMとして物語を進めてほしい",
    "goal": "プレイヤーの選択に応じて場面を語り、手番を渡す",
    "execution_mode": "COLLABORATIVE_SESSION",
    "interaction_mode": "NARRATION",
    "continuation_policy": "UNTIL_CANCELLED",
    "segment_target_seconds": 25,
    "needs_user_input": True,
    "allowed_tools": [],
    "search_policy": "NO_SEARCH",
    "stop_conditions": ["ユーザーが終わりにする"],
    "success_criteria": ["筋の通った場面を積み重ねる"],
    "initial_plan": ["導入の場面", "最初の選択"],
})


def session(raw=NARRATION_PLAN, **overrides):
    controller = make_controller(**overrides)
    plan = parse_plan_proposal(raw)
    assert plan is not None
    directive = controller.start_directive(
        plan, conversation_id="speaker:1", user_text="TRPGのGMをやって",
    )
    return controller, directive


def due(controller):
    return controller.due_directive(
        "speaker:1", floor_busy=False, has_environment_event=False,
    )


# ---------------------------------------------------------------------------
# "Wait" in a turn-based session is a state, not a retry
# ---------------------------------------------------------------------------


def wait_segment(controller, directive):
    async def generate(_messages):
        return json.dumps({"action": "WAIT", "wait_condition": "プレイヤーの選択待ち"})

    async def speak(_text, _segment_id, _state_version):
        raise AssertionError("a WAIT segment must not speak")

    return run(controller.execute_next_segment(
        directive, generate=generate, speak=speak,
    ))


def test_a_turn_based_session_stops_polling_when_told_to_wait():
    """Regression: 20 generations in 90s, none of them spoken."""
    controller, directive = session()
    directive.status = DirectiveStatus.ACTIVE
    outcome = wait_segment(controller, directive)
    assert outcome.status == "waited"
    assert directive.status is DirectiveStatus.WAITING_FOR_USER
    assert due(controller) is None


def test_a_monologue_told_to_wait_keeps_its_turn():
    """A radio show waiting for a beat is not waiting for the listener."""
    controller, directive = session(RADIO_PLAN)
    directive.status = DirectiveStatus.ACTIVE
    outcome = wait_segment(controller, directive)
    assert outcome.status == "waited"
    assert directive.status is DirectiveStatus.ACTIVE
    assert due(controller) is directive


def test_wait_hands_the_floor_to_the_player():
    controller, directive = session()
    directive.status = DirectiveStatus.ACTIVE
    controller.note_reply(directive, "扉が開いた。どうする？")
    assert directive.status is DirectiveStatus.WAITING_FOR_USER
    assert due(controller) is None


def test_an_untimed_monologue_still_retries_after_a_wait():
    """The change must not make a radio show give up on one WAIT."""
    controller, directive = session(RADIO_PLAN)
    assert directive.needs_user_input is False
    directive.status = DirectiveStatus.ACTIVE
    directive.waited_segment_count = 3
    assert due(controller) is directive


def test_the_player_answering_reopens_the_session():
    controller, directive = session()
    controller.note_reply(directive, "扉が開いた。どうする？")
    controller.note_user_turn_started(directive)
    assert directive.status is DirectiveStatus.ACTIVE
    assert due(controller) is directive


# ---------------------------------------------------------------------------
# The story has to survive context trimming
# ---------------------------------------------------------------------------


def test_the_opening_is_remembered_for_ever():
    _controller, directive = session()
    directive.record_progress("埃の積もった屋根裏部屋。古いトランクから光が漏れている。")
    for i in range(20):
        directive.record_progress(f"場面{i}")
    assert "屋根裏部屋" in directive.opening_note


def test_only_a_window_of_later_beats_is_kept():
    _controller, directive = session()
    for i in range(20):
        directive.record_progress(f"場面{i}")
    assert len(directive.progress_notes) <= 6
    assert directive.progress_notes[-1] == "場面19"


def test_a_repeated_line_is_not_stored_twice():
    _controller, directive = session()
    directive.record_progress("導入")
    directive.record_progress("恐竜が目を覚ました")
    directive.record_progress("恐竜が目を覚ました")
    assert directive.progress_notes == ["恐竜が目を覚ました"]


def test_empty_text_is_ignored():
    _controller, directive = session()
    directive.record_progress("   ")
    assert directive.opening_note == ""


def test_a_long_line_is_trimmed():
    _controller, directive = session()
    directive.record_progress("あ" * 400)
    assert len(directive.opening_note) <= 110


def test_replies_are_recorded_as_progress():
    controller, directive = session()
    controller.note_reply(directive, "屋根裏部屋の古いトランクが光っている。")
    controller.note_user_turn_started(directive)
    controller.note_reply(directive, "きみは恐竜のおもちゃを手に取った。")
    assert "トランク" in directive.opening_note
    assert any("恐竜" in note for note in directive.progress_notes)


def test_the_prompt_carries_the_story_forward():
    """This block is what remains after the history has been trimmed away."""
    controller, directive = session()
    controller.note_reply(directive, "屋根裏部屋の古いトランクが光っている。")
    controller.note_user_turn_started(directive)
    controller.note_reply(directive, "恐竜のおもちゃが目を覚ました。")
    block = directive_prompt_block(directive)
    assert "この依頼の始まり" in block
    assert "トランク" in block
    assert "ここまでの流れ" in block
    assert "恐竜" in block


def test_a_fresh_directive_adds_nothing_to_the_prompt():
    _controller, directive = session()
    block = directive_prompt_block(directive)
    assert "この依頼の始まり" not in block
    assert "ここまでの流れ" not in block
