"""Atomic cognitive-to-legacy fallback policy.

This module owns only the admission decision.  It intentionally cannot replay
speech, tools, or memory writes; callers retain their existing delivery ledger.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FallbackState:
    action_decision_committed: bool = False
    tool_execution_started: bool = False
    tool_side_effect_committed: bool = False
    memory_write_committed: bool = False
    speech_request_accepted: bool = False
    playback_started: bool = False
    irreversible_effect_started: bool = False
    fallback_count: int = 0

    def side_effect_started(self) -> bool:
        return any((
            self.tool_execution_started, self.tool_side_effect_committed,
            self.memory_write_committed, self.speech_request_accepted,
            self.playback_started, self.irreversible_effect_started,
        ))


def legacy_fallback_allowed(
    state: FallbackState, *, technical_error: bool,
    selected_action: str = "", speech_gate_reason: str = "",
    stale_epoch: bool = False, recall_not_found: bool = False,
) -> tuple[bool, str]:
    """Allow at most one same-frame fallback before irreversible effects."""
    if not technical_error:
        return False, "not_a_technical_cognitive_error"
    if stale_epoch:
        return False, "stale_epoch"
    if selected_action == "remain_silent":
        return False, "intentional_remain_silent"
    if recall_not_found:
        return False, "explicit_recall_not_found"
    if speech_gate_reason:
        return False, "intentional_speech_gate"
    if state.fallback_count:
        return False, "fallback_already_used"
    if state.side_effect_started():
        return False, "side_effect_already_started"
    state.fallback_count += 1
    return True, "technical_before_side_effect"
