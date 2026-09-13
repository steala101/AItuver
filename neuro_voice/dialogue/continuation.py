"""Generic continuation controller for every BehaviorDirective.

One controller runs a radio monologue, a game commentary session, a co-thinking
session and a quiet watch.  The difference between them lives entirely in the
directive's model-authored fields, not in a subclass.

The controller owns: the clock, segment sequencing, stale-output rejection,
limits, loop detection and status transitions.  It never decides *what* to say;
that is one structured LLM call per segment via the injected ``generate``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from neuro_voice.dialogue.directive import (
    BehaviorDirective,
    DirectiveAction,
    DirectiveRelation,
    DirectiveStatus,
    DirectiveStore,
    ExecutionMode,
    InteractionMode,
    PlanProposal,
    SearchPolicy,
    SegmentAction,
    SegmentActionType,
    SegmentLoopDetector,
    directive_from_plan,
    parse_segment_action,
    trim_echoed_opening,
)
from neuro_voice.dialogue.intent_plan import (
    build_revision_messages,
    build_segment_messages,
    classify_follow_up,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SegmentOutcome:
    """Result of one continuation segment, for logging and metrics."""

    directive_id: str
    segment_id: str
    state_version: int
    status: str = "waited"          # spoke / waited / finished / stale / error / tool
    action: str = str(SegmentActionType.WAIT)
    topic: str = ""
    spoken_chars: int = 0
    reason: str = ""
    tool_request: dict[str, Any] = field(default_factory=dict)

    @property
    def spoke(self) -> bool:
        return self.status == "spoke"


class ContinuationController:
    """Keep a directive moving without letting it run away."""

    def __init__(
        self,
        cfg,
        *,
        store: DirectiveStore | None = None,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self._cfg = cfg
        self.enabled = bool(cfg.get("behavior_directive.enabled", True))
        self.controller_enabled = bool(
            cfg.get("behavior_directive.continuation_controller_enabled", True)
        )
        self.store = store or DirectiveStore(
            max_active_per_conversation=int(
                cfg.get("behavior_directive.max_active_per_conversation", 1)
            ),
        )
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._loops: dict[str, SegmentLoopDetector] = {}
        self._recent_segments: dict[str, list[str]] = {}
        self._pending_events: dict[str, list[str]] = {}
        self._inflight: set[str] = set()
        self.metrics: dict[str, int] = {
            "directives_created": 0, "directives_completed": 0,
            "directives_cancelled": 0, "directives_failed": 0,
            "segments_generated": 0, "segments_spoken": 0, "segments_waited": 0,
            "segments_discarded_stale": 0, "directive_revisions": 0,
            "directive_pauses": 0, "directive_resumes": 0,
            "human_interruptions": 0, "directive_loops_detected": 0,
            "commentary_events_received": 0, "commentary_events_spoken": 0,
            "commentary_events_ignored": 0, "directive_rekeys": 0,
            "segment_echo_trimmed": 0,
        }

    # -- limits ----------------------------------------------------------
    @property
    def max_duration_s(self) -> float:
        return max(30.0, float(self._cfg.get("behavior_directive.max_duration_seconds", 1800)))

    @property
    def max_segments(self) -> int:
        return max(2, int(self._cfg.get("behavior_directive.max_segments", 100)))

    @property
    def max_consecutive_errors(self) -> int:
        return max(1, int(self._cfg.get("behavior_directive.max_consecutive_errors", 2)))

    @property
    def max_queued_segments(self) -> int:
        return max(1, int(self._cfg.get("behavior_directive.tts_max_queued_segments", 2)))

    @property
    def max_ahead_seconds(self) -> float:
        return max(2.0, float(self._cfg.get("behavior_directive.tts_max_ahead_seconds", 15)))

    # -- lifecycle -------------------------------------------------------
    def active(self, conversation_id: str = "local") -> BehaviorDirective | None:
        if not self.enabled:
            return None
        return self.store.active(conversation_id)

    def start_directive(
        self,
        plan: PlanProposal,
        *,
        conversation_id: str = "local",
        user_text: str = "",
        owner_user_id: str = "",
        source_turn_id: str = "",
        voice_session_id: str = "",
        audience: str = "local",
    ) -> BehaviorDirective | None:
        """Create and activate one directive.  ``None`` for a one-shot plan."""
        if not self.enabled or not plan.execution_mode.is_continuing:
            return None
        persist = bool(
            plan.execution_mode is ExecutionMode.ONGOING_DIRECTIVE
            and plan.needs_environment_events
            and self._cfg.get("behavior_directive.persist_activity_bound_tasks", True)
        )
        directive = directive_from_plan(
            plan,
            conversation_id=conversation_id,
            original_request=user_text,
            owner_user_id=owner_user_id,
            source_turn_id=source_turn_id,
            voice_session_id=voice_session_id,
            audience=audience,
            max_duration_seconds=self.max_duration_s,
            persist_across_restart=persist,
        )
        self.store.put(directive)
        directive.start()
        self._loops[directive.directive_id] = SegmentLoopDetector(
            max_repeated_topic=int(
                self._cfg.get("behavior_directive.max_repeated_topic_count", 2)
            ),
        )
        self._recent_segments[directive.directive_id] = []
        self._pending_events[directive.directive_id] = []
        self.metrics["directives_created"] += 1
        self._emit(
            "behavior_directive", event="created",
            directive=directive.snapshot(public=True),
        )
        logger.info(
            "Directive created id=%s mode=%s interaction=%s policy=%s duration=%s",
            directive.directive_id, directive.execution_mode,
            directive.interaction_mode, directive.continuation_policy,
            directive.duration_hint_seconds,
        )
        return directive

    def rekey(self, old_id: str, new_id: str) -> BehaviorDirective | None:
        """Follow a conversation whose key changed (speaker resolved/merged)."""
        directive = self.store.rekey(old_id, new_id)
        if directive is not None:
            self.metrics["directive_rekeys"] += 1
            self._emit(
                "behavior_directive", event="rekeyed",
                directive=directive.snapshot(public=True),
            )
        return directive

    def pause(self, directive: BehaviorDirective, reason: str) -> None:
        if directive.status is DirectiveStatus.PAUSED or directive.status.is_terminal:
            return
        directive.pause(reason)
        self.metrics["directive_pauses"] += 1
        if reason.startswith("human"):
            self.metrics["human_interruptions"] += 1
        self._emit(
            "behavior_directive", event="paused", reason=reason,
            directive=directive.snapshot(public=True),
        )

    def resume(self, directive: BehaviorDirective) -> None:
        if directive.status is not DirectiveStatus.PAUSED:
            return
        directive.resume()
        self.metrics["directive_resumes"] += 1
        self._emit(
            "behavior_directive", event="resumed",
            directive=directive.snapshot(public=True),
        )

    def finish(
        self, directive: BehaviorDirective, reason: str,
        *, status: DirectiveStatus = DirectiveStatus.COMPLETED,
    ) -> None:
        if directive.status.is_terminal:
            return
        directive.terminate(status, reason)
        self.store.retire(directive)
        self._loops.pop(directive.directive_id, None)
        self._recent_segments.pop(directive.directive_id, None)
        self._pending_events.pop(directive.directive_id, None)
        key = {
            DirectiveStatus.COMPLETED: "directives_completed",
            DirectiveStatus.FAILED: "directives_failed",
        }.get(status, "directives_cancelled")
        self.metrics[key] += 1
        self._emit(
            "behavior_directive", event="finished", reason=reason,
            directive=directive.snapshot(public=True),
        )
        logger.info(
            "Directive finished id=%s status=%s reason=%s segments=%s elapsed=%.0fs",
            directive.directive_id, status, reason,
            directive.completed_segment_count, directive.elapsed_seconds(),
        )

    def cancel(self, directive: BehaviorDirective, reason: str = "user_cancel") -> None:
        self.finish(directive, reason, status=DirectiveStatus.CANCELLED)

    def cancel_all_for_restart(self) -> int:
        count = self.store.cancel_all_for_restart()
        if count:
            self.metrics["directives_cancelled"] += count
        return count

    @property
    def min_duration_s(self) -> float:
        return max(0.0, float(self._cfg.get("behavior_directive.min_duration_seconds", 120)))

    @property
    def max_early_finishes(self) -> int:
        return max(0, int(self._cfg.get("behavior_directive.max_early_finishes", 2)))

    def _finish_is_premature(self, directive: BehaviorDirective) -> str:
        """Reason code when the model is wrapping up far too soon.

        Only applies to a requested monologue.  An event-driven or
        collaborative directive ends when its subject does, not on a clock.
        """
        if directive.execution_mode is not ExecutionMode.STREAMED_LONG_RESPONSE:
            return ""
        if directive.early_finish_count >= self.max_early_finishes:
            return ""
        elapsed = directive.elapsed_seconds()
        target = directive.duration_hint_seconds
        if target is not None and elapsed < min(float(target) * 0.6, self.min_duration_s):
            return "before_requested_duration"
        if target is None and elapsed < self.min_duration_s:
            return "before_minimum_duration"
        return ""

    # -- code-side stop conditions ---------------------------------------
    def code_stop_reason(
        self, directive: BehaviorDirective, *, now: float | None = None,
    ) -> str:
        now = time.time() if now is None else now
        if directive.expires_at and now > directive.expires_at:
            return "expired"
        if directive.completed_segment_count >= self.max_segments:
            return "max_segments"
        if directive.consecutive_error_count >= self.max_consecutive_errors:
            return "max_consecutive_errors"
        remaining = directive.remaining_seconds(now=now)
        if remaining is not None and remaining <= 0:
            return "duration_reached"
        if directive.elapsed_seconds(now=now) > self.max_duration_s:
            return "max_duration"
        return ""

    def due_directive(
        self,
        conversation_id: str = "local",
        *,
        now: float | None = None,
        floor_busy: bool = False,
        playback_ahead_s: float = 0.0,
        queued_segments: int = 0,
        has_environment_event: bool = False,
    ) -> BehaviorDirective | None:
        """Return the directive that should produce a segment right now."""
        if not (self.enabled and self.controller_enabled):
            return None
        directive = self.store.active(conversation_id)
        if directive is None:
            return None
        now = time.time() if now is None else now
        stop = self.code_stop_reason(directive, now=now)
        if stop:
            self.finish(
                directive, stop,
                status=DirectiveStatus.EXPIRED if stop == "expired"
                else DirectiveStatus.FAILED if stop == "max_consecutive_errors"
                else DirectiveStatus.COMPLETED,
            )
            return None
        if directive.status in {DirectiveStatus.PAUSED, DirectiveStatus.WAITING_FOR_USER}:
            return None
        if directive.directive_id in self._inflight:
            return None
        if floor_busy:
            # The human always owns the floor; a directive never queues over it.
            return None
        # TTS backpressure: never build a queue far ahead of what is playing.
        if queued_segments >= self.max_queued_segments:
            return None
        if playback_ahead_s >= self.max_ahead_seconds:
            return None
        if directive.needs_environment_events and not has_environment_event:
            pending = self._pending_events.get(directive.directive_id) or []
            if not pending:
                # Watching is a valid state.  Silence here is the correct output.
                if directive.status is not DirectiveStatus.WAITING_FOR_EVENT:
                    directive.status = DirectiveStatus.WAITING_FOR_EVENT
                    directive.updated_at = now
                return None
        if directive.status is DirectiveStatus.WAITING_FOR_EVENT:
            directive.status = DirectiveStatus.ACTIVE
        return directive

    # -- environment events ----------------------------------------------
    def note_environment_event(
        self, summary: str, *, conversation_id: str = "local", importance: float = 0.5,
    ) -> bool:
        """Feed one observed event to a directive that asked to watch."""
        directive = self.store.active(conversation_id)
        if directive is None or not directive.needs_environment_events:
            return False
        if directive.status.is_terminal or directive.status is DirectiveStatus.PAUSED:
            return False
        self.metrics["commentary_events_received"] += 1
        text = " ".join(str(summary or "").split())[:200]
        if not text:
            return False
        minimum = float(
            self._cfg.get("behavior_directive.observation_min_importance", 0.55)
        )
        if (
            directive.interaction_mode is InteractionMode.AMBIENT_WATCH
            and importance < minimum
        ):
            self.metrics["commentary_events_ignored"] += 1
            return False
        queue = self._pending_events.setdefault(directive.directive_id, [])
        if text in queue:
            return False
        queue.append(text)
        del queue[:-6]
        return True

    def note_opening_segment(self, directive: BehaviorDirective, spoken_text: str) -> bool:
        """Record the creating turn's reply as segment 1 of this directive.

        Without this the first thing the model says would not count toward
        progress and the next segment would happily repeat its opening.
        """
        text = " ".join(str(spoken_text or "").split())
        if not text or directive.status.is_terminal or directive.completed_segment_count:
            return False
        directive.completed_segment_count = 1
        directive.updated_at = time.time()
        recent = self._recent_segments.setdefault(directive.directive_id, [])
        recent.append(text[:200])
        del recent[:-4]
        detector = self._loops.setdefault(directive.directive_id, SegmentLoopDetector())
        detector.record(SegmentAction(
            action=SegmentActionType.SPEAK,
            spoken_content=text,
            topic=directive.current_topic,
        ))
        return True

    def note_reply(self, directive: BehaviorDirective, spoken_text: str) -> bool:
        """Record an ordinary reply as this directive's turn.

        In a turn-based session the reply already *is* the assistant's move.
        Generating a segment afterwards made the game master describe the same
        scene and ask the same question twice in a row.  Recording the reply
        and handing the floor back is what "交互に進める" actually means.
        """
        text = " ".join(str(spoken_text or "").split())
        if not text or directive.status.is_terminal:
            return False
        directive.completed_segment_count += 1
        directive.updated_at = time.time()
        directive.record_progress(text)
        recent = self._recent_segments.setdefault(directive.directive_id, [])
        recent.append(text[:200])
        del recent[:-4]
        detector = self._loops.setdefault(directive.directive_id, SegmentLoopDetector())
        detector.record(SegmentAction(
            action=SegmentActionType.SPEAK,
            spoken_content=text,
            topic=directive.current_topic,
        ))
        if directive.needs_user_input:
            directive.status = DirectiveStatus.WAITING_FOR_USER
        return True

    def note_user_turn_started(self, directive: BehaviorDirective) -> None:
        """The person answered, so the session may act again."""
        if directive.status is DirectiveStatus.WAITING_FOR_USER:
            directive.status = DirectiveStatus.ACTIVE
            directive.updated_at = time.time()

    def pending_events(self, directive: BehaviorDirective) -> list[str]:
        return list(self._pending_events.get(directive.directive_id, []))

    # -- user turns ------------------------------------------------------
    def handle_user_turn(
        self, text: str, *, conversation_id: str = "local",
    ) -> tuple[DirectiveAction, DirectiveRelation, str, BehaviorDirective | None]:
        """Classify a user turn against the running directive.

        The directive is *never* cancelled merely because a human spoke.  It is
        paused, the new turn is answered, and the kernel decides afterwards.
        """
        directive = self.store.active(conversation_id)
        if directive is None:
            return DirectiveAction.NONE, DirectiveRelation.NEW_REQUEST, "no_active_directive", None
        action, relation, reason = classify_follow_up(text, directive)
        if action is DirectiveAction.CANCEL:
            self.cancel(directive, "user_stop_request")
            return action, relation, reason, directive
        if action is DirectiveAction.PAUSE:
            self.pause(directive, "user_pause_request")
        elif action is DirectiveAction.RESUME:
            self.resume(directive)
        return action, relation, reason, directive

    def apply_revision(
        self, directive: BehaviorDirective, changes: dict[str, Any], *, reason: str = "user_revision",
    ) -> bool:
        applied = directive.revise(changes, reason=reason)
        if applied:
            self.metrics["directive_revisions"] += 1
            self._emit(
                "behavior_directive", event="revised", reason=reason,
                directive=directive.snapshot(public=True),
            )
        return applied

    def revision_messages(
        self, directive: BehaviorDirective, user_text: str, *, persona_name: str = "AI",
    ) -> list[dict[str, str]]:
        return build_revision_messages(directive, user_text, persona_name=persona_name)

    # -- segments --------------------------------------------------------
    async def execute_next_segment(
        self,
        directive: BehaviorDirective,
        *,
        generate: Callable[[list[dict[str, str]]], Awaitable[str]],
        speak: Callable[[str, str, int], Awaitable[bool]],
        persona_name: str = "AI",
        conversation_tail: Iterable[str] = (),
        memory_notes: Iterable[str] = (),
        interests: Iterable[str] = (),
        affect_summary: str = "",
        run_tool: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
    ) -> SegmentOutcome:
        """Generate and perform one segment.  Exactly one LLM call per segment."""
        segment_id = uuid4().hex[:8]
        state_version = directive.state_version
        outcome = SegmentOutcome(
            directive_id=directive.directive_id,
            segment_id=segment_id,
            state_version=state_version,
        )
        if directive.directive_id in self._inflight:
            outcome.status, outcome.reason = "stale", "already_inflight"
            return outcome
        self._inflight.add(directive.directive_id)
        detector = self._loops.setdefault(
            directive.directive_id, SegmentLoopDetector(),
        )
        events = self._pending_events.get(directive.directive_id, [])
        previous = self._recent_segments.get(directive.directive_id, [])
        try:
            # A story must be self-contained.  Handing a narration segment
            # unrelated memories is how an invented world from an earlier radio
            # programme walked into the middle of a TRPG session.
            narrative = directive.interaction_mode is InteractionMode.NARRATION
            messages = build_segment_messages(
                directive,
                persona_name=persona_name,
                recent_segments=previous,
                environment_events=events,
                conversation_tail=list(conversation_tail),
                memory_notes=[] if narrative else list(memory_notes),
                interests=[] if narrative else list(interests),
                affect_summary=affect_summary,
                loop_warning=directive.last_error if directive.last_error else "",
            )
            raw = await generate(messages)
            self.metrics["segments_generated"] += 1
            if not directive.accepts(state_version=state_version):
                # The directive changed while this segment was being written.
                self.metrics["segments_discarded_stale"] += 1
                outcome.status, outcome.reason = "stale", "state_version_changed"
                return outcome
            action = parse_segment_action(
                raw, allowed_tools=directive.allowed_tools,
                max_chars=int(self._cfg.get("behavior_directive.segment_max_chars", 500)),
            )
            if action is None:
                directive.consecutive_error_count += 1
                directive.last_error = "segment_json_unparseable"
                outcome.status, outcome.reason = "error", "segment_json_unparseable"
                return outcome
            outcome.action = str(action.action)
            outcome.topic = action.topic

            if action.action is SegmentActionType.FINISH:
                early = self._finish_is_premature(directive)
                if early:
                    # 「3分くらい話して」 and stopping after 40 seconds is not a
                    # natural ending, it is the model looking for an exit.  Ask
                    # for a new topic instead — but only a couple of times, or
                    # we would be arguing with it forever.
                    directive.early_finish_count += 1
                    directive.next_action_hint = (
                        "まだ依頼された長さの途中。締めずに、別の話題へ移って続ける。"
                    )
                    logger.info(
                        "Directive finish deferred (%s): elapsed=%.0fs id=%s",
                        early, directive.elapsed_seconds(), directive.directive_id,
                    )
                    outcome.status, outcome.reason = "waited", f"finish_deferred:{early}"
                    return outcome
                self.finish(directive, action.finish_reason or "model_finished")
                outcome.status = "finished"
                outcome.reason = action.finish_reason or "model_finished"
                return outcome

            if action.action is SegmentActionType.REVISE_PLAN:
                self.apply_revision(
                    directive, action.directive_revision_proposal, reason="model_revision",
                )
                directive.consecutive_error_count = 0
                outcome.status, outcome.reason = "waited", "plan_revised"
                return outcome

            if action.action is SegmentActionType.USE_TOOL:
                outcome.tool_request = dict(action.tool_request)
                if directive.search_policy is SearchPolicy.NO_SEARCH or run_tool is None:
                    directive.last_error = "tool_not_permitted"
                    outcome.status, outcome.reason = "waited", "tool_not_permitted"
                    return outcome
                directive.status = DirectiveStatus.WAITING_FOR_EVENT
                result = await run_tool(action.tool_request)
                if not directive.accepts(state_version=state_version):
                    self.metrics["segments_discarded_stale"] += 1
                    outcome.status, outcome.reason = "stale", "state_version_changed"
                    return outcome
                # Returning to the directive after a tool is the whole point;
                # a tool result must never become an orphan task.
                directive.status = DirectiveStatus.ACTIVE
                directive.progress_summary = (
                    f"{directive.progress_summary} / 調べた: {str(result)[:120]}"
                ).strip(" /")[:400]
                directive.consecutive_error_count = 0
                detector.record(action)
                outcome.status, outcome.reason = "tool", "tool_result_stored"
                return outcome

            if action.action is SegmentActionType.WAIT:
                directive.waited_segment_count += 1
                directive.consecutive_error_count = 0
                if action.wait_condition:
                    directive.next_action_hint = action.wait_condition
                self.metrics["segments_waited"] += 1
                if directive.needs_user_input:
                    # In a turn-based session "wait" means "it is the player's
                    # move", which is a state, not a retry.  Leaving it ACTIVE
                    # made the tick loop ask the model again every few seconds
                    # — 20 generations in 90s, all discarded, while the person
                    # waited behind them on a serial local LLM (D-017).
                    directive.status = DirectiveStatus.WAITING_FOR_USER
                outcome.status, outcome.reason = "waited", action.wait_condition or "model_wait"
                return outcome

            if action.action is SegmentActionType.ASK_USER:
                directive.status = DirectiveStatus.WAITING_FOR_USER

            # 「前の区間の最後の考えから続ける」と指示したうえで前の区間を見せる
            # ので、モデルはその締めの一文をそのまま言い直して始めることがある。
            # 聞いている側には同じ台詞が二度流れる。文字を比べれば分かることなので
            # プロンプトではなくコードで落とす (第2条)。
            if action.spoken_content and previous:
                kept = trim_echoed_opening(action.spoken_content, previous[-1])
                if kept != action.spoken_content:
                    self.metrics["segment_echo_trimmed"] += 1
                    logger.info(
                        "区間の冒頭が前区間の繰り返しだったため除去 (%d→%d文字)",
                        len(action.spoken_content), len(kept),
                    )
                    action = replace(action, spoken_content=kept)
                    if not kept:
                        # Nothing was left: this segment said only what the
                        # previous one already said.
                        detector.detections += 1
                        self.metrics["directive_loops_detected"] += 1
                        directive.last_error = "loop:echoed_previous_segment"
                        directive.waited_segment_count += 1
                        outcome.status = "waited"
                        outcome.reason = "echoed_previous_segment"
                        return outcome

            loop_reason = detector.would_loop(action)
            if loop_reason:
                detector.detections += 1
                self.metrics["directive_loops_detected"] += 1
                directive.last_error = f"loop:{loop_reason}"
                if directive.interaction_mode in {
                    InteractionMode.AMBIENT_WATCH, InteractionMode.ACTIVITY_COMMENTARY,
                }:
                    directive.waited_segment_count += 1
                    outcome.status, outcome.reason = "waited", loop_reason
                    return outcome
                if detector.detections >= max(
                    2, int(self._cfg.get("behavior_directive.max_loop_detections", 2)),
                ):
                    self.finish(directive, f"loop_detected:{loop_reason}")
                    outcome.status, outcome.reason = "finished", loop_reason
                    return outcome
                outcome.status, outcome.reason = "waited", loop_reason
                return outcome

            spoken = await speak(action.spoken_content, segment_id, state_version)
            if not spoken:
                self.metrics["segments_discarded_stale"] += 1
                outcome.status, outcome.reason = "stale", "playback_rejected"
                return outcome

            detector.record(action)
            directive.completed_segment_count += 1
            directive.consecutive_error_count = 0
            directive.last_error = ""
            if action.topic:
                directive.current_topic = action.topic
            if action.progress_update:
                directive.progress_summary = action.progress_update
            if action.purpose:
                directive.next_action_hint = action.purpose
            directive.updated_at = time.time()
            directive.record_progress(action.spoken_content)
            recent = self._recent_segments.setdefault(directive.directive_id, [])
            recent.append(action.spoken_content[:200])
            del recent[:-4]
            if events:
                self.metrics["commentary_events_spoken"] += 1
                events.clear()
            self.metrics["segments_spoken"] += 1
            outcome.status = "spoke"
            outcome.spoken_chars = len(action.spoken_content)
            self._emit(
                "behavior_directive", event="segment", segment_id=segment_id,
                action=outcome.action, topic=outcome.topic,
                directive=directive.snapshot(public=True),
            )
            return outcome
        except Exception as exc:  # pragma: no cover - defensive
            directive.consecutive_error_count += 1
            directive.last_error = str(exc)[:160]
            logger.exception("Directive segment failed id=%s", directive.directive_id)
            outcome.status, outcome.reason = "error", str(exc)[:120]
            return outcome
        finally:
            self._inflight.discard(directive.directive_id)
            stop = self.code_stop_reason(directive)
            if stop and not directive.status.is_terminal:
                self.finish(
                    directive, stop,
                    status=DirectiveStatus.FAILED if stop == "max_consecutive_errors"
                    else DirectiveStatus.EXPIRED if stop == "expired"
                    else DirectiveStatus.COMPLETED,
                )

    # -- reporting -------------------------------------------------------
    def health_status(self, conversation_id: str = "local") -> dict[str, Any]:
        directive = self.store.active(conversation_id)
        if directive is None:
            return {"active": False, "metrics": dict(self.metrics)}
        detector = self._loops.get(directive.directive_id)
        return {
            "active": True,
            "directive": directive.snapshot(public=True),
            "loop": detector.snapshot() if detector else {},
            "pending_events": len(self._pending_events.get(directive.directive_id, [])),
            "metrics": dict(self.metrics),
        }

    def snapshot(self, conversation_id: str = "local") -> dict[str, Any]:
        value = self.store.snapshot(conversation_id, public=True)
        value["enabled"] = self.enabled and self.controller_enabled
        value["metrics"] = dict(self.metrics)
        return value
