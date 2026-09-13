"""Running a story session, and not hijacking the player's turn.

Two observed failures during a TRPG attempt:

* 「背後の本棚から適当な一行を読み上げてみる。」 was answered with
  「声の音量を30パーセントにしたよ。」 — the verb 読み上げ**て** satisfied both
  halves of a volume command.
* The assistant then said 「背後の本棚から適当な一行を読み上げてみようかな」,
  taking the player's turn for them.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.dialogue.directive import (
    ExecutionMode, InteractionMode, parse_plan_proposal,
)
from neuro_voice.dialogue.intent_plan import (
    _MODE_GUIDE, continuation_candidate, is_stop_request, plan_from_request,
)
from neuro_voice.tts.volume import parse_tts_volume_command


# ---------------------------------------------------------------------------
# The volume command must not fire on ordinary speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "うん。はい、この本だから適当な一行を読み上げてみる。",
    "背後の本棚から適当な一行を読み上げてみる。",
    "巻物を持ち上げてみる",
    "看板を見上げた",
    "石を積み上げて壁を作る",
    "旗を引き上げる",
])
def test_an_ordinary_sentence_does_not_change_the_volume(text):
    assert parse_tts_volume_command(text) is None


@pytest.mark.parametrize("text,mode,value", [
    ("声の音量を上げて", "adjust", 0.1),
    ("ポッポの声もう少し小さくして", "adjust", -0.1),
    ("君の声を50パーセントにして", "set", 0.5),
    ("読み上げの音量を上げて", "adjust", 0.1),
    ("君の声ミュートにして", "set", 0.0),
])
def test_a_real_volume_request_still_works(text, mode, value):
    command = parse_tts_volume_command(text)
    assert command is not None, text
    assert command.mode == mode
    assert command.value == pytest.approx(value)


def test_music_volume_is_still_not_stolen_from_the_player():
    assert parse_tts_volume_command("曲の音量下げて") is None


# ---------------------------------------------------------------------------
# A story session is a continuing request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "TRPGやろう",
    "ゲームマスターやって",
    "GMになってよ",
    "なりきりで話そう",
    "ロールプレイしてみたい",
    "物語を始めよう",
])
def test_a_story_request_is_nominated_and_shaped(text):
    assert continuation_candidate(text)[0] is True, text
    shape = plan_from_request(text)
    assert shape is not None, text
    assert shape["interaction_mode"] == "NARRATION"


def test_the_session_waits_for_the_player():
    """The player's turn is the point; the assistant must not fill it."""
    plan = parse_plan_proposal(
        json.dumps(plan_from_request("TRPGやろう"), ensure_ascii=False),
    )
    assert plan is not None
    assert plan.execution_mode is ExecutionMode.COLLABORATIVE_SESSION
    assert plan.interaction_mode is InteractionMode.NARRATION
    assert plan.needs_user_input is True
    assert plan.search_policy.value == "NO_SEARCH"


def test_the_narration_guide_forbids_taking_the_players_turn():
    guide = _MODE_GUIDE[InteractionMode.NARRATION]
    assert "勝手に決めない" in guide or "こちらで決めない" in guide
    assert "ASK_USER" in guide
    assert "WAIT" in guide


def test_a_radio_request_is_not_mistaken_for_a_story():
    shape = plan_from_request("ラジオトークやって")
    assert shape["interaction_mode"] == "MONOLOGUE"


def test_the_reply_is_the_turn_and_no_segment_follows_it():
    """Regression: the game master described the scene and asked the same
    question twice — once as the reply, once as a segment."""
    from neuro_voice.dialogue.continuation import ContinuationController
    from neuro_voice.dialogue.directive import DirectiveStatus
    from tests.test_behavior_directive import FakeConfig

    controller = ContinuationController(FakeConfig())
    plan = parse_plan_proposal(
        json.dumps(plan_from_request("TRPGやろう"), ensure_ascii=False),
    )
    directive = controller.start_directive(
        plan, conversation_id="local", user_text="TRPGやろう",
    )
    controller.note_reply(directive, "二つの道があるみたい。どっちから進んでみる？")
    assert directive.status is DirectiveStatus.WAITING_FOR_USER
    assert controller.due_directive("local") is None

    # The player answers, and only then may the session act again.
    controller.note_user_turn_started(directive)
    assert directive.status is DirectiveStatus.ACTIVE
    assert controller.due_directive("local") is directive


