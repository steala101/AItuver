"""Rule-first autonomous action selection.

The caller asks this layer before generating optional speech.  It is purposely
conservative: silence, low-value comments, stale events, and private state all
prefer ``DO_NOTHING`` over an unnecessary LLM call.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.autonomy.events import AutonomyEventBus
from neuro_voice.autonomy.state import AgentState
from neuro_voice.autonomy.types import ActionCandidate, ActionType, AutonomyEvent, AutonomyEventType, PrivacyScope, TriggerReason
from neuro_voice.dialogue.intent_plan import is_stop_request

logger = logging.getLogger(__name__)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    event_id: str
    action_type: ActionType
    trigger_reason: TriggerReason
    utility: float
    should_speak: bool
    should_call_llm: bool
    reason_code: str
    target_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class UtilityScorer:
    """Explainable action utility; privacy/safety are not persona parameters."""

    def __init__(self, cfg) -> None:
        self._min_speech = float(cfg.get("autonomy.min_speech_utility", .68))
        self._min_action = float(cfg.get("autonomy.min_action_utility", .58))
        self._nothing_base = float(cfg.get("autonomy.do_nothing_base_score", .45))
        self._group_bonus = float(cfg.get("autonomy.group_speech_threshold_bonus", .12))

    def score(self, candidate: ActionCandidate, *, group: bool, human_speaking: bool,
              assistant_busy: bool, speech_ratio: float) -> float:
        value = (
            candidate.relevance * .18 + candidate.usefulness * .16 + candidate.goal_progress * .12
            + candidate.novelty * .08 + candidate.personality_fit * .05 + candidate.social_value * .10
            + candidate.timing_quality * .12 + candidate.continuity * .10
            - candidate.interruption_cost * .16 - candidate.repetition * .12
            - candidate.privacy_risk * .35 - candidate.hallucination_risk * .18
            - candidate.verbosity_risk * .07 - candidate.social_dominance * .16
            - candidate.stale_event_penalty * .30
        )
        # The terms above express quality dimensions, not a probability.  A
        # modest action prior keeps a clearly relevant answer/result from
        # losing solely because it has no "goal" field populated yet.
        if candidate.action_type is not ActionType.DO_NOTHING:
            value += .18
        if candidate.action_type is ActionType.DO_NOTHING:
            value = max(value, self._nothing_base + (.20 if human_speaking or assistant_busy else 0.0))
        if candidate.should_speak:
            if group:
                value -= self._group_bonus
            value -= max(0.0, speech_ratio - .30) * .45
            if human_speaking or assistant_busy:
                value -= .75
        return _clamp(value)

    def allowed(self, candidate: ActionCandidate, utility: float, *, group: bool) -> bool:
        threshold = self._min_speech + (self._group_bonus if group else 0.0) if candidate.should_speak else self._min_action
        return utility >= threshold


class AutonomousActionSystem:
    _QUESTION = re.compile(r"[?？]|(?:教えて|どう|なに|何|どこ|いつ|なぜ|なんで|できますか|してくれる)")
    _ACK = re.compile(r"^(?:うん|はい|ええ|あー|なるほど|そう|了解|ok)[!！。 ]*$", re.I)
    _DIRECTIVE = re.compile(r"(?:流して|止めて|再生|音量|上げて|下げて|検索して|調べて|設定して|して$)")
    _CONTINUATION_REQUEST = re.compile(
        r"(?:ラジオ風|ラジオみたい).{0,20}(?:喋|しゃべ|話)"
        r"|(?:しばらく|少しの間|準備が終わるまで|作業中).{0,20}(?:喋|しゃべ|話して)"
        r"|(?:話し続け|喋り続け|しゃべり続け|何か話してて)"
    )
    _CONTINUATION_STOP = re.compile(r"(?:もういい|止めて|黙って|話すのをやめ|ラジオ.*終了)")
    _VAGUE_TOPIC = re.compile(
        r"^(?:それ|あれ|これ|話|こと|内容|続き|その後|その話|あの話|この話|"
        r"さっき言ってた.*|前に言ってた.*|また覚えてるって話)$"
    )

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self.enabled = bool(cfg.get("autonomous_action_system.enabled", True))
        self.legacy_enabled = bool(cfg.get("legacy_spontaneous_mode.enabled", False))
        if self.enabled and self.legacy_enabled:
            logger.warning("Legacy spontaneous mode disabled because autonomy is enabled")
            self.legacy_enabled = False
        self.state = AgentState(
            max_open_threads=int(cfg.get("autonomy.open_thread_max_active", 10)),
            max_agenda_items=int(cfg.get("autonomy.micro_agenda_max_items", 8)),
            max_recent_events=int(cfg.get("autonomy.recent_event_max_items", 30)),
            max_callbacks=int(cfg.get("autonomy.open_thread_max_callbacks", 2)),
            default_thread_ttl_s=float(cfg.get("autonomy.open_thread_default_ttl_minutes", 30)) * 60,
        )
        self.events = AutonomyEventBus(max_recent=int(cfg.get("autonomy.recent_event_max_items", 30)))
        self.events.subscribe(self.state.ingest)
        self._scorer = UtilityScorer(cfg)
        self._questions_at: deque[float] = deque()
        self._silence_speech_at: deque[float] = deque()
        self._last_question_at = 0.0
        self._last_silence_evaluation = 0.0
        self._silence_started_at = 0.0
        self._last_open_thread_recall_at = 0.0
        self._recent_spontaneous_kinds: deque[str] = deque(maxlen=6)
        # What was actually said, so the next idle move can be told to avoid it.
        self._recent_openings: deque[str] = deque(maxlen=5)
        # Idle initiative is a dialogue move, not a free-running monologue.
        # Keep the grounded subject and the performed move separately so the
        # selector can choose a different subject/shape or remain silent.
        self._recent_focus_signatures: deque[str] = deque(maxlen=10)
        self._recent_initiative_moves: deque[str] = deque(maxlen=8)
        self._conversation_context: dict[str, Any] = {}
        self._continuation: dict[str, Any] | None = None
        self._quiet_until = 0.0
        self._quiet_reason = ""
        self.metrics: dict[str, int] = {
            "events": 0, "llm_calls": 0, "speech": 0, "do_nothing": 0, "wait": 0,
            "open_threads_created": 0, "open_threads_recalled": 0, "privacy_blocked": 0,
            "heartbeat_evaluations": 0, "heartbeat_do_nothing": 0,
            "continuation_segments": 0, "fresh_topics_started": 0,
        }

    @property
    def active(self) -> bool:
        return self.enabled and not self.legacy_enabled

    def configure(self, *, enabled: bool | None = None) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if self.enabled and self.legacy_enabled:
            self.legacy_enabled = False

    def note_human_speech_started(self, *, now: float | None = None) -> None:
        self._silence_started_at = 0.0
        self._last_silence_evaluation = 0.0

    def note_human_speech_ended(self, *, now: float | None = None) -> None:
        self._silence_started_at = time.monotonic() if now is None else now

    def suppress_speech(
        self, *, duration_s: float, reason: str = "user_stop_request",
        now: float | None = None,
    ) -> None:
        """Cancel legacy continuation and honour a request for quiet time."""
        now = time.monotonic() if now is None else now
        self._continuation = None
        self._quiet_until = max(self._quiet_until, now + max(0.0, float(duration_s)))
        self._quiet_reason = str(reason or "user_stop_request")[:80]
        self._silence_started_at = now
        self._last_silence_evaluation = now
        logger.info(
            "Autonomy speech suppressed reason=%s duration=%.1fs",
            self._quiet_reason, max(0.0, float(duration_s)),
        )

    def update_context(self, context: dict[str, Any] | None) -> None:
        """Refresh bounded present-time seeds used by idle topic selection."""
        context = context if isinstance(context, dict) else {}
        self._conversation_context = {
            "active_theme": str(context.get("active_theme") or "")[:100],
            "continuing_thought": str(context.get("continuing_thought") or "")[:180],
            "recent_topics": [str(item)[:80] for item in (context.get("recent_topics") or [])[-4:]],
            "interests": [str(item)[:80] for item in (context.get("interests") or [])[-5:]],
            "time_context": str(context.get("time_context") or "")[:40],
            "mood": str(context.get("mood") or "")[:60],
            "recent_reflection": str(context.get("recent_reflection") or "")[:180],
        }

    def note_conversation_turn(
        self, user_text: str, assistant_text: str = "", *, now: float | None = None,
    ) -> bool:
        """Start or cancel a bounded user-requested continuation session."""
        now = time.monotonic() if now is None else now
        text = " ".join(str(user_text or "").split())
        if is_stop_request(text) or self._CONTINUATION_STOP.search(text):
            self.suppress_speech(
                duration_s=float(self._cfg.get("autonomy.post_stop_quiet_seconds", 300)),
                now=now,
            )
            return False
        if self._CONTINUATION_REQUEST.search(text):
            segments = max(1, min(6, int(self._cfg.get(
                "autonomy.explicit_continuation_segments", 3,
            ))))
            self._continuation = {
                "request": text[:220],
                "previous_segment": str(assistant_text or "")[-500:],
                "remaining": segments,
                "segment_index": 0,
                "next_due_at": now + max(.5, float(self._cfg.get(
                    "autonomy.explicit_continuation_interval_s", 2.5,
                ))),
            }
            return True
        if self._continuation is not None and not self._ACK.fullmatch(text):
            # A substantive new turn takes the floor. A backchannel may let
            # the explicitly requested monologue continue.
            self._continuation = None
        return False

    @property
    def continuation_active(self) -> bool:
        return bool(self._continuation and int(self._continuation.get("remaining", 0)) > 0)

    def add_grounded_followup(
        self, *, topic: str, summary: str, owner_user_id: str | None,
        callback_after_s: float = 90.0, privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION,
        source_excerpt: str = "",
    ) -> bool:
        """Bridge an already-grounded working-memory item into idle recall."""
        topic = " ".join(str(topic or "").split())
        summary = " ".join(str(summary or "").split())
        if (
            len(topic) < 3
            or not summary
            or self._VAGUE_TOPIC.fullmatch(topic)
            or re.search(r"^(?:さっき|前に).{0,20}(?:話|言)", topic)
        ):
            return False
        if source_excerpt:
            self._conversation_context["last_followup_grounding"] = str(source_excerpt)[:180]
        before = len(self.state.open_threads)
        item = self.state.open_thread(
            topic=topic, summary=summary, owner_user_id=owner_user_id,
            importance=.72, privacy_scope=privacy_scope,
            callback_after_s=callback_after_s,
            source_excerpt=source_excerpt,
        )
        created = item is not None and len(self.state.open_threads) > before
        if created:
            self.metrics["open_threads_created"] += 1
        return created

    def heartbeat(self, *, source: str, group: bool, human_speaking: bool,
                  assistant_busy: bool, now: float | None = None) -> AutonomyDecision:
        """Evaluate the current silence with a fresh non-expiring event.

        This deliberately does not call an LLM.  ``DO_NOTHING`` is a normal
        result and the next scheduler tick will evaluate again.
        """
        now = time.monotonic() if now is None else now
        if self._silence_started_at <= 0:
            self._silence_started_at = now
        silence_ms = max(0, round((now - self._silence_started_at) * 1000))
        short = int(self._cfg.get("autonomy.silence_short_ms", 5000))
        medium = int(self._cfg.get("autonomy.silence_medium_ms", 15000))
        long = int(self._cfg.get("autonomy.silence_long_ms", 30000))
        extended = int(self._cfg.get("autonomy.silence_extended_ms", 90000))
        stage = "short" if silence_ms < medium else "medium" if silence_ms < long else "long" if silence_ms < extended else "extended"
        event = AutonomyEvent(
            AutonomyEventType.SILENCE_CONTINUED, source,
            payload={"silence_duration_ms": silence_ms, "silence_stage": stage, "short_ms": short},
            timestamp=now,
        )
        self.metrics["heartbeat_evaluations"] += 1
        decision = self.publish(event, group=group, human_speaking=human_speaking,
                                assistant_busy=assistant_busy, now=now)
        if decision.action_type is ActionType.DO_NOTHING:
            self.metrics["heartbeat_do_nothing"] += 1
        return decision

    def publish(self, event: AutonomyEvent, **context) -> AutonomyDecision:
        if not self.active:
            return self._decision(event, ActionType.DO_NOTHING, TriggerReason.OBSERVED_EVENT, 1.0, "disabled")
        if not self.events.publish(event):
            return self._decision(event, ActionType.DO_NOTHING, TriggerReason.OBSERVED_EVENT, 1.0, "expired_or_duplicate")
        self.metrics["events"] += 1
        before = len(self.state.open_threads)
        decision = self.decide(event, **context)
        self.metrics["open_threads_created"] += max(0, len(self.state.open_threads) - before)
        self.state.record_action(decision.action_type.value, spoke=decision.should_speak)
        if decision.action_type is ActionType.DO_NOTHING:
            self.metrics["do_nothing"] += 1
        if decision.action_type is ActionType.WAIT:
            self.metrics["wait"] += 1
        return decision

    def decide(self, event: AutonomyEvent, *, group: bool = False, human_speaking: bool = False,
               assistant_busy: bool = False, speech_ratio: float = 0.0,
               now: float | None = None) -> AutonomyDecision:
        # Event timestamps are monotonic and are the authoritative time for
        # expiry/callback ordering.  Using a later wall clock here can make a
        # queued event look expired before it is even evaluated in tests or
        # after a delayed dispatcher hand-off.
        now = event.timestamp if now is None else now
        if event.expired(now=now):
            return self._decision(event, ActionType.DO_NOTHING, TriggerReason.OBSERVED_EVENT, 1.0, "stale_event")
        if (
            now < self._quiet_until
            and event.event_type in {
                AutonomyEventType.SILENCE_STARTED,
                AutonomyEventType.SILENCE_CONTINUED,
                AutonomyEventType.OPEN_THREAD_DUE,
            }
        ):
            return self._decision(
                event, ActionType.DO_NOTHING, TriggerReason.OBSERVED_EVENT, 1.0,
                f"quiet_after_stop:{self._quiet_reason}",
                details={"quiet_remaining_s": round(self._quiet_until - now, 1)},
            )
        candidates = self._candidates(event, group=group, now=now)
        candidates.append(ActionCandidate(ActionType.DO_NOTHING, TriggerReason.OBSERVED_EVENT,
                                          relevance=.2, timing_quality=.6, interruption_cost=.1 if group else 0.0))
        for candidate in candidates:
            candidate.utility = self._scorer.score(candidate, group=group, human_speaking=human_speaking,
                                                    assistant_busy=assistant_busy, speech_ratio=speech_ratio)
        selected = max(candidates, key=lambda candidate: candidate.utility)
        if selected.action_type is not ActionType.DO_NOTHING and not self._scorer.allowed(selected, selected.utility, group=group):
            selected = next(item for item in candidates if item.action_type is ActionType.DO_NOTHING)
        if selected.privacy_risk >= .5:
            self.metrics["privacy_blocked"] += 1
            selected = next(item for item in candidates if item.action_type is ActionType.DO_NOTHING)
        if selected.should_speak and selected.trigger_reason in {
            TriggerReason.OPEN_THREAD_DUE, TriggerReason.SILENCE_WITH_RELEVANT_CONTEXT,
            TriggerReason.SELF_INITIATED_TOPIC,
        }:
            cutoff = now - 300.0
            while self._silence_speech_at and self._silence_speech_at[0] < cutoff:
                self._silence_speech_at.popleft()
            maximum = max(0, int(self._cfg.get("autonomy.max_silence_triggered_speech_per_5min", 2)))
            if len(self._silence_speech_at) >= maximum:
                selected = next(item for item in candidates if item.action_type is ActionType.DO_NOTHING)
        return self._decision(event, selected.action_type, selected.trigger_reason, selected.utility,
                              f"{selected.trigger_reason.value}:{selected.action_type.value}",
                              target_id=selected.target_id, details=selected.details)

    def _candidates(self, event: AutonomyEvent, *, group: bool, now: float) -> list[ActionCandidate]:
        text = event.text.strip()
        if event.privacy_scope in {PrivacyScope.OWNER_AND_AI, PrivacyScope.DO_NOT_STORE}:
            return [ActionCandidate(ActionType.UPDATE_MEMORY, TriggerReason.OBSERVED_EVENT, usefulness=.7, privacy_risk=.55)]
        if event.event_type is AutonomyEventType.HUMAN_UTTERANCE_FINALIZED:
            if not text or self._ACK.fullmatch(text):
                return [ActionCandidate(ActionType.UPDATE_MEMORY, TriggerReason.OBSERVED_EVENT, usefulness=.62, timing_quality=.9)]
            if self._QUESTION.search(text) or self._DIRECTIVE.search(text):
                return [ActionCandidate(ActionType.ANSWER, TriggerReason.OBSERVED_EVENT, relevance=.98, usefulness=.90,
                                        timing_quality=.95, social_value=.75, novelty=.7)]
            return [ActionCandidate(ActionType.UPDATE_MEMORY, TriggerReason.OBSERVED_EVENT,
                                    relevance=.55, usefulness=.68, timing_quality=.8)]
        if event.event_type in {AutonomyEventType.SILENCE_STARTED, AutonomyEventType.SILENCE_CONTINUED, AutonomyEventType.OPEN_THREAD_DUE}:
            if self._continuation and int(self._continuation.get("remaining", 0)) > 0:
                if now >= float(self._continuation.get("next_due_at", 0.0) or 0.0):
                    return [ActionCandidate(
                        ActionType.START_TOPIC, TriggerReason.EXPLICIT_CONTINUATION,
                        relevance=.99, usefulness=.86, goal_progress=.92, novelty=.82,
                        personality_fit=.82, social_value=.90, timing_quality=.96,
                        continuity=.99, interruption_cost=.04,
                        details={
                            "kind": "explicit_continuation",
                            "request": str(self._continuation.get("request", "")),
                            "previous_segment": str(self._continuation.get("previous_segment", "")),
                            "segment_index": int(self._continuation.get("segment_index", 0)) + 1,
                        },
                    )]
                return []
            if event.event_type is AutonomyEventType.SILENCE_CONTINUED:
                min_s = float(self._cfg.get("autonomy.silence_evaluation_min_ms", 5000)) / 1000
                recheck = float(self._cfg.get("autonomy.silence_reevaluation_ms", 15000)) / 1000
                if now - self._last_silence_evaluation < min_s or (self._last_silence_evaluation and now - self._last_silence_evaluation < recheck):
                    return []
                self._last_silence_evaluation = now
            silence_ms = int(event.payload.get("silence_duration_ms", 0) or 0)
            group_min = int(self._cfg.get("autonomy.group_idle_min_silence_ms", 30000))
            if group and silence_ms < group_min:
                return []
            speech_cooldown_s = max(5.0, float(self._cfg.get(
                "autonomy.spontaneous_speech_cooldown_ms", 60_000,
            )) / 1000.0)
            if self.state.last_spoken_at and now - self.state.last_spoken_at < speech_cooldown_s:
                return []
            candidates: list[ActionCandidate] = []
            due = (
                self.state.due_threads(now=now, group=group)
                if bool(self._cfg.get("autonomy.open_thread_callbacks_enabled", True))
                else []
            )
            recall_cooldown_s = max(60.0, float(self._cfg.get(
                "autonomy.open_thread_recall_cooldown_ms", 900_000,
            )) / 1000.0)
            recent_recall = "open_thread" in tuple(self._recent_spontaneous_kinds)[-4:]
            if due and not recent_recall and (
                not self._last_open_thread_recall_at
                or now - self._last_open_thread_recall_at >= recall_cooldown_s
            ):
                item = due[0]
                candidates.append(ActionCandidate(
                    ActionType.RECALL_OPEN_THREAD, TriggerReason.OPEN_THREAD_DUE,
                    relevance=.76,
                    usefulness=.62,
                    novelty=.50,
                    timing_quality=.68,
                    continuity=.82,
                    social_value=.58,
                    repetition=.14 if self._recent_spontaneous_kinds else 0.0,
                    target_id=item.thread_id,
                    details={
                        "kind": "open_thread",
                        "topic": item.topic,
                        "summary": item.summary,
                        "source_excerpt": item.source_excerpt,
                        "elapsed_minutes": int(
                            max(0.0, time.time() - item.source_wall_time) / 60
                        ),
                    },
                ))
            fresh_min_ms = int(self._cfg.get("autonomy.fresh_topic_min_silence_ms", 30000))
            fresh = self._fresh_topic_candidate(
                silence_ms=silence_ms, minimum_ms=fresh_min_ms, now=now,
            )
            if fresh is not None:
                candidates.append(fresh)
            return candidates
        if event.event_type in {AutonomyEventType.GAME_EVENT, AutonomyEventType.GAME_GOAL_PROGRESS,
                                AutonomyEventType.GAME_GOAL_COMPLETED, AutonomyEventType.GAME_GOAL_BLOCKED}:
            priority = _clamp(float(event.payload.get("priority", .6)))
            action = ActionType.REPORT_RESULT if event.event_type in {AutonomyEventType.GAME_GOAL_COMPLETED, AutonomyEventType.GAME_GOAL_BLOCKED} else ActionType.COMMENT
            reason = TriggerReason.GOAL_COMPLETED if event.event_type is AutonomyEventType.GAME_GOAL_COMPLETED else TriggerReason.OBSERVED_EVENT
            return [ActionCandidate(action, reason, relevance=priority, usefulness=.55 + priority * .35,
                                    goal_progress=priority, timing_quality=.72, social_value=.54, novelty=.72,
                                    details={"summary": str(event.payload.get("summary", ""))[:300]})]
        if event.event_type is AutonomyEventType.TOOL_RESULT:
            return [ActionCandidate(ActionType.REPORT_RESULT, TriggerReason.TOOL_RESULT_READY, relevance=.9,
                                    usefulness=.92, goal_progress=.75, timing_quality=.86, social_value=.75,
                                    details={"result": str(event.payload.get("result", ""))[:500]})]
        if event.event_type is AutonomyEventType.TOOL_FAILED:
            return [ActionCandidate(ActionType.REPORT_RESULT, TriggerReason.TOOL_RESULT_READY, relevance=.82,
                                    usefulness=.72, timing_quality=.78,
                                    details={"result": str(event.payload.get("error", ""))[:300]})]
        if event.event_type is AutonomyEventType.HUMAN_SPEECH_STARTED:
            return [ActionCandidate(ActionType.WAIT, TriggerReason.OBSERVED_EVENT, usefulness=.85, timing_quality=.98)]
        return []

    def _fresh_topic_candidate(
        self, *, silence_ms: int, minimum_ms: int, now: float,
    ) -> ActionCandidate | None:
        """Choose one unused, grounded subject and one conversational move.

        Passing every memory and interest to the model made it repeatedly pick
        the same salient fragment.  Selection therefore happens here, before
        generation.  Once the available subjects have been used, silence is
        preferred until the live context changes.
        """
        if silence_ms < minimum_ms:
            return None
        context = self._conversation_context
        active_theme = str(context.get("active_theme") or "").strip()
        if self._VAGUE_TOPIC.fullmatch(active_theme):
            active_theme = ""
        focuses: list[tuple[str, str]] = []

        def add(kind: str, value: Any) -> None:
            text = " ".join(str(value or "").split()).strip()
            if text and not self._VAGUE_TOPIC.fullmatch(text):
                focuses.append((kind, text[:180]))

        add("active_theme", active_theme)
        add("continuing_thought", context.get("continuing_thought"))
        for item in reversed(list(context.get("recent_topics") or [])[-4:]):
            add("recent_topic", item)
        for item in reversed(list(context.get("interests") or [])[-5:]):
            add("interest", item)
        add("recent_reflection", context.get("recent_reflection"))
        time_context = str(context.get("time_context") or "").strip()
        mood = str(context.get("mood") or "").strip()
        if time_context or mood:
            add("present_moment", " / ".join(x for x in (time_context, mood) if x))

        unique: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for kind, text in focuses:
            signature = self._focus_signature(text)
            if not signature or signature in seen:
                continue
            seen.add(signature)
            if self._focus_was_recent(signature):
                continue
            unique.append((kind, text, signature))
        if not unique:
            # Repeating a used subject is worse than a natural silence.
            return None
        kind, focus, signature = unique[0]
        supporting = unique[1][1] if len(unique) > 1 else ""
        can_question = self._can_initiate_question(now) and kind in {
            "active_theme", "continuing_thought", "recent_topic", "interest",
        }
        if can_question:
            move = (
                "share_then_ask"
                if not self._recent_initiative_moves
                or self._recent_initiative_moves[-1] != "question"
                else "observation"
            )
        elif kind == "present_moment":
            move = "present_observation"
        elif kind == "recent_reflection":
            move = "share_thought"
        else:
            move = "observation"
        streak = list(self._recent_spontaneous_kinds)[-3:].count("self_initiated")
        return ActionCandidate(
            ActionType.START_TOPIC, TriggerReason.SELF_INITIATED_TOPIC,
            relevance=.80, usefulness=.62, novelty=max(.42, .88 - .18 * streak),
            personality_fit=.76, social_value=.72, timing_quality=.80,
            continuity=.64, interruption_cost=.08, repetition=min(.45, .15 * streak),
            details={
                "kind": "self_initiated",
                "focus_kind": kind,
                "focus": focus,
                "focus_signature": signature,
                "supporting_context": supporting,
                "initiative_move": move,
                "question_allowed": move == "share_then_ask",
                "recent_openings": list(self._recent_openings),
            },
        )

    @staticmethod
    def _focus_signature(text: str) -> str:
        return re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)[:160]

    def _focus_was_recent(self, signature: str) -> bool:
        if not signature:
            return True
        for previous in self._recent_focus_signatures:
            if signature == previous:
                return True
            if min(len(signature), len(previous)) >= 8 and (
                signature in previous or previous in signature
            ):
                return True
            left = {signature[i:i + 2] for i in range(max(0, len(signature) - 1))}
            right = {previous[i:i + 2] for i in range(max(0, len(previous) - 1))}
            union = left | right
            if union and len(left & right) / len(union) >= .72:
                return True
        return False

    def _can_initiate_question(self, now: float) -> bool:
        cutoff = now - 300.0
        while self._questions_at and self._questions_at[0] < cutoff:
            self._questions_at.popleft()
        maximum = max(0, int(self._cfg.get(
            "autonomy.max_self_initiated_questions_per_5min", 3,
        )))
        if maximum == 0 or len(self._questions_at) >= maximum:
            return False
        cooldown_s = max(0.0, float(self._cfg.get(
            "autonomy.question_cooldown_ms", 30_000,
        )) / 1000.0)
        if self._last_question_at and now - self._last_question_at < cooldown_s:
            return False
        max_consecutive = max(0, int(self._cfg.get(
            "autonomy.max_consecutive_questions", 2,
        )))
        consecutive = 0
        for move in reversed(self._recent_initiative_moves):
            if move != "question":
                break
            consecutive += 1
        return consecutive < max_consecutive

    def mark_speech_started(self, decision: AutonomyDecision, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.metrics["speech"] += 1
        if decision.should_call_llm:
            self.metrics["llm_calls"] += 1
        if decision.trigger_reason in {
            TriggerReason.OPEN_THREAD_DUE,
            TriggerReason.SILENCE_WITH_RELEVANT_CONTEXT,
            TriggerReason.SELF_INITIATED_TOPIC,
        }:
            self._silence_speech_at.append(now)
        if decision.action_type is ActionType.RECALL_OPEN_THREAD and decision.target_id:
            self.state.mark_thread_recalled(decision.target_id, now=now)
            self.metrics["open_threads_recalled"] += 1
            self._last_open_thread_recall_at = now
            self._recent_spontaneous_kinds.append("open_thread")
        elif decision.trigger_reason is TriggerReason.EXPLICIT_CONTINUATION and self._continuation:
            self._continuation["remaining"] = max(
                0, int(self._continuation.get("remaining", 0)) - 1,
            )
            self._continuation["segment_index"] = int(
                self._continuation.get("segment_index", 0),
            ) + 1
            self._continuation["next_due_at"] = now + max(.5, float(self._cfg.get(
                "autonomy.explicit_continuation_interval_s", 2.5,
            )))
            self._recent_spontaneous_kinds.append("explicit_continuation")
            self.metrics["continuation_segments"] += 1
        elif decision.trigger_reason is TriggerReason.SELF_INITIATED_TOPIC:
            kind = str(decision.details.get("kind") or "fresh_topic")
            self._recent_spontaneous_kinds.append(kind)
            self.metrics["fresh_topics_started"] += 1

    def record_autonomous_response(
        self, decision: AutonomyDecision, reply: str, *, now: float | None = None,
    ) -> None:
        text = str(reply or "").strip()
        if (
            decision.trigger_reason is TriggerReason.EXPLICIT_CONTINUATION
            and self._continuation is not None
            and text
        ):
            self._continuation["previous_segment"] = text[-500:]
        if text and decision.trigger_reason in {
            TriggerReason.SELF_INITIATED_TOPIC, TriggerReason.OPEN_THREAD_DUE,
            TriggerReason.SILENCE_WITH_RELEVANT_CONTEXT,
        }:
            # The next idle move is shown these verbatim and told not to reuse
            # them.  This is the concrete fix for repetitive self-talk.
            self._recent_openings.append(text[:60])
        if text and decision.trigger_reason is TriggerReason.SELF_INITIATED_TOPIC:
            signature = str(decision.details.get("focus_signature") or "").strip()
            if signature:
                self._recent_focus_signatures.append(signature)
            asked = self._looks_like_question(text)
            self._recent_initiative_moves.append("question" if asked else "statement")
            if asked:
                stamp = time.monotonic() if now is None else now
                self._questions_at.append(stamp)
                self._last_question_at = stamp

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        compact = " ".join(str(text or "").split())
        return bool(
            compact.endswith(("?", "？"))
            or re.search(
                r"(?:どう思う|どう感じる|どっち|なぜ|なんで|何が|教えて|聞かせて|気になる)[^。！]{0,12}[。！]?$",
                compact,
            )
        )

    def response_asked_question(self, text: str) -> bool:
        """Public turn-boundary query used by both Local and Discord."""
        return self._looks_like_question(text)

    def snapshot(self) -> dict[str, Any]:
        """Safe diagnostics for the UI/logs; no hidden reasoning is exposed."""
        return {
            "active": self.active,
            "legacy_enabled": self.legacy_enabled,
            "metrics": dict(self.metrics),
            "state": self.state.snapshot(),
            "recent_spontaneous_kinds": list(self._recent_spontaneous_kinds),
            "recent_initiative_moves": list(self._recent_initiative_moves),
            "recent_focus_count": len(self._recent_focus_signatures),
            "continuation": {
                "active": self.continuation_active,
                "remaining": int((self._continuation or {}).get("remaining", 0)),
                "segment_index": int((self._continuation or {}).get("segment_index", 0)),
            },
            "quiet_after_stop": {
                "active": time.monotonic() < self._quiet_until,
                "remaining_s": round(max(0.0, self._quiet_until - time.monotonic()), 1),
                "reason": self._quiet_reason,
            },
        }

    def autonomous_prompt(self, decision: AutonomyDecision) -> str:
        details = decision.details
        if decision.action_type is ActionType.RECALL_OPEN_THREAD:
            excerpt = str(details.get("source_excerpt") or "").strip()
            minutes = int(details.get("elapsed_minutes") or 0)
            when = (
                "さっき" if minutes < 10 else
                f"{minutes}分くらい前" if minutes < 60 else
                f"{minutes // 60}時間くらい前"
            )
            return (
                "[internal autonomous action: grounded callback]\n"
                "本人が未完了として残した話題を一度だけ回収する。\n"
                f"話題: {details.get('topic', '')}\n要約: {details.get('summary', '')}\n"
                + (f"本人の実際の発言: 「{excerpt}」\n" if excerpt else "")
                + f"経過: {when}\n"
                "話し方の必須条件:\n"
                "- 何の話かが、それだけ聞いて分かる形で言う。相手が思い出す作業を必要としない。\n"
                "- 上の『本人の実際の発言』にある具体語（固有名詞・目的・出来事）を必ず1つ以上入れる。\n"
                "- 『あれ』『さっきの話』『前の件』『例の』だけで指さない。\n"
                f"- いつの話かを『{when}』のように添えて、時間の位置を分かるようにする。\n"
                "- 尋問にしない。答えづらければ流していいと分かる軽さで、1〜2文。\n"
                "- 同じ確認を繰り返さない。相手が既に答えた内容を聞き直さない。"
            )
        if decision.trigger_reason is TriggerReason.EXPLICIT_CONTINUATION:
            return (
                "[internal autonomous action: explicit continuous talk]\n"
                "ユーザーは、しばらくラジオのように話し続けることを明示的に頼んでいる。\n"
                f"元の依頼: {details.get('request', '')}\n"
                f"直前に話した区間: {details.get('previous_segment', '')}\n"
                f"区間番号: {details.get('segment_index', 1)}\n"
                "直前の最後の考えから自然につながる新しい角度を2～4文で話す。"
                "同じ説明を繰り返さず、過去の未解決質問を持ち出さず、ユーザーへ質問しない。"
                "導入や『続きだけど』を毎回付けず、ラジオの一つの話として滑らかにつなぐ。"
            )
        if decision.trigger_reason is TriggerReason.SELF_INITIATED_TOPIC:
            focus = str(details.get("focus") or "").strip()
            supporting = str(details.get("supporting_context") or "").strip()
            move = str(details.get("initiative_move") or "observation")
            avoid = [str(item) for item in (details.get("recent_openings") or []) if str(item).strip()]
            avoid_block = (
                "\n直前に自分が言ったこと（言い回しも中身も繰り返さない）:\n"
                + "\n".join(f"- 「{item}」" for item in avoid[-3:])
                if avoid else ""
            )
            return (
                "[internal autonomous action: self-initiated present-time move]\n"
                "これは独り言ではなく、進行中の対話へ差し込む一つの会話行為。\n"
                f"今回の中心（これだけを根拠にする）: {focus}\n"
                + (f"補助的な現在文脈: {supporting}\n" if supporting else "")
                + f"今回の会話行為: {move}\n"
                + avoid_block
                + "\n\n守ること:\n"
                "- 中心から自分の感想・意見・連想を一つ出す。情報を説明するだけのナレーションにしない。\n"
                "- 前回と同じ入り方・同じ話題・同じ形にしない。\n"
                "- 相手が何かの途中（物語・ゲーム・作業）なら、その進行を横取りしない。"
                "相手の代わりに次の行動を決めたり、相手の番を埋めたりしない。\n"
                "- 過去の話の進捗確認にはしない。『さっき言ってた』『前に話した』『あれどうなった』は使わない。\n"
                "- 存在しない出来事や記憶を作らない。調べただけの話を自分の体験として語らない。\n"
                "- 単独で聞いて意味が通る1〜2文。長い独演にしない。\n"
                + (
                    "- 自分の考えを短く示したあと、この中心について相手が答えやすい具体的な質問を一つだけしてよい。"
                    "質問は対話を前へ進めるためのもので、進捗確認や尋問にしない。"
                    if bool(details.get("question_allowed"))
                    else "- 今回は質問を足さず、相手が返しても返さなくても成立する短い一手にする。"
                )
            )
        if decision.action_type is ActionType.REPORT_RESULT:
            return ("[internal autonomous action]\n結果を短く報告する。事実を増やさない。\n"
                    f"内容: {details.get('result') or details.get('summary') or ''}\n最大2文。")
        if decision.action_type is ActionType.COMMENT:
            return ("[internal autonomous action]\n今起きたゲーム上の出来事に短く役立つ反応を一言だけ返す。\n"
                    f"内容: {details.get('summary', '')}\n不要な追加質問はしない。")
        return "[internal autonomous action] 現在のイベントに対して短く自然に応答する。"

    @staticmethod
    def validate_llm_action(raw: str, *, max_chars: int = 140) -> dict[str, Any] | None:
        """Validate optional one-call structured output; errors safely mean silence."""
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0]
            data = json.loads(clean)
            action = ActionType(str((data.get("action") or {}).get("type", "")))
            speech = data.get("speech") or {}
            should_speak, text = bool(speech.get("should_speak", False)), str(speech.get("text", "") or "").strip()
            if len(text) > max_chars or (should_speak and not text):
                return None
            return {"action_type": action, "should_speak": should_speak, "text": text,
                    "reason_code": str((data.get("action") or {}).get("reason_code", "llm"))[:64]}
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _decision(event: AutonomyEvent, action: ActionType, reason: TriggerReason, utility: float,
                  reason_code: str, *, target_id: str | None = None, details: dict[str, Any] | None = None) -> AutonomyDecision:
        should_speak = action in {ActionType.ANSWER, ActionType.ASK_FOLLOWUP, ActionType.COMMENT,
                                  ActionType.MAKE_JOKE, ActionType.RECALL_OPEN_THREAD,
                                  ActionType.REPORT_RESULT, ActionType.START_TOPIC, ActionType.ACKNOWLEDGE}
        return AutonomyDecision(event.event_id, action, reason, round(_clamp(utility), 3), should_speak,
                                 should_speak and action is not ActionType.ANSWER, reason_code,
                                 target_id=target_id, details=details or {})
