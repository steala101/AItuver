"""Backend-neutral media values and inference priorities.

No caller outside an LLM adapter should need to know whether a provider uses
paths, base64, multipart, or an SDK-specific image object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO_FRAME = "video_frame"
    AUDIO = "audio"


class InferencePriority(IntEnum):
    LIVE_CONVERSATION = 0
    EXPLICIT_VISION_QUERY = 1
    BACKGROUND_VISION = 2
    IDLE_ANALYSIS = 3


@dataclass(slots=True)
class MediaInput:
    media_type: MediaType
    path: Path | None = None
    data: bytes | None = None
    mime_type: str | None = None
    timestamp: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def read_bytes(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is not None:
            return self.path.read_bytes()
        raise ValueError("MediaInput requires data or path")


class InferencePriorityGate:
    """One-model gate that lets a higher-priority request preempt stale work."""

    def __init__(self):
        import asyncio

        self._lock = asyncio.Lock()
        self._active_task = None
        self._active_priority: InferencePriority | None = None

    async def run(self, priority: InferencePriority, operation):
        import asyncio

        current = asyncio.current_task()
        if (
            self._active_task is not None and not self._active_task.done()
            and self._active_priority is not None and priority < self._active_priority
        ):
            self._active_task.cancel()
        async with self._lock:
            self._active_task = current
            self._active_priority = priority
            try:
                return await operation()
            finally:
                if self._active_task is current:
                    self._active_task = None
                    self._active_priority = None

    def cancel_background(self) -> None:
        if (
            self._active_task is not None and not self._active_task.done()
            and self._active_priority is not None
            and self._active_priority >= InferencePriority.BACKGROUND_VISION
        ):
            self._active_task.cancel()
