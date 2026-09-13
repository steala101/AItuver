"""Cognitive outcome completion keeps one authoritative payload per turn."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.cognition.types import ActionDecision, ActionType
from neuro_voice.utils.latency import TurnMetrics


class _Turns:
    def __init__(self, speech_requests: int = 1, logical_sessions: int = 1) -> None:
        self.speech_requests = speech_requests
        self.logical_sessions = logical_sessions

    def delivery_snapshot(self, _metrics):
        return {
            "speech_request": {"accepted": self.speech_requests},
            "logical_playback_session_count": self.logical_sessions,
            "tts_job": {},
            "playback": {},
            "playback_segments": ([{"completed": True, "started_at": 1.0}]
                                  if self.logical_sessions else []),
            "first_duplicate_stage": "none",
        }

    def note_text(self, *_args, **_kwargs):
        pass


class _PendingPlaybackTurns(_Turns):
    def playback_finalized(self, _metrics):
        return False


class _BrokenPlaybackTurns(_Turns):
    def playback_finalized(self, _metrics):
        raise RuntimeError("finalizer failure")


class _Mind:
    def __init__(self) -> None:
        self.observed: list[object] = []

    def cognitive_state(self, **_kwargs):
        return SimpleNamespace(
            unresolved_obligations=(), end_signal=0.0, current_topic="",
        )

    def observe_internal_outcome(self, **kwargs):
        self.observed.append(kwargs["outcome"])
        return []

    def internal_state_status(self):
        return {"appraisal": ""}


class _Writer:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.traces: list[CognitiveTrace] = []

    def emit(self, trace):
        if self.raises:
            raise OSError("test trace output failure")
        self.traces.append(trace)
        return True


def _pipeline(*, decision: ActionDecision, trace: CognitiveTrace,
              speech_requests: int = 1, writer: _Writer | None = None):
    from neuro_voice.pipeline import VoicePipeline

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._cognitive_decision = decision
    pipeline._cognitive_trace = trace
    pipeline._mind = _Mind()
    pipeline._last_interrupted_text = ""
    pipeline._turns = _Turns(speech_requests)
    pipeline._on_event = None
    pipeline._trace_writer = lambda: writer or _Writer()
    pipeline._trace_closure_tasks = set()
    pipeline.flush_world_state = lambda: None
    return pipeline


def _decision(action: ActionType = ActionType.ANSWER) -> ActionDecision:
    return ActionDecision(
        selected_action=action, action_id="decision-1", turn_id="turn-1",
        decision_reason=("user_is_ending" if action is ActionType.REMAIN_SILENT
                         else "addressed"),
    )


def _metrics() -> TurnMetrics:
    return TurnMetrics(turn_id="turn-1")


def test_shutdown_cancellation_emits_one_partial_delivery_trace():
    """Normal shutdown must not discard a queued streaming-delivery trace."""
    from neuro_voice.pipeline import VoicePipeline

    async def scenario() -> None:
        writer = _Writer()
        pipeline = _pipeline(
            decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
        )
        pipeline._turns = _PendingPlaybackTurns()
        trace = pipeline._cognitive_trace
        VoicePipeline._emit_trace_after_playback(pipeline, trace, _metrics())
        task = next(iter(pipeline._trace_closure_tasks))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(writer.traces) == 1
        assert writer.traces[0].speech_delivery["trace_delivery_finalized"] is False

    asyncio.run(scenario())


def test_delivery_finalizer_error_is_recorded_without_a_second_delivery():
    from neuro_voice.pipeline import VoicePipeline

    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
    )
    pipeline._turns = _BrokenPlaybackTurns()
    VoicePipeline._emit_trace_after_playback(
        pipeline, pipeline._cognitive_trace, _metrics())
    assert len(writer.traces) == 1
    delivery = writer.traces[0].snapshot()["response_delivery"]
    assert delivery["trace_delivery_finalized"] is False
    assert delivery["delivery_finalize_error"] == "finalizer_error"


def test_answer_empty_obligations_emits_one_event_and_one_trace(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(
        lambda *_args: {"obligations": [], "state_changes": []},
    ))
    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
    )
    events: list[tuple[str, dict]] = []
    pipeline._on_event = lambda name, payload: events.append((name, payload))

    VoicePipeline._cognitive_outcome(pipeline, "4", metrics=_metrics())

    assert events == [("cognitive_outcome", {
        "action_id": "decision-1", "turn_id": "turn-1", "status": "completed",
        "interrupted": False, "error": "", "speech_generated": False,
        "planner_called": False, "realizer_called": False, "tts_called": False,
        "pending_response_cleared": False, "turn_closed": False,
        "topic_closed": False, "obligations": [], "effects": {"spoken": ""},
    })]
    assert len(writer.traces) == 1
    assert writer.traces[0].speech_request_count == 1
    assert writer.traces[0].outcome_summary == events[0][1]


def test_answer_obligations_are_confirmed_once_on_outcome(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(
        lambda *_args: {"obligations": ["follow_up"], "state_changes": []},
    ))
    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
    )
    events: list[dict] = []
    pipeline._on_event = lambda _name, payload: events.append(payload)

    VoicePipeline._cognitive_outcome(pipeline, "answer", metrics=_metrics())

    assert len(events) == 1
    assert events[0]["obligations"] == ["follow_up"]
    assert pipeline._mind.observed[0].affected_obligation_ids == ("follow_up",)
    assert writer.traces[0].outcome_summary["obligations"] == ["follow_up"]


def test_trace_writer_failure_does_not_fail_the_cognitive_turn(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(
        lambda *_args: {"obligations": [], "state_changes": []},
    ))
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"),
        writer=_Writer(raises=True),
    )

    VoicePipeline._cognitive_outcome(pipeline, "answer", metrics=_metrics())

    assert len(pipeline._mind.observed) == 1


def test_action_outcome_failure_is_not_hidden(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    def fail(*_args):
        raise RuntimeError("outcome application failed")

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(fail))
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=_Writer(),
    )

    with pytest.raises(RuntimeError, match="outcome application failed"):
        VoicePipeline._cognitive_outcome(pipeline, "answer", metrics=_metrics())


def test_remain_silent_emits_one_trace_without_speech_request():
    from neuro_voice.pipeline import VoicePipeline

    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(ActionType.REMAIN_SILENT),
        trace=CognitiveTrace(turn_id="turn-1"),
        speech_requests=0,
        writer=writer,
    )
    pipeline._turn_cognition_active = lambda _metrics: True
    events: list[dict] = []
    pipeline._on_event = lambda _name, payload: events.append(payload)

    assert VoicePipeline._execute_or_stay_silent(pipeline, None, _metrics()) is False

    assert len(events) == 1
    assert events[0]["speech_generated"] is False
    assert events[0]["turn_closed"] is True
    assert len(writer.traces) == 1
    assert writer.traces[0].speech_request_count == 0
    assert writer.traces[0].outcome_summary == events[0]


def test_speech_gate_failure_is_not_hidden(monkeypatch):
    from neuro_voice.cognition.executor import ActionExecutionGate
    from neuro_voice.pipeline import VoicePipeline

    def fail(*_args, **_kwargs):
        raise RuntimeError("speech gate failed")

    monkeypatch.setattr(ActionExecutionGate, "plan", staticmethod(fail))
    pipeline = _pipeline(
        decision=_decision(ActionType.REMAIN_SILENT),
        trace=CognitiveTrace(turn_id="turn-1"), writer=_Writer(),
    )

    with pytest.raises(RuntimeError, match="speech gate failed"):
        VoicePipeline._execute_or_stay_silent(pipeline, None, _metrics())


def test_redelivered_outcome_for_the_same_turn_emits_once(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(
        lambda *_args: {"obligations": [], "state_changes": []},
    ))
    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
    )
    events: list[dict] = []
    pipeline._on_event = lambda _name, payload: events.append(payload)

    VoicePipeline._cognitive_outcome(pipeline, "answer", metrics=_metrics())
    VoicePipeline._cognitive_outcome(pipeline, "answer", metrics=_metrics())

    assert len(events) == 1
    assert len(writer.traces) == 1


def test_answer_without_a_playback_session_is_a_failed_outcome(monkeypatch):
    from neuro_voice.cognition import CognitiveKernel
    from neuro_voice.pipeline import VoicePipeline

    monkeypatch.setattr(CognitiveKernel, "apply_outcome", staticmethod(
        lambda *_args: {"obligations": [], "state_changes": []},
    ))
    writer = _Writer()
    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=writer,
    )
    pipeline._turns = _Turns(logical_sessions=0)
    pipeline._turn_cognition_active = lambda _metrics: True
    pipeline._cognitive_speech_allowed = True
    status, stage, delivered = VoicePipeline._cognitive_answer_delivery_result(
        pipeline, "valid answer", _metrics(),
    )
    assert (status, stage, delivered) == ("failed", "tts_or_playback", False)

    VoicePipeline._cognitive_outcome(
        pipeline, "valid answer", status=status, error=stage,
        speech_generated=delivered, tts_called=delivered, turn_closed=delivered,
        metrics=_metrics(),
    )
    response_delivery = writer.traces[0].snapshot()["response_delivery"]
    assert response_delivery["action_outcome_status"] == "failed"
    assert response_delivery["turn_closure_completed"] is False
    assert response_delivery["delivery_failure_stage"] == "tts_or_playback"


def test_answer_with_one_logical_session_closes_successfully():
    from neuro_voice.pipeline import VoicePipeline

    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=_Writer(),
    )
    pipeline._turn_cognition_active = lambda _metrics: True
    pipeline._cognitive_speech_allowed = True
    assert VoicePipeline._cognitive_answer_delivery_result(
        pipeline, "valid answer", _metrics(),
    ) == ("completed", "", True)


def test_incomplete_playback_cannot_close_as_completed():
    from neuro_voice.pipeline import VoicePipeline

    pipeline = _pipeline(
        decision=_decision(), trace=CognitiveTrace(turn_id="turn-1"), writer=_Writer(),
    )
    pipeline._turns.delivery_snapshot = lambda _metrics: {
        "speech_request": {"accepted": 1},
        "logical_playback_session_count": 1,
        "playback_segments": [{"completed": False, "started_at": 1.0}],
    }
    pipeline._turn_cognition_active = lambda _metrics: True
    pipeline._cognitive_speech_allowed = True
    assert VoicePipeline._cognitive_answer_delivery_result(
        pipeline, "valid answer", _metrics(),
    ) == ("interrupted", "playback_interrupted", False)


def test_tool_presenter_direct_reply_uses_shared_delivery_and_terminal_outcome():
    from neuro_voice.pipeline import VoicePipeline

    class Conv:
        def __init__(self):
            self.items = []

        def add_assistant(self, text):
            self.items.append(text)

    class Manager:
        def assistant_done(self):
            pass

    async def scenario() -> None:
        pipeline = VoicePipeline.__new__(VoicePipeline)
        pipeline._emit = lambda *_args, **_kwargs: None
        pipeline._turns = _Turns()
        pipeline._conv = Conv()
        pipeline._turn_manager = Manager()
        pipeline._cognitive_decision = object()  # a Tool outcome action is still cognitive
        pipeline._active_response_id = "tool-response"
        pipeline._active_playback_tracker = None
        pipeline._last_assistant_text = ""
        pipeline._last_activity = 0.0
        pipeline._finish_cognition_turn = lambda _metrics: None
        pipeline._report_latency = lambda _metrics: None
        calls = []
        outcomes = []

        async def shared_loop(queue, metrics, **kwargs):
            calls.append((await queue.get(), await queue.get(), kwargs["response_id"]))

        async def finalized(metrics):
            return "completed", ""

        pipeline._speak_loop = shared_loop
        pipeline._await_delivery_finalization = finalized
        pipeline._cognitive_outcome = lambda *args, **kwargs: outcomes.append(kwargs)
        await VoicePipeline._respond_direct_text(
            pipeline, "grounded tool result", _metrics(), "tool-response")
        assert calls == [("grounded tool result", None, "tool-response")]
        assert pipeline._conv.items == ["grounded tool result"]
        assert outcomes[0]["status"] == "completed"
        assert outcomes[0]["turn_closed"] is True

    asyncio.run(scenario())


def test_direct_reply_without_delivery_cannot_complete_action_outcome():
    from neuro_voice.pipeline import VoicePipeline

    async def scenario() -> None:
        pipeline = VoicePipeline.__new__(VoicePipeline)
        pipeline._emit = lambda *_args, **_kwargs: None
        pipeline._turns = _Turns(logical_sessions=0)
        pipeline._conv = type("Conv", (), {"add_assistant": lambda *_args: None})()
        pipeline._turn_manager = type("Manager", (), {"assistant_done": lambda *_args: None})()
        pipeline._cognitive_decision = object()
        pipeline._active_response_id = "tool-response"
        pipeline._active_playback_tracker = None
        pipeline._last_assistant_text = ""
        pipeline._last_activity = 0.0
        pipeline._finish_cognition_turn = lambda _metrics: None
        pipeline._report_latency = lambda _metrics: None
        async def shared_loop(*_args, **_kwargs):
            return None
        async def failed_delivery(_metrics):
            return "failed", "tts_or_playback"
        outcomes = []
        pipeline._speak_loop = shared_loop
        pipeline._await_delivery_finalization = failed_delivery
        pipeline._cognitive_outcome = lambda *args, **kwargs: outcomes.append(kwargs)
        await VoicePipeline._respond_direct_text(
            pipeline, "grounded tool result", _metrics(), "tool-response")
        assert outcomes[0]["status"] == "failed"
        assert outcomes[0]["turn_closed"] is False

    asyncio.run(scenario())
