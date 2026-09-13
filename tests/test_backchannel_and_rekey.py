"""Two failures observed during one TRPG session (2026-07-26 23:07-23:16).

**A backchannel killed the reading.**  ``_on_speech_start`` runs at VAD onset,
before STT, and it discarded every synthesized chunk and cancelled generation.
By the time 「うんうん」 was classified as a backchannel and
``resume_from_pause`` was called there was nothing left to resume, and the
directive stayed PAUSED because a backchannel never reaches the reply path.

**The story was orphaned.**  ``conversation_id`` comes from the speaker, and
voiceprint matching finishes ~370ms *after* the first directive is created:

    23:07:24,193  Directive created 70e74988d1c0 interaction=NARRATION
    23:07:24,561  話者照合: チビ に類似度 0.97
    23:08:02,786  Directive created 9ab77062aa28 interaction=DIALOGUE

The NARRATION directive was filed under ``local:user``; every later turn looked
under ``speaker:1``, found nothing, and built a second directive.  The first
one never reached a terminal state and never appeared in the log again.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.continuation import ContinuationController
from neuro_voice.dialogue.directive import DirectiveStatus
from tests.test_behavior_directive import FakeConfig, RADIO_PLAN
from tests.test_directive_runtime import CANDIDATE_FRAME, build, run


class MovingMind:
    """A Mind whose conversation key changes when the speaker resolves."""

    def __init__(self, cfg):
        self.continuation = ContinuationController(cfg)
        self.key = "local:user"

    def directive_conversation_id(self, _source="local"):
        return self.key

    def directive_plan_context(self, _source="local"):
        return {"interests": [], "activity_summary": "", "affect_summary": ""}

    def directive_segment_context(self, _source="local"):
        return {"interests": [], "affect_summary": "", "memory_notes": []}

    def identify(self, new_key):
        previous, self.key = self.key, new_key
        return self.continuation.rekey(previous, new_key)


def radio():
    runtime, mind, llm, surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    assert directive is not None
    return runtime, mind, llm, surface, directive


# ---------------------------------------------------------------------------
# A backchannel must not destroy what is already synthesized
# ---------------------------------------------------------------------------


def test_vad_onset_keeps_the_audio_already_made():
    runtime, _mind, _llm, surface, _directive = radio()
    runtime.pause_for_human(discard_audio=False)
    assert surface.discarded == 0
    assert surface.cancelled == []


def test_vad_onset_still_stops_producing_more():
    runtime, mind, _llm, _surface, directive = radio()
    runtime.pause_for_human(discard_audio=False)
    assert directive.status is DirectiveStatus.PAUSED
    assert mind.continuation.due_directive(
        "local:user", floor_busy=False, has_environment_event=False,
    ) is None


def test_a_confirmed_interruption_does_discard():
    runtime, _mind, _llm, surface, directive = radio()
    runtime.pause_for_human(discard_audio=False)
    runtime.pause_for_human("human_barge_in", discard_audio=True)
    assert surface.discarded == 1
    assert surface.cancelled == [f"directive:{directive.directive_id}"]


def test_a_backchannel_hands_the_floor_straight_back():
    runtime, _mind, _llm, _surface, directive = radio()
    runtime.pause_for_human(discard_audio=False)
    assert runtime.resume_after_backchannel() is directive
    assert directive.status is DirectiveStatus.ACTIVE


def test_the_show_can_continue_after_a_backchannel():
    """Regression: one 「うん」 ended the radio show for good."""
    runtime, mind, _llm, _surface, directive = radio()
    runtime.pause_for_human(discard_audio=False)
    runtime.resume_after_backchannel()
    assert mind.continuation.due_directive(
        "local:user", floor_busy=False, has_environment_event=False,
    ) is directive


def test_resuming_a_finished_directive_is_refused():
    runtime, mind, _llm, _surface, directive = radio()
    mind.continuation.cancel(directive, "user_stop_request")
    assert runtime.resume_after_backchannel() is None


def test_pausing_without_a_directive_is_harmless():
    runtime, _mind, _llm, surface = build()
    runtime.pause_for_human(discard_audio=False)
    runtime.pause_for_human(discard_audio=True)
    assert surface.discarded == 0


# ---------------------------------------------------------------------------
# Following the person when their identity resolves
# ---------------------------------------------------------------------------


def moving():
    cfg = FakeConfig()
    mind = MovingMind(cfg)
    from neuro_voice.dialogue.directive_runtime import DirectiveRuntime
    from tests.test_directive_runtime import FakeLLM, FakeSurface

    surface = FakeSurface(name="local")
    runtime = DirectiveRuntime(
        cfg, mind=mind, llm=FakeLLM([RADIO_PLAN]), source="local",
        host=surface.host(), persona_name=lambda: "ポッポ",
    )
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    assert directive is not None
    return runtime, mind, directive


def test_the_directive_starts_under_the_provisional_key():
    _runtime, mind, directive = moving()
    assert directive.conversation_id == "local:user"


def test_the_session_survives_the_speaker_becoming_known():
    runtime, mind, directive = moving()
    assert mind.identify("speaker:1") is directive
    assert directive.conversation_id == "speaker:1"
    assert runtime.active() is directive


def test_without_the_move_the_session_would_be_unreachable():
    """This is the exact shape of the orphaned TRPG."""
    _runtime, mind, directive = moving()
    mind.key = "speaker:1"                      # identity resolved, no rekey
    assert mind.continuation.active("speaker:1") is None
    assert mind.continuation.active("local:user") is directive
    assert not directive.status.is_terminal     # alive, and unreachable


def test_the_directive_is_not_left_behind_at_the_old_key():
    _runtime, mind, _directive = moving()
    mind.identify("speaker:1")
    assert mind.continuation.active("local:user") is None


def test_an_unchanged_key_is_not_moved():
    _runtime, mind, directive = moving()
    assert mind.continuation.rekey("local:user", "local:user") is None
    assert mind.continuation.active("local:user") is directive


def test_a_newer_session_at_the_destination_wins():
    """Never resurrect an older directive over one already running there."""
    from neuro_voice.dialogue.directive import parse_plan_proposal

    _runtime, mind, first = moving()
    second = mind.continuation.start_directive(
        parse_plan_proposal(RADIO_PLAN), conversation_id="speaker:1",
        user_text="ラジオ風に話して",
    )
    assert mind.continuation.rekey("local:user", "speaker:1") is None
    assert mind.continuation.active("speaker:1") is second


def test_a_finished_directive_is_not_moved():
    _runtime, mind, directive = moving()
    mind.continuation.cancel(directive, "user_stop_request")
    assert mind.continuation.rekey("local:user", "speaker:1") is None


def test_moving_counts_as_an_observable_event():
    _runtime, mind, _directive = moving()
    mind.identify("speaker:1")
    assert mind.continuation.metrics["directive_rekeys"] == 1
