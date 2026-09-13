"""Persona-scoped SQLite evidence and research history store."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import ResearchQuestion, ResearchStatus


class ResearchStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS research_questions (
              question_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL,
              status TEXT NOT NULL, normalized_query TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_research_questions_status ON research_questions(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS research_runs (
              run_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, data TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_research_runs_question ON research_runs(question_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS research_evidence (
              evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, question_id TEXT NOT NULL,
              data TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS research_knowledge (
              knowledge_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, data TEXT NOT NULL,
              created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS research_reflections (
              reflection_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, data TEXT NOT NULL,
              created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS research_interests (
              topic_key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS research_preferences (
              topic_key TEXT PRIMARY KEY, auto_disabled INTEGER NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL);
            """)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _dump(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def save_question(self, question: ResearchQuestion, *, normalized_query: str = "") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("""INSERT INTO research_questions(question_id,data,created_at,status,normalized_query,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET data=excluded.data,status=excluded.status,
                normalized_query=CASE WHEN excluded.normalized_query!='' THEN excluded.normalized_query ELSE normalized_query END,
                updated_at=excluded.updated_at""",
                (question.question_id, self._dump(question.as_dict()), question.created_at, question.status, normalized_query, now))
            self._conn.commit()

    def get_question(self, question_id: str) -> ResearchQuestion | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM research_questions WHERE question_id=?", (question_id,)).fetchone()
        return ResearchQuestion.from_dict(self._load(row["data"])) if row else None

    def pending_questions(self, limit: int = 20) -> list[ResearchQuestion]:
        statuses = (ResearchStatus.PENDING.value, ResearchStatus.APPROVED.value)
        with self._lock:
            rows = self._conn.execute("SELECT data FROM research_questions WHERE status IN (?,?) ORDER BY created_at ASC LIMIT ?", (*statuses, int(limit))).fetchall()
        return [ResearchQuestion.from_dict(self._load(row["data"])) for row in rows]

    def recent_duplicate(self, query: str, after: float, *, exclude_question_id: str = "") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM research_questions WHERE normalized_query=? AND updated_at>=? AND question_id<>? LIMIT 1",
                (query, after, exclude_question_id),
            ).fetchone()
        return row is not None

    def save_run(self, data: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("INSERT INTO research_runs(run_id,question_id,data,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                               (data["run_id"], data["question_id"], self._dump(data), float(data.get("started_at") or now), now))
            self._conn.commit()

    def add_evidence(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO research_evidence(evidence_id,run_id,question_id,data,created_at) VALUES(?,?,?,?,?)",
                               (data["evidence_id"], data["research_run_id"], data["question_id"], self._dump(data), time.time()))
            self._conn.commit()

    def save_knowledge(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO research_knowledge(knowledge_id,question_id,data,created_at,active) VALUES(?,?,?,?,?)",
                               (data["knowledge_id"], data["question_id"], self._dump(data), time.time(), int(data.get("status") in {"ACTIVE", "PROVISIONAL"})))
            self._conn.commit()

    def save_reflection(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO research_reflections(reflection_id,question_id,data,created_at) VALUES(?,?,?,?)",
                               (data["reflection_id"], data["question_id"], self._dump(data), time.time()))
            self._conn.commit()

    def update_interest(self, topic_key: str, *, topic: str, success: bool, unanswered: bool) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM research_interests WHERE topic_key=?", (topic_key,)).fetchone()
            data = self._load(row["data"]) if row else {}
            data = {
                "interest_id": data.get("interest_id", topic_key), "topic": topic,
                "interest_level": min(1.0, float(data.get("interest_level", .3)) + (.06 if success else -.02)),
                "familiarity": min(1.0, float(data.get("familiarity", 0)) + (.12 if success else .02)),
                "curiosity": max(0.0, min(1.0, float(data.get("curiosity", .5)) + (.04 if unanswered else -.03))),
                "positive_experiences": int(data.get("positive_experiences", 0)) + int(success),
                "negative_experiences": int(data.get("negative_experiences", 0)) + int(not success),
                "unanswered_question_count": int(data.get("unanswered_question_count", 0)) + int(unanswered),
                "research_count": int(data.get("research_count", 0)) + 1,
                "saturation": min(1.0, float(data.get("saturation", 0)) + .08),
                "last_researched_at": time.time(), "last_used_at": data.get("last_used_at"), "updated_at": time.time(),
            }
            self._conn.execute("INSERT INTO research_interests(topic_key,data,updated_at) VALUES(?,?,?) ON CONFLICT(topic_key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at", (topic_key, self._dump(data), time.time()))
            self._conn.commit()
        return data

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT data, normalized_query, updated_at FROM research_questions ORDER BY updated_at DESC LIMIT ?", (int(limit),)).fetchall()
        result = []
        for row in rows:
            item = self._load(row["data"])
            item["sanitized_query"] = row["normalized_query"]
            item["updated_at"] = row["updated_at"]
            result.append(item)
        return result

    def detail(self, question_id: str) -> dict[str, Any] | None:
        question = self.get_question(question_id)
        if question is None:
            return None
        with self._lock:
            runs = self._conn.execute("SELECT data FROM research_runs WHERE question_id=? ORDER BY created_at DESC", (question_id,)).fetchall()
            evidence = self._conn.execute("SELECT data FROM research_evidence WHERE question_id=? ORDER BY created_at DESC", (question_id,)).fetchall()
            knowledge = self._conn.execute("SELECT data FROM research_knowledge WHERE question_id=? ORDER BY created_at DESC", (question_id,)).fetchall()
            reflections = self._conn.execute("SELECT data FROM research_reflections WHERE question_id=? ORDER BY created_at DESC", (question_id,)).fetchall()
        return {"question": question.as_dict(), "runs": [self._load(r["data"]) for r in runs],
                "evidence": [self._load(r["data"]) for r in evidence], "knowledge": [self._load(r["data"]) for r in knowledge],
                "reflections": [self._load(r["data"]) for r in reflections]}

    def set_auto_disabled(self, topic_key: str, disabled: bool) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO research_preferences(topic_key,auto_disabled,updated_at) VALUES(?,?,?) ON CONFLICT(topic_key) DO UPDATE SET auto_disabled=excluded.auto_disabled,updated_at=excluded.updated_at", (topic_key, int(disabled), time.time()))
            self._conn.commit()

    def auto_disabled(self, topic_key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT auto_disabled FROM research_preferences WHERE topic_key=?", (topic_key,)).fetchone()
        return bool(row and row["auto_disabled"])

    def summary(self) -> dict[str, int]:
        with self._lock:
            q = self._conn.execute("SELECT COUNT(*) FROM research_questions").fetchone()[0]
            today = self._conn.execute("SELECT COUNT(*) FROM research_runs WHERE created_at>=?", (time.time() - 86400,)).fetchone()[0]
            pending = self._conn.execute("SELECT COUNT(*) FROM research_questions WHERE status IN (?,?)", (ResearchStatus.PENDING.value, ResearchStatus.APPROVED.value)).fetchone()[0]
            learned = self._conn.execute("SELECT COUNT(*) FROM research_knowledge WHERE active=1").fetchone()[0]
        return {"questions": int(q), "today_runs": int(today), "pending": int(pending), "knowledge": int(learned)}

    def prune_history(self, retention_days: float) -> int:
        """Bound history/evidence lifetime without touching active knowledge."""
        cutoff = time.time() - max(1.0, float(retention_days)) * 86400
        with self._lock:
            ids = [row[0] for row in self._conn.execute("SELECT question_id FROM research_questions WHERE updated_at<?", (cutoff,)).fetchall()]
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            self._conn.execute(f"DELETE FROM research_evidence WHERE question_id IN ({marks})", ids)
            self._conn.execute(f"DELETE FROM research_reflections WHERE question_id IN ({marks})", ids)
            self._conn.execute(f"DELETE FROM research_runs WHERE question_id IN ({marks})", ids)
            self._conn.execute(f"DELETE FROM research_questions WHERE question_id IN ({marks})", ids)
            self._conn.commit()
        return len(ids)
