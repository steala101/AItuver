"""Bounded in-process event bus for autonomous decisions."""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from neuro_voice.autonomy.types import AutonomyEvent


class AutonomyEventBus:
    """Reject expired/duplicate events before they can revive an old action."""

    def __init__(self, *, max_recent: int = 128) -> None:
        self._handlers: list[Callable[[AutonomyEvent], None]] = []
        self._seen: deque[str] = deque(maxlen=max(16, max_recent))
        self._recent: deque[AutonomyEvent] = deque(maxlen=max(16, max_recent))

    def subscribe(self, handler: Callable[[AutonomyEvent], None]) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    def publish(self, event: AutonomyEvent, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if event.event_id in self._seen or event.expired(now=now):
            return False
        self._seen.append(event.event_id)
        self._recent.append(event)
        for handler in tuple(self._handlers):
            handler(event)
        return True

    def recent(self) -> list[AutonomyEvent]:
        now = time.monotonic()
        return [event for event in self._recent if not event.expired(now=now)]
