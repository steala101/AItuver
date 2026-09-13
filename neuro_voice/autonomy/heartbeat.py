"""Independent, observable scheduler for autonomous idle re-evaluation.

The scheduler intentionally knows nothing about LLMs, TTS, or Discord.  Its
only job is to keep issuing inexpensive *ticks* while a session is alive.  A
transport decides whether a particular tick has a good enough reason to start
an autonomous turn.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger(__name__)


class AutonomyHeartbeatScheduler:
    """Run a lightweight callback repeatedly without depending on legacy mode."""

    def __init__(self, cfg, callback: Callable[[str], Awaitable[None] | None], *, name: str) -> None:
        self._cfg = cfg
        self._callback = callback
        self._name = name
        self._task: asyncio.Task | None = None
        self._paused = False
        self._stopped = True
        self._generation = 0
        self._next_due_at = 0.0
        self._last_fired_at = 0.0
        self._last_error = ""
        self._metrics = {"ticks": 0, "failures": 0, "cancelled_ticks": 0}
        self._wakeup = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("autonomy.heartbeat_enabled", True))

    @property
    def interval_s(self) -> float:
        return max(.01, float(self._cfg.get("autonomy.heartbeat_interval_ms", 20_000)) / 1000.0)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._next_due_at = time.monotonic() + self.interval_s
        self._task = asyncio.create_task(self._run(), name=f"autonomy-heartbeat:{self._name}")
        logger.info("Autonomy heartbeat started: source=%s interval_ms=%d", self._name, round(self.interval_s * 1000))

    async def stop(self) -> None:
        self._stopped = True
        self.cancel_pending_tick()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def pause(self) -> None:
        self._paused = True
        self.cancel_pending_tick()

    def resume(self) -> None:
        self._paused = False
        self._next_due_at = time.monotonic() + self.interval_s
        self._wakeup.set()

    def request_tick(self, *, delay_s: float = 0.0) -> None:
        """Request an earlier evaluation without running policy in this layer.

        This is used by explicit continuation modes such as 「しばらく話して」.
        It wakes the scheduler's existing loop instead of creating a second
        autonomous timer/task.
        """
        if self._stopped:
            return
        requested = time.monotonic() + max(0.0, float(delay_s))
        if self._next_due_at <= 0.0 or requested < self._next_due_at:
            self._next_due_at = requested
        self._wakeup.set()

    def cancel_pending_tick(self) -> None:
        """Invalidate a callback result that was created before human speech."""
        self._generation += 1
        self._metrics["cancelled_ticks"] += 1

    def health_status(self) -> dict[str, Any]:
        task = self._task
        return {
            "scheduler": self._name,
            "enabled": self.enabled,
            "started": not self._stopped,
            "paused": self._paused,
            "task_alive": task is not None and not task.done(),
            "heartbeat_fired_count": self._metrics["ticks"],
            "heartbeat_failures": self._metrics["failures"],
            "heartbeat_last_fired_at": self._last_fired_at,
            "heartbeat_next_due_at": self._next_due_at,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        while not self._stopped:
            delay = max(0.0, self._next_due_at - time.monotonic())
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
                self._wakeup.clear()
                continue
            except asyncio.TimeoutError:
                pass
            self._next_due_at = time.monotonic() + self.interval_s
            if self._stopped or self._paused or not self.enabled:
                continue
            generation = self._generation
            heartbeat_id = f"{self._name}:{uuid.uuid4().hex[:10]}"
            self._last_fired_at = time.monotonic()
            self._metrics["ticks"] += 1
            try:
                result = self._callback(heartbeat_id)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # A bad tick must never stop future re-evaluation.
                self._metrics["failures"] += 1
                self._last_error = f"{type(exc).__name__}: {exc}"[:240]
                logger.exception("Autonomy heartbeat failed: source=%s id=%s", self._name, heartbeat_id)
            finally:
                # Human speech may have invalidated this tick while its callback
                # was running.  The next tick remains scheduled either way.
                if generation != self._generation:
                    logger.debug("Autonomy heartbeat result invalidated: source=%s id=%s", self._name, heartbeat_id)
