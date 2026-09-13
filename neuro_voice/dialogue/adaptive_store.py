"""SQLite persistence for observable conversation adaptation.

This database is persona-scoped.  It stores only compact structured state and
never raw audio, hidden reasoning, or fictional events as real experiences.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from neuro_voice.dialogue.feature_models import ValueProfile

logger = logging.getLogger(__name__)

_TABLES = (
    "ai_value_profile", "ai_preferences", "ai_active_curiosities",
    "ai_experience_episodes", "ai_generalized_lessons", "relationship_states",
    "relationship_events", "conversation_feature_usage",
    "conversation_feature_feedback", "humor_pattern_history", "topic_lifecycle",
    "unresolved_topics", "candidate_evaluations", "critic_results",
    "conversation_learning_updates", "conversation_reaction_signals",
    "semantic_graph_nodes", "semantic_graph_edges",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS adaptive_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_type TEXT NOT NULL,
  record_key TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 1.0,
  importance REAL NOT NULL DEFAULT 0.5,
  expiry REAL,
  user_id TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_adaptive_type_user
  ON adaptive_records(record_type, user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_adaptive_key
  ON adaptive_records(record_type, record_key, user_id);

CREATE TABLE IF NOT EXISTS conversation_turn_traces (
  turn_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_traces_user
  ON conversation_turn_traces(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS adaptive_schema (
  name TEXT PRIMARY KEY,
  created_at REAL NOT NULL
);
"""


