"""Generic continuation substrate: execution modes, plans and directives.

There is deliberately no ``RadioTalkEngine`` / ``GameCommentaryEngine`` /
``SilentWatchEngine`` here.  A radio monologue, a game commentary session and a
quiet watch are the *same* object with different model-authored fields.

Division of responsibility:

* the LLM decides what the user wants, how to proceed and what to say;
* this module owns state, clocks, versioning, limits and safety validation.

Nothing in this file calls an LLM or performs I/O.  It is pure state so it can
be unit tested without a model, audio device or event loop.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Iterable
from uuid import uuid4

logger = logging.getLogger(__name__)


class ExecutionMode(StrEnum):
    """How a response obligation is discharged over time."""

    ONE_SHOT_RESPONSE = "ONE_SHOT_RESPONSE"
    STREAMED_LONG_RESPONSE = "STREAMED_LONG_RESPONSE"
    ONGOING_DIRECTIVE = "ONGOING_DIRECTIVE"
    COLLABORATIVE_SESSION = "COLLABORATIVE_SESSION"
    OBSERVATION_DIRECTIVE = "OBSERVATION_DIRECTIVE"

    @property
    def is_continuing(self) -> bool:
        return self is not ExecutionMode.ONE_SHOT_RESPONSE


class InteractionMode(StrEnum):
    DIALOGUE = "DIALOGUE"
    MONOLOGUE = "MONOLOGUE"
    ACTIVITY_COMMENTARY = "ACTIVITY_COMMENTARY"
    CO_THINKING = "CO_THINKING"
    AMBIENT_WATCH = "AMBIENT_WATCH"
    #: Game master / storyteller.  Describes a scene, then hands the turn back
    #: and waits.  Distinct from CO_THINKING because the assistant must never
    #: decide what the other person's character does.
    NARRATION = "NARRATION"


class ContinuationPolicy(StrEnum):
    SINGLE_PASS = "SINGLE_PASS"
    UNTIL_DURATION_OR_INTERRUPTED = "UNTIL_DURATION_OR_INTERRUPTED"
    UNTIL_ACTIVITY_END_OR_CANCELLED = "UNTIL_ACTIVITY_END_OR_CANCELLED"
    UNTIL_GOAL_OR_CANCELLED = "UNTIL_GOAL_OR_CANCELLED"
    UNTIL_CANCELLED = "UNTIL_CANCELLED"


class DirectiveStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    WAITING_FOR_EVENT = "WAITING_FOR_EVENT"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PAUSED = "PAUSED"
    REVISING = "REVISING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CANCELLED_BY_RESTART = "CANCELLED_BY_RESTART"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DirectiveStatus.COMPLETED, DirectiveStatus.CANCELLED,
            DirectiveStatus.CANCELLED_BY_RESTART, DirectiveStatus.FAILED,
            DirectiveStatus.EXPIRED,
        }

    @property
    def is_runnable(self) -> bool:
        return self in {
            DirectiveStatus.ACTIVE, DirectiveStatus.WAITING_FOR_EVENT,
            DirectiveStatus.REVISING,
        }


class SearchPolicy(StrEnum):
    NO_SEARCH = "NO_SEARCH"
    NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED = "NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED"
    SEARCH_WHEN_NEEDED = "SEARCH_WHEN_NEEDED"


class SegmentActionType(StrEnum):
    SPEAK = "SPEAK"
    ASK_USER = "ASK_USER"
    USE_TOOL = "USE_TOOL"
    COMMENT_ON_EVENT = "COMMENT_ON_EVENT"
    WAIT = "WAIT"
    CHANGE_TOPIC = "CHANGE_TOPIC"
    REVISE_PLAN = "REVISE_PLAN"
    FINISH = "FINISH"

    @property
    def speaks(self) -> bool:
        return self in {
            SegmentActionType.SPEAK, SegmentActionType.ASK_USER,
            SegmentActionType.COMMENT_ON_EVENT, SegmentActionType.CHANGE_TOPIC,
        }


class DirectiveAction(StrEnum):
    NONE = "NONE"
    CREATE = "CREATE"
    CONTINUE = "CONTINUE"
    REVISE = "REVISE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    FINISH = "FINISH"
    STATUS = "STATUS"


class DirectiveRelation(StrEnum):
    NEW_REQUEST = "NEW_REQUEST"
    REVISION_OF_ACTIVE = "REVISION_OF_ACTIVE"
    RESPONSE_TO_ACTIVE = "RESPONSE_TO_ACTIVE"
    UNRELATED = "UNRELATED"
    CONFLICTING = "CONFLICTING"


#: Tools a plan may ever request.  A model-authored name outside this set is
#: dropped during validation; a directive never widens its own permissions.
ALLOWED_TOOL_NAMES = frozenset({
    "game_state_read_only",
    "vision_read_only",
    "memory_read_only",
    "web_search",
})

_MAX_LIST = 8
_MAX_TEXT = 240


def _clamp(value: Any, low: float = 0.0, high: float = 1.0, default: float = 0.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _string_list(value: Any, *, limit: int = _MAX_LIST, item_limit: int = 120) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for raw in value:
        item = _text(raw, item_limit)
        if item and item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _enum(cls, value: Any, default):
    try:
        return cls(str(value or "").strip().upper())
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Plan proposal
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PlanProposal:
    """A validated interpretation of what the user actually asked for."""

    understanding: str = ""
    goal: str = ""
    execution_mode: ExecutionMode = ExecutionMode.ONE_SHOT_RESPONSE
    interaction_mode: InteractionMode = InteractionMode.DIALOGUE
    continuation_policy: ContinuationPolicy = ContinuationPolicy.SINGLE_PASS
    duration_hint_seconds: float | None = None
    segment_target_seconds: float = 20.0
    needs_user_input: bool = False
    needs_environment_events: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    search_policy: SearchPolicy = SearchPolicy.NO_SEARCH
    style_constraints: list[str] = field(default_factory=list)
    content_constraints: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    initial_plan: list[str] = field(default_factory=list)
    uncertainty: float = 0.0
    clarification_question: str = ""

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "execution_mode", "interaction_mode", "continuation_policy", "search_policy",
        ):
            value[key] = str(value[key])
        return value


PLAN_SCHEMA_HINT = """{
  "understanding": "ユーザーが最終的に望んでいる体験を1文で",
  "goal": "その体験を成立させるための目的を1文で",
  "execution_mode": "ONE_SHOT_RESPONSE | STREAMED_LONG_RESPONSE | ONGOING_DIRECTIVE | COLLABORATIVE_SESSION | OBSERVATION_DIRECTIVE",
  "interaction_mode": "DIALOGUE | MONOLOGUE | ACTIVITY_COMMENTARY | CO_THINKING | AMBIENT_WATCH",
  "continuation_policy": "SINGLE_PASS | UNTIL_DURATION_OR_INTERRUPTED | UNTIL_ACTIVITY_END_OR_CANCELLED | UNTIL_GOAL_OR_CANCELLED | UNTIL_CANCELLED",
  "duration_hint_seconds": 180,
  "segment_target_seconds": 20,
  "needs_user_input": false,
  "needs_environment_events": false,
  "allowed_tools": [],
  "search_policy": "NO_SEARCH | NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED | SEARCH_WHEN_NEEDED",
  "style_constraints": ["話し方の方針"],
  "content_constraints": ["内容の制約"],
  "stop_conditions": ["終わる条件"],
  "success_criteria": ["成功と言える状態"],
  "initial_plan": ["最初の進め方の手順"],
  "uncertainty": 0.0,
  "clarification_question": null
}"""


def _loose_json(raw: str) -> dict[str, Any] | None:
    text = re.sub(r"```(?:json)?", "", str(raw or ""))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    body = text[start:end + 1]
    for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body)):
        try:
            data = json.loads(attempt)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def loose_json(raw: str) -> dict[str, Any]:
    """Public, forgiving JSON extraction for model output.  ``{}`` on failure."""
    return _loose_json(raw) or {}


def parse_plan_proposal(
    raw: str,
    *,
    search_allowed: bool = False,
    max_duration_seconds: float = 1800.0,
    default_duration_seconds: float = 300.0,
) -> PlanProposal | None:
    """Validate one model-authored plan.  ``None`` means "fall back to one shot".

    Validation never trusts the model for permissions: an unknown tool name is
    dropped and ``SEARCH_WHEN_NEEDED`` is downgraded unless the caller already
    established that this conversation may search.
    """
    data = _loose_json(raw)
    if data is None:
        return None
    mode = _enum(ExecutionMode, data.get("execution_mode"), ExecutionMode.ONE_SHOT_RESPONSE)
    policy = _enum(
        ContinuationPolicy, data.get("continuation_policy"),
        ContinuationPolicy.SINGLE_PASS if mode is ExecutionMode.ONE_SHOT_RESPONSE
        else ContinuationPolicy.UNTIL_DURATION_OR_INTERRUPTED,
    )
    if mode is ExecutionMode.ONE_SHOT_RESPONSE:
        policy = ContinuationPolicy.SINGLE_PASS
    elif policy is ContinuationPolicy.SINGLE_PASS:
        policy = ContinuationPolicy.UNTIL_DURATION_OR_INTERRUPTED

    duration = data.get("duration_hint_seconds")
    if duration in (None, "", "null"):
        duration_value = None
    else:
        duration_value = _clamp(duration, 5.0, float(max_duration_seconds), 0.0) or None
    if duration_value is None and mode is ExecutionMode.STREAMED_LONG_RESPONSE:
        # A monologue with no target has nothing to pace itself against and
        # wraps up after a couple of segments.  An event-driven directive is
        # different: "until the game ends" genuinely has no duration.
        duration_value = min(float(default_duration_seconds), float(max_duration_seconds))

    search = _enum(SearchPolicy, data.get("search_policy"), SearchPolicy.NO_SEARCH)
    if search is SearchPolicy.SEARCH_WHEN_NEEDED and not search_allowed:
        search = SearchPolicy.NO_SEARCH_UNLESS_EXPLICITLY_REQUESTED
    tools = [
        name for name in _string_list(data.get("allowed_tools"), item_limit=48)
        if name in ALLOWED_TOOL_NAMES
    ]
    if "web_search" in tools and search is SearchPolicy.NO_SEARCH:
        tools.remove("web_search")

    interaction_default = {
        ExecutionMode.STREAMED_LONG_RESPONSE: InteractionMode.MONOLOGUE,
        ExecutionMode.ONGOING_DIRECTIVE: InteractionMode.ACTIVITY_COMMENTARY,
        ExecutionMode.COLLABORATIVE_SESSION: InteractionMode.CO_THINKING,
        ExecutionMode.OBSERVATION_DIRECTIVE: InteractionMode.AMBIENT_WATCH,
    }.get(mode, InteractionMode.DIALOGUE)

    proposal = PlanProposal(
        understanding=_text(data.get("understanding")),
        goal=_text(data.get("goal")),
        execution_mode=mode,
        interaction_mode=_enum(
            InteractionMode, data.get("interaction_mode"), interaction_default,
        ),
        continuation_policy=policy,
        duration_hint_seconds=duration_value,
        segment_target_seconds=_clamp(
            data.get("segment_target_seconds", 20), 4.0, 60.0, 20.0,
        ),
        needs_user_input=bool(data.get("needs_user_input", False)),
        needs_environment_events=bool(data.get("needs_environment_events", False)),
        allowed_tools=tools,
        search_policy=search,
        style_constraints=_string_list(data.get("style_constraints")),
        content_constraints=_string_list(data.get("content_constraints")),
        stop_conditions=_string_list(data.get("stop_conditions")),
        success_criteria=_string_list(data.get("success_criteria")),
        initial_plan=_string_list(data.get("initial_plan"), limit=10),
        uncertainty=_clamp(data.get("uncertainty", 0.0)),
        clarification_question=_text(data.get("clarification_question"), 160),
    )
    if proposal.clarification_question.lower() in {"null", "none", "-"}:
        proposal.clarification_question = ""
    if not proposal.goal and not proposal.understanding:
        return None
    if proposal.execution_mode is ExecutionMode.OBSERVATION_DIRECTIVE:
        # Watching is not an excuse to fill silence with monologue.
        proposal.needs_environment_events = True
    return proposal


# --------------------------------------------------------------------------
# Segment action
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SegmentAction:
    action: SegmentActionType = SegmentActionType.WAIT
    spoken_content: str = ""
    topic: str = ""
    purpose: str = ""
    expected_duration_seconds: float = 0.0
    tool_request: dict[str, Any] = field(default_factory=dict)
    wait_condition: str = ""
    progress_update: str = ""
    directive_revision_proposal: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    confidence: float = 0.6

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value["action"] = str(value["action"])
        return value


SEGMENT_SCHEMA_HINT = """{
  "action": "SPEAK | ASK_USER | USE_TOOL | COMMENT_ON_EVENT | WAIT | CHANGE_TOPIC | REVISE_PLAN | FINISH",
  "spoken_content": "実際に声に出す日本語の本文 (SPEAK系のときだけ)",
  "topic": "この区間の話題を短く",
  "purpose": "この区間の狙いを短く",
  "expected_duration_seconds": 20,
  "tool_request": {"tool": "許可されたツール名", "query": "検索語など"},
  "wait_condition": "WAITのとき、何を待つのか",
  "progress_update": "進捗の一言メモ",
  "directive_revision_proposal": {"style_constraints": [], "content_constraints": []},
  "finish_reason": "FINISHのとき、終わる理由",
  "confidence": 0.0
}"""


_JSON_NOISE = re.compile(r"^[\s`{}\[\]\"']+|[\s`{}\[\]\"']+$")
_SPEAKABLE = re.compile(r"[ぁ-ゖァ-ヺ一-龥]")


def _prose_fallback(raw: str, max_chars: int) -> SegmentAction | None:
    """Rescue a segment when the model wrote prose instead of JSON.

    A local model does not emit clean JSON every time, and two failures in a
    row used to kill the whole directive — the radio show simply stopped.  If
    what came back is speakable Japanese, saying it is far better than dying.
    """
    text = _JSON_NOISE.sub("", str(raw or "")).strip()
    if not text or not _SPEAKABLE.search(text):
        return None
    # Anything that still looks like a broken structure is not speech.
    if text.count('":') >= 2 or text.lstrip().startswith(("{", "[")):
        return None
    return SegmentAction(
        action=SegmentActionType.SPEAK,
        spoken_content=" ".join(text.split())[:max_chars],
        purpose="prose_fallback",
        confidence=0.4,
    )


def parse_segment_action(
    raw: str, *, allowed_tools: Iterable[str] = (), max_chars: int = 500,
) -> SegmentAction | None:
    """Validate one model-authored segment.  ``None`` means "treat as WAIT"."""
    data = _loose_json(raw)
    if data is None:
        return _prose_fallback(raw, max_chars)
    action = _enum(SegmentActionType, data.get("action"), SegmentActionType.WAIT)
    spoken = " ".join(str(data.get("spoken_content") or "").split())[:max_chars]
    if action.speaks and not spoken:
        # A speaking action with nothing to say is silence, not a failure.
        action = SegmentActionType.WAIT
    if not action.speaks:
        spoken = ""
    tool_request: dict[str, Any] = {}
    raw_tool = data.get("tool_request")
    if action is SegmentActionType.USE_TOOL and isinstance(raw_tool, dict):
        name = _text(raw_tool.get("tool") or raw_tool.get("name"), 48)
        if name in set(allowed_tools):
            tool_request = {"tool": name, "query": _text(raw_tool.get("query"), 160)}
    if action is SegmentActionType.USE_TOOL and not tool_request:
        action = SegmentActionType.WAIT
    revision = data.get("directive_revision_proposal")
    revision_value: dict[str, Any] = {}
    if isinstance(revision, dict):
        for key in ("style_constraints", "content_constraints", "initial_plan"):
            items = _string_list(revision.get(key))
            if items:
                revision_value[key] = items
        goal = _text(revision.get("goal"))
        if goal:
            revision_value["goal"] = goal
    return SegmentAction(
        action=action,
        spoken_content=spoken,
        topic=_text(data.get("topic"), 100),
        purpose=_text(data.get("purpose"), 120),
        expected_duration_seconds=_clamp(
            data.get("expected_duration_seconds", 0), 0.0, 120.0, 0.0,
        ),
        tool_request=tool_request,
        wait_condition=_text(data.get("wait_condition"), 120),
        progress_update=_text(data.get("progress_update"), 160),
        directive_revision_proposal=revision_value,
        finish_reason=_text(data.get("finish_reason"), 120),
        confidence=_clamp(data.get("confidence", 0.6), default=0.6),
    )


# --------------------------------------------------------------------------
# Loop detection
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[、。,.!！?？「」『』…\s]+")


def _bigrams(text: str) -> set[str]:
    compact = _PUNCT.sub("", text)
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


# The text comparison lives in dialogue/echo.py: the same problem appears
# on the reply path, where a turn that adds nothing makes the model repeat
# its own previous answer verbatim.
from neuro_voice.dialogue.echo import (  # noqa: E402  (kept re-exported)
    containment as _containment,
    is_echo,
    sentences,
    trim_echoed_opening,
)


class SegmentLoopDetector:
    """Detect a directive that is talking without going anywhere.

    Surface wording is a poor signal: "今日はマイクラについて話そう" and "さて、
    マイクラの話なんだけど" are different strings and the same move.  Topic
    repetition and n-gram overlap of the *opening* are compared instead.
    """

    def __init__(self, *, max_repeated_topic: int = 2, similarity_threshold: float = 0.62):
        self.max_repeated_topic = max(1, int(max_repeated_topic))
        self.similarity_threshold = float(similarity_threshold)
        self.recent_topics: list[str] = []
        self.recent_openings: list[str] = []
        self.recent_claims: list[str] = []
        self.recent_questions: list[str] = []
        self.recent_action_types: list[str] = []
        self.recent_tool_requests: list[str] = []
        self.detections = 0

    def record(self, action: SegmentAction) -> None:
        self.recent_action_types.append(str(action.action))
        del self.recent_action_types[:-10]
        if action.topic:
            self.recent_topics.append(action.topic)
            del self.recent_topics[:-10]
        if action.tool_request.get("tool"):
            self.recent_tool_requests.append(
                f"{action.tool_request['tool']}:{action.tool_request.get('query', '')}"
            )
            del self.recent_tool_requests[:-6]
        text = action.spoken_content.strip()
        if not text:
            return
        self.recent_openings.append(text[:40])
        del self.recent_openings[:-6]
        self.recent_claims.append(text)
        del self.recent_claims[:-6]
        if text.rstrip().endswith(("?", "？")):
            self.recent_questions.append(text[-60:])
            del self.recent_questions[:-6]

    def would_loop(self, action: SegmentAction) -> str:
        """Return a non-empty reason code when this segment adds nothing new."""
        tool = action.tool_request.get("tool")
        if tool:
            key = f"{tool}:{action.tool_request.get('query', '')}"
            if key in self.recent_tool_requests:
                return "repeated_tool_request"
        topic = action.topic.strip()
        if topic and self.recent_topics.count(topic) >= self.max_repeated_topic:
            body = _bigrams(action.spoken_content)
            if not body:
                return "repeated_topic_without_content"
            for previous in self.recent_claims[-self.max_repeated_topic:]:
                if self._similar(body, _bigrams(previous)):
                    return "repeated_topic_and_content"
        text = action.spoken_content.strip()
        if not text:
            return ""
        opening = _bigrams(text[:40])
        for previous in self.recent_openings:
            if self._similar(opening, _bigrams(previous)):
                return "repeated_opening"
        if text.rstrip().endswith(("?", "？")):
            question = _bigrams(text[-60:])
            for previous in self.recent_questions:
                if self._similar(question, _bigrams(previous)):
                    return "repeated_question"
        return ""

    def _similar(self, left: set[str], right: set[str]) -> bool:
        if not left or not right:
            return False
        overlap = len(left & right) / len(left | right)
        return overlap >= self.similarity_threshold

    def snapshot(self) -> dict[str, Any]:
        return {
            "recent_topics": list(self.recent_topics[-4:]),
            "recent_action_types": list(self.recent_action_types[-4:]),
            "detections": self.detections,
        }


# --------------------------------------------------------------------------
# Behavior directive
# --------------------------------------------------------------------------


@dataclass(slots=True)
class BehaviorDirective:
    """One continuing request, held until it is finished or stopped."""

    directive_id: str
    conversation_id: str
    original_request: str
    interpreted_goal: str
    execution_mode: ExecutionMode
    interaction_mode: InteractionMode
    continuation_policy: ContinuationPolicy
    understanding: str = ""
    owner_user_id: str = ""
    source_turn_id: str = ""
    voice_session_id: str = ""
    audience: str = "local"
    status: DirectiveStatus = DirectiveStatus.PROPOSED
    priority: float = 0.8
    duration_hint_seconds: float | None = None
    segment_target_seconds: float = 20.0
    needs_user_input: bool = False
    needs_environment_events: bool = False
    started_at: float = 0.0
    paused_at: float = 0.0
    completed_at: float = 0.0
    expires_at: float = 0.0
    paused_duration_s: float = 0.0
    pause_reason: str = ""
    style_constraints: list[str] = field(default_factory=list)
    content_constraints: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    search_policy: SearchPolicy = SearchPolicy.NO_SEARCH
    stop_conditions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    plan_steps: list[str] = field(default_factory=list)
    progress_summary: str = ""
    #: What has actually happened so far, in the assistant's own words.
    #:
    #: The conversation history is trimmed to fit ``num_ctx``, and in a long
    #: session the opening scene is the first thing to go — by turn fifteen the
    #: game master no longer knew what story it was telling and fell back to
    #: friendly small talk.  These notes live on the directive instead, so they
    #: survive trimming.  The opening is kept for ever; the rest is a window.
    opening_note: str = ""
    progress_notes: list[str] = field(default_factory=list)
    current_topic: str = ""
    next_action_hint: str = ""
    completed_segment_count: int = 0
    waited_segment_count: int = 0
    consecutive_error_count: int = 0
    #: How often the model tried to wrap up before the requested length.
    early_finish_count: int = 0
    revision_count: int = 0
    state_version: int = 1
    generation_id: str = ""
    privacy_scope: str = "current_conversation"
    last_error: str = ""
    finish_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    persist_across_restart: bool = False

    # -- clocks ----------------------------------------------------------
    def elapsed_seconds(self, *, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        end = self.completed_at or (now if now is not None else time.time())
        return max(0.0, end - self.started_at - self.paused_duration_s)

    def remaining_seconds(self, *, now: float | None = None) -> float | None:
        if self.duration_hint_seconds is None:
            return None
        return max(0.0, self.duration_hint_seconds - self.elapsed_seconds(now=now))

    # -- what has happened so far ----------------------------------------
    def record_progress(self, spoken_text: str, *, keep: int = 6) -> None:
        """Remember one move of this session in compact form.

        Deterministic on purpose: summarising with the model would add a call
        per turn to a serial local LLM (D-017).  A trimmed literal line keeps
        the thread of the story at a cost of a few dozen tokens.
        """
        line = " ".join(str(spoken_text or "").split())[:110]
        if not line:
            return
        if not self.opening_note:
            self.opening_note = line
            return
        if self.progress_notes and self.progress_notes[-1] == line:
            return
        self.progress_notes.append(line)
        del self.progress_notes[:-max(1, int(keep))]

    # -- versioning ------------------------------------------------------
    def bump(self, reason: str = "") -> int:
        """Invalidate every in-flight segment and audio job for this directive."""
        self.state_version += 1
        self.updated_at = time.time()
        if reason:
            self.next_action_hint = _text(reason, 120)
        return self.state_version

    def segment_token(self, segment_id: str) -> str:
        return f"{self.directive_id}:{self.state_version}:{segment_id}"

    def accepts(self, *, state_version: int) -> bool:
        return int(state_version) == self.state_version and not self.status.is_terminal

    # -- transitions -----------------------------------------------------
    def start(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.started_at = now
        self.status = DirectiveStatus.ACTIVE
        self.updated_at = now

    def pause(self, reason: str, *, now: float | None = None) -> None:
        if self.status.is_terminal or self.status is DirectiveStatus.PAUSED:
            return
        now = time.time() if now is None else now
        self.paused_at = now
        self.pause_reason = _text(reason, 120)
        self.status = DirectiveStatus.PAUSED
        self.bump(f"paused:{self.pause_reason}")

    def resume(self, *, now: float | None = None) -> None:
        if self.status is not DirectiveStatus.PAUSED:
            return
        now = time.time() if now is None else now
        if self.paused_at:
            self.paused_duration_s += max(0.0, now - self.paused_at)
        self.paused_at = 0.0
        self.pause_reason = ""
        self.status = DirectiveStatus.ACTIVE
        self.updated_at = now

    def terminate(
        self, status: DirectiveStatus, reason: str = "", *, now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self.status = status
        self.completed_at = now
        self.finish_reason = _text(reason, 160)
        self.bump(f"terminated:{status}")

    # -- revision --------------------------------------------------------
    def revise(self, changes: dict[str, Any], *, reason: str = "user_revision") -> bool:
        """Apply an in-place change without creating a second directive."""
        applied = False
        for key in ("style_constraints", "content_constraints", "plan_steps"):
            items = _string_list(changes.get(key))
            if items:
                setattr(self, key, items)
                applied = True
        goal = _text(changes.get("goal") or changes.get("interpreted_goal"))
        if goal:
            self.interpreted_goal = goal
            applied = True
        topic = _text(changes.get("current_topic"), 100)
        if topic:
            self.current_topic = topic
            applied = True
        duration = changes.get("duration_hint_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            self.duration_hint_seconds = float(duration)
            applied = True
        interaction = changes.get("interaction_mode")
        if interaction:
            self.interaction_mode = _enum(
                InteractionMode, interaction, self.interaction_mode,
            )
            applied = True
        if applied:
            self.revision_count += 1
            self.bump(reason)
        return applied

    # -- reporting -------------------------------------------------------
    def snapshot(self, *, public: bool = False, now: float | None = None) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "execution_mode", "interaction_mode", "continuation_policy",
            "status", "search_policy",
        ):
            value[key] = str(value[key])
        value["elapsed_seconds"] = round(self.elapsed_seconds(now=now), 1)
        remaining = self.remaining_seconds(now=now)
        value["remaining_seconds"] = None if remaining is None else round(remaining, 1)
        if public:
            # The full natural-language request may quote private context.
            value["original_request"] = _text(self.original_request, 60)
            value["understanding"] = _text(self.understanding, 80)
        return value


def directive_from_plan(
    plan: PlanProposal,
    *,
    conversation_id: str,
    original_request: str,
    owner_user_id: str = "",
    source_turn_id: str = "",
    voice_session_id: str = "",
    audience: str = "local",
    max_duration_seconds: float = 1800.0,
    persist_across_restart: bool = False,
) -> BehaviorDirective:
    duration = plan.duration_hint_seconds
    if duration is not None:
        duration = min(float(duration), float(max_duration_seconds))
    now = time.time()
    return BehaviorDirective(
        directive_id=uuid4().hex[:12],
        conversation_id=str(conversation_id or "local"),
        original_request=_text(original_request, 400),
        interpreted_goal=plan.goal or plan.understanding,
        understanding=plan.understanding,
        execution_mode=plan.execution_mode,
        interaction_mode=plan.interaction_mode,
        continuation_policy=plan.continuation_policy,
        owner_user_id=str(owner_user_id or ""),
        source_turn_id=str(source_turn_id or ""),
        voice_session_id=str(voice_session_id or ""),
        audience=str(audience or "local"),
        duration_hint_seconds=duration,
        segment_target_seconds=plan.segment_target_seconds,
        needs_user_input=plan.needs_user_input,
        needs_environment_events=plan.needs_environment_events,
        expires_at=now + float(max_duration_seconds),
        style_constraints=list(plan.style_constraints),
        content_constraints=list(plan.content_constraints),
        allowed_tools=list(plan.allowed_tools),
        search_policy=plan.search_policy,
        stop_conditions=list(plan.stop_conditions),
        success_criteria=list(plan.success_criteria),
        plan_steps=list(plan.initial_plan),
        persist_across_restart=persist_across_restart,
    )


class DirectiveStore:
    """At most one active directive per conversation, plus a bounded history."""

    def __init__(self, *, max_active_per_conversation: int = 1, history: int = 12):
        self._max_active = max(1, int(max_active_per_conversation))
        self._history_limit = max(2, int(history))
        self._active: dict[str, BehaviorDirective] = {}
        self._history: list[BehaviorDirective] = []
        self._lock = threading.RLock()

    def active(self, conversation_id: str) -> BehaviorDirective | None:
        with self._lock:
            item = self._active.get(str(conversation_id))
            if item is not None and item.status.is_terminal:
                self._retire(item)
                return None
            return item

    def get(self, directive_id: str) -> BehaviorDirective | None:
        with self._lock:
            for item in self._active.values():
                if item.directive_id == directive_id:
                    return item
            return next(
                (item for item in self._history if item.directive_id == directive_id), None,
            )

    def put(self, directive: BehaviorDirective) -> BehaviorDirective:
        with self._lock:
            previous = self._active.get(directive.conversation_id)
            if previous is not None and previous is not directive:
                if not previous.status.is_terminal:
                    previous.terminate(
                        DirectiveStatus.CANCELLED, "replaced_by_new_directive",
                    )
                    # Losing a running session silently is how a whole TRPG
                    # disappeared without one line of evidence.
                    logger.info(
                        "Directive replaced id=%s mode=%s interaction=%s "
                        "segments=%d conversation=%s",
                        previous.directive_id, previous.execution_mode,
                        previous.interaction_mode, previous.completed_segment_count,
                        previous.conversation_id,
                    )
                self._retire(previous)
            self._active[directive.conversation_id] = directive
            return directive

    def rekey(self, old_id: str, new_id: str) -> BehaviorDirective | None:
        """Move a running directive to the conversation key it now belongs to.

        ``conversation_id`` comes from the speaker, and the speaker is not
        known yet when the first turn creates a directive: voiceprint matching
        finishes a few hundred milliseconds later.  So a directive was filed
        under ``local:user`` and every later turn looked under ``speaker:1``,
        found nothing, and started a second directive — the first one was
        orphaned mid-story with no terminal state and no log line.

        Returns the directive that now lives at ``new_id``, or ``None``.
        """
        old_id, new_id = str(old_id), str(new_id)
        if old_id == new_id:
            return None
        with self._lock:
            item = self._active.get(old_id)
            if item is None or item.status.is_terminal:
                return None
            existing = self._active.get(new_id)
            if existing is not None and existing is not item:
                # Someone is already talking under the new key.  The newer
                # session wins; do not resurrect the old one over it.
                return None
            self._active.pop(old_id, None)
            item.conversation_id = new_id
            self._active[new_id] = item
            logger.info(
                "Directive rekeyed id=%s %s → %s (segments=%d)",
                item.directive_id, old_id, new_id, item.completed_segment_count,
            )
            return item

    def retire(self, directive: BehaviorDirective) -> None:
        with self._lock:
            self._retire(directive)

    def _retire(self, directive: BehaviorDirective) -> None:
        if self._active.get(directive.conversation_id) is directive:
            self._active.pop(directive.conversation_id, None)
        if directive not in self._history:
            self._history.append(directive)
            del self._history[:-self._history_limit]

    def cancel_all_for_restart(self) -> int:
        with self._lock:
            items = [
                item for item in self._active.values()
                if not item.persist_across_restart
            ]
            for item in items:
                item.terminate(
                    DirectiveStatus.CANCELLED_BY_RESTART, "process_restart",
                )
                self._retire(item)
            return len(items)

    def snapshot(self, conversation_id: str = "local", *, public: bool = True) -> dict[str, Any]:
        item = self.active(conversation_id)
        with self._lock:
            history = [
                {
                    "directive_id": entry.directive_id,
                    "status": str(entry.status),
                    "execution_mode": str(entry.execution_mode),
                    "segments": entry.completed_segment_count,
                    "finish_reason": entry.finish_reason,
                }
                for entry in self._history[-4:]
            ]
        return {
            "active": None if item is None else item.snapshot(public=public),
            "recent": history,
        }
