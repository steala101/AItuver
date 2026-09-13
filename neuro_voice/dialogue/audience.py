"""Audience boundary for current direct/group and future broadcast inputs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ConversationAudienceMode(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    BROADCAST = "broadcast"


@dataclass(frozen=True, slots=True)
class CommentCluster:
    cluster_id: str
    summary: str
    representative_text: str
    message_count: int
    topicality: float
    safety_score: float


class BroadcastCommentClusterer(Protocol):
    """Future extension; no livestream reader is connected in the current app."""

    def cluster(self, comments: list[str]) -> tuple[CommentCluster, ...]: ...