class AdaptiveConversationStore:
    def __init__(self, path: str | Path, *, session_id: str | None = None):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.session_id = session_id or uuid.uuid4().hex[:16]
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                conn.executemany(
                    "INSERT OR IGNORE INTO adaptive_schema(name, created_at) VALUES(?, ?)",
                    [(name, time.time()) for name in _TABLES],
                )

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        # Connections are deliberately short-lived.  Holding a sqlite handle for
        # the lifetime of the GUI prevents persona databases and test temporary
        # directories from being moved/deleted on Windows.
        return None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def upsert(
        self, record_type: str, record_key: str, payload: dict[str, Any], *,
        user_id: str = "", source: str = "system", confidence: float = 1.0,
        importance: float = .5, expiry: float | None = None,
    ) -> None:
        if record_type not in _TABLES:
            raise ValueError(f"unsupported adaptive record type: {record_type}")
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id, created_at FROM adaptive_records "
                    "WHERE record_type=? AND record_key=? AND user_id=? ORDER BY id DESC LIMIT 1",
                    (record_type, str(record_key)[:160], str(user_id)[:120]),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE adaptive_records SET payload=?,updated_at=?,source=?,confidence=?,"
                        "importance=?,expiry=?,session_id=? WHERE id=?",
                        (self._json(payload), now, source, float(confidence), float(importance),
                         expiry, self.session_id, row["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO adaptive_records(record_type,record_key,payload,created_at,updated_at,"
                        "source,confidence,importance,expiry,user_id,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (record_type, str(record_key)[:160], self._json(payload), now, now, source,
                         float(confidence), float(importance), expiry, str(user_id)[:120], self.session_id),
                    )

    def append(
        self, record_type: str, payload: dict[str, Any], *, user_id: str = "",
        record_key: str | None = None, source: str = "conversation",
        confidence: float = 1.0, importance: float = .5, expiry: float | None = None,
    ) -> str:
        key = record_key or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO adaptive_records(record_type,record_key,payload,created_at,updated_at,"
                    "source,confidence,importance,expiry,user_id,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (record_type, key[:160], self._json(payload), now, now, source,
                     float(confidence), float(importance), expiry, str(user_id)[:120], self.session_id),
                )
        return key

    def latest(self, record_type: str, *, user_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM adaptive_records WHERE record_type=? AND user_id=? "
                    "AND (expiry IS NULL OR expiry>?) ORDER BY updated_at DESC LIMIT ?",
                    (record_type, str(user_id)[:120], now, max(1, int(limit))),
                ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = {}
            out.append({**dict(row), "payload": payload})
        return out

    def feature_weights(self, user_id: str) -> dict[str, float]:
        rows = self.latest("conversation_learning_updates", user_id=user_id, limit=100)
        result: dict[str, float] = {}
        for row in reversed(rows):
            data = row["payload"]
            feature = str(data.get("feature", ""))
            if feature:
                result[feature] = max(.25, min(1.75, float(data.get("weight", 1.0))))
        return result

    def value_profile(self, user_id: str = "") -> ValueProfile:
        rows = self.latest("ai_value_profile", user_id=user_id, limit=1)
        defaults = ValueProfile()
        if not rows:
            self.upsert("ai_value_profile", "stable_values", defaults.snapshot(), user_id=user_id,
                        source="default", confidence=1.0, importance=.95)
            return defaults
        data = rows[0]["payload"]
        values = {}
        for key, default in defaults.snapshot().items():
            try:
                values[key] = max(0.0, min(1.0, float(data.get(key, default))))
            except (TypeError, ValueError):
                values[key] = default
        return ValueProfile(**values)

    def adjust_value(
        self, user_id: str, axis: str, delta: float, *, evidence_count: int,
    ) -> ValueProfile:
        """Apply a deliberately tiny, evidence-backed change to a stable value.

        Values are identity anchors, not per-turn mood.  A single reaction can
        never move them; callers must first establish repeated evidence.
        """
        current = self.value_profile(user_id)
        values = current.snapshot()
        if axis not in values or int(evidence_count) < 3:
            return current
        bounded_delta = max(-.01, min(.01, float(delta)))
        values[axis] = round(max(0.0, min(1.0, values[axis] + bounded_delta)), 4)
        self.upsert(
            "ai_value_profile", "stable_values", values, user_id=user_id,
            source="repeated_verified_feedback", confidence=min(.95, .60 + .05 * evidence_count),
            importance=.95,
        )
        return ValueProfile(**values)

    def record_trace(self, turn_id: str, user_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO conversation_turn_traces(turn_id,user_id,session_id,created_at,payload)"
                    " VALUES(?,?,?,?,?)",
                    (str(turn_id), str(user_id)[:120], self.session_id, time.time(), self._json(payload)),
                )

    def recent_traces(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM conversation_turn_traces WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (str(user_id)[:120], max(1, int(limit))),
                ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = {}
            out.append({"turn_id": row["turn_id"], "created_at": row["created_at"], **payload})
        return out

    def reassign_user(self, source_user_id: str, target_user_id: str) -> int:
        """Move every adaptive record from one speaker key to another.

        Called when two profiles are found to be the same person; without this
        the learned feature weights and value profile stay split.
        """
        source = str(source_user_id or "")[:120]
        target = str(target_user_id or "")[:120]
        if not source or not target or source == target:
            return 0
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    "UPDATE adaptive_records SET user_id=? WHERE user_id=?", (target, source),
                )
                return int(cursor.rowcount or 0)

    def prune(self, *, max_debug: int = 300, max_low_value: int = 800) -> dict[str, int]:
        """Forget expired/low-value data without touching durable lessons or values."""
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                expired = conn.execute(
                    "DELETE FROM adaptive_records WHERE expiry IS NOT NULL AND expiry<=?", (now,)
                ).rowcount
                trace_ids = conn.execute(
                    "SELECT turn_id FROM conversation_turn_traces ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                    (max(20, int(max_debug)),),
                ).fetchall()
                if trace_ids:
                    conn.executemany(
                        "DELETE FROM conversation_turn_traces WHERE turn_id=?",
                        [(row["turn_id"],) for row in trace_ids],
                    )
                disposable = conn.execute(
                    "SELECT id FROM adaptive_records WHERE importance<0.35 "
                    "ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
                    (max(100, int(max_low_value)),),
                ).fetchall()
                if disposable:
                    conn.executemany(
                        "DELETE FROM adaptive_records WHERE id=?", [(row["id"],) for row in disposable],
                    )
        return {"expired": max(0, expired), "traces": len(trace_ids), "low_value": len(disposable)}
