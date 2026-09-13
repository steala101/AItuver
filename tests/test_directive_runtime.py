"""DirectiveRuntime: the surface-neutral half of running a continuation.

Local voice and Discord share this object.  These tests drive it with two
different fake hosts to prove the two surfaces cannot drift apart in what they
decide, only in how they speak.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from neuro_voice.dialogue.continuation import ContinuationController
from neuro_voice.dialogue.directive import DirectiveStatus, parse_plan_proposal
from neuro_voice.dialogue.directive_runtime import DirectiveHost, DirectiveRuntime
from tests.test_behavior_directive import COMMENTARY_PLAN, RADIO_PLAN, FakeConfig


class FakeMind:
    """Only the surface the runtime actually depends on."""

    def __init__(self, cfg):
        self.continuation = ContinuationController(cfg)

    def directive_conversation_id(self, source="local"):
        return f"{source}:user"

    def directive_plan_context(self, _source="local"):
        return {"interests": ["音の出る仕組み"], "activity_summary": "", "affect_summary": ""}

    def directive_segment_context(self, _source="local"):
        return {"interests": ["音の出る仕組み"], "affect_summary": "", "memory_notes": []}


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.cancelled = []

    def generate(self, _messages):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else "{}"

        async def _stream():
            yield reply

        return _stream()

    def cancel_request(self, response_id):
        self.cancelled.append(response_id)


class FakeSurface:
    """A stand-in for either the local pipeline or the Discord bridge."""

    def __init__(self, *, name, ready=True):
        self.name = name
        self.spoken: list[str] = []
        self.busy = False
        self.ready_flag = ready
        self.ahead = 0.0
        self.ticks: list[float] = []
        self.discarded = 0
        self.cancelled: list[str] = []
        self.user_stops: list[str] = []
        self.speak_result = True

    async def speak(self, text, _segment_id, _state_version):
        if self.busy:
            return False
        self.spoken.append(text)
        return self.speak_result

    def host(self, **overrides):
        base = dict(
            speak=self.speak,
            floor_busy=lambda: self.busy,
            playback_ahead_s=lambda: self.ahead,
            request_tick=self.ticks.append,
            ready=lambda: self.ready_flag,
            conversation_tail=lambda: [f"相手: こんばんは"],
            environment_available=lambda: False,
            cancel_generation=self.cancelled.append,
            discard_pending_audio=self._discard,
            search_enabled=lambda: False,
            on_user_stop=self.user_stops.append,
        )
        base.update(overrides)
        return DirectiveHost(**base)

    def _discard(self):
        self.discarded += 1


def build(source="local", replies=(), ready=True, cfg=None, **host_overrides):
    cfg = cfg or FakeConfig()
    mind = FakeMind(cfg)
    llm = FakeLLM(replies)
    surface = FakeSurface(name=source, ready=ready)
    runtime = DirectiveRuntime(
        cfg, mind=mind, llm=llm, source=source,
        host=surface.host(**host_overrides), persona_name=lambda: "ポッポ",
    )
    return runtime, mind, llm, surface


def segment(**fields):
    payload = {"action": "SPEAK", "spoken_content": "本文", "topic": "話題"}
    payload.update(fields)
    return json.dumps(payload)


CANDIDATE_FRAME = {"plan_candidate": True, "turn_id": "t1", "search_decision": {}}
#: Nominated as continuing, but its shape is not obvious — the model decides.
AMBIGUOUS = "必要そうなことを調べながら進めて"
ONE_SHOT_FRAME = {"plan_candidate": False, "turn_id": "t1", "search_decision": {}}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def test_continuing_request_creates_a_directive():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    assert directive is not None
    assert directive.status is DirectiveStatus.ACTIVE
    # A request this explicit is read directly; no model round-trip is spent.
    assert llm.calls == 0


def test_an_ambiguous_request_still_asks_the_model():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn(AMBIGUOUS, CANDIDATE_FRAME))
    assert directive is not None
    assert llm.calls == 1


# ---------------------------------------------------------------------------
# Interpreting must not delay the reply
# ---------------------------------------------------------------------------


def test_planning_does_not_block_the_reply():
    """Regression: 「ラジオやって」 took 14 seconds to produce its first word.

    Interpreting the request costs a model round-trip.  Doing it before the
    reply put that whole latency in front of the person.
    """
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])

    async def drive():
        # The instant part returns without having called the model at all.
        immediate = runtime.begin_user_turn(AMBIGUOUS, CANDIDATE_FRAME)
        assert immediate is None
        assert llm.calls == 0
        # ... the reply happens here in real life ...
        return await runtime.settle(timeout=5.0)

    directive = run(drive())
    assert directive is not None
    assert llm.calls == 1


def test_a_directive_created_mid_conversation_inherits_the_subject():
    """Regression: 「一緒にやろう」 in the middle of a story started an unrelated
    brainstorm and the story was simply abandoned."""
    cfg = FakeConfig()
    mind = FakeMind(cfg)
    mind.directive_plan_context = lambda _source="local": {
        "interests": [], "activity_summary": "", "affect_summary": "",
        "active_theme": "灰色の平原と石像の物語",
    }
    surface = FakeSurface(name="local")
    runtime = DirectiveRuntime(
        cfg, mind=mind, llm=FakeLLM([]), source="local",
        host=surface.host(), persona_name=lambda: "ポッポ",
    )
    directive = runtime.begin_user_turn("一緒に考えよう", CANDIDATE_FRAME)
    assert directive is not None
    assert directive.current_topic == "灰色の平原と石像の物語"
    assert any("灰色の平原" in step for step in directive.plan_steps)


def test_an_explicit_request_needs_no_background_work_at_all():
    """「3分ラジオ」 is understood before the reply is even generated."""
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    directive = runtime.begin_user_turn("ちょっと3分間ぐらいでラジオトークやって。", CANDIDATE_FRAME)
    assert directive is not None
    assert directive.duration_hint_seconds == 180
    assert llm.calls == 0


def test_a_one_shot_turn_starts_no_background_work():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])

    async def drive():
        assert runtime.begin_user_turn("日本の首都は？", ONE_SHOT_FRAME) is None
        return await runtime.settle(timeout=1.0)

    assert run(drive()) is None
    assert llm.calls == 0


def test_a_slow_plan_does_not_hold_up_the_turn():
    """If interpretation is still running, the segment loop starts it later."""
    cfg = FakeConfig()
    mind = FakeMind(cfg)
    surface = FakeSurface(name="local")

    class SlowLLM(FakeLLM):
        def generate(self, _messages):
            self.calls += 1

            async def _stream():
                await asyncio.sleep(0.5)
                yield RADIO_PLAN

            return _stream()

    runtime = DirectiveRuntime(
        cfg, mind=mind, llm=SlowLLM([]), source="local",
        host=surface.host(), persona_name=lambda: "ポッポ",
    )

    async def drive():
        runtime.begin_user_turn(AMBIGUOUS, CANDIDATE_FRAME)
        return await runtime.settle(timeout=0.05)

    assert run(drive()) is None


def test_a_stop_request_is_instant_and_needs_no_model():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    before = llm.calls
    assert runtime.begin_user_turn("もう終わり", ONE_SHOT_FRAME) is None
    assert directive.status is DirectiveStatus.CANCELLED
    assert llm.calls == before


def test_a_revision_is_applied_in_the_background():
    revision = json.dumps({"current_topic": "ゲーム"})
    runtime, _mind, _llm, _surface = build(replies=[revision])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))

    async def drive():
        same = runtime.begin_user_turn("ゲームの話を中心にして", ONE_SHOT_FRAME)
        assert same is directive
        return await runtime.settle(timeout=5.0)

    run(drive())
    assert directive.current_topic == "ゲーム"
    assert directive.status is DirectiveStatus.ACTIVE


def test_one_shot_turn_spends_no_extra_llm_call():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    assert run(runtime.handle_user_turn("日本の首都は？", ONE_SHOT_FRAME)) is None
    assert llm.calls == 0


def test_unparseable_plan_falls_back_without_a_directive():
    runtime, _mind, _llm, _surface = build(replies=["すみません、よくわかりません"])
    assert run(runtime.handle_user_turn("しばらく話して", CANDIDATE_FRAME)) is None
    assert runtime.active() is None


def test_one_shot_plan_does_not_create_a_directive():
    plan = json.dumps({"goal": "答える", "execution_mode": "ONE_SHOT_RESPONSE"})
    runtime, _mind, _llm, _surface = build(replies=[plan])
    assert run(runtime.handle_user_turn("しばらく話して", CANDIDATE_FRAME)) is None


def test_intent_to_plan_can_be_disabled_by_flag():
    cfg = FakeConfig(**{"behavior_directive.intent_to_plan_enabled": False})
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN], cfg=cfg)
    assert run(runtime.handle_user_turn("しばらく話して", CANDIDATE_FRAME)) is None
    assert llm.calls == 0


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def test_tick_speaks_a_segment_and_schedules_the_next():
    runtime, _mind, llm, surface = build(
        replies=[segment(spoken_content="一本目の話をするね")],
    )
    run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))

    async def drive():
        assert await runtime.tick() is True
        await runtime._task

    run(drive())
    assert surface.spoken == ["一本目の話をするね"]
    assert surface.ticks  # the next evaluation was scheduled


def test_tick_does_nothing_without_a_directive():
    runtime, _mind, _llm, surface = build()
    assert run(runtime.tick()) is False
    assert surface.spoken == []


def test_busy_floor_defers_the_segment_and_keeps_the_directive():
    runtime, _mind, _llm, surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    surface.busy = True
    assert run(runtime.tick()) is True     # still owns the turn
    assert surface.spoken == []
    assert directive.status is DirectiveStatus.ACTIVE


def test_unready_surface_never_speaks():
    """An empty Discord channel, or a suspended local pipeline."""
    runtime, _mind, _llm, surface = build(replies=[RADIO_PLAN], ready=True)
    run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    surface.ready_flag = False
    assert run(runtime.tick()) is True     # directive alive, but silent
    assert surface.spoken == []


def test_backpressure_defers_when_audio_is_queued_far_ahead():
    runtime, _mind, _llm, surface = build(replies=[RADIO_PLAN])
    run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    surface.ahead = 30.0
    assert run(runtime.tick()) is True
    assert surface.spoken == []


def test_rejected_playback_is_not_counted_as_progress():
    runtime, _mind, _llm, surface = build(
        replies=[segment(spoken_content="遅れた区間")],
    )
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    surface.speak_result = False

    async def drive():
        await runtime.tick()
        await runtime._task

    run(drive())
    assert directive.completed_segment_count == 0


# ---------------------------------------------------------------------------
# Interruption and teardown
# ---------------------------------------------------------------------------


def test_human_speech_pauses_and_discards_pending_audio():
    runtime, _mind, _llm, surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    before = directive.state_version
    runtime.pause_for_human()
    assert directive.status is DirectiveStatus.PAUSED
    assert surface.discarded == 1
    assert surface.cancelled == [f"directive:{directive.directive_id}"]
    # Anything generated under the old version can no longer be spoken.
    assert directive.accepts(state_version=before) is False


def test_answering_the_human_resumes_rather_than_restarts():
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    runtime.pause_for_human()
    same = run(runtime.handle_user_turn("へえ、それで？", ONE_SHOT_FRAME))
    assert same is directive
    assert directive.status is DirectiveStatus.ACTIVE
    assert llm.calls == 0  # neither a plan nor a revision was needed


def test_revision_updates_the_running_directive():
    revision = json.dumps({"current_topic": "ゲーム", "style_constraints": ["ゆっくり"]})
    runtime, _mind, _llm, _surface = build(replies=[revision])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    same = run(runtime.handle_user_turn("ゲームの話を中心にして", ONE_SHOT_FRAME))
    assert same is directive
    assert directive.current_topic == "ゲーム"
    assert directive.style_constraints == ["ゆっくり"]
    assert directive.status is DirectiveStatus.ACTIVE


def test_stop_request_cancels_through_the_runtime():
    runtime, _mind, _llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    assert run(runtime.handle_user_turn("もう終わり", ONE_SHOT_FRAME)) is None
    assert directive.status is DirectiveStatus.CANCELLED
    assert runtime.active() is None


@pytest.mark.parametrize("source", ["local", "discord"])
@pytest.mark.parametrize("stop_text", [
    "よし、ラジオは一旦終わろう。",
    "いや、もうラジオは終わったよ。",
])
def test_real_radio_stop_phrases_cancel_every_surface_artifact(source, stop_text):
    runtime, _mind, _llm, surface = build(source=source, replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    assert runtime.begin_user_turn(stop_text, CANDIDATE_FRAME) is None
    assert directive.status is DirectiveStatus.CANCELLED
    assert runtime.active() is None
    assert surface.discarded == 1
    assert surface.cancelled == [f"directive:{directive.directive_id}"]
    assert surface.user_stops == [stop_text]


def test_completed_radio_stop_phrase_does_not_create_a_new_directive():
    runtime, _mind, llm, surface = build(replies=[RADIO_PLAN])
    assert runtime.begin_user_turn(
        "いや、もうラジオは終わったよ。", CANDIDATE_FRAME,
    ) is None
    assert runtime.active() is None
    assert llm.calls == 0
    assert surface.user_stops == ["いや、もうラジオは終わったよ。"]


def test_stop_cancels_a_plan_that_is_still_being_interpreted():
    cfg = FakeConfig()
    mind = FakeMind(cfg)
    surface = FakeSurface(name="local")

    class SlowLLM(FakeLLM):
        def generate(self, _messages):
            self.calls += 1

            async def _stream():
                await asyncio.sleep(0.2)
                yield RADIO_PLAN

            return _stream()

    runtime = DirectiveRuntime(
        cfg, mind=mind, llm=SlowLLM([]), source="local",
        host=surface.host(), persona_name=lambda: "ポッポ",
    )

    async def drive():
        runtime.begin_user_turn(AMBIGUOUS, CANDIDATE_FRAME)
        await asyncio.sleep(0)
        runtime.begin_user_turn("もうラジオは終わったよ", CANDIDATE_FRAME)
        await asyncio.sleep(0.25)

    run(drive())
    assert runtime.active() is None
    assert runtime._pending is None
    assert surface.user_stops == ["もうラジオは終わったよ"]


def test_leaving_the_session_stops_the_directive():
    runtime, _mind, _llm, _surface = build(source="discord", replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    runtime.stop("voice_session_ended")
    assert directive.status is DirectiveStatus.CANCELLED
    assert directive.finish_reason == "voice_session_ended"
    assert runtime.active() is None


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------


def test_opening_turn_is_told_not_to_close_the_topic():
    runtime, _mind, _llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    block = runtime.prompt_block(directive)
    assert "最初の区間" in block
    assert "一問一答で締めず" in block


def test_prompt_block_disappears_once_the_directive_ends():
    runtime, _mind, _llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    runtime.stop("done")
    assert runtime.prompt_block(directive) == ""


def test_later_segments_are_not_told_it_is_the_opening():
    runtime, _mind, _llm, _surface = build(replies=[RADIO_PLAN])
    directive = run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))
    runtime.note_opening_segment(directive, "じゃあ始めるね")
    assert "最初の区間" not in runtime.prompt_block(directive)


# ---------------------------------------------------------------------------
# Environment events
# ---------------------------------------------------------------------------


def test_environment_event_wakes_a_commentary_directive():
    runtime, _mind, _llm, surface = build(
        replies=[COMMENTARY_PLAN], environment_available=lambda: True,
    )
    directive = run(runtime.handle_user_turn("ゲームが終わるまで実況して", CANDIDATE_FRAME))
    assert run(runtime.tick()) is True
    assert surface.spoken == []
    assert directive.status is DirectiveStatus.WAITING_FOR_EVENT
    assert runtime.note_environment_event("BOSS_DEFEATED: ボスを倒した", importance=.9)
    assert surface.ticks  # a tick was requested for the event


def test_environment_event_is_ignored_without_a_watching_directive():
    runtime, _mind, _llm, _surface = build()
    assert runtime.note_environment_event("何か起きた", importance=.9) is False


# ---------------------------------------------------------------------------
# Local / Discord parity
# ---------------------------------------------------------------------------


def test_both_surfaces_build_the_same_directive_from_one_request():
    text = "ラジオ風に3分くらい話して"
    local, _lm, _ll, local_surface = build(source="local", replies=[RADIO_PLAN])
    discord, _dm, _dl, discord_surface = build(source="discord", replies=[RADIO_PLAN])
    a = run(local.handle_user_turn(text, CANDIDATE_FRAME))
    b = run(discord.handle_user_turn(text, CANDIDATE_FRAME))
    assert a.execution_mode is b.execution_mode
    assert a.interaction_mode is b.interaction_mode
    assert a.continuation_policy is b.continuation_policy
    assert a.duration_hint_seconds == b.duration_hint_seconds
    assert a.search_policy is b.search_policy
    # Only the audience differs, which is the one surface-specific field.
    assert (a.audience, b.audience) == ("local", "discord")
    assert local_surface.spoken == discord_surface.spoken == []


def test_each_surface_keeps_its_own_conversation_id():
    local, _lm, _ll, _ls = build(source="local", replies=[RADIO_PLAN])
    discord, _dm, _dl, _ds = build(source="discord", replies=[RADIO_PLAN])
    assert local.conversation_id != discord.conversation_id


def test_directive_runs_even_when_spontaneous_talk_is_off():
    """An explicitly requested continuation is not unsolicited speech."""
    runtime, _mind, _llm, surface = build(
        replies=[segment(spoken_content="続きの話")],
    )
    run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME))

    async def drive():
        await runtime.tick()
        await runtime._task

    run(drive())
    # The runtime never consults the autonomy suppressors at all.
    assert surface.spoken == ["続きの話"]


def test_disabled_feature_flag_turns_the_whole_path_off():
    cfg = FakeConfig(**{"behavior_directive.enabled": False})
    runtime, _mind, llm, _surface = build(replies=[RADIO_PLAN], cfg=cfg)
    assert runtime.enabled is False
    assert run(runtime.handle_user_turn("ラジオ風に3分話して", CANDIDATE_FRAME)) is None
    assert run(runtime.tick()) is False
    assert llm.calls == 0