def test_every_reply_counts_as_a_turn_in_a_session():
    from neuro_voice.dialogue.continuation import ContinuationController
    from tests.test_behavior_directive import FakeConfig

    controller = ContinuationController(FakeConfig())
    plan = parse_plan_proposal(
        json.dumps(plan_from_request("TRPGやろう"), ensure_ascii=False),
    )
    directive = controller.start_directive(
        plan, conversation_id="local", user_text="TRPGやろう",
    )
    for index in range(3):
        controller.note_user_turn_started(directive)
        controller.note_reply(directive, f"場面{index}の描写。どうする？")
    assert directive.completed_segment_count == 3


def test_a_monologue_is_unaffected_by_the_turn_rule():
    """A radio programme must keep producing segments on its own."""
    from neuro_voice.dialogue.continuation import ContinuationController
    from neuro_voice.dialogue.directive import DirectiveStatus
    from tests.test_behavior_directive import FakeConfig

    controller = ContinuationController(FakeConfig())
    plan = parse_plan_proposal(
        json.dumps(plan_from_request("ラジオトークやって"), ensure_ascii=False),
    )
    directive = controller.start_directive(
        plan, conversation_id="local", user_text="ラジオトークやって",
    )
    controller.note_reply(directive, "じゃあ始めるね。今日の一本目は…")
    assert directive.status is not DirectiveStatus.WAITING_FOR_USER
    assert controller.due_directive("local") is directive


def test_the_normal_reply_is_told_it_is_the_game_master():
    """Regression: only segments got the role guide, so the reply — which is
    what the player actually hears first — played a character in the story."""
    from neuro_voice.dialogue.directive import directive_from_plan
    from neuro_voice.dialogue.intent_plan import directive_prompt_block

    plan = parse_plan_proposal(
        json.dumps(plan_from_request("TRPGやろう"), ensure_ascii=False),
    )
    directive = directive_from_plan(
        plan, conversation_id="local", original_request="TRPGやろう",
    )
    block = directive_prompt_block(directive)
    assert "物語の外にいる" in block
    assert "登場人物ではない" in block
    assert "二度しない" in block


def test_a_story_segment_is_not_given_unrelated_memories():
    """Regression: an invented world from an earlier radio programme walked
    into the middle of a TRPG and replaced it."""
    import asyncio

    from neuro_voice.dialogue.continuation import ContinuationController
    from tests.test_behavior_directive import FakeConfig

    controller = ContinuationController(FakeConfig())
    plan = parse_plan_proposal(
        json.dumps(plan_from_request("TRPGやろう"), ensure_ascii=False),
    )
    directive = controller.start_directive(
        plan, conversation_id="local", user_text="TRPGやろう",
    )
    seen: dict[str, str] = {}

    async def generate(messages):
        seen["prompt"] = messages[-1]["content"]
        return json.dumps({"action": "WAIT", "wait_condition": "相手の選択待ち"})

    async def speak(_text, _segment_id, _version):
        return True

    asyncio.run(controller.execute_next_segment(
        directive, generate=generate, speak=speak,
        memory_notes=["ヨッシーが主役でマリオを背中に乗せる逆漫画の世界"],
        interests=["逆漫画"],
    ))
    assert "逆漫画" not in seen["prompt"]


def test_other_modes_are_told_when_to_ignore_a_memory():
    from neuro_voice.dialogue.directive import directive_from_plan
    from neuro_voice.dialogue.intent_plan import build_segment_messages

    plan = parse_plan_proposal(
        json.dumps(plan_from_request("ラジオトークやって"), ensure_ascii=False),
    )
    directive = directive_from_plan(
        plan, conversation_id="local", original_request="ラジオトークやって",
    )
    messages = build_segment_messages(directive, memory_notes=["昔の話"])
    body = messages[-1]["content"]
    assert "関係なければ完全に無視" in body


def test_an_ordinary_turn_inside_a_story_is_not_a_new_request():
    """Player actions must not be read as fresh continuing requests."""
    for text in ("背後の本棚の一行を読んでみる。", "扉を開ける", "その設定でやってみようよ"):
        assert plan_from_request(text) is None


@pytest.mark.parametrize("text", [
    "よし、ラジオは一旦終わろう。",
    "いや、もうラジオは終わったよ。",
    "ラジオトークはこれで終わりにしよう",
    "独り言はやめて",
])
def test_a_stop_sentence_can_never_start_another_continuing_plan(text):
    assert is_stop_request(text), text
    assert continuation_candidate(text) == (False, "stop_request")
    assert plan_from_request(text) is None


def test_a_negative_stop_sentence_is_not_a_stop_request():
    assert not is_stop_request("ラジオを止めないで")
