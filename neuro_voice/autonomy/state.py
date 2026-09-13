"""Short-lived, observable intent state for autonomous actions.

This deliberately contains no hidden chain-of-thought.  It tracks only
goals, deferred matters, and social context that a participant can reasonably
carry between turns.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.autonomy.types import AutonomyEvent, AutonomyEventType, PrivacyScope


class GoalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ThreadStatus(StrEnum):
    OPEN = "open"
    WAITING_FOR_HUMAN = "waiting_for_human"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_GAME = "waiting_for_game"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


@dataclass(slots=True)
class Goal:
    description: str
    type: str = "conversation"
    owner: str | None = None
    priority: float = .5
    status: GoalStatus = GoalStatus.PENDING
    expires_at: float | None = None
    source_event_id: str | None = None
    related_user_ids: tuple[str, ...] = ()
    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION
    completion_condition: str = ""
    goal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class OpenThread:
    topic: str
    summary: str
    owner_user_id: str | None = None
    involved_user_ids: tuple[str, ...] = ()
    status: ThreadStatus = ThreadStatus.OPEN
    importance: float = .5
    callback_earliest_at: float | None = None
    callback_deadline: float | None = None
    callback_count: int = 0
    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source_utterance_id: str | None = None
    last_updated_at: float = field(default_factory=time.monotonic)
    # What the person actually said, and when.  Without these a callback can
    # only manage "あれどうなった？", which is the vague question users hate.
    source_excerpt: str = ""
    source_wall_time: float = field(default_factory=time.time)


@dataclass(slots=True)
class CuriosityTarget:
    subject: str
    reason: str
    priority: float = .5
    related_topic: str = ""
    expires_at: float | None = None
    asked_count: int = 0
    privacy_risk: float = 0.0
    target_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class Commitment:
    description: str
    target_user_id: str | None = None
    status: GoalStatus = GoalStatus.PENDING
    due_condition: str = ""
    expires_at: float | None = None
    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION
    commitment_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class AgendaItem:
    action_type: str
    description: str
    priority: float = .5
    source: str = "event"
    status: GoalStatus = GoalStatus.PENDING
    not_before: float | None = None
    expires_at: float | None = None
    target_user_id: str | None = None
    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION
    agenda_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(slots=True)
class PersistentAgentState:
    """Restart-safe values.  Private/restricted session content is excluded."""
    long_term_goals: list[Goal] = field(default_factory=list)
    commitments: list[Commitment] = field(default_factory=list)
    personality_deltas: dict[str, float] = field(default_factory=dict)


_DEFERRED = re.compile(r"(?:後で|あとで).{0,24}(?:確認|見る|試す|話す|教える|調べる)|(?:確認|見る|試す|話す|調べる).{0,16}(?:後で|あとで)")
_RESOLVED = re.compile(r"(?:できた|完了|解決|確認した|終わった)")


class AgentState:
    def __init__(self, *, max_open_threads: int = 10, max_agenda_items: int = 8,
                 max_recent_events: int = 30, max_callbacks: int = 2,
                 default_thread_ttl_s: float = 1800.0) -> None:
        self.current_topic = ""
        self.current_goal: str | None = None
        self.active_goals: list[Goal] = []
        self.open_threads: list[OpenThread] = []
        self.curiosity_targets: list[CuriosityTarget] = []
        self.commitments: list[Commitment] = []
        self.micro_agenda: list[AgendaItem] = []
        self.social_focus: str | None = None
        self.current_activity = "idle"
        self.recent_events: list[AutonomyEvent] = []
        self.mood_tendencies: dict[str, float] = {}
        self.boredom_level = 0.0
        self.speaking_pressure = 0.0
        self.last_action = "do_nothing"
        self.last_spoken_at = 0.0
        self.state_version = 1
        self.persistent = PersistentAgentState()
        self._max_open_threads = max(1, max_open_threads)
        self._max_agenda_items = max(1, max_agenda_items)
        self._max_recent_events = max(1, max_recent_events)
        self._max_callbacks = max(1, max_callbacks)
        self._default_thread_ttl_s = max(60.0, default_thread_ttl_s)

    def ingest(self, event: AutonomyEvent) -> None:
        now = event.timestamp
        self.recent_events.append(event)
        self.recent_events = self.recent_events[-self._max_recent_events:]
        if event.event_type is AutonomyEventType.HUMAN_UTTERANCE_FINALIZED:
            self.social_focus = event.speaker_id
            text = event.text.strip()
            if text:
                self.current_topic = text[:120]
                self.current_activity = "conversation"
                self.boredom_level = 0.0
                if event.privacy_scope not in {PrivacyScope.OWNER_AND_AI, PrivacyScope.DO_NOT_STORE}:
                    if _DEFERRED.search(text):
                        self.open_thread(topic=text[:80], summary=text[:220], owner_user_id=event.speaker_id,
                                         source_utterance_id=event.event_id, now=now)
                    elif _RESOLVED.search(text):
                        self.resolve_matching_thread(now=now)
        elif event.event_type in {AutonomyEventType.GAME_EVENT, AutonomyEventType.GAME_GOAL_PROGRESS}:
            self.current_activity = "game"
        elif event.event_type is AutonomyEventType.SILENCE_CONTINUED:
            self.boredom_level = min(1.0, self.boredom_level + .03)
        elif event.event_type is AutonomyEventType.HUMAN_SPEECH_STARTED:
            self.speaking_pressure = 1.0
        elif event.event_type in {AutonomyEventType.HUMAN_SPEECH_ENDED, AutonomyEventType.AI_SPEECH_FINISHED}:
            self.speaking_pressure = max(0.0, self.speaking_pressure - .35)
        self.prune(now=now)

    def open_thread(self, *, topic: str, summary: str, owner_user_id: str | None,
                    source_utterance_id: str | None = None, importance: float = .58,
                    privacy_scope: PrivacyScope = PrivacyScope.CURRENT_CONVERSATION,
                    callback_after_s: float = 90.0, now: float | None = None,
                    source_excerpt: str = "") -> OpenThread | None:
        if privacy_scope in {PrivacyScope.OWNER_AND_AI, PrivacyScope.DO_NOT_STORE}:
            return None
        now = time.monotonic() if now is None else now
        for item in self.open_threads:
            if item.status in {ThreadStatus.OPEN, ThreadStatus.WAITING_FOR_HUMAN} and item.topic == topic:
                item.summary = summary[:220]
                item.last_updated_at = now
                if source_excerpt and not item.source_excerpt:
                    item.source_excerpt = str(source_excerpt)[:180]
                return item
        item = OpenThread(topic=topic[:120], summary=summary[:220], owner_user_id=owner_user_id,
                          source_utterance_id=source_utterance_id, importance=max(0.0, min(1.0, importance)),
                          callback_earliest_at=now + max(10.0, callback_after_s),
                          callback_deadline=now + self._default_thread_ttl_s, privacy_scope=privacy_scope,
                          source_excerpt=str(source_excerpt or "")[:180])
        self.open_threads.append(item)
        self.open_threads = self.open_threads[-self._max_open_threads:]
        return item

    def resolve_matching_thread(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for item in self.open_threads:
            if item.status in {ThreadStatus.OPEN, ThreadStatus.WAITING_FOR_HUMAN}:
                item.status, item.last_updated_at = ThreadStatus.RESOLVED, now

    def due_threads(self, *, now: float | None = None, group: bool = False) -> list[OpenThread]:
        now = time.monotonic() if now is None else now
        due: list[OpenThread] = []
        for item in self.open_threads:
            if item.status not in {ThreadStatus.OPEN, ThreadStatus.WAITING_FOR_HUMAN}:
                continue
            if item.callback_count >= self._max_callbacks:
                item.status = ThreadStatus.ABANDONED
            elif item.callback_deadline is not None and now > item.callback_deadline:
                item.status = ThreadStatus.EXPIRED
            elif (item.callback_earliest_at is None or now >= item.callback_earliest_at) and not (group and item.privacy_scope is not PrivacyScope.PUBLIC):
                due.append(item)
        return sorted(due, key=lambda item: item.importance, reverse=True)

    def mark_thread_recalled(self, thread_id: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for item in self.open_threads:
            if item.thread_id == thread_id:
                item.callback_count += 1
                item.status = ThreadStatus.WAITING_FOR_HUMAN
                item.callback_earliest_at = now + 180.0
                item.last_updated_at = now
                return

    def record_action(self, action: str, *, spoke: bool = False, now: float | None = None) -> None:
        self.last_action = action
        if spoke:
            self.last_spoken_at = time.monotonic() if now is None else now
        self.state_version += 1

    def prune(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.open_threads = [item for item in self.open_threads if not (
            item.callback_deadline is not None and now > item.callback_deadline
        ) and item.status not in {ThreadStatus.RESOLVED, ThreadStatus.ABANDONED, ThreadStatus.EXPIRED}][-self._max_open_threads:]
        self.micro_agenda = [item for item in self.micro_agenda if item.expires_at is None or item.expires_at >= now][-self._max_agenda_items:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "current_topic": self.current_topic, "current_goal": self.current_goal,
            "active_goals": [asdict(item) for item in self.active_goals[-5:]],
            "open_threads": [asdict(item) for item in self.open_threads[-5:]],
            "curiosity_targets": [asdict(item) for item in self.curiosity_targets[-4:]],
            "commitments": [asdict(item) for item in self.commitments[-4:]],
            "micro_agenda": [asdict(item) for item in self.micro_agenda[-self._max_agenda_items:]],
            "social_focus": self.social_focus, "current_activity": self.current_activity,
            "last_action": self.last_action, "state_version": self.state_version,
        }
