"""Event-driven autonomous action policy.

This package deliberately decides *whether* an action is worthwhile before
any expensive response generation is requested.  It does not own audio, TTS,
or an LLM connection; the local and Discord transports remain responsible for
those real-time concerns.
"""

from neuro_voice.autonomy.engine import AutonomousActionSystem, AutonomyDecision
from neuro_voice.autonomy.events import AutonomyEventBus
from neuro_voice.autonomy.heartbeat import AutonomyHeartbeatScheduler
from neuro_voice.autonomy.state import AgentState, PersistentAgentState
from neuro_voice.autonomy.types import ActionType, AutonomyEvent, AutonomyEventType, PrivacyScope, TriggerReason

__all__ = [
    "ActionType", "AgentState", "AutonomousActionSystem", "AutonomyDecision", "AutonomyHeartbeatScheduler",
    "AutonomyEvent", "AutonomyEventBus", "AutonomyEventType", "PersistentAgentState", "PrivacyScope",
    "TriggerReason",
]
