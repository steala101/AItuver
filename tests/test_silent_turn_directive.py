"""A turn answered with silence is still a turn.

`TurnClosurePolicy` may decide that a short 「うん」 needs no reply.  That
decision is made before the LLM and, on both surfaces, returns from the
speech handler early — before the directive layer is reached.

For a turn-based session that early return is fatal: the game master hands the
floor over with ``WAITING_FOR_USER``, the player answers 「うん」, the turn is
silenced, and nothing ever clears the wait.  The story stops with no error and
no log line saying why.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from neuro_voice.dialogue.directive import DirectiveStatus, InteractionMode
from neuro_voice.dialogue.events import ConversationEvent, ConversationEventType
from neuro_voice.dialogue.state import ConversationState
from neuro_voice.dialogue.turn_closure import TurnClosurePolicy, TurnDisposition
from neuro_voice.mind.mind import Mind
from tests.test_behavior_directive import FakeConfig, RADIO_PLAN
from tests.test_directive_runtime import CANDIDATE_FRAME, build, run

NARRATION_PLAN = json.dumps({
    "understanding": "TRPGのゲームマスターとして物語を進めてほしい",
    "goal": "プレイヤーの選択に応じて場面を語り、手番を渡す",
    "execution_mode": "COLLABORATIVE_SESSION",
    "interaction_mode": "NARRATION",
    "continuation_policy": "UNTIL_USER_STOPS",
    "segment_target_seconds": 25,
    "needs_user_input": True,
    "needs_environment_events": False,
    "allowed_tools": [],
    "search_policy": "NO_SEARCH",
    "stop_conditions": ["ユーザーが終わりにする"],
    "success_criteria": ["筋の通った場面を積み重ねる"],
    "initial_plan": ["導入の場面", "最初の選択"],
})


class FakeAddressing:
    explicit_wake_word = False


def state_after_assistant(**fields) -> ConversationState:
    state = ConversationState(
        previous_speaker="assistant",
        last_assistant_text="扉が軋みながら開いた。中は真っ暗だ。",
        conversation_mode="dialogue",
        participants={"local:mic": "チビ"},
        voice_human_count=1,
    )
    for key, value in fields.items():
        setattr(state, key, value)
    return state


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


def test_an_ordinary_acknowledgement_still_closes_the_exchange():
    """The feature Codex added must keep working."""
    policy = TurnClosurePolicy(FakeConfig())
    decision = policy.decide("なるほど", FakeAddressing(), state_after_assistant())
    assert decision.disposition is TurnDisposition.SILENCE


def test_a_session_waiting_for_the_player_is_never_silenced():
    policy = TurnClosurePolicy(FakeConfig())
    decision = policy.decide(
        "うん", FakeAddressing(),
        state_after_assistant(directive_waiting_for_user=True),
    )
    assert decision.disposition is TurnDisposition.CONTINUE
    assert decision.reason == "directive_awaiting_user_move"


def test_the_flag_arrives_from_the_surface_metadata():
    state = ConversationState()
    state.update(ConversationEvent(
        ConversationEventType.SPEECH_FINAL,
        source="local",
        text="うん",
        metadata={
            "speaker_key": "local:mic",
            "speaker_name": "チビ",
            "directive_waiting_for_user": True,
        },
    ))
    assert state.directive_waiting_for_user is True
    assert state.snapshot()["directive_waiting_for_user"] is True


def test_the_flag_defaults_to_false():
    state = ConversationState()
    state.update(ConversationEvent(
        ConversationEventType.SPEECH_FINAL,
        source="local", text="うん",
        metadata={"speaker_key": "local:mic", "speaker_name": "チビ"},
    ))
    assert state.directive_waiting_for_user is False


class _Continuation:
    def __init__(self, directive):
        self.directive = directive

    def active(self, _key):
        return self.directive


def _mind_waiting_for(directive):
    mind = object.__new__(Mind)
    mind._continuation = _Continuation(directive)
    mind._dialogue_user_key = lambda _source="local": "local:user"
    return mind.directive_waiting_for_user("local")


def test_generic_dialogue_directive_cannot_bypass_turn_closure():
    directive = SimpleNamespace(
        status=DirectiveStatus.WAITING_FOR_USER,
        interaction_mode=InteractionMode.DIALOGUE,
        needs_user_input=True,
    )
    assert _mind_waiting_for(directive) is False


@pytest.mark.parametrize(
    "mode", [InteractionMode.NARRATION, InteractionMode.CO_THINKING],
)
def test_real_turn_based_directive_can_bypass_turn_closure(mode):
    directive = SimpleNamespace(
        status=DirectiveStatus.WAITING_FOR_USER,
        interaction_mode=mode,
        needs_user_input=True,
    )
    assert _mind_waiting_for(directive) is True


# ---------------------------------------------------------------------------
# The runtime path taken when nothing is spoken
# ---------------------------------------------------------------------------


def waiting_narration(source="local"):
    runtime, mind, llm, surface = build(source=source, replies=[NARRATION_PLAN])
    directive = run(runtime.handle_user_turn("TRPGのGMをやって", CANDIDATE_FRAME))
    assert directive is not None
    mind.continuation.note_reply(directive, "扉が軋みながら開いた。どうする？")
    return runtime, mind, llm, surface, directive


def test_a_narration_directive_waits_after_its_move():
    _runtime, _mind, _llm, _surface, directive = waiting_narration()
    assert directive.interaction_mode is InteractionMode.NARRATION
    assert directive.status is DirectiveStatus.WAITING_FOR_USER


def test_a_silenced_answer_hands_the_floor_back():
    """The regression: without this the session waits for ever."""
    runtime, _mind, _llm, _surface, directive = waiting_narration()
    runtime.note_suppressed_user_turn("うん")
    assert directive.status is DirectiveStatus.ACTIVE


def test_the_stalled_session_can_act_again():
    runtime, mind, _llm, _surface, directive = waiting_narration()
    assert mind.continuation.due_directive(
        "local:user", floor_busy=False, has_environment_event=False,
    ) is None
    runtime.note_suppressed_user_turn("うん")
    assert mind.continuation.due_directive(
        "local:user", floor_busy=False, has_environment_event=False,
    ) is directive


def test_a_silenced_turn_never_creates_a_directive():
    """An acknowledgement is not a request for a radio show."""
    runtime, mind, llm, _surface = build(replies=[RADIO_PLAN])
    assert runtime.note_suppressed_user_turn("なるほど") is None
    assert mind.continuation.active("local:user") is None
    assert llm.calls == 0


def test_a_silenced_turn_does_not_revise_the_plan():
    runtime, _mind, llm, _surface, directive = waiting_narration()
    before = llm.calls
    runtime.note_suppressed_user_turn("うん")
    assert llm.calls == before
    assert directive.status is DirectiveStatus.ACTIVE


def test_a_short_stop_request_still_stops():
    """「もういいよ」 is short enough to look like feedback."""
    runtime, mind, _llm, surface, directive = waiting_narration()
    runtime.note_suppressed_user_turn("もういいよ")
    assert directive.status.is_terminal
    assert mind.continuation.active("local:user") is None
    assert surface.user_stops == ["もういいよ"]


def test_a_paused_directive_resumes_on_a_silenced_turn():
    runtime, mind, _llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    mind.continuation.pause(directive, "human_speech")
    assert directive.status is DirectiveStatus.PAUSED
    runtime.note_suppressed_user_turn("うんうん")
    assert directive.status is DirectiveStatus.ACTIVE


# ---------------------------------------------------------------------------
# Both surfaces, one meaning (第19条)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["local", "discord"])
def test_both_surfaces_clear_the_wait_identically(source):
    runtime, _mind, _llm, _surface, directive = waiting_narration(source=source)
    runtime.note_suppressed_user_turn("うん")
    assert directive.status is DirectiveStatus.ACTIVE


@pytest.mark.parametrize("source", ["local", "discord"])
def test_both_surfaces_stop_identically(source):
    runtime, mind, _llm, _surface, _directive = waiting_narration(source=source)
    runtime.note_suppressed_user_turn("もう終わりにしよう")
    assert mind.continuation.active(f"{source}:user") is None
