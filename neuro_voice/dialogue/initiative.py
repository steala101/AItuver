"""Conservative policy for assistant-initiated speech."""
from __future__ import annotations

import time
from dataclasses import dataclass

from neuro_voice.dialogue.state import ConversationState


@dataclass(frozen=True, slots=True)
class InitiativeDecision:
    should_speak: bool
    reason: str
    relevance: float
    interruption_risk: float
    cooldown_satisfied: bool


class InitiativePolicy:
    """Never use silence alone as a reason to speak."""

    def __init__(self, cfg) -> None:
        self._enabled = bool(cfg.get("proactive.enabled", False))
        self._quiet_s = max(1.0, float(cfg.get("proactive.quiet_s", 10)))
        self._cooldown_s = max(5.0, float(cfg.get("proactive.min_interval_s", 30)))
        self._threshold = float(cfg.get("proactive.min_relevance", 0.70))

    def configure(self, *, enabled: bool | None = None,
                  min_interval_s: float | None = None) -> None:
        """Apply safe UI changes without recreating the conversation state."""
        if enabled is not None:
            self._enabled = bool(enabled)
        if min_interval_s is not None:
            self._cooldown_s = max(5.0, float(min_interval_s))

    def decide(
        self, state: ConversationState, *, last_user_at: float, last_proactive_at: float,
        participant_count: int, now: float | None = None, assistant_busy: bool = False,
    ) -> InitiativeDecision:
        now = time.monotonic() if now is None else now
        cooldown = now - last_proactive_at >= self._cooldown_s
        if not self._enabled:
            return InitiativeDecision(False, "disabled", 0.0, 1.0, cooldown)
        if assistant_busy:
            return InitiativeDecision(False, "assistant_busy", 0.0, 1.0, cooldown)
        quiet_for = now - last_user_at
        if quiet_for < self._quiet_s:
            return InitiativeDecision(False, "recent_human_speech", 0.0, 1.0, cooldown)
        if not cooldown:
            return InitiativeDecision(False, "cooldown", 0.0, 0.0, False)

        relevance = 0.0
        if state.unresolved_questions:
            relevance += 0.50
        if state.assistant_turn_open:
            relevance += 0.30
        if state.active_topic:
            relevance += 0.20
        # In a group, a mere topic is not enough.  Require an unresolved item
        # or an open assistant turn so the bot does not fill human silence.
        risk = 0.20 if participant_count <= 1 else 0.55
        if participant_count > 1 and relevance < 0.70:
            return InitiativeDecision(False, "group_conversation_may_continue", relevance, risk, True)
        if relevance < self._threshold:
            return InitiativeDecision(False, "insufficient_relevance", relevance, risk, True)
        reason = "unresolved_topic" if state.unresolved_questions else "open_assistant_turn"
        return InitiativeDecision(True, reason, relevance, risk, True)
