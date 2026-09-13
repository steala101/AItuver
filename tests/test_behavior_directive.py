"""BehaviorDirective / Intent-to-Plan / ContinuationController.

These tests deliberately never touch audio, a model or an event loop's real
clock.  The controller is state, so it must be testable as state.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from neuro_voice.dialogue.continuation import ContinuationController
from neuro_voice.dialogue.directive import (
    ContinuationPolicy,
    DirectiveAction,
    DirectiveStatus,
    ExecutionMode,
    InteractionMode,
    SearchPolicy,
    SegmentActionType,
    SegmentLoopDetector,
    parse_plan_proposal,
    parse_segment_action,
)
from neuro_voice.dialogue.intent_plan import (
    classify_follow_up, continuation_candidate, is_activity_start_request,
    is_backchannel,
)
from neuro_voice.dialogue.kernel import ConversationKernel


class FakeConfig:
    def __init__(self, **overrides):
        self._values = {
            "behavior_directive.enabled": True,
            "behavior_directive.continuation_controller_enabled": True,
            "behavior_directive.max_duration_seconds": 1800,
            "behavior_directive.max_segments": 100,
            "behavior_directive.max_consecutive_errors": 2,
            "behavior_directive.tts_max_queued_segments": 2,
            "behavior_directive.tts_max_ahead_seconds": 15,
            "behavior_directive.max_repeated_topic_count": 2,
            "behavior_directive.max_loop_detections": 2,
            "behavior_directive.segment_max_chars": 500,
            "behavior_directive.observation_min_importance": 0.55,
        }
        self._values.update(overrides)

    def get(self, key, default=None):
        return self._values.get(key, default)


RADIO_PLAN = json.dumps({
    "understanding": "数分間ラジオのような一人語りを聞きたい",
    "goal": "リスナーを退屈させず自然に話題を展開する",
    "execution_mode": "STREAMED_LONG_RESPONSE",
    "interaction_mode": "MONOLOGUE",
    "continuation_policy": "UNTIL_DURATION_OR_INTERRUPTED",
    "duration_hint_seconds": 180,
    "segment_target_seconds": 20,
    "needs_user_input": False,
    "needs_environment_events": False,
    "allowed_tools": [],
    "search_policy": "NO_SEARCH",
    "style_constraints": ["ラジオパーソナリティ風"],
    "content_constraints": ["同じ内容を繰り返さない"],
    "stop_conditions": ["ユーザーが止める", "約3分経過"],
    "success_criteria": ["複数の話題を自然につなぐ"],
    "initial_plan": ["導入", "今の関心", "締め"],
    "uncertainty": 0.1,
    "clarification_question": None,
})

COMMENTARY_PLAN = json.dumps({
    "understanding": "ゲーム中の出来事に反応し続ける",
    "goal": "プレイを邪魔せず重要な場面へ反応する",
    "execution_mode": "ONGOING_DIRECTIVE",
    "interaction_mode": "ACTIVITY_COMMENTARY",
    "continuation_policy": "UNTIL_ACTIVITY_END_OR_CANCELLED",
    "duration_hint_seconds": None,
    "segment_target_seconds": 8,
    "needs_environment_events": True,
    "allowed_tools": ["game_state_read_only"],
    "search_policy": "NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED",
    "stop_conditions": ["ゲームセッション終了"],
    "success_criteria": ["イベントに関連した発言をする"],
    "initial_plan": ["状況を観察", "重要イベントを選ぶ"],
})


def make_controller(**overrides):
    return ContinuationController(FakeConfig(**overrides))


def start(controller, raw=RADIO_PLAN, text="ラジオトーク風に3分くらい話して"):
    plan = parse_plan_proposal(raw)
    assert plan is not None
    return controller.start_directive(plan, conversation_id="local", user_text=text)


def segment(**fields):
    payload = {"action": "SPEAK", "spoken_content": "本文", "topic": "話題"}
    payload.update(fields)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Nomination and classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "ラジオトーク風に3分くらい話して",
    "ゲームが終わるまで実況して",
    "今日は静かに見守って。必要な時だけ話して",
    "この企画を一緒に考えて",
    "必要そうなことを調べながら進めて",
    "面白い話を何個か続けて",
])
def test_continuing_requests_are_nominated(text):
    nominated, reason = continuation_candidate(text)
    assert nominated, text
    assert reason


@pytest.mark.parametrize("text", [
    "日本の首都は？",
    "この文章を短くして",
    "おはよう",
    "",
])
def test_one_shot_requests_are_not_nominated(text):
    assert continuation_candidate(text)[0] is False


def test_describing_a_conversation_is_not_a_request_to_keep_talking():
    text = "今のAIって話しててもAI感が強いんだよね"
    assert continuation_candidate(text) == (False, "")


@pytest.mark.parametrize("text", [
    "前にTRPGやったの覚えてる？",
    "TRPGって何？",
    "TRPGの話をしてたよ",
    "ラジオはどう思う？",
    "実況って難しいね",
])
def test_activity_mentions_and_history_do_not_start_a_directive(text):
    assert is_activity_start_request(text) is False
    assert continuation_candidate(text) == (False, "")


@pytest.mark.parametrize("text", [
    "TRPGやろう",
    "TRPGのGMやって",
    "ラジオトークして",
    "ゲーム実況して",
])
def test_explicit_activity_actions_still_start_a_directive(text):
    assert is_activity_start_request(text) is True
    assert continuation_candidate(text)[0] is True


@pytest.mark.parametrize("text", [
    "準備が終わるまで何か話して",
    "準備中は話してて",
    "ラジオみたいに話しててよ",
])
def test_explicit_keep_talking_requests_are_still_nominated(text):
    assert continuation_candidate(text)[0] is True


def test_backchannel_does_not_take_the_floor():
    assert is_backchannel("うん")
    assert is_backchannel("なるほど")
    assert not is_backchannel("なるほどね、それでどうなったの")


def test_kernel_marks_continuing_request_as_plan_candidate():
    kernel = ConversationKernel()
    frame = kernel.build(
        "ラジオトーク風に3分くらい話して", source="local", speaker_id="u1",
    )
    assert frame.plan_candidate is True
    assert frame.directive_action == str(DirectiveAction.CREATE)
    assert "一問一答" not in frame.response_goal


def test_kernel_leaves_one_shot_turns_alone():
    kernel = ConversationKernel()
    frame = kernel.build("日本の首都は？", source="local", speaker_id="u1")
    assert frame.plan_candidate is False
    assert frame.execution_mode == "ONE_SHOT_RESPONSE"
    assert frame.directive_action == "NONE"


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------


def test_radio_plan_parses_into_a_streamed_monologue():
    plan = parse_plan_proposal(RADIO_PLAN)
    assert plan.execution_mode is ExecutionMode.STREAMED_LONG_RESPONSE
    assert plan.interaction_mode is InteractionMode.MONOLOGUE
    assert plan.continuation_policy is ContinuationPolicy.UNTIL_DURATION_OR_INTERRUPTED
    assert plan.duration_hint_seconds == pytest.approx(180)
    assert plan.needs_user_input is False


def test_plan_cannot_grant_itself_search_permission():
    raw = json.dumps({
        "goal": "調べながら解説する",
        "execution_mode": "ONGOING_DIRECTIVE",
        "search_policy": "SEARCH_WHEN_NEEDED",
        "allowed_tools": ["web_search", "rm -rf", "shell"],
    })
    denied = parse_plan_proposal(raw, search_allowed=False)
    assert denied.search_policy is SearchPolicy.NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED
    assert denied.allowed_tools == ["web_search"]
    granted = parse_plan_proposal(raw, search_allowed=True)
    assert granted.search_policy is SearchPolicy.SEARCH_WHEN_NEEDED


def test_plan_duration_is_capped():
    raw = json.dumps({
        "goal": "ずっと話す", "execution_mode": "STREAMED_LONG_RESPONSE",
        "duration_hint_seconds": 99999,
    })
    plan = parse_plan_proposal(raw, max_duration_seconds=600)
    assert plan.duration_hint_seconds == pytest.approx(600)


def test_unparseable_plan_falls_back_to_one_shot():
    assert parse_plan_proposal("すみません、よくわかりません") is None


def test_observation_plan_always_waits_for_events():
    raw = json.dumps({
        "goal": "静かに見守る", "execution_mode": "OBSERVATION_DIRECTIVE",
        "needs_environment_events": False,
    })
    plan = parse_plan_proposal(raw)
    assert plan.needs_environment_events is True


# ---------------------------------------------------------------------------
# Segment validation
# ---------------------------------------------------------------------------


def test_prose_instead_of_json_is_spoken_rather_than_counted_as_failure():
    """A local model does not emit clean JSON every time.

    Two failures in a row used to end the directive — the radio show simply
    stopped mid-programme (`status=FAILED reason=max_consecutive_errors`).
    """
    action = parse_segment_action(
        "さて、都市伝説の話に戻るけど、あのノイズの正体って結局なんだったんだろうね。"
    )
    assert action is not None
    assert action.action is SegmentActionType.SPEAK
    assert "都市伝説" in action.spoken_content
    assert action.purpose == "prose_fallback"


def test_broken_json_is_not_read_out_loud():
    """Rescuing prose must not mean reading a half-written object aloud."""
    assert parse_segment_action('{"action": "SPEAK", "spoken_content": "途中で') is None
    assert parse_segment_action('"action":"SPEAK","topic":"x","purpose":"y"') is None


def test_non_speech_junk_is_still_a_failure():
    assert parse_segment_action("") is None
    assert parse_segment_action("```") is None
    assert parse_segment_action("null") is None


def test_a_prose_fallback_still_respects_the_length_cap():
    action = parse_segment_action("あ" * 900, max_chars=100)
    assert len(action.spoken_content) == 100


def test_valid_json_is_preferred_over_the_fallback():
    action = parse_segment_action(segment(spoken_content="本文", topic="話題"))
    assert action.purpose != "prose_fallback"
    assert action.topic == "話題"


def test_speaking_action_without_content_becomes_wait():
    action = parse_segment_action(segment(spoken_content=""))
    assert action.action is SegmentActionType.WAIT


def test_tool_request_outside_permissions_becomes_wait():
    action = parse_segment_action(
        json.dumps({"action": "USE_TOOL", "tool_request": {"tool": "web_search"}}),
        allowed_tools=["game_state_read_only"],
    )
    assert action.action is SegmentActionType.WAIT


def test_tool_request_within_permissions_is_kept():
    action = parse_segment_action(
        json.dumps({
            "action": "USE_TOOL",
            "tool_request": {"tool": "game_state_read_only", "query": "hp"},
        }),
        allowed_tools=["game_state_read_only"],
    )
    assert action.action is SegmentActionType.USE_TOOL
    assert action.tool_request["tool"] == "game_state_read_only"


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


def test_semantically_repeated_opening_is_detected():
    detector = SegmentLoopDetector()
    first = parse_segment_action(segment(
        spoken_content="今日はマイクラについて話そうと思うんだけど", topic="マイクラ",
    ))
    detector.record(first)
    again = parse_segment_action(segment(
        spoken_content="今日はマイクラについて話そうと思うんだよね", topic="マイクラ",
    ))
    assert detector.would_loop(again)


def test_a_genuinely_new_segment_is_not_a_loop():
    detector = SegmentLoopDetector()
    detector.record(parse_segment_action(segment(
        spoken_content="今日はマイクラについて話そうと思う", topic="マイクラ",
    )))
    fresh = parse_segment_action(segment(
        spoken_content="そういえば、赤石回路って結局は論理回路なんだよね", topic="回路",
    ))
    assert detector.would_loop(fresh) == ""


# ---------------------------------------------------------------------------
# Controller lifecycle
# ---------------------------------------------------------------------------


def test_directive_is_created_and_becomes_due():
    controller = make_controller()
    directive = start(controller)
    assert directive.status is DirectiveStatus.ACTIVE
    assert controller.due_directive("local") is directive


def test_human_floor_blocks_a_segment_without_cancelling_the_directive():
    controller = make_controller()
    directive = start(controller)
    assert controller.due_directive("local", floor_busy=True) is None
    assert directive.status is DirectiveStatus.ACTIVE


def test_tts_backpressure_blocks_generating_far_ahead():
    controller = make_controller()
    start(controller)
    assert controller.due_directive("local", queued_segments=2) is None
    assert controller.due_directive("local", playback_ahead_s=20.0) is None
    assert controller.due_directive("local", playback_ahead_s=1.0) is not None


def test_the_next_segment_is_written_while_the_current_one_plays():
    """Otherwise every gap between segments costs a full model round-trip.

    Our own audio being queued is backpressure, not a busy floor.  Only a human
    turn stops the next segment from being prepared.
    """
    controller = make_controller()
    directive = start(controller)
    # 6 s of our own speech still queued: comfortably inside the 15 s budget.
    assert controller.due_directive("local", playback_ahead_s=6.0) is directive
    # A human speaking is a different matter entirely.
    assert controller.due_directive(
        "local", floor_busy=True, playback_ahead_s=6.0,
    ) is None


def test_commentary_directive_waits_when_nothing_happened():
    controller = make_controller()
    directive = start(controller, COMMENTARY_PLAN, "ゲームが終わるまで実況して")
    assert controller.due_directive("local") is None
    assert directive.status is DirectiveStatus.WAITING_FOR_EVENT
    assert controller.note_environment_event("BOSS_DEFEATED: ボスを倒した", importance=.9)
    assert controller.due_directive("local") is directive


def test_low_importance_event_is_ignored_by_a_watch_directive():
    controller = make_controller()
    plan = parse_plan_proposal(json.dumps({
        "goal": "静かに見守る", "execution_mode": "OBSERVATION_DIRECTIVE",
    }))
    controller.start_directive(plan, conversation_id="local", user_text="静かに見守って")
    assert controller.note_environment_event("小さな物音", importance=.2) is False
    assert controller.metrics["commentary_events_ignored"] == 1


def test_duration_limit_completes_the_directive():
    controller = make_controller()
    directive = start(controller)
    directive.started_at -= 400  # past the 180s hint
    assert controller.due_directive("local") is None
    assert directive.status is DirectiveStatus.COMPLETED
    assert directive.finish_reason == "duration_reached"


def test_stop_request_cancels_and_a_revision_does_not():
    controller = make_controller()
    directive = start(controller)
    action, _relation, _reason, _item = controller.handle_user_turn("もう終わり")
    assert action is DirectiveAction.CANCEL
    assert directive.status is DirectiveStatus.CANCELLED

    controller = make_controller()
    directive = start(controller)
    action, relation, _reason, item = controller.handle_user_turn("ゲームの話を中心にして")
    assert action is DirectiveAction.REVISE
    assert item is directive
    assert directive.status is DirectiveStatus.ACTIVE


def test_revision_updates_in_place_and_invalidates_old_output():
    controller = make_controller()
    directive = start(controller)
    before = directive.state_version
    assert controller.apply_revision(directive, {"current_topic": "ゲーム"})
    assert directive.current_topic == "ゲーム"
    assert directive.state_version > before
    assert directive.accepts(state_version=before) is False
    assert controller.store.active("local") is directive  # not a second directive


def test_pause_then_resume_preserves_the_directive():
    controller = make_controller()
    directive = start(controller)
    controller.pause(directive, "human_speech")
    assert directive.status is DirectiveStatus.PAUSED
    assert controller.due_directive("local") is None
    controller.resume(directive)
    assert directive.status is DirectiveStatus.ACTIVE
    assert controller.due_directive("local") is directive


def test_restart_cancels_a_short_directive_but_keeps_it_in_history():
    controller = make_controller()
    directive = start(controller)
    assert controller.cancel_all_for_restart() == 1
    assert directive.status is DirectiveStatus.CANCELLED_BY_RESTART
    assert controller.store.active("local") is None


def test_restart_keeps_an_activity_bound_directive():
    controller = make_controller()
    directive = start(controller, COMMENTARY_PLAN, "ゲームが終わるまで実況して")
    assert directive.persist_across_restart is True
    assert controller.cancel_all_for_restart() == 0


# ---------------------------------------------------------------------------
# Segment execution
# ---------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def execute(controller, directive, raw, spoken=None):
    said: list[str] = []

    async def generate(_messages):
        return raw

    async def speak(text, _segment_id, _state_version):
        said.append(text)
        return True

    outcome = run(controller.execute_next_segment(
        directive, generate=generate, speak=speak,
    ))
    if spoken is not None:
        assert said == spoken
    return outcome, said


def test_segment_speaks_and_advances_progress():
    controller = make_controller()
    directive = start(controller)
    outcome, said = execute(controller, directive, segment(
        spoken_content="こんばんは、今日はこの話から。", topic="導入",
        progress_update="導入を終えた",
    ))
    assert outcome.spoke
    assert said == ["こんばんは、今日はこの話から。"]
    assert directive.completed_segment_count == 1
    assert directive.current_topic == "導入"
    assert directive.progress_summary == "導入を終えた"


def test_model_finish_completes_the_directive():
    controller = make_controller()
    directive = start(controller)
    directive.started_at -= 170          # past the 3-minute plan's 60% mark
    outcome, _said = execute(controller, directive, json.dumps({
        "action": "FINISH", "finish_reason": "自然な締めに到達",
    }))
    assert outcome.status == "finished"
    assert directive.status is DirectiveStatus.COMPLETED


# ---------------------------------------------------------------------------
# Length of a requested monologue
# ---------------------------------------------------------------------------


def test_wrapping_up_far_too_early_is_declined():
    """「3分くらい話して」 and stopping after 20 seconds is not a natural ending."""
    controller = make_controller()
    directive = start(controller)
    outcome, said = execute(controller, directive, json.dumps({
        "action": "FINISH", "finish_reason": "自然な締めに到達",
    }))
    assert outcome.status == "waited"
    assert outcome.reason.startswith("finish_deferred")
    assert directive.status is DirectiveStatus.ACTIVE
    assert directive.early_finish_count == 1
    assert "別の話題" in directive.next_action_hint
    assert said == []


def test_a_persistent_finish_is_eventually_honoured():
    """We ask for another topic, but we do not argue with it forever."""
    controller = make_controller()
    directive = start(controller)
    finish = json.dumps({"action": "FINISH", "finish_reason": "もう話すことがない"})
    for _ in range(controller.max_early_finishes):
        execute(controller, directive, finish)
    assert directive.status is DirectiveStatus.ACTIVE
    execute(controller, directive, finish)
    assert directive.status is DirectiveStatus.COMPLETED


def test_an_event_driven_directive_may_finish_whenever_it_likes():
    """「ゲームが終わるまで」 ends when the game does, not on a clock."""
    controller = make_controller()
    directive = start(controller, COMMENTARY_PLAN, "ゲームが終わるまで実況して")
    outcome, _said = execute(controller, directive, json.dumps({
        "action": "FINISH", "finish_reason": "ゲームが終わった",
    }))
    assert outcome.status == "finished"
    assert directive.status is DirectiveStatus.COMPLETED


def test_a_monologue_without_a_stated_length_still_gets_one():
    """Without a target the model wraps up after a couple of segments."""
    raw = json.dumps({
        "goal": "ラジオみたいに話す",
        "execution_mode": "STREAMED_LONG_RESPONSE",
        "duration_hint_seconds": None,
    })
    plan = parse_plan_proposal(raw, default_duration_seconds=300)
    assert plan.duration_hint_seconds == pytest.approx(300)


def test_an_event_driven_plan_keeps_its_open_ended_duration():
    plan = parse_plan_proposal(COMMENTARY_PLAN, default_duration_seconds=300)
    assert plan.duration_hint_seconds is None


def test_a_stated_length_is_never_overridden():
    plan = parse_plan_proposal(RADIO_PLAN, default_duration_seconds=999)
    assert plan.duration_hint_seconds == pytest.approx(180)


def test_stale_segment_is_discarded_not_spoken():
    controller = make_controller()
    directive = start(controller)
    said: list[str] = []

    async def generate(_messages):
        # The user interrupts while this segment is being written.
        controller.pause(directive, "human_speech")
        return segment(spoken_content="古い区間")

    async def speak(text, _segment_id, _state_version):
        said.append(text)
        return True

    outcome = run(controller.execute_next_segment(
        directive, generate=generate, speak=speak,
    ))
    assert outcome.status == "stale"
    assert said == []
    assert controller.metrics["segments_discarded_stale"] == 1


def test_playback_rejection_does_not_count_as_progress():
    controller = make_controller()
    directive = start(controller)

    async def generate(_messages):
        return segment(spoken_content="間に合わなかった区間")

    async def speak(_text, _segment_id, _state_version):
        return False

    outcome = run(controller.execute_next_segment(
        directive, generate=generate, speak=speak,
    ))
    assert outcome.status == "stale"
    assert directive.completed_segment_count == 0


def test_repeated_error_fails_the_directive():
    controller = make_controller()
    directive = start(controller)
    broken = '{"action": "SPEAK", "spoken_content": "途中で'
    for _ in range(controller.max_consecutive_errors):
        execute(controller, directive, broken)
    assert directive.status is DirectiveStatus.FAILED


def test_tool_result_returns_to_the_directive():
    controller = make_controller()
    plan = parse_plan_proposal(
        json.dumps({
            "goal": "調べながら解説する",
            "execution_mode": "ONGOING_DIRECTIVE",
            "search_policy": "SEARCH_WHEN_NEEDED",
            "allowed_tools": ["web_search"],
        }),
        search_allowed=True,
    )
    directive = controller.start_directive(
        plan, conversation_id="local", user_text="必要なことはWebで調べながら解説して",
    )

    async def generate(_messages):
        return json.dumps({
            "action": "USE_TOOL",
            "tool_request": {"tool": "web_search", "query": "最新の仕様"},
        })

    async def speak(_text, _segment_id, _state_version):
        return True

    async def run_tool(_request):
        return "検索結果の要約"

    outcome = run(controller.execute_next_segment(
        directive, generate=generate, speak=speak, run_tool=run_tool,
    ))
    assert outcome.status == "tool"
    # The directive is still the owner of the conversation after the tool ran.
    assert directive.status is DirectiveStatus.ACTIVE
    assert "検索結果の要約" in directive.progress_summary


def test_opening_reply_counts_as_the_first_segment():
    controller = make_controller()
    directive = start(controller)
    assert controller.note_opening_segment(directive, "じゃあ始めるね。今日の一本目は…")
    assert directive.completed_segment_count == 1
    # A near-identical follow-up must now be caught as a loop.
    again = parse_segment_action(segment(
        spoken_content="じゃあ始めるね。今日の一本目は…",
    ))
    assert controller._loops[directive.directive_id].would_loop(again)


def test_local_and_discord_reach_the_same_conclusion():
    kernel = ConversationKernel()
    text = "ラジオ風に少し話して"
    local = kernel.build(text, source="local", speaker_id="u1")
    discord = kernel.build(text, source="discord", speaker_id="u1", group=True)
    assert local.plan_candidate == discord.plan_candidate
    assert local.directive_action == discord.directive_action
    assert local.execution_mode == discord.execution_mode
