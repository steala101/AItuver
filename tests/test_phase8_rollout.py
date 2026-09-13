from neuro_voice.cognition.fallback import FallbackState, legacy_fallback_allowed
from neuro_voice.cognition.rollout import CognitionRolloutResolver


class _Cfg:
    def __init__(self, **values):
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)


class _Session:
    def __init__(self, enabled, mode):
        self.effective_enabled = enabled
        self.rollout_mode = mode


def test_production_requires_explicit_persistent_mode():
    cfg = _Cfg(**{"cognition.enabled": True, "cognition.rollout_mode": "production"})
    result = CognitionRolloutResolver.resolve(cfg)
    assert (result.effective_enabled, result.resolved_mode, result.execution_path) == (
        True, "production", "cognitive")


def test_session_override_is_ephemeral_and_wins_over_disabled_config():
    cfg = _Cfg(**{"cognition.enabled": False, "cognition.rollout_mode": "disabled"})
    result = CognitionRolloutResolver.resolve(
        cfg, session=_Session(True, "production_session"))
    assert (result.effective_enabled, result.resolved_mode, result.activation_source) == (
        True, "production_session", "session_override")


def test_session_mode_in_config_fails_closed():
    cfg = _Cfg(**{"cognition.enabled": True, "cognition.rollout_mode": "test_session"})
    result = CognitionRolloutResolver.resolve(cfg)
    assert result.effective_enabled is False
    assert result.warning == "session_mode_requires_override"


def test_atomic_fallback_allows_one_technical_pre_effect_retry_only():
    state = FallbackState()
    assert legacy_fallback_allowed(state, technical_error=True) == (
        True, "technical_before_side_effect")
    assert legacy_fallback_allowed(state, technical_error=True)[0] is False


def test_atomic_fallback_refuses_speech_tools_and_intentional_outcomes():
    assert legacy_fallback_allowed(
        FallbackState(speech_request_accepted=True), technical_error=True,
    )[0] is False
    assert legacy_fallback_allowed(
        FallbackState(tool_execution_started=True), technical_error=True,
    )[0] is False
    assert legacy_fallback_allowed(
        FallbackState(), technical_error=True, selected_action="remain_silent",
    )[0] is False
    assert legacy_fallback_allowed(
        FallbackState(), technical_error=True, stale_epoch=True,
    )[0] is False
