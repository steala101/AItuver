from neuro_voice.cognition.test_session import CognitionTestSession
from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.utils.latency import TurnMetrics


class _Cfg:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Turns:
    def begin(self, metrics, **_kwargs):
        return metrics


def _pipeline_for_boundary(values=None):
    from neuro_voice.pipeline import VoicePipeline

    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._cfg = _Cfg(values or {
        "cognition.enabled": False,
        "cognition.rollout_mode": "disabled",
    })
    pipeline._cognition_test_session = CognitionTestSession()
    pipeline._turns = _Turns()
    pipeline._turn_session_id = "session"
    pipeline._mind = None
    pipeline._user_speaking = False
    pipeline._is_responding = lambda: False
    return pipeline


def test_session_only_activates_when_explicitly_requested_at_idle():
    session = CognitionTestSession()
    assert session.snapshot().effective_enabled is False
    starting = session.request(True, idle=False)
    assert starting.state == "test_session_starting"
    assert starting.effective_enabled is False
    assert session.apply_if_idle() is True
    active = session.snapshot()
    assert active.effective_enabled is True
    assert active.rollout_mode == "test_session"
    assert active.epoch == 1


def test_stop_is_deferred_until_turn_boundary_and_advances_epoch():
    session = CognitionTestSession()
    session.request(True, idle=True)
    stopping = session.request(False, idle=False)
    assert stopping.state == "test_session_stopping"
    assert stopping.effective_enabled is False
    assert session.apply_if_idle() is True
    stopped = session.snapshot()
    assert stopped.state == "idle"
    assert stopped.epoch == 2


def test_turn_metrics_snapshot_freezes_cognition_context():
    metrics = TurnMetrics(
        effective_cognition_enabled=True,
        cognition_rollout_mode="test_session",
        cognition_execution_path="cognitive",
        cognition_session_epoch=7,
    )
    assert metrics.effective_cognition_enabled is True
    assert metrics.cognition_rollout_mode == "test_session"
    assert metrics.cognition_execution_path == "cognitive"
    assert metrics.cognition_session_epoch == 7


def test_trace_exposes_session_and_delivery_contract_without_text():
    trace = CognitiveTrace(
        cognition_enabled=True,
        rollout_mode="test_session",
        execution_path="cognitive",
        cognition_session_epoch=3,
        action_selector_used=True,
        speech_gate_active=True,
        legacy_response_path_used=False,
        speech_request_count=1,
    )
    assert trace.snapshot()["cognition"] == {
        "enabled": True,
        "rollout_mode": "test_session",
        "requested_rollout_mode": "disabled",
        "execution_path": "cognitive",
        "session_epoch": 3,
        "activation_source": "config",
        "config_fingerprint": "",
        "transport": "LOCAL",
        "action_selector_used": True,
        "speech_gate_active": True,
        "legacy_response_path_used": False,
        "speech_request_count": 1,
        "fallback_used": False,
        "fallback_reason": "",
        "fallback_count": 0,
        "side_effect_state": {},
    }


def test_turn_boundary_separates_configured_rollout_from_execution_path():
    from neuro_voice.pipeline import VoicePipeline

    pipeline = _pipeline_for_boundary()
    legacy = TurnMetrics()
    VoicePipeline._begin_turn(pipeline, legacy)
    assert (legacy.effective_cognition_enabled, legacy.cognition_rollout_mode,
            legacy.cognition_execution_path) == (False, "disabled", "legacy")

    pipeline._cognition_test_session.request(True, idle=True)
    cognitive = TurnMetrics()
    VoicePipeline._begin_turn(pipeline, cognitive)
    assert (cognitive.effective_cognition_enabled, cognitive.cognition_rollout_mode,
            cognitive.cognition_execution_path) == (True, "test_session", "cognitive")

    pipeline._cognition_test_session.request(False, idle=True)
    stopped = TurnMetrics()
    VoicePipeline._begin_turn(pipeline, stopped)
    assert (stopped.effective_cognition_enabled, stopped.cognition_rollout_mode,
            stopped.cognition_execution_path) == (False, "disabled", "legacy")


def test_old_cognition_epoch_cannot_pass_the_final_speech_gate():
    from neuro_voice.pipeline import VoicePipeline

    pipeline = _pipeline_for_boundary()
    pipeline._cognition_test_session.request(True, idle=True)
    pipeline._cognition_test_session.request(False, idle=True)
    stale = TurnMetrics(
        effective_cognition_enabled=True,
        cognition_rollout_mode="test_session",
        cognition_execution_path="cognitive",
        cognition_session_epoch=1,
    )
    assert VoicePipeline._speech_allowed_now(pipeline, metrics=stale) is False


def test_production_session_is_process_local_and_uses_a_new_epoch():
    session = CognitionTestSession()
    active = session.request_mode("production_session", idle=True)
    assert active.effective_enabled is True
    assert active.rollout_mode == "production_session"
    assert active.epoch == 1
    stopped = session.request_mode("disabled", idle=True)
    assert stopped.effective_enabled is False
    assert stopped.rollout_mode == "disabled"
    assert stopped.epoch == 2
