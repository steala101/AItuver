"""Deterministic turn state machine used by the audio pipeline.

This layer has no LLM dependency.  It exists so acknowledgement, ducking, and
barge-in decisions are observable state transitions rather than scattered
boolean checks in the pipeline.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class TurnState(StrEnum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    USER_PAUSED = "user_paused"
    STT_FINALIZING = "stt_finalizing"
    AI_THINKING = "ai_thinking"
    AI_SPEAKING = "ai_speaking"
    USER_BACKCHANNEL = "user_backchannel"
    BARGE_IN_PENDING = "barge_in_pending"
    BARGE_IN_CONFIRMED = "barge_in_confirmed"
    REPAIRING = "repairing"


@dataclass(frozen=True)
class TurnTransition:
    previous: TurnState
    current: TurnState
    reason: str
    at: float


class TurnManager:
    """Small explicit state machine for a single realtime conversation."""

    def __init__(self, on_transition: Callable[[TurnTransition], None] | None = None):
        self._state = TurnState.IDLE
        self._on_transition = on_transition
        self._last = TurnTransition(TurnState.IDLE, TurnState.IDLE, "init", time.monotonic())

    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def last_transition(self) -> TurnTransition:
        return self._last

    def _set(self, target: TurnState, reason: str) -> None:
        previous = self._state
        self._state = target
        self._last = TurnTransition(previous, target, reason, time.monotonic())
        if self._on_transition is not None:
            self._on_transition(self._last)

    def user_started(self, *, assistant_busy: bool) -> None:
        self._set(
            TurnState.BARGE_IN_PENDING if assistant_busy else TurnState.USER_SPEAKING,
            "user_speech_started",
        )

    def user_paused(self) -> None:
        self._set(TurnState.USER_PAUSED, "user_speech_ended")

    def stt_finalizing(self) -> None:
        self._set(TurnState.STT_FINALIZING, "stt_finalizing")

    def backchannel(self, *, assistant_busy: bool) -> None:
        self._set(TurnState.USER_BACKCHANNEL, "short_acknowledgement")
        self._set(TurnState.AI_SPEAKING if assistant_busy else TurnState.IDLE, "acknowledgement_resolved")

    def speech_ignored(self) -> None:
        self._set(TurnState.AI_SPEAKING, "false_positive_resolved")

    def barge_in_confirmed(self) -> None:
        self._set(TurnState.BARGE_IN_CONFIRMED, "substantive_barge_in")

    def assistant_thinking(self) -> None:
        self._set(TurnState.AI_THINKING, "response_started")

    def assistant_speaking(self) -> None:
        self._set(TurnState.AI_SPEAKING, "playback_started")

    def assistant_done(self) -> None:
        self._set(TurnState.IDLE, "response_finished")

    def repairing(self) -> None:
        self._set(TurnState.REPAIRING, "repairing")

    def snapshot(self) -> dict[str, str | float]:
        return {
            "state": self._state.value,
            "previous": self._last.previous.value,
            "reason": self._last.reason,
            "changed_at": self._last.at,
        }
