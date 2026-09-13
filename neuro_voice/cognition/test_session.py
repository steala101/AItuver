"""Process-local, turn-safe cognition test-session override.

This deliberately owns no configuration and never persists a setting.  A
requested change is applied only between turns, and every turn receives an
immutable snapshot (including its epoch).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CognitionSessionState(StrEnum):
    IDLE = "idle"
    TEST_SESSION_STARTING = "test_session_starting"
    TEST_SESSION_ACTIVE = "test_session_active"
    TEST_SESSION_STOPPING = "test_session_stopping"
    PRODUCTION_SESSION_STARTING = "production_session_starting"
    PRODUCTION_SESSION_ACTIVE = "production_session_active"
    PRODUCTION_SESSION_STOPPING = "production_session_stopping"


@dataclass(frozen=True, slots=True)
class CognitionSessionSnapshot:
    effective_enabled: bool
    rollout_mode: str
    epoch: int
    state: str


class CognitionTestSession:
    """A tiny state machine; callers decide when the pipeline is idle."""

    def __init__(self) -> None:
        self._state = CognitionSessionState.IDLE
        self._epoch = 0
        self._requested_mode = "disabled"

    def request(self, active: bool, *, idle: bool) -> CognitionSessionSnapshot:
        return self.request_mode("test_session" if active else "disabled", idle=idle)

    def request_mode(self, mode: str, *, idle: bool) -> CognitionSessionSnapshot:
        """Request a non-persistent rollout override at a turn boundary."""
        mode = str(mode or "disabled")
        if mode not in {"disabled", "test_session", "production_session"}:
            mode = "disabled"
        self._requested_mode = mode
        active = self.snapshot().effective_enabled
        if mode == "disabled":
            if not active and self._state is CognitionSessionState.IDLE:
                return self.snapshot()
            self._state = (CognitionSessionState.PRODUCTION_SESSION_STOPPING
                           if self.snapshot().rollout_mode == "production_session"
                           else CognitionSessionState.TEST_SESSION_STOPPING)
        elif mode == "test_session":
            if active and self.snapshot().rollout_mode == mode:
                return self.snapshot()
            self._state = CognitionSessionState.TEST_SESSION_STARTING
        else:
            if active and self.snapshot().rollout_mode == mode:
                return self.snapshot()
            self._state = CognitionSessionState.PRODUCTION_SESSION_STARTING
        if idle:
            self.apply_if_idle()
        return self.snapshot()

    def apply_if_idle(self) -> bool:
        if self._state is CognitionSessionState.TEST_SESSION_STARTING:
            self._epoch += 1
            self._state = CognitionSessionState.TEST_SESSION_ACTIVE
            return True
        if self._state is CognitionSessionState.PRODUCTION_SESSION_STARTING:
            self._epoch += 1
            self._state = CognitionSessionState.PRODUCTION_SESSION_ACTIVE
            return True
        if self._state is CognitionSessionState.TEST_SESSION_STOPPING:
            self._epoch += 1
            self._state = CognitionSessionState.IDLE
            return True
        if self._state is CognitionSessionState.PRODUCTION_SESSION_STOPPING:
            self._epoch += 1
            self._state = CognitionSessionState.IDLE
            return True
        return False

    def snapshot(self) -> CognitionSessionSnapshot:
        active = self._state in {
            CognitionSessionState.TEST_SESSION_ACTIVE,
            CognitionSessionState.PRODUCTION_SESSION_ACTIVE,
        }
        return CognitionSessionSnapshot(
            effective_enabled=active,
            rollout_mode=("test_session" if self._state is CognitionSessionState.TEST_SESSION_ACTIVE
                          else "production_session" if active else "disabled"),
            epoch=self._epoch,
            state=str(self._state),
        )
