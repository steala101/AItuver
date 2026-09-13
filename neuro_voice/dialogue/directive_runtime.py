"""Surface-neutral runtime for continuing requests.

Local voice and Discord must not each grow their own copy of "interpret the
request, run a segment, stop when a human speaks".  Two copies drift, and the
first symptom is the two surfaces disagreeing about the same sentence.

Everything generic lives here.  A host supplies only what is genuinely
surface-specific: how to speak, whether the floor is busy, how much audio is
already queued, and which tools this surface can actually run.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from neuro_voice.dialogue.directive import (
    BehaviorDirective,
    DirectiveAction,
    loose_json,
    parse_plan_proposal,
)
from neuro_voice.dialogue.intent_plan import (
    build_plan_messages, directive_prompt_block, is_stop_request, plan_from_request,
)
from neuro_voice.utils.textseg import strip_think

logger = logging.getLogger(__name__)


def _no_tail() -> list[str]:
    return []


@dataclass(slots=True)
class DirectiveHost:
    """The surface-specific half of running a directive."""

    #: Speak one segment.  Returns False if the words were already overtaken.
    speak: Callable[[str, str, int], Awaitable[bool]]
    #: True while a human owns the floor or the assistant is already busy.
    floor_busy: Callable[[], bool]
    #: Seconds of audio queued but not yet heard, for TTS backpressure.
    playback_ahead_s: Callable[[], float] = lambda: 0.0
    #: Schedule the next evaluation after N seconds.
    request_tick: Callable[[float], None] = lambda _delay: None
    #: False when this surface cannot speak at all (not in VC, nobody present).
    ready: Callable[[], bool] = lambda: True
    #: Run a permitted tool request; None means this surface has no tools.
    run_tool: Callable[[dict[str, Any]], Awaitable[str]] | None = None
    #: A few recent lines of conversation for segment context.
    conversation_tail: Callable[[], list[str]] = _no_tail
    #: Whether environment events (game/screen) can reach this surface at all.
    environment_available: Callable[[], bool] = lambda: False
    #: Cancel any in-flight generation for a directive response id.
    cancel_generation: Callable[[str], None] = lambda _response_id: None
    #: Drop audio that has been synthesized but not yet heard.
    discard_pending_audio: Callable[[], None] = lambda: None
    #: True when this surface may search at all.
    search_enabled: Callable[[], bool] = lambda: False
    #: Stop optional autonomous speech after an explicit continuing-task stop.
    on_user_stop: Callable[[str], None] = lambda _text: None


class DirectiveRuntime:
    """Own one surface's execution of the shared continuation controller."""

    def __init__(
        self,
        cfg,
        *,
        mind,
        llm,
        source: str,
        host: DirectiveHost,
        emit: Callable[..., None] | None = None,
        persona_name: Callable[[], str] | None = None,
    ) -> None:
        self._cfg = cfg
        self._mind = mind
        self._llm = llm
        self._source = str(source or "local")
        self._host = host
        self._emit = emit or (lambda *_a, **_k: None)
        self._persona_name = persona_name or (lambda: "AI")
        self._task: asyncio.Task | None = None
        #: Plan/revision work running alongside the reply, collected by settle().
        self._pending: asyncio.Task | None = None
        self._queued_segments = 0
        #: The directive this surface is currently speaking for, if any.
        self.active_directive: BehaviorDirective | None = None

    # -- accessors -------------------------------------------------------
    @property
    def controller(self):
        return None if self._mind is None else self._mind.continuation

    @property
    def enabled(self) -> bool:
        controller = self.controller
        return bool(controller is not None and controller.enabled)

    @property
    def conversation_id(self) -> str:
        if self._mind is None:
            return self._source
        return self._mind.directive_conversation_id(self._source)

    def active(self) -> BehaviorDirective | None:
        controller = self.controller
        return None if controller is None else controller.active(self.conversation_id)

    def tick_delay(self) -> float:
        return max(0.6, float(self._cfg.get("behavior_directive.segment_gap_s", 1.2)))

    # -- model access ----------------------------------------------------
    async def _generate(self, messages: list[dict[str, str]]) -> str:
        chunks: list[str] = []
        async for token in strip_think(self._llm.generate(messages)):
            chunks.append(token)
        return "".join(chunks)

    # -- user turns ------------------------------------------------------
    def begin_user_turn(self, user_text: str, frame: dict[str, Any]) -> BehaviorDirective | None:
        """Apply the instant part of the decision and start the rest in parallel.

        Interpreting a request as a plan costs a full model round-trip.  Doing
        that before the reply made "ラジオやって" take 14 seconds to get its
        first word out.  Cancel/pause/resume are pure classification and happen
        now; plan creation and revision run alongside the reply and are picked
        up by :meth:`settle` once the person has already been answered.
        """
        controller = self.controller
        if controller is None or not controller.enabled:
            return None
        if is_stop_request(user_text):
            self._stop_for_user(user_text)
            return None
        active = controller.active(self.conversation_id)
        if active is None:
            if not frame.get("plan_candidate"):
                return None
            # A request that already states its shape is interpreted right here,
            # with no model call, so the reply can be shaped by it immediately.
            direct = self._direct_plan(user_text, frame)
            if direct is not None:
                self.active_directive = direct
                return direct
            self._pending = asyncio.create_task(
                self._create_from_plan(user_text, frame), name="intent-to-plan",
            )
            return None
        action, _relation, reason, directive = controller.handle_user_turn(
            user_text, conversation_id=self.conversation_id,
        )
        if directive is not None:
            controller.note_user_turn_started(directive)
        self._emit(
            "directive_user_turn", source=self._source, action=str(action),
            reason=reason, directive_id="" if directive is None else directive.directive_id,
        )
        if action is DirectiveAction.CANCEL:
            self.active_directive = None
            return None
        if action is DirectiveAction.REVISE and directive is not None:
            self._pending = asyncio.create_task(
                self._revise_and_resume(directive, user_text), name="directive-revision",
            )
        elif directive is not None:
            # Answer the human first; the continuation resumes on the next tick.
            controller.resume(directive)
        self.active_directive = directive
        return directive

    def note_suppressed_user_turn(self, user_text: str) -> BehaviorDirective | None:
        """A human turn that the conversation layer answered with silence.

        Turn closure may decide that a short 「うん」 needs no reply.  That is a
        decision about *speaking*, not about the session.  A game master
        waiting for the player must still learn that the player moved,
        otherwise the directive stays in ``WAITING_FOR_USER`` for ever and the
        story never continues — the reply path is the only place that used to
        clear it, and a silenced turn never reaches the reply path.

        Deliberately narrower than :meth:`begin_user_turn`: an acknowledgement
        must never create a directive or revise a plan.
        """
        controller = self.controller
        if controller is None or not controller.enabled:
            return None
        if is_stop_request(user_text):
            # 「もういいよ」 is short enough to look like feedback; stopping is
            # still the only correct reading of it.
            self._stop_for_user(user_text)
            return None
        directive = controller.active(self.conversation_id)
        if directive is None:
            return None
        controller.note_user_turn_started(directive)
        controller.resume(directive)
        self.active_directive = directive
        self._emit(
            "directive_user_turn", source=self._source, action="silent_turn",
            reason="turn_closed_without_reply", directive_id=directive.directive_id,
        )
        return directive

    async def _revise_and_resume(self, directive: BehaviorDirective, user_text: str) -> BehaviorDirective:
        await self._revise(directive, user_text)
        controller = self.controller
        if controller is not None:
            controller.resume(directive)
        return directive

    async def settle(self, *, timeout: float = 8.0) -> BehaviorDirective | None:
        """Collect background plan/revision work once the reply is done."""
        task, self._pending = self._pending, None
        if task is None:
            return self.active()
        try:
            directive = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            # Still thinking.  The directive will simply start from its own
            # first segment instead of continuing this reply.
            logger.info("Intent-to-Plan がまだ完了していないため、区間側で開始します")
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("継続依頼の背景処理に失敗")
            return None
        if directive is not None:
            self.active_directive = directive
        return directive

    async def handle_user_turn(
        self, user_text: str, frame: dict[str, Any],
    ) -> BehaviorDirective | None:
        """Apply the kernel's directive decision for one finalized user turn.

        A human turn never silently kills a continuing request: it pauses, gets
        answered, and the classification decides resume / revise / cancel.
        """
        controller = self.controller
        if controller is None or not controller.enabled:
            return None
        if is_stop_request(user_text):
            self._stop_for_user(user_text)
            return None
        active = controller.active(self.conversation_id)
        if active is None:
            directive = await self._create_from_plan(user_text, frame)
            self.active_directive = directive
            return directive
        action, _relation, reason, directive = controller.handle_user_turn(
            user_text, conversation_id=self.conversation_id,
        )
        if directive is not None:
            controller.note_user_turn_started(directive)
        self._emit(
            "directive_user_turn", source=self._source, action=str(action),
            reason=reason, directive_id="" if directive is None else directive.directive_id,
        )
        if action is DirectiveAction.CANCEL:
            self.active_directive = None
            return None
        if action is DirectiveAction.REVISE and directive is not None:
            await self._revise(directive, user_text)
            controller.resume(directive)
        elif directive is not None:
            # Answer the human first; the continuation resumes on the next tick.
            controller.resume(directive)
        self.active_directive = directive
        return directive

    async def _create_from_plan(
        self, user_text: str, frame: dict[str, Any],
    ) -> BehaviorDirective | None:
        controller = self.controller
        if controller is None or not frame.get("plan_candidate"):
            return None
        direct = self._direct_plan(user_text, frame)
        if direct is not None:
            return direct
        if not bool(self._cfg.get("behavior_directive.intent_to_plan_enabled", True)):
            logger.info("Intent-to-Plan は無効。継続依頼を単発応答として扱います")
            return None
        context = self._mind.directive_plan_context(self._source)
        search_decision = frame.get("search_decision") or {}
        messages = build_plan_messages(
            user_text,
            persona_name=self._persona_name(),
            search_allowed=bool(
                self._host.search_enabled()
                and search_decision.get("explicit_request", False)
            ),
            activity_summary=str(context.get("activity_summary") or ""),
            environment_summary=(
                "ゲーム/画面イベントを取得できる" if self._host.environment_available()
                else "外部イベントは取得できない"
            ),
            interests=context.get("interests") or [],
            affect_summary=str(context.get("affect_summary") or ""),
            audience=self._source,
            max_duration_seconds=controller.max_duration_s,
        )
        try:
            raw = await self._generate(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Intent-to-Plan generation failed (%s)", self._source)
            return None
        plan = parse_plan_proposal(
            raw,
            search_allowed=bool(self._host.search_enabled()),
            max_duration_seconds=controller.max_duration_s,
            default_duration_seconds=float(self._cfg.get(
                "behavior_directive.default_duration_seconds", 300,
            )),
        )
        if plan is None:
            # Silence here used to make this indistinguishable from "the user
            # never asked".  Say which step gave up.
            logger.info(
                "継続依頼の計画を解釈できませんでした (単発応答として継続): %.80s",
                " ".join(str(raw or "").split()),
            )
            self._emit("directive_plan_failed", source=self._source, reason="unparseable")
            return None
        if not plan.execution_mode.is_continuing:
            logger.info("計画は単発応答と判断されました: %s", plan.execution_mode)
            self._emit("directive_plan_failed", source=self._source, reason="one_shot_plan")
            return None
        return self._start(plan, user_text, frame)

    def _direct_plan(self, user_text: str, frame: dict[str, Any]) -> BehaviorDirective | None:
        """Interpret an unambiguous request without a model call.

        A local backend runs one request at a time, so a planning call and the
        reply queue behind each other: asking the model to rediscover what
        「3分間ラジオトークやって」 plainly says cost twelve seconds of silence.
        """
        controller = self.controller
        if controller is None:
            return None
        default_duration = float(
            self._cfg.get("behavior_directive.default_duration_seconds", 300)
        )
        shape = plan_from_request(user_text, default_duration_seconds=default_duration)
        if shape is None:
            return None
        plan = parse_plan_proposal(
            json.dumps(shape, ensure_ascii=False),
            search_allowed=bool(self._host.search_enabled()),
            max_duration_seconds=controller.max_duration_s,
            default_duration_seconds=default_duration,
        )
        if plan is None or not plan.execution_mode.is_continuing:
            return None
        logger.info(
            "継続依頼を規則から解釈 (LLM未使用): mode=%s duration=%s",
            plan.execution_mode, plan.duration_hint_seconds,
        )
        return self._start(plan, user_text, frame)

    def _start(self, plan, user_text: str, frame: dict[str, Any]) -> BehaviorDirective | None:
        controller = self.controller
        if controller is None:
            return None
        directive = controller.start_directive(
            plan,
            conversation_id=self.conversation_id,
            user_text=user_text,
            source_turn_id=str(frame.get("turn_id") or ""),
            audience=self._source,
        )
        if directive is not None:
            # A continuation asked for *during* something is a continuation of
            # that thing.  Without carrying the subject over, 「一緒にやろう」 in
            # the middle of a story started an unrelated brainstorm and the
            # story was simply abandoned.
            subject = self._current_subject()
            if subject and not directive.current_topic:
                directive.current_topic = subject
                directive.plan_steps = [
                    f"今すでに進んでいる「{subject}」から続ける", *directive.plan_steps,
                ][:10]
            self._emit(
                "directive_created", source=self._source,
                directive_id=directive.directive_id,
                execution_mode=str(directive.execution_mode),
                interaction_mode=str(directive.interaction_mode),
                goal=directive.interpreted_goal, current_topic=directive.current_topic,
            )
        return directive

    def _current_subject(self) -> str:
        """What the conversation is already about, if anything."""
        if self._mind is None:
            return ""
        try:
            context = self._mind.directive_plan_context(self._source)
        except Exception:
            return ""
        return " ".join(str(context.get("active_theme") or "").split())[:80]

    async def _revise(self, directive: BehaviorDirective, user_text: str) -> bool:
        controller = self.controller
        if controller is None:
            return False
        if not bool(self._cfg.get("behavior_directive.allow_revision", True)):
            return False
        messages = controller.revision_messages(
            directive, user_text, persona_name=self._persona_name(),
        )
        try:
            raw = await self._generate(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Directive revision generation failed (%s)", self._source)
            return False
        return controller.apply_revision(directive, loose_json(raw), reason="user_revision")

    # -- prompt ----------------------------------------------------------
    def prompt_block(self, directive: BehaviorDirective | None) -> str:
        if not self.enabled or directive is None or directive.status.is_terminal:
            return ""
        block = directive_prompt_block(directive)
        if directive.completed_segment_count == 0:
            block += (
                "\n今回の応答は、この継続依頼の最初の区間として話す。"
                "一問一答で締めず、次の区間へ自然につながる終わり方にする。"
                "ユーザーの返事を待つ必要がある場合を除き、質問で終わらない。"
            )
        return block

    def note_opening_segment(
        self, directive: BehaviorDirective | None, spoken_text: str,
    ) -> None:
        """Record the reply the person just heard as this directive's turn."""
        controller = self.controller
        if controller is None or directive is None:
            return
        if directive.needs_user_input or directive.completed_segment_count:
            # A turn-based session speaks only in reply, so every reply counts;
            # otherwise only the opening one does and segments take over.
            controller.note_reply(directive, spoken_text)
        else:
            controller.note_opening_segment(directive, spoken_text)

    # -- segments --------------------------------------------------------
    async def tick(self) -> bool:
        """Run one segment if one is due.  ``True`` when the directive owns now."""
        controller = self.controller
        if controller is None or not controller.enabled:
            return False
        task = self._task
        if task is not None and not task.done():
            return True
        if not self._host.ready():
            # Not in a position to speak at all.  Keep the directive intact.
            return bool(controller.active(self.conversation_id))
        directive = controller.due_directive(
            self.conversation_id,
            floor_busy=bool(self._host.floor_busy()),
            playback_ahead_s=float(self._host.playback_ahead_s()),
            queued_segments=self._queued_segments,
        )
        if directive is None:
            if controller.active(self.conversation_id) is not None:
                # Alive but not due yet (waiting for an event, the floor or
                # playback).  Keep the clock running without an LLM call.
                self._host.request_tick(self.tick_delay())
                return True
            return False
        self.active_directive = directive
        self._task = asyncio.create_task(
            self._run_segment(directive), name=f"directive-segment:{directive.directive_id}",
        )
        self._task.add_done_callback(self._clear_task)
        return True

    def _clear_task(self, task: asyncio.Task) -> None:
        if self._task is task:
            self._task = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _run_segment(self, directive: BehaviorDirective) -> None:
        controller = self.controller
        if controller is None:
            return
        context = self._mind.directive_segment_context(self._source)
        try:
            outcome = await controller.execute_next_segment(
                directive,
                generate=self._generate,
                speak=self._speak,
                persona_name=self._persona_name(),
                conversation_tail=self._host.conversation_tail(),
                memory_notes=context.get("memory_notes") or [],
                interests=context.get("interests") or [],
                affect_summary=str(context.get("affect_summary") or ""),
                run_tool=self._host.run_tool,
            )
        except asyncio.CancelledError:
            controller.pause(directive, "human_speech")
            raise
        self._emit(
            "directive_segment", source=self._source,
            directive_id=outcome.directive_id, segment_id=outcome.segment_id,
            status=outcome.status, action=outcome.action, topic=outcome.topic,
            reason=outcome.reason,
        )
        if outcome.status in {"spoke", "waited", "tool"}:
            self._host.request_tick(self.tick_delay())

    async def _speak(self, text: str, segment_id: str, state_version: int) -> bool:
        directive = self.active()
        if directive is None or not directive.accepts(state_version=state_version):
            return False
        if self._host.floor_busy():
            return False
        self._queued_segments += 1
        try:
            spoken = await self._host.speak(text, segment_id, state_version)
        finally:
            self._queued_segments = max(0, self._queued_segments - 1)
        # A state bump during synthesis means the words are already stale.
        return bool(spoken) and directive.accepts(state_version=state_version)

    # -- interruption and teardown ---------------------------------------
    @staticmethod
    def _cancel_task(task: asyncio.Task | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()

        def consume(done: asyncio.Task) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.exception()

        task.add_done_callback(consume)

    def _discard_surface_work(self, directive: BehaviorDirective | None) -> None:
        """Invalidate every async/audio artifact belonging to a directive."""
        self._cancel_task(self._task)
        self._task = None
        self._cancel_task(self._pending)
        self._pending = None
        if directive is not None:
            self._host.cancel_generation(f"directive:{directive.directive_id}")
        self._host.discard_pending_audio()
        self._queued_segments = 0
        self.active_directive = None

    def _stop_for_user(self, user_text: str) -> None:
        """Apply an explicit stop even when no directive is currently active.

        The no-active case matters: a completed radio request can still have a
        plan task about to commit, and the word 「ラジオ」 in
        「もうラジオは終わった」 must never create another directive.
        """
        controller = self.controller
        directive = None if controller is None else controller.active(self.conversation_id)
        self._discard_surface_work(directive)
        if controller is not None and directive is not None:
            controller.cancel(directive, "user_stop_request")
        self._host.on_user_stop(user_text)
        logger.info(
            "BehaviorDirective user stop source=%s directive=%s "
            "(plan/segment/generation/audio discarded)",
            self._source, "none" if directive is None else directive.directive_id,
        )
        self._emit(
            "directive_user_turn", source=self._source, action=str(DirectiveAction.CANCEL),
            reason="stop_request", directive_id="" if directive is None else directive.directive_id,
        )

    def pause_for_human(
        self, reason: str = "human_speech", *, discard_audio: bool = True,
    ) -> None:
        """Stop producing.  ``discard_audio`` decides whether to also forget.

        This is called twice for one human turn, and the difference matters.

        At VAD onset nobody knows yet whether 「うん」 is a backchannel or a
        real interruption, so the only safe action is to stop *making* more:
        pause the directive and cancel the segment task, but keep the audio
        that is already synthesized.  Throwing it away there meant a
        backchannel silenced the assistant permanently — ``resume_from_pause``
        had nothing left to resume, which is exactly what 第7条 forbids.

        Once STT has confirmed a real interruption the caller asks again with
        ``discard_audio=True``, and only then is the old plan's audio dropped.
        """
        controller = self.controller
        directive = None if controller is None else controller.active(self.conversation_id)
        if directive is None:
            return
        controller.pause(directive, reason)
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._queued_segments = 0
        if not discard_audio:
            return
        # Bumping the state version makes every in-flight segment and queued
        # audio job stale, so nothing from the old plan is heard later.
        self._host.cancel_generation(f"directive:{directive.directive_id}")
        self._host.discard_pending_audio()

    def resume_after_backchannel(self) -> BehaviorDirective | None:
        """The noise was 「うん」.  Hand the floor straight back.

        Without this the directive stayed PAUSED after every backchannel: the
        reply path is what normally resumes it, and a backchannel deliberately
        never reaches the reply path.  One 「うん」 ended the radio show.
        """
        controller = self.controller
        directive = None if controller is None else controller.active(self.conversation_id)
        if directive is None or directive.status.is_terminal:
            return None
        controller.resume(directive)
        self.active_directive = directive
        return directive

    def stop(self, reason: str) -> None:
        """End the directive because this surface can no longer honour it."""
        controller = self.controller
        directive = None if controller is None else controller.active(self.conversation_id)
        self._discard_surface_work(directive)
        if directive is not None:
            controller.cancel(directive, reason)

    def note_environment_event(self, summary: str, *, importance: float = 0.6) -> bool:
        controller = self.controller
        if controller is None:
            return False
        accepted = controller.note_environment_event(
            summary, conversation_id=self.conversation_id, importance=importance,
        )
        if accepted:
            self._host.request_tick(0.8)
        return accepted

    def response_id_for(self, directive: BehaviorDirective, segment_id: str) -> str:
        return f"directive:{directive.directive_id}:{segment_id}"

    def snapshot(self) -> dict[str, Any]:
        controller = self.controller
        if controller is None:
            return {"enabled": False}
        value = controller.snapshot(self.conversation_id)
        value["source"] = self._source
        value["queued_segments"] = self._queued_segments
        return value
