"""Small persona-scoped semantic association graph.

This is an association aid, not a fact database.  Nodes contain short topic
labels only; edges record co-occurrence and correction relationships.  Group
edges expire with the current runtime session so private one-to-one memory
cannot leak into a Discord group prompt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
import time
from typing import Any


@dataclass(frozen=True, slots=True)
class SemanticAssociation:
    source: str
    target: str
    relation: str
    weight: float
    confidence: float

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


def _key(*parts: str) -> str:
    value = "\x1f".join(parts)
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:24]


_CORRECTION = re.compile(
    r"([一-龥々ぁ-んァ-ヴーA-Za-z0-9_-]{2,20}?)"
    r"(?:じゃなくて|ではなく|じゃない[、,\s]*)"
    r"([一-龥々ぁ-んァ-ヴーA-Za-z0-9_-]{2,20}?)"
    r"(?=だよ|です|って|と読む|[。！!、,]|$)"
)


class SemanticAssociationGraph:
    def __init__(self, store, *, enabled: bool = True, max_nodes: int = 120):
        self._store = store
        self._enabled = bool(enabled)
        self._max_nodes = max(20, min(400, int(max_nodes)))

    @staticmethod
    def _labels(topics: list[str] | tuple[str, ...]) -> list[str]:
        return list(dict.fromkeys(
            str(item).strip()[:24] for item in topics
            if 2 <= len(str(item).strip()) <= 24
        ))[-4:]

    def observe(
        self, user_id: str, topics: list[str], *, group: bool,
        source: str, allow_store: bool = True,
    ) -> None:
        if not self._enabled or not allow_store:
            return
        labels = self._labels(topics)
        if not labels:
            return
        now = time.time()
        scope = "group_session" if group else "direct_private"
        expiry = now + 4 * 3600 if group else None
        node_rows = self._store.latest(
            "semantic_graph_nodes", user_id=user_id, limit=self._max_nodes,
        )
        edge_rows = self._store.latest(
            "semantic_graph_edges", user_id=user_id, limit=200,
        )
        for label in labels:
            existing = next(
                (row["payload"] for row in node_rows
                 if row["payload"].get("label") == label
                 and row["payload"].get("scope") == scope),
                {},
            )
            mentions = min(999, int(existing.get("mentions", 0)) + 1)
            self._store.upsert(
                "semantic_graph_nodes", _key(scope, label),
                {
                    "label": label, "kind": "topic", "scope": scope,
                    "mentions": mentions, "last_seen": now,
                },
                user_id=user_id, source=source, confidence=.82,
                importance=min(.75, .25 + mentions * .04), expiry=expiry,
            )
        for index, left in enumerate(labels):
            for right in labels[index + 1:]:
                a, b = sorted((left, right))
                edge_key = _key(scope, "co_occurs", a, b)
                existing = next(
                    (row["payload"] for row in edge_rows if row["record_key"] == edge_key),
                    {},
                )
                weight = min(1.0, float(existing.get("weight", .20)) + .10)
                self._store.upsert(
                    "semantic_graph_edges", edge_key,
                    {
                        "source": a, "target": b, "relation": "co_occurs",
                        "scope": scope, "weight": round(weight, 3),
                        "last_seen": now,
                    },
                    user_id=user_id, source=source, confidence=.72,
                    importance=.35, expiry=expiry,
                )

    def associations(
        self, user_id: str, topics: list[str], *, group: bool, limit: int = 2,
    ) -> tuple[SemanticAssociation, ...]:
        if not self._enabled:
            return ()
        labels = set(self._labels(topics))
        if not labels:
            return ()
        scope = "group_session" if group else "direct_private"
        rows = self._store.latest("semantic_graph_edges", user_id=user_id, limit=200)
        found: list[SemanticAssociation] = []
        for row in rows:
            data = row["payload"]
            if data.get("scope") != scope:
                continue
            if group and row.get("session_id") != self._store.session_id:
                continue
            left, right = str(data.get("source", "")), str(data.get("target", ""))
            if left in labels and right not in labels:
                target = right
                source = left
            elif right in labels and left not in labels:
                target = left
                source = right
            else:
                continue
            found.append(SemanticAssociation(
                source, target, str(data.get("relation", "associated")),
                float(data.get("weight", .0)), float(row.get("confidence", .0)),
            ))
        found.sort(key=lambda item: (item.weight, item.confidence), reverse=True)
        return tuple(found[:max(0, min(4, int(limit)))])

    def observe_correction(
        self, user_id: str, text: str, *, group: bool, source: str,
        allow_store: bool = True,
    ) -> bool:
        if not self._enabled or not allow_store:
            return False
        match = _CORRECTION.search(str(text or ""))
        if match is None:
            return False
        old, new = match.group(1), match.group(2)
        scope = "group_session" if group else "direct_private"
        now = time.time()
        expiry = now + 4 * 3600 if group else None
        self._store.upsert(
            "semantic_graph_edges", _key(scope, "corrected_to", old, new),
            {
                "source": old, "target": new, "relation": "corrected_to",
                "scope": scope, "weight": .92, "last_seen": now,
            },
            user_id=user_id, source=source, confidence=.94,
            importance=.68, expiry=expiry,
        )
        return True
