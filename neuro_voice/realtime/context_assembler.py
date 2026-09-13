"""Deadline-bounded assembly of optional Mind context for a response turn."""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable

from neuro_voice.cognition.recall import retrieval_trigger


class ContextAssembler:
    """Keeps slow enrichments from delaying the first LLM token."""

    def __init__(self, cfg, mind, emit: Callable[..., None]):
        self._cfg = cfg
        self._mind = mind
        self._emit = emit

    async def build(
        self, user_text: str, messages: list[dict], *, speaker_task=None,
        user_emotion=None, metrics=None, conversation_topic: str = "",
        response_id: str = "", utterance_id: str = "",
    ) -> list[dict]:
        if self._mind is None:
            return messages
        started = time.perf_counter()
        deadline_s = max(0.01, float(self._cfg.get("realtime.context_deadline_ms", 150)) / 1000)
        speaker_wait_s = min(
            deadline_s,
            max(0.0, float(self._cfg.get("realtime.speaker_context_wait_ms", 120)) / 1000),
        )
        if speaker_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(speaker_task), timeout=speaker_wait_s)
            if speaker_task.done():
                if metrics is not None:
                    metrics.mark("speaker_ready")
            else:
                if metrics is not None:
                    metrics.mark("speaker_deferred")
                self._emit("context_deferred", source="speaker", deadline_ms=round(speaker_wait_s * 1000))
        try:
            self._mind.observe_dialogue_turn(
                user_text,
                getattr(user_emotion, "label", None),
                float(getattr(user_emotion, "confidence", 0.0)),
            )
            remaining = max(0.01, deadline_s - (time.perf_counter() - started))
            recall_budget = max(0.0, float(self._cfg.get("realtime.memory_recall_budget_ms", 60)) / 1000)
            # The budget limits a retrieval that is needed; it must not turn
            # every ordinary turn into a retrieval request.
            trigger_reason = retrieval_trigger(user_text)
            include_recall = bool(trigger_reason and recall_budget > 0)
            context = await asyncio.wait_for(
                self._mind.build_context(
                    user_text,
                    include_recall=include_recall,
                    retrieval_trigger_reason=(trigger_reason if include_recall else "not_applicable"),
                    conversation_topic=conversation_topic,
                    recall_timeout_s=min(recall_budget, remaining),
                    source="local",
                    response_id=response_id,
                    utterance_id=utterance_id,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            self._emit("context_deferred", source="mind", deadline_ms=round(deadline_s * 1000))
            if metrics is not None:
                metrics.mark("context_deferred")
                metrics.mark("context_ready")
            return messages
        except Exception:
            return messages
        if metrics is not None:
            metrics.mark("context_ready")
        if not context:
            return messages
        # This block is 4700-5800 tokens and is rebuilt every turn.  At index 1
        # it made the persona the only reusable part of the prompt (measured:
        # 28-33% reuse, first token 2.5s).  Placed next to the user turn, the
        # whole stable prefix in front of it survives.
        from neuro_voice.llm.prompt_layout import insert_before_user_turn

        return insert_before_user_turn(list(messages), context)
