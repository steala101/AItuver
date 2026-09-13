"""長期記憶の永続化 (SQLite)。

kind:
  fact       … ユーザーや世界についての事実 (「猫を飼っている」など)
  preference … AI自身の好み・価値観の変化の記録
  episode    … セッション要約 (日記のような思い出)
"""
from __future__ import annotations

import contextlib
from functools import lru_cache
import hashlib
from itertools import combinations
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

logger = logging.getLogger(__name__)


class ExplicitDeletionReason(StrEnum):
    """Reasons that may intentionally remove a retained source record."""

    USER_REQUEST = "user_request"
    PRIVACY_CORRECTION = "privacy_correction"


class MemoryWriteUnavailable(RuntimeError):
    """A source write failed and this Store has entered read-only mode."""


class MemoryStoreMigrationRequired(RuntimeError):
    """A read-only source cannot be opened until its schema is migrated."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Single owner for automatic source-record retention.

    Numeric retrieval/context limits are deliberately absent.  They bound what
    is *read*, not how long the canonical source record exists.
    """

    source_records: str = "retain"
    transcript_source_records: str = "retain"

    @classmethod
    def from_config(cls, cfg: Any) -> "RetentionPolicy":
        get = getattr(cfg, "get", None) or (lambda _key, default=None: default)
        memory = str(get("mind.retention.source_records", "retain") or "retain").lower()
        transcript = str(
            get("mind.transcript.retention_policy", "retain") or "retain"
        ).lower()
        # Stage M has no automatic destructive mode.  Unknown/legacy values
        # fail closed instead of silently restoring the old DELETE behaviour.
        if memory != "retain":
            logger.warning("未知のMemory保持policy=%s。retainへ倒します", memory)
            memory = "retain"
        if transcript != "retain":
            logger.warning("未知のTranscript保持policy=%s。retainへ倒します", transcript)
            transcript = "retain"
        return cls(source_records=memory, transcript_source_records=transcript)


_T = TypeVar("_T")
_SEARCH_INDEX_SCHEMA_VERSION = "8"
_ANN_TABLES = 14
_ANN_BITS_PER_TABLE = 16
_ANN_PROBE_RADIUS = 2
_ANN_MAX_PROBES_PER_TABLE = 137  # sum(C(16, distance), distance=0..2)
_ANN_MAX_ROWS_PER_TABLE = 1280
_ANN_FAST_TABLES = 4
_ANN_FAST_PROBE_RADIUS = 1
_ANN_FAST_ROWS_PER_TABLE = 256
_ANN_FAST_ACCEPT_COSINE = 0.95
_MAX_EMBEDDING_DIM = 16_384
_DEFAULT_STORAGE_WARNING_FREE_PERCENT = 20.0


@lru_cache(maxsize=16)
def _ann_planes(dim: int):
    """Deterministic dense hyperplanes cached per embedding dimension."""
    import numpy as np

    total_bits = _ANN_TABLES * _ANN_BITS_PER_TABLE * int(dim)
    blocks = []
    for counter in range((total_bits + 255) // 256):
        blocks.append(hashlib.sha256(
            b"memory-ann-v1:" + int(dim).to_bytes(4, "little")
            + counter.to_bytes(4, "little")
        ).digest())
    bits = np.unpackbits(np.frombuffer(b"".join(blocks), dtype=np.uint8))[:total_bits]
    return np.where(
        bits.reshape(_ANN_TABLES * _ANN_BITS_PER_TABLE, int(dim)) != 0,
        1.0,
        -1.0,
    ).astype(np.float32)


def _normalised_embedding_blob(
    blob: bytes | None,
    dim: int | None,
    *,
    expected_dim: int | None = None,
) -> bytes | None:
    """Validate a canonical float32 vector and return a unit-length copy."""
    import numpy as np

    try:
        size = int(dim or 0)
        raw = bytes(blob or b"")
    except (TypeError, ValueError):
        return None
    if (
        size <= 0
        or size > _MAX_EMBEDDING_DIM
        or (expected_dim is not None and size != int(expected_dim))
        or len(raw) != size * 4
    ):
        return None
    vector = np.frombuffer(raw, dtype="<f4")
    if vector.size != size or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector.astype(np.float64)))
    if not np.isfinite(norm) or norm <= 1e-12:
        return None
    normalised = (vector.astype(np.float64) / norm).astype("<f4")
    if not np.isfinite(normalised).all():
        return None
    return normalised.tobytes()


def _candidate_similarity_at_least(
    rows: list[dict[str, Any]], query_blob: bytes, threshold: float,
) -> bool:
    """Check a small validated candidate set without scanning source rows."""
    import numpy as np

    query = np.frombuffer(query_blob, dtype="<f4")
    for row in rows:
        candidate = np.frombuffer(bytes(row["embedding"]), dtype="<f4")
        if candidate.shape == query.shape and float(candidate @ query) >= threshold:
            return True
    return False


def _search_terms(text: str, *, maximum: int = 48) -> tuple[str, ...]:
    """Privacy-local character grams for the rebuildable sidecar index."""
    value = re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u3400-\u9fff]+", "", str(text).lower())
    terms = [value[index:index + 2] for index in range(max(0, len(value) - 1))]
    if value and len(value) == 1:
        terms.append(value)
    return tuple(dict.fromkeys(terms))[:maximum]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'fact',
  text TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 3,
  embedding BLOB,
  dim INTEGER,
  source TEXT DEFAULT '',
  created_at REAL NOT NULL,
  last_accessed REAL,
  access_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE TABLE IF NOT EXISTS transcripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  speaker TEXT DEFAULT '',
  user_text TEXT NOT NULL,
  assistant_text TEXT NOT NULL,
  emotion TEXT DEFAULT '',
  topic TEXT DEFAULT '',
  embedding BLOB,
  dim INTEGER,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_created ON transcripts(created_at);
CREATE TABLE IF NOT EXISTS reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reflection_id TEXT NOT NULL UNIQUE,
  target_type TEXT NOT NULL,
  statement TEXT NOT NULL,
  source_memory_ids TEXT DEFAULT '',
  support_count INTEGER NOT NULL DEFAULT 0,
  contradiction_count INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0.3,
  action_deltas TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  last_verified_at REAL NOT NULL,
  persona_id TEXT DEFAULT '',
  scope TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reflections_target ON reflections(target_type, status);
CREATE TABLE IF NOT EXISTS world_entities (
  entity_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL DEFAULT 'unknown',
  canonical_name TEXT NOT NULL,
  external_id TEXT DEFAULT '',
  aliases TEXT DEFAULT '[]',
  attributes TEXT DEFAULT '{}',
  confidence REAL NOT NULL DEFAULT 0.6,
  status TEXT NOT NULL DEFAULT 'active',
  source_event_ids TEXT DEFAULT '[]',
  first_observed_at REAL NOT NULL,
  last_observed_at REAL NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_world_entities_external ON world_entities(external_id);
CREATE TABLE IF NOT EXISTS world_facts (
  fact_key TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.6,
  source_type TEXT DEFAULT 'observation',
  source_event_ids TEXT DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  observed_at REAL NOT NULL,
  last_verified_at REAL NOT NULL,
  ttl REAL,
  session_scoped INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS goals (
  goal_id TEXT PRIMARY KEY,
  goal_type TEXT NOT NULL DEFAULT 'user_goal',
  description TEXT NOT NULL,
  owner_ids TEXT DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'proposed',
  priority REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.6,
  source_event_ids TEXT DEFAULT '[]',
  related_entity_ids TEXT DEFAULT '[]',
  related_memory_ids TEXT DEFAULT '[]',
  completion_conditions TEXT DEFAULT '[]',
  obligation_ids TEXT DEFAULT '[]',
  blocked_reason TEXT DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
CREATE TABLE IF NOT EXISTS goal_obligations (
  goal_id TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  linked_at REAL NOT NULL,
  last_event_id TEXT DEFAULT '',
  PRIMARY KEY (goal_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS person_identities (
  person_id TEXT PRIMARY KEY,
  canonical_name TEXT DEFAULT '',
  created_at REAL NOT NULL
);
-- 人物と、接続または声紋の対応 (Phase 6D)。
--
-- **取り消した分も残す。** 物理削除すると、誤統合が判明した時に
-- 「何をどう間違えたか」が消え、同じ間違いを繰り返す。
-- `link_id` が主キーなので、同じ相手への繋ぎ直しは行が増えるだけ。
CREATE TABLE IF NOT EXISTS identity_links (
  link_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  identity_type TEXT NOT NULL,
  identity_value TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence REAL DEFAULT 0,
  support_count INTEGER DEFAULT 0,
  contradiction_count INTEGER DEFAULT 0,
  model_version TEXT DEFAULT '',
  revoked_reason TEXT DEFAULT '',
  superseded_by TEXT DEFAULT '',
  source_event_ids TEXT DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_value ON identity_links(identity_value);
CREATE INDEX IF NOT EXISTS idx_links_person ON identity_links(person_id);
-- 関係値を speaker_id から person_id へ写した記録 (Phase 6E)。
--
-- `previous_data` は**戻すのに要る最小限**のスナップショット。
-- 声紋ベクトルも会話本文も入らない（第12条）。
-- **移行を消さない。** 消すと、間違えた時に元の値へ戻せない。
CREATE TABLE IF NOT EXISTS identity_migrations (
  migration_id TEXT PRIMARY KEY,
  speaker_id TEXT NOT NULL,
  person_id TEXT DEFAULT '',
  identity_link_id TEXT DEFAULT '',
  source_relationship_id TEXT DEFAULT '',
  target_relationship_id TEXT DEFAULT '',
  previous_data TEXT DEFAULT '{}',
  migrated_data TEXT DEFAULT '{}',
  migration_status TEXT NOT NULL,
  conflict_reason TEXT DEFAULT '',
  created_at REAL NOT NULL,
  applied_at REAL DEFAULT 0,
  rolled_back_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_migrations_speaker
  ON identity_migrations(speaker_id);
-- 移行の作業そのもの (Phase 6G)。
--
-- 移行の**記録**（identity_migrations）が冪等でも、**作業の状態**が
-- 消えると、利用者は次に何をすればよいか分からない。落ちた時に
-- 「途中で止まっている」と言えるように、ここへ残す。
--
-- `process_id` は「そのプロセスがまだ生きているか」を見るため。
-- 起動IDが違えば、RUNNING のまま残っているのは前回の落ちた分。
CREATE TABLE IF NOT EXISTS migration_jobs (
  job_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  requested_mode TEXT DEFAULT '',
  previous_mode TEXT DEFAULT '',
  status TEXT NOT NULL,
  progress_cursor TEXT DEFAULT '',
  processed_count INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  conflict_count INTEGER DEFAULT 0,
  failure_count INTEGER DEFAULT 0,
  process_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  started_at REAL DEFAULT 0,
  updated_at REAL DEFAULT 0,
  completed_at REAL DEFAULT 0,
  error_summary TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON migration_jobs(status);

CREATE TABLE IF NOT EXISTS bounded_plans (
  plan_id TEXT PRIMARY KEY,
  goal_id TEXT DEFAULT '',
  status TEXT NOT NULL,
  requester_person_id TEXT DEFAULT '',
  replan_count INTEGER DEFAULT 0,
  step_count INTEGER DEFAULT 0,
  steps_json TEXT DEFAULT '',
  closed_reason TEXT DEFAULT '',
  process_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS confirmations (
  confirmation_id TEXT PRIMARY KEY,
  intent_id TEXT DEFAULT '',
  plan_id TEXT DEFAULT '',
  step_id TEXT DEFAULT '',
  tool_id TEXT DEFAULT '',
  operation_id TEXT DEFAULT '',
  effect_category TEXT DEFAULT '',
  confirmation_hash TEXT DEFAULT '',
  requester_person_id TEXT DEFAULT '',
  approver_person_id TEXT DEFAULT '',
  target_ids TEXT DEFAULT '',
  conversation_id TEXT DEFAULT '',
  channel_id TEXT DEFAULT '',
  status TEXT NOT NULL,
  process_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL DEFAULT 0,
  decided_at REAL DEFAULT 0,
  consumed_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_executions (
  execution_id TEXT PRIMARY KEY,
  plan_id TEXT DEFAULT '',
  step_id TEXT DEFAULT '',
  action_intent_id TEXT DEFAULT '',
  tool_id TEXT DEFAULT '',
  operation_id TEXT DEFAULT '',
  effect_category TEXT DEFAULT '',
  idempotency_key TEXT DEFAULT '',
  requester_person_id TEXT DEFAULT '',
  confirmation_id TEXT DEFAULT '',
  status TEXT NOT NULL,
  side_effect_confirmed INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 1,
  error_type TEXT DEFAULT '',
  process_id TEXT DEFAULT '',
  created_at REAL NOT NULL,
  started_at REAL DEFAULT 0,
  completed_at REAL DEFAULT 0,
  -- 報告済みかどうか（Phase 7B）。**再起動後に二度言わないため。**
  reported_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exec_key ON tool_executions(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_exec_status ON tool_executions(status);
"""

#: エピソード記憶のために `memories` へ足す列。
#:
#: **新しいテーブルを作らない。** 既存の fact / preference / episode は
#: そのまま読み書きでき、足りないメタデータだけを後付けする。
#: 列を足すだけなので、古いDBをそのまま開いても壊れない。
#:
#: `meta` は JSON。**問い合わせや順位付けに使う値だけを実列にして**、
#: 残り（source_event_ids・session_id・各salience）はここへ入れる。
#: 全部を列にすると、次に1つ増やすたびにマイグレーションが要る。
_EPISODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("information_type", "TEXT DEFAULT 'observation'"),
    ("event_type", "TEXT DEFAULT 'conversation'"),
    ("confidence", "REAL DEFAULT 0.5"),
    ("status", "TEXT DEFAULT 'active'"),
    ("superseded_by", "INTEGER DEFAULT 0"),
    ("speaker_key", "TEXT DEFAULT ''"),
    ("support_count", "INTEGER DEFAULT 1"),
    ("contradiction_count", "INTEGER DEFAULT 0"),
    ("occurred_at", "REAL"),
    ("meta", "TEXT DEFAULT ''"),
    # Phase 7C: 誰の記憶か・誰が読んでよいか。
    #
    # **既定を空にしてある。** 「不明」を「共有」に読み替えないため。
    # 空のまま残った古い行は `readable()` が隔離側へ倒す。
    ("persona_id", "TEXT DEFAULT ''"),
    ("scope", "TEXT DEFAULT ''"),
)


def _decode(row: dict[str, Any], lists: tuple[str, ...], objects: tuple[str, ...]) -> dict[str, Any]:
    """JSON で入れた列を戻す。**壊れていても行ごと捨てない。**"""
    for key in lists:
        with contextlib.suppress(Exception):
            row[key] = json.loads(row.get(key) or "[]")
    for key in objects:
        with contextlib.suppress(Exception):
            row[key] = json.loads(row.get(key) or "{}")
    return row


class MemoryStore:
    """SQLite による長期記憶ストア。複数スレッドから安全に呼べる。"""

    #: 書き込み時の持ち主。`bind_persona()` で入れる。
    _write_persona_id: str = ""
    #: 持ち主不明の私的記憶を**保存しない**か。既定は保存する（警告のみ）。
    #:
    #: `persona_scope_enabled` が false の間は、持ち主が空でも検索条件に
    #: 入らないので読める。そこで拒否すると、**読める記憶を落とす**
    #: ことになる。分離を有効にしてから拒否へ切り替える。
    _strict_persona: bool = False

    def bind_persona(self, persona_id: str, *, strict: bool = False) -> None:
        """これ以降に書く記憶の持ち主。**書き込み側の1箇所で決める。**

        Phase 7C で読み側だけ直したため、**新しく作られる記憶は
        `persona_id` が空のまま**溜まっていた。空のまま溜めると、
        あとで分離を有効にした瞬間に全部「所有者不明」で隠れる。
        """
        self._write_persona_id = str(persona_id or "")
        self._strict_persona = bool(strict)

    @property
    def bound_persona_id(self) -> str:
        """Persona that owns both new writes and strict reads."""
        return self._write_persona_id

    def _read_scope(self) -> tuple[str, tuple[str, ...]]:
        if self._strict_persona and self._write_persona_id:
            return " AND persona_id = ?", (self._write_persona_id,)
        return "", ()

    def _owner(self, persona_id: str, scope: str) -> tuple[str, str, bool]:
        """書く持ち主とスコープ、そして**書いてよいか**を返す。"""
        from neuro_voice.cognition.persona_scope import PRIVATE_SCOPES

        owner = str(persona_id or self._write_persona_id or "")
        tag = str(scope or "")
        if tag in PRIVATE_SCOPES and not owner:
            # **持ち主の分からない私的記憶。** 共有へ昇格させない。
            logger.warning(
                "持ち主不明の私的記憶: scope=%s（%s）", tag,
                "保存しない" if self._strict_persona else "持ち主が空のまま保存")
            if self._strict_persona:
                return (owner, tag, False)
        return (owner, tag, True)

    def __init__(
        self,
        db_path: str | Path,
        *,
        retention_policy: RetentionPolicy | None = None,
        capacity_warning_free_percent: float = _DEFAULT_STORAGE_WARNING_FREE_PERCENT,
        capacity_probe_paths: Mapping[str, str | Path] | None = None,
    ):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Search-index repair deliberately re-enters the store lock through
        # the public drop/rebuild methods while one owner holds the complete
        # close -> unlink -> recreate interval.
        self._lock = threading.RLock()
        self._retention_policy = retention_policy or RetentionPolicy()
        self._automatic_source_deletions = 0
        self._explicit_source_deletions = 0
        self._write_available = True
        self._last_write_error = ""
        self._search_conn: sqlite3.Connection | None = None
        self._search_path = (
            Path(":memory:") if str(db_path) == ":memory:"
            else self._path.with_name(f"{self._path.name}.search-index")
        )
        try:
            warning_threshold = float(capacity_warning_free_percent)
        except (TypeError, ValueError, OverflowError):
            warning_threshold = _DEFAULT_STORAGE_WARNING_FREE_PERCENT
        if not math.isfinite(warning_threshold):
            warning_threshold = _DEFAULT_STORAGE_WARNING_FREE_PERCENT
        self._capacity_warning_free_percent = min(100.0, max(0.0, warning_threshold))
        probe_paths: dict[str, Path] = {}
        if str(db_path) != ":memory:":
            probe_paths.update({
                "canonical": self._path.parent,
                "sidecar": self._search_path.parent,
            })
        for label, candidate in (capacity_probe_paths or {}).items():
            name = str(label or "").strip()
            if name:
                probe_paths[name] = Path(candidate)
        self._capacity_probe_paths = tuple(probe_paths.items())
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._migrate_episode_columns()
                self._migrate_search_revision()
                self._migrate_tool_columns()
                self._migrate_reflection_columns()
                self._conn.commit()
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                self._conn.rollback()
            if self._schema_is_current():
                self._write_available = False
                self._last_write_error = "sqlite_read_only"
                logger.warning("Memory DBはread-onlyのまま既存schemaを使用します")
            else:
                self._conn.close()
                raise MemoryStoreMigrationRequired(
                    "memory store migration required before read-only startup"
                ) from exc

    def _schema_is_current(self) -> bool:
        """Check every additive migration required by current readers exists."""
        try:
            required_tables = set(re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+(\w+)", _SCHEMA, re.IGNORECASE,
            )) | {"memory_search_state"}
            present = {
                str(row[0]) for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not required_tables.issubset(present):
                return False

            def columns(table: str) -> set[str]:
                return {
                    str(row[1]) for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }

            memory_required = {name for name, _ddl in _EPISODE_COLUMNS} | {
                "id", "text", "embedding", "dim", "created_at",
            }
            if not memory_required.issubset(columns("memories")):
                return False
            if not {
                "id", "user_text", "assistant_text", "embedding", "dim", "created_at",
            }.issubset(columns("transcripts")):
                return False
            if not {"persona_id", "scope"}.issubset(columns("reflections")):
                return False
            if not {"conversation_id", "channel_id"}.issubset(columns("confirmations")):
                return False
            if "reported_at" not in columns("tool_executions"):
                return False
            if self._conn.execute(
                "SELECT revision FROM memory_search_state WHERE singleton=1"
            ).fetchone() is None:
                return False
            triggers = {
                str(row[0]) for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            return {
                "memory_search_insert", "memory_search_delete", "memory_search_update",
                "transcript_search_insert", "transcript_search_delete",
                "transcript_search_update",
            }.issubset(triggers)
        except (sqlite3.Error, TypeError, IndexError):
            return False

    def _migrate_search_revision(self) -> None:
        """Persist a cheap owner-controlled revision for indexed source fields.

        Access bookkeeping is intentionally absent: recalling a memory may
        update ``last_accessed``/``access_count`` without invalidating a
        rebuildable index containing only text, ACL, status, time and vectors.
        """
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS memory_search_state ("
            " singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            " revision INTEGER NOT NULL);"
            "INSERT OR IGNORE INTO memory_search_state(singleton,revision) VALUES(1,0);"
            "CREATE TRIGGER IF NOT EXISTS memory_search_insert AFTER INSERT ON memories "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
            "CREATE TRIGGER IF NOT EXISTS memory_search_delete AFTER DELETE ON memories "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
            "CREATE TRIGGER IF NOT EXISTS memory_search_update "
            "AFTER UPDATE OF text,persona_id,scope,status,created_at,embedding,dim ON memories "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
            "CREATE TRIGGER IF NOT EXISTS transcript_search_insert AFTER INSERT ON transcripts "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
            "CREATE TRIGGER IF NOT EXISTS transcript_search_delete AFTER DELETE ON transcripts "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
            "CREATE TRIGGER IF NOT EXISTS transcript_search_update "
            "AFTER UPDATE OF user_text,assistant_text,created_at,embedding,dim ON transcripts "
            "BEGIN UPDATE memory_search_state SET revision=revision+1 WHERE singleton=1; END;"
        )

    def _migrate_episode_columns(self) -> None:
        """`memories` へエピソード用の列を足す。**既存の行は壊さない。**

        `ALTER TABLE ADD COLUMN` だけなので、古いDBを開いても既存の
        fact / preference / episode はそのまま読める。新しい列は既定値で埋まる。
        """
        known = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        for name, ddl in _EPISODE_COLUMNS:
            if name in known:
                continue
            try:
                self._conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")
            except sqlite3.OperationalError:
                logger.debug("列 %s の追加をスキップ", name, exc_info=True)
        with contextlib.suppress(sqlite3.OperationalError):
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_status"
                " ON memories(status, event_type)"
            )

    def _migrate_reflection_columns(self) -> None:
        """`reflections` へ持ち主とスコープを足す（Phase 7D ⑤）。

        `CREATE TABLE IF NOT EXISTS` は**既にある表に列を足さない**ので、
        既存 DB を開くと列が無く、保存が `OperationalError` で落ちる。
        既定は空——「不明」を「共有」に読み替えないため。
        """
        known = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(reflections)").fetchall()
        }
        for name, ddl in (("persona_id", "TEXT DEFAULT ''"),
                          ("scope", "TEXT DEFAULT ''")):
            if name in known:
                continue
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE reflections ADD COLUMN {name} {ddl}")

    def _migrate_tool_columns(self) -> None:
        """Phase 7B で足した列を、既存のDBへも入れる。

        `CREATE TABLE IF NOT EXISTS` は**既にある表に列を足さない**。
        Phase 7 の DB をそのまま開くと `reported_at` が無く、
        **報告済み判定が黙って効かなくなる**——同じ結果を再起動のたびに
        言い直す形になる。気づきにくいので、ここで足しておく。
        """
        for table, columns in (
            ("confirmations", (("conversation_id", "TEXT DEFAULT ''"),
                               ("channel_id", "TEXT DEFAULT ''"))),
            ("tool_executions", (("reported_at", "REAL DEFAULT 0"),)),
        ):
            try:
                known = {
                    str(row["name"]) for row in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.OperationalError:
                continue
            if not known:
                continue
            for name, ddl in columns:
                if name in known:
                    continue
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        with self._lock:
            if self._search_conn is not None:
                self._search_conn.close()
                self._search_conn = None
            self._conn.close()

    def retention_snapshot(self) -> dict[str, Any]:
        """Content-free retention diagnostics."""
        return {
            "source_record_policy": self._retention_policy.source_records,
            "transcript_source_record_policy": (
                self._retention_policy.transcript_source_records
            ),
            "automatic_source_deletions": self._automatic_source_deletions,
            "explicit_source_deletions": self._explicit_source_deletions,
        }

    def storage_health(self) -> dict[str, Any]:
        """Content-free write and capacity health.

        Capacity is advisory only: low space or a failed probe never deletes a
        canonical row and never changes the source write state.
        """
        return {
            "state": "writable" if self._write_available else "read_only",
            "last_error": self._last_write_error,
            "capacity": self._storage_capacity_health(),
        }

    @staticmethod
    def _nearest_existing_path(candidate: Path) -> Path:
        current = candidate.resolve(strict=False)
        while not current.exists() and current.parent != current:
            current = current.parent
        return current

    def _storage_capacity_health(self) -> dict[str, Any]:
        threshold = self._capacity_warning_free_percent
        if not self._capacity_probe_paths:
            return {
                "state": "unknown",
                "free_percent": None,
                "warning_threshold_percent": threshold,
                "measurement_path": "",
                "volumes": [],
                "probe_failures": 0,
            }

        # Several logical targets commonly share one volume.  Probe each
        # physical volume once, but retain the content-free target labels so
        # an operator can see what the measurement covers.
        grouped: dict[tuple[str, int], dict[str, Any]] = {}
        unknown: list[dict[str, Any]] = []
        for label, candidate in self._capacity_probe_paths:
            try:
                measured = self._nearest_existing_path(candidate)
                stat_result = os.stat(measured)
                volume_key = (str(measured.anchor).casefold(), int(stat_result.st_dev))
                group = grouped.setdefault(volume_key, {
                    "measurement_path": str(measured),
                    "targets": [],
                })
                group["targets"].append(label)
            except Exception as exc:
                logger.warning(
                    "Memory容量測定pathの解決に失敗: %s", type(exc).__name__,
                )
                unknown.append({
                    "state": "unknown",
                    "free_percent": None,
                    "measurement_path": str(candidate),
                    "targets": [label],
                })

        volumes: list[dict[str, Any]] = []
        for group in grouped.values():
            try:
                usage = shutil.disk_usage(group["measurement_path"])
                total = int(usage.total)
                free = int(usage.free)
                if total <= 0:
                    raise ValueError("disk total is not positive")
                raw_free_percent = (free / total) * 100.0
                free_percent = round(raw_free_percent, 2)
                volumes.append({
                    "state": "warning" if raw_free_percent < threshold else "ok",
                    "free_percent": free_percent,
                    "measurement_path": group["measurement_path"],
                    "targets": sorted(set(group["targets"])),
                })
            except Exception as exc:
                logger.warning(
                    "Memory容量測定に失敗（会話と保存方針は継続）: %s",
                    type(exc).__name__,
                )
                unknown.append({
                    "state": "unknown",
                    "free_percent": None,
                    "measurement_path": group["measurement_path"],
                    "targets": sorted(set(group["targets"])),
                })

        volumes.extend(unknown)
        successful = [row for row in volumes if row["free_percent"] is not None]
        worst = min(successful, key=lambda row: row["free_percent"]) if successful else None
        if any(row["state"] == "warning" for row in successful):
            state = "warning"
        elif unknown:
            state = "unknown"
        else:
            state = "ok"
        return {
            "state": state,
            "free_percent": worst["free_percent"] if worst else None,
            "warning_threshold_percent": threshold,
            "measurement_path": (
                worst["measurement_path"] if worst
                else (unknown[0]["measurement_path"] if unknown else "")
            ),
            "volumes": volumes,
            "probe_failures": len(unknown),
        }

    def _source_write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            # Check under the same lock as the operation.  A writer queued
            # behind a failing writer must observe the read-only transition.
            if not self._write_available:
                raise MemoryWriteUnavailable("memory store is read-only")
            try:
                result = operation(self._conn)
                self._conn.commit()
                return result
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.rollback()
                self._write_available = False
                self._last_write_error = "sqlite_write_failed"
                logger.error(
                    "Memory source書込みに失敗したためread-onlyへ移行: %s",
                    type(exc).__name__,
                )
                raise MemoryWriteUnavailable("memory source write failed") from exc

    # ---------- 追加 ----------

    def add(
        self,
        text: str,
        kind: str = "fact",
        importance: int = 3,
        embedding: bytes | None = None,
        dim: int | None = None,
        source: str = "",
        persona_id: str = "",
        scope: str = "",
    ) -> int:
        """記憶を1件追加する。同一テキストが既にあれば重要度だけ引き上げる。"""
        text = text.strip()
        if not text:
            return 0
        owner, tag, allowed = self._owner(persona_id, scope)
        if not allowed:
            return 0
        importance = max(1, min(5, int(importance)))
        def write(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT id, importance FROM memories WHERE text = ? AND kind = ?",
                (text, kind),
            ).fetchone()
            if row is not None:
                new_imp = max(row["importance"], importance)
                conn.execute(
                    "UPDATE memories SET importance = ?, last_accessed = ? WHERE id = ?",
                    (new_imp, time.time(), row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO memories (kind, text, importance, embedding, dim, source,"
                " created_at, persona_id, scope)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, text, importance, embedding, dim, source, time.time(),
                 owner, tag),
            )
            return int(cur.lastrowid)
        return self._source_write(write)

    # ---------- 取得 ----------

    def all_embeddings(self) -> list[dict[str, Any]]:
        """embedding を持つ全記憶 (id, text, kind, importance, embedding, dim)。"""
        clause, args = self._read_scope()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, text, importance, embedding, dim, created_at"
                " FROM memories WHERE embedding IS NOT NULL" + clause, args,
            ).fetchall()
        return [dict(r) for r in rows]

    def all_texts(self) -> list[dict[str, Any]]:
        """全記憶のテキスト情報 (embedding なし運用のキーワード検索用)。"""
        clause, args = self._read_scope()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, text, importance, created_at FROM memories WHERE 1=1"
                + clause, args,
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, n: int = 10, kind: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, kind, text, importance, created_at FROM memories"
        args: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        q += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(q, (*args, int(n))).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS c FROM memories GROUP BY kind"
            ).fetchall()
        out = {r["kind"]: int(r["c"]) for r in rows}
        out["total"] = sum(out.values())
        return out

    # ---------- 更新・整理 ----------

    def touch(self, ids: list[int]) -> None:
        """想起された記憶のアクセス情報を更新する (よく思い出す記憶は残りやすくなる)。"""
        if not ids:
            return
        now = time.time()
        self._source_write(lambda conn: conn.executemany(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1"
                " WHERE id = ?",
                [(now, i) for i in ids],
            ))

    # ---------- エピソード記憶 ----------
    #
    # 保存先は上の `memories` テーブルのまま。**別の記憶基盤を作らない。**
    # 違いは、出典・確からしさ・状態・訂正の繋がりを持つこと。

    def owners_of(self, memory_ids) -> dict[int, tuple[str, str]]:
        """取れた記憶の**持ち主とスコープだけ**を引く（本文は読まない）。

        `id` は主キーなので1回の索引引きで済む。分離が効いているかは、
        取れたものの持ち主を見ないと確かめられない。
        """
        ids = [int(item) for item in list(memory_ids or [])[:64]]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with contextlib.suppress(sqlite3.Error):
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT id, coalesce(persona_id,''), coalesce(scope,'')"
                    f" FROM memories WHERE id IN ({marks})", ids).fetchall()
            return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
        return {}

    def scope_rejections(self, persona_id: str, *,
                         statuses: tuple[str, ...] = ("active", "low_priority"),
                         limit: int = 8) -> dict[str, int]:
        """**このペルソナが読めなかった**候補を、持ち主ごとに数える。

        「Bで0件だった」を目で確かめるための数字。1回の集計クエリだけ
        ——毎ターン全記憶を読み出すと、観測のために遅くなる（第8項）。
        本文は読まない。持ち主と件数だけ。
        """
        owner = str(persona_id or "")
        if not owner:
            return {}
        marks = ",".join("?" for _ in statuses)
        sql = (
            "SELECT coalesce(persona_id,'') AS owner, count(*) FROM memories"
            f" WHERE status IN ({marks})"
            " AND coalesce(persona_id,'') <> ?"
            " AND coalesce(scope,'') NOT IN ('global_system','world_shared',"
            "'explicit_shared')"
            " GROUP BY owner ORDER BY 2 DESC LIMIT ?")
        with contextlib.suppress(sqlite3.Error):
            with self._lock:
                rows = self._conn.execute(
                    sql, (*statuses, owner, int(limit))).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}
        return {}

    def add_episode(
        self, text: str, *, kind: str = "episode", importance: int = 3,
        information_type: str = "observation", event_type: str = "conversation",
        confidence: float = .5, status: str = "active", speaker_key: str = "",
        occurred_at: float | None = None, meta: dict[str, Any] | None = None,
        embedding: bytes | None = None, dim: int | None = None, source: str = "",
        persona_id: str = "", scope: str = "",
    ) -> int:
        """エピソードを1件追加する。**重複判定はここではやらない。**

        覚えるかどうかは `cognition.episodic.MemoryWriteGate` が決める。
        ここまで来たものは書く。判断を2箇所に置くと必ず食い違う。

        ただし**持ち主だけはここで確定させる**（Phase 7D ⑤）。呼び出し側に
        任せると、経路が増えるたびに `persona_id` が空の記憶が生まれる。
        """
        text = str(text or "").strip()
        if not text:
            return 0
        owner, tag, allowed = self._owner(persona_id, scope)
        if not allowed:
            return 0
        now = time.time()
        def write(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO memories (kind, text, importance, embedding, dim, source,"
                " created_at, information_type, event_type, confidence, status,"
                " speaker_key, occurred_at, meta, persona_id, scope, support_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (kind, text, max(1, min(5, int(importance))), embedding, dim, source,
                 now, information_type, event_type, float(confidence), status,
                 speaker_key, float(occurred_at if occurred_at is not None else now),
                 json.dumps(meta or {}, ensure_ascii=False), owner, tag),
            )
            return int(cur.lastrowid)
        return self._source_write(write)

    def episodes(
        self, *, limit: int = 200, statuses: tuple[str, ...] = ("active", "low_priority"),
        speaker_key: str = "",
        persona_id: str = "",
        allow_legacy_unscoped: bool = False,
    ) -> list[dict[str, Any]]:
        """検索候補になるエピソード。**訂正済みは既定で出さない。**

        `persona_id` を渡すと、**DB の検索条件**でペルソナを絞る。
        取ってからプロンプトで外す形にしないのは、`LIMIT` を別人格の
        記憶が先に埋めてしまうため——漏れてはいないのに、自分の記憶が
        1件も出てこない状態になる。
        """
        placeholders = ",".join("?" for _ in statuses) or "''"
        query = (
            "SELECT id, kind, text, importance, created_at, occurred_at, access_count,"
            " information_type, event_type, confidence, status, superseded_by,"
            " speaker_key, support_count, contradiction_count, meta,"
            " persona_id, scope"
            f" FROM memories WHERE COALESCE(status,'active') IN ({placeholders})"
        )
        args: list[Any] = list(statuses)
        if speaker_key:
            query += " AND (speaker_key = ? OR speaker_key = '')"
            args.append(speaker_key)
        if persona_id:
            from neuro_voice.cognition.persona_scope import sql_scope_filter

            clause, scope_args = sql_scope_filter(
                persona_id, allow_legacy=bool(allow_legacy_unscoped))
            query += f" AND {clause}"
            args.extend(scope_args)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def reinforce_episode(self, memory_id: int, *, confidence: float | None = None) -> None:
        """同じことをまた言われた。**新しい行を足さずに、支持を1つ増やす。**"""
        def write(conn: sqlite3.Connection) -> None:
            if confidence is None:
                conn.execute(
                    "UPDATE memories SET support_count = COALESCE(support_count,1) + 1,"
                    " last_accessed = ? WHERE id = ?",
                    (time.time(), int(memory_id)),
                )
            else:
                conn.execute(
                    "UPDATE memories SET support_count = COALESCE(support_count,1) + 1,"
                    " confidence = ?, last_accessed = ? WHERE id = ?",
                    (float(confidence), time.time(), int(memory_id)),
                )
        self._source_write(write)

    def mark_episode(
        self, memory_id: int, status: str, *, superseded_by: int = 0,
        count_contradiction: bool = False,
    ) -> None:
        """訂正された記憶に印を付ける。**消さない。**

        何を信じ直したのかを後から読めないと、間違った訂正に気づけない。
        `superseded_by` で新しい記憶へ繋いでおく。
        """
        self._source_write(lambda conn: conn.execute(
                "UPDATE memories SET status = ?, superseded_by = ?,"
                " contradiction_count = COALESCE(contradiction_count,0) + ?"
                " WHERE id = ?",
                (str(status), int(superseded_by), 1 if count_contradiction else 0,
                 int(memory_id)),
            ))

    def upgrade_episode(
        self, memory_id: int, *, information_type: str, confidence: float,
    ) -> None:
        """推論だったものが、本人の明言で裏付けられた時。"""
        self._source_write(lambda conn: conn.execute(
                "UPDATE memories SET information_type = ?, confidence = ?,"
                " support_count = COALESCE(support_count,1) + 1, last_accessed = ?"
                " WHERE id = ?",
                (str(information_type), float(confidence), time.time(), int(memory_id)),
            ))

    # ---------- Reflection ----------

    def save_reflection(self, record: dict[str, Any]) -> int:
        """仮説を保存または更新する。`reflection_id` が同じなら上書き。

        **仮説にも持ち主を付ける**（Phase 7D ⑤）。仮説は複数の記憶から
        作られるので、元の記憶が分かれていても仮説が共有だと、そこから
        逆に漏れる。
        """
        now = time.time()
        from neuro_voice.cognition.persona_scope import PersonaScope

        owner, tag, allowed = self._owner(
            str(record.get("persona_id", "")),
            str(record.get("scope", "") or PersonaScope.PERSONA_PRIVATE))
        if not allowed:
            return 0
        with self._lock:
            self._conn.execute(
                "INSERT INTO reflections (reflection_id, target_type, statement,"
                " source_memory_ids, support_count, contradiction_count, confidence,"
                " action_deltas, status, created_at, last_verified_at,"
                " persona_id, scope)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(reflection_id) DO UPDATE SET"
                " statement=excluded.statement, source_memory_ids=excluded.source_memory_ids,"
                " support_count=excluded.support_count,"
                " contradiction_count=excluded.contradiction_count,"
                " confidence=excluded.confidence, action_deltas=excluded.action_deltas,"
                " status=excluded.status, last_verified_at=excluded.last_verified_at,"
                " persona_id=excluded.persona_id, scope=excluded.scope",
                (
                    str(record.get("reflection_id", "")),
                    str(record.get("target_type", "conversation_strategy")),
                    str(record.get("statement", "")),
                    json.dumps(list(record.get("source_memory_ids", [])), ensure_ascii=False),
                    int(record.get("support_count", 0)),
                    int(record.get("contradiction_count", 0)),
                    float(record.get("confidence", .3)),
                    json.dumps(dict(record.get("action_deltas", {})), ensure_ascii=False),
                    str(record.get("status", "active")),
                    float(record.get("created_at", now)), now, owner, tag,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM reflections WHERE reflection_id = ?",
                (str(record.get("reflection_id", "")),),
            ).fetchone()
        return int(row["id"]) if row else 0

    def reflections(
        self, *, target_type: str = "", statuses: tuple[str, ...] = ("active",),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses) or "''"
        query = f"SELECT * FROM reflections WHERE status IN ({placeholders})"
        args: list[Any] = list(statuses)
        if target_type:
            query += " AND target_type = ?"
            args.append(target_type)
        query += " ORDER BY confidence DESC, last_verified_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, tuple(args)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            with contextlib.suppress(Exception):
                item["source_memory_ids"] = json.loads(item.get("source_memory_ids") or "[]")
            with contextlib.suppress(Exception):
                item["action_deltas"] = json.loads(item.get("action_deltas") or "{}")
            out.append(item)
        return out

    # ---------- いまどうなっているか / 何を目指しているか (Phase 6B) ----------
    #
    # **新しいDB製品へは移らない。** 既存の SQLite に表を足すだけ。
    # 保存に失敗しても会話は続ける（第17条）——呼び出し側が例外を握る。

    def save_world_entities(self, rows: list[dict[str, Any]]) -> int:
        """対象をまとめて書く。**1件ずつ同期で書かない。**"""
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO world_entities (entity_id, entity_type, canonical_name,"
                " external_id, aliases, attributes, confidence, status,"
                " source_event_ids, first_observed_at, last_observed_at, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(entity_id) DO UPDATE SET"
                " canonical_name=excluded.canonical_name,"
                " entity_type=excluded.entity_type, external_id=excluded.external_id,"
                " aliases=excluded.aliases, attributes=excluded.attributes,"
                " confidence=excluded.confidence, status=excluded.status,"
                " source_event_ids=excluded.source_event_ids,"
                " last_observed_at=excluded.last_observed_at,"
                " version=world_entities.version + 1",
                [(
                    str(r["entity_id"]), str(r.get("entity_type", "unknown")),
                    str(r.get("canonical_name", "")), str(r.get("external_id", "")),
                    json.dumps(list(r.get("aliases", [])), ensure_ascii=False),
                    json.dumps(dict(r.get("attributes", {})), ensure_ascii=False),
                    float(r.get("confidence", .6)), str(r.get("status", "active")),
                    json.dumps(list(r.get("source_event_ids", [])), ensure_ascii=False),
                    float(r.get("first_observed_at", time.time())),
                    float(r.get("last_observed_at", time.time())), 1,
                ) for r in rows],
            )
            self._conn.commit()
        return len(rows)

    def save_world_facts(self, rows: list[dict[str, Any]]) -> int:
        """事実をまとめて書く。`session_scoped` は起動時に古い扱いへ落とす。"""
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO world_facts (fact_key, subject_id, predicate, value,"
                " confidence, source_type, source_event_ids, status, observed_at,"
                " last_verified_at, ttl, session_scoped, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(fact_key) DO UPDATE SET"
                " value=excluded.value, confidence=excluded.confidence,"
                " source_type=excluded.source_type,"
                " source_event_ids=excluded.source_event_ids,"
                " status=excluded.status, last_verified_at=excluded.last_verified_at,"
                " ttl=excluded.ttl, session_scoped=excluded.session_scoped,"
                " version=world_facts.version + 1",
                [(
                    str(r["fact_key"]), str(r.get("subject_id", "")),
                    str(r.get("predicate", "")), json.dumps(r.get("value"), ensure_ascii=False),
                    float(r.get("confidence", .6)), str(r.get("source_type", "observation")),
                    json.dumps(list(r.get("source_event_ids", [])), ensure_ascii=False),
                    str(r.get("status", "active")),
                    float(r.get("observed_at", time.time())),
                    float(r.get("last_verified_at", time.time())),
                    r.get("ttl"), 1 if r.get("session_scoped") else 0, 1,
                ) for r in rows],
            )
            self._conn.commit()
        return len(rows)

    def world_entities(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM world_entities WHERE status != 'removed'"
                " ORDER BY last_observed_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [_decode(dict(r), ("aliases", "source_event_ids"), ("attributes",))
                for r in rows]

    def world_facts(self, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM world_facts ORDER BY last_verified_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = _decode(dict(row), ("source_event_ids",), ())
            with contextlib.suppress(Exception):
                item["value"] = json.loads(item.get("value") or "null")
            out.append(item)
        return out

    def save_goals(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO goals (goal_id, goal_type, description, owner_ids, status,"
                " priority, confidence, source_event_ids, related_entity_ids,"
                " related_memory_ids, completion_conditions, obligation_ids,"
                " blocked_reason, created_at, updated_at, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(goal_id) DO UPDATE SET"
                " description=excluded.description, status=excluded.status,"
                " priority=excluded.priority, confidence=excluded.confidence,"
                " owner_ids=excluded.owner_ids,"
                " related_entity_ids=excluded.related_entity_ids,"
                " related_memory_ids=excluded.related_memory_ids,"
                " completion_conditions=excluded.completion_conditions,"
                " obligation_ids=excluded.obligation_ids,"
                " blocked_reason=excluded.blocked_reason,"
                " updated_at=excluded.updated_at, version=goals.version + 1",
                [(
                    str(r["goal_id"]), str(r.get("goal_type", "user_goal")),
                    str(r.get("description", "")),
                    json.dumps(list(r.get("owner_ids", [])), ensure_ascii=False),
                    str(r.get("status", "proposed")), float(r.get("priority", .5)),
                    float(r.get("confidence", .6)),
                    json.dumps(list(r.get("source_event_ids", [])), ensure_ascii=False),
                    json.dumps(list(r.get("related_entity_ids", [])), ensure_ascii=False),
                    json.dumps(list(r.get("related_memory_ids", [])), ensure_ascii=False),
                    json.dumps(list(r.get("completion_conditions", [])), ensure_ascii=False),
                    json.dumps(list(r.get("obligation_ids", [])), ensure_ascii=False),
                    str(r.get("blocked_reason", "")),
                    float(r.get("created_at", time.time())),
                    float(r.get("updated_at", time.time())), 1,
                ) for r in rows],
            )
            self._conn.commit()
        return len(rows)

    def goals(self, *, statuses: tuple[str, ...] = (), limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM goals"
        args: list[Any] = []
        if statuses:
            query += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            args.extend(statuses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, tuple(args)).fetchall()
        return [_decode(dict(r), (
            "owner_ids", "source_event_ids", "related_entity_ids",
            "related_memory_ids", "completion_conditions", "obligation_ids"), ())
            for r in rows]

    def link_goal_obligation(
        self, goal_id: str, obligation_id: str, *, event_id: str = "",
    ) -> None:
        """目標と未完了事項を繋ぐ。**既存Obligationは置き換えない。**"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO goal_obligations (goal_id, obligation_id, linked_at,"
                " last_event_id) VALUES (?,?,?,?)"
                " ON CONFLICT(goal_id, obligation_id) DO UPDATE SET"
                " last_event_id=excluded.last_event_id",
                (str(goal_id), str(obligation_id), time.time(), str(event_id)))
            self._conn.commit()

    def goal_obligations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM goal_obligations").fetchall()
        return [dict(r) for r in rows]

    # ---------- 誰なのか (Phase 6D) ----------
    #
    # **人物と、接続・声紋の対応は、再起動を越えて残す。**
    # 参加している状態は残さない（起動時に確かめ直す）が、
    # 「この Discord ID はこの人」は消える理由が無い。

    def save_identity_links(self, people, links) -> None:
        """人物とリンクをまとめて書く。**取り消した分も書く。**"""
        with self._lock:
            for row in people or ():
                self._conn.execute(
                    "INSERT INTO person_identities (person_id, canonical_name,"
                    " created_at) VALUES (?,?,?)"
                    " ON CONFLICT(person_id) DO UPDATE SET"
                    " canonical_name=excluded.canonical_name",
                    (str(row["person_id"]), str(row.get("canonical_name", "")),
                     float(row.get("created_at", time.time()))))
            for row in links or ():
                self._conn.execute(
                    "INSERT INTO identity_links (link_id, person_id, identity_type,"
                    " identity_value, status, confidence, support_count,"
                    " contradiction_count, model_version, revoked_reason,"
                    " superseded_by, source_event_ids, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(link_id) DO UPDATE SET"
                    " person_id=excluded.person_id, status=excluded.status,"
                    " confidence=excluded.confidence,"
                    " support_count=excluded.support_count,"
                    " contradiction_count=excluded.contradiction_count,"
                    " revoked_reason=excluded.revoked_reason,"
                    " superseded_by=excluded.superseded_by,"
                    " source_event_ids=excluded.source_event_ids,"
                    " updated_at=excluded.updated_at",
                    (str(row["link_id"]), str(row["person_id"]),
                     str(row["identity_type"]), str(row["identity_value"]),
                     str(row["status"]), float(row.get("confidence", 0.0)),
                     int(row.get("support_count", 0)),
                     int(row.get("contradiction_count", 0)),
                     str(row.get("model_version", "")),
                     str(row.get("revoked_reason", "")),
                     str(row.get("superseded_by", "")),
                     json.dumps(list(row.get("source_event_ids", ())),
                                ensure_ascii=False),
                     float(row.get("created_at", time.time())),
                     float(row.get("updated_at", time.time()))))
            self._conn.commit()

    def person_identities(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM person_identities").fetchall()
        return [dict(r) for r in rows]

    def identity_links(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identity_links ORDER BY created_at").fetchall()
        return [_decode(dict(r), ("source_event_ids",), ()) for r in rows]

    def save_identity_migration(self, row) -> None:
        """移行を1件記録する。**上書きはするが、行は消さない。**"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO identity_migrations (migration_id, speaker_id,"
                " person_id, identity_link_id, source_relationship_id,"
                " target_relationship_id, previous_data, migrated_data,"
                " migration_status, conflict_reason, created_at, applied_at,"
                " rolled_back_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(migration_id) DO UPDATE SET"
                " person_id=excluded.person_id,"
                " identity_link_id=excluded.identity_link_id,"
                " target_relationship_id=excluded.target_relationship_id,"
                " previous_data=excluded.previous_data,"
                " migrated_data=excluded.migrated_data,"
                " migration_status=excluded.migration_status,"
                " conflict_reason=excluded.conflict_reason,"
                " applied_at=excluded.applied_at,"
                " rolled_back_at=excluded.rolled_back_at",
                (str(row["migration_id"]), str(row["speaker_id"]),
                 str(row.get("person_id", "")),
                 str(row.get("identity_link_id", "")),
                 str(row.get("source_relationship_id", "")),
                 str(row.get("target_relationship_id", "")),
                 json.dumps(dict(row.get("previous_data") or {}), ensure_ascii=False),
                 json.dumps(dict(row.get("migrated_data") or {}), ensure_ascii=False),
                 str(row["migration_status"]),
                 str(row.get("conflict_reason", "")),
                 float(row.get("created_at", time.time())),
                 float(row.get("applied_at", 0.0)),
                 float(row.get("rolled_back_at", 0.0))))
            self._conn.commit()

    def identity_migrations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identity_migrations ORDER BY created_at").fetchall()
        return [_decode(dict(r), (), ("previous_data", "migrated_data"))
                for r in rows]

    def save_migration_job(self, row) -> None:
        """作業の状態を1件。**声紋も記憶本文も関係値も入れない**（第12条）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO migration_jobs (job_id, operation, requested_mode,"
                " previous_mode, status, progress_cursor, processed_count,"
                " success_count, conflict_count, failure_count, process_id,"
                " created_at, started_at, updated_at, completed_at, error_summary)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " status=excluded.status,"
                " progress_cursor=excluded.progress_cursor,"
                " processed_count=excluded.processed_count,"
                " success_count=excluded.success_count,"
                " conflict_count=excluded.conflict_count,"
                " failure_count=excluded.failure_count,"
                " process_id=excluded.process_id,"
                " started_at=excluded.started_at,"
                " updated_at=excluded.updated_at,"
                " completed_at=excluded.completed_at,"
                " error_summary=excluded.error_summary",
                (str(row["job_id"]), str(row["operation"]),
                 str(row.get("requested_mode", "")),
                 str(row.get("previous_mode", "")), str(row["status"]),
                 str(row.get("progress_cursor", "")),
                 int(row.get("processed_count", 0)),
                 int(row.get("success_count", 0)),
                 int(row.get("conflict_count", 0)),
                 int(row.get("failure_count", 0)),
                 str(row.get("process_id", "")),
                 float(row.get("created_at", time.time())),
                 float(row.get("started_at", 0.0)),
                 float(row.get("updated_at", time.time())),
                 float(row.get("completed_at", 0.0)),
                 str(row.get("error_summary", ""))[:200]))
            self._conn.commit()

    def migration_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM migration_jobs ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # -- Phase 7: 計画・確認・実行 --------------------------------------
    #
    # **引数の値は `tool_executions` へ入れない**（第30項）。
    # 記録してよい形（機密を伏せたもの）は `bounded_plans.steps_json`
    # にだけ置く。実行の記録から本文を復元できないようにしておく。

    def save_plan(self, row) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bounded_plans (plan_id, goal_id, status,"
                " requester_person_id, replan_count, step_count, steps_json,"
                " closed_reason, process_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(plan_id) DO UPDATE SET"
                " status=excluded.status, replan_count=excluded.replan_count,"
                " step_count=excluded.step_count, steps_json=excluded.steps_json,"
                " closed_reason=excluded.closed_reason,"
                " updated_at=excluded.updated_at",
                (str(row["plan_id"]), str(row.get("goal_id", "")),
                 str(row["status"]), str(row.get("requester_person_id", "")),
                 int(row.get("replan_count", 0)),
                 int(row.get("step_count", 0)),
                 str(row.get("steps_json", ""))[:4000],
                 str(row.get("closed_reason", ""))[:80],
                 str(row.get("process_id", "")),
                 float(row.get("created_at", time.time())),
                 float(row.get("updated_at", time.time()))))
            self._conn.commit()

    def plans(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bounded_plans ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def save_confirmation(self, row) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO confirmations (confirmation_id, intent_id,"
                " plan_id, step_id, tool_id, operation_id, effect_category,"
                " confirmation_hash, requester_person_id, approver_person_id,"
                " target_ids, conversation_id, channel_id, status, process_id,"
                " created_at, expires_at, decided_at, consumed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(confirmation_id) DO UPDATE SET"
                " status=excluded.status,"
                " approver_person_id=excluded.approver_person_id,"
                " decided_at=excluded.decided_at,"
                " consumed_at=excluded.consumed_at",
                (str(row["confirmation_id"]), str(row.get("intent_id", "")),
                 str(row.get("plan_id", "")), str(row.get("step_id", "")),
                 str(row.get("tool_id", "")), str(row.get("operation_id", "")),
                 str(row.get("effect_category", "")),
                 str(row.get("confirmation_hash", "")),
                 str(row.get("requester_person_id", "")),
                 str(row.get("approver_person_id", "")),
                 str(row.get("target_ids", ""))[:200],
                 str(row.get("conversation_id", ""))[:80],
                 str(row.get("channel_id", ""))[:80], str(row["status"]),
                 str(row.get("process_id", "")),
                 float(row.get("created_at", time.time())),
                 float(row.get("expires_at", 0.0)),
                 float(row.get("decided_at", 0.0)),
                 float(row.get("consumed_at", 0.0))))
            self._conn.commit()

    def confirmations(self, *, pending_only: bool = False,
                      limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM confirmations"
        if pending_only:
            query += " WHERE status IN ('pending','approved')"
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def save_tool_execution(self, row) -> None:
        """実行1件。**引数の値は入れない**（第30項）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_executions (execution_id, plan_id, step_id,"
                " action_intent_id, tool_id, operation_id, effect_category,"
                " idempotency_key, requester_person_id, confirmation_id,"
                " status, side_effect_confirmed, attempts, error_type,"
                " process_id, created_at, started_at, completed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(execution_id) DO UPDATE SET"
                " status=excluded.status,"
                " side_effect_confirmed=excluded.side_effect_confirmed,"
                " attempts=excluded.attempts, error_type=excluded.error_type,"
                " completed_at=excluded.completed_at",
                (str(row["execution_id"]), str(row.get("plan_id", "")),
                 str(row.get("step_id", "")),
                 str(row.get("action_intent_id", "")),
                 str(row.get("tool_id", "")), str(row.get("operation_id", "")),
                 str(row.get("effect_category", "")),
                 str(row.get("idempotency_key", "")),
                 str(row.get("requester_person_id", "")),
                 str(row.get("confirmation_id", "")), str(row["status"]),
                 int(row.get("side_effect_confirmed", 0)),
                 int(row.get("attempts", 1)),
                 str(row.get("error_type", ""))[:60],
                 str(row.get("process_id", "")),
                 float(row.get("created_at", time.time())),
                 float(row.get("started_at", 0.0)),
                 float(row.get("completed_at", 0.0))))
            self._conn.commit()

    def tool_executions(self, *, unfinished_only: bool = False,
                        limit: int = 40) -> list[dict[str, Any]]:
        query = "SELECT * FROM tool_executions"
        if unfinished_only:
            query += (" WHERE status NOT IN ('succeeded','failed','cancelled',"
                      "'partial_success','unknown_outcome')")
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def mark_execution_reported(self, execution_id: str) -> None:
        """報告した印（Phase 7B）。**再起動後に二度言わないため。**"""
        with self._lock:
            self._conn.execute(
                "UPDATE tool_executions SET reported_at = ?"
                " WHERE execution_id = ? AND reported_at = 0",
                (time.time(), str(execution_id)))
            self._conn.commit()

    def unreported_executions(self, limit: int = 20) -> list[dict[str, Any]]:
        """成功したが、まだ伝えていないもの。**再実行はしない。**"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_executions"
                " WHERE status = 'succeeded' AND reported_at = 0"
                " ORDER BY completed_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def mark_executions_unknown(self, process_id: str) -> int:
        """再起動時。**前回のプロセスで走っていた実行を「不明」にする。**

        成功にも失敗にもしない。届いたかどうか分からないものを
        分かったことにすると、そこから先の判断が全部ずれる。
        **ここで自動再実行はしない**（第24項）。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tool_executions SET status = 'unknown_outcome',"
                " completed_at = ?"
                " WHERE status IN ('running','pending') AND process_id != ?",
                (time.time(), str(process_id)))
            self._conn.commit()
            return cur.rowcount or 0

    def interrupt_stale_jobs(self, process_id: str) -> int:
        """前回のプロセスが残した `RUNNING` を、中断として印を付ける。

        **起動IDが違えば、走っているのは前回の落ちた分。**
        そのまま `RUNNING` で残すと、次の作業が永久に始められない。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE migration_jobs SET status = 'interrupted_recoverable',"
                " updated_at = ?"
                " WHERE status IN ('running','pending') AND process_id != ?",
                (time.time(), str(process_id)))
            self._conn.commit()
            return cur.rowcount or 0

    def mark_session_facts_stale(self) -> int:
        """起動時に、一時的な事実を古い扱いへ落とす。

        **再起動しただけでゲーム途中の状態を「いまこう」と言わない。**
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE world_facts SET status = 'stale'"
                " WHERE session_scoped = 1 AND status = 'active'")
            self._conn.commit()
            return cur.rowcount or 0

    # ---------- 会話ログ (要約でなく実際の発言+感情の記録) ----------

    def add_transcript(
        self,
        user_text: str,
        assistant_text: str,
        *,
        speaker: str = "",
        emotion: str = "",
        topic: str = "",
        embedding: bytes | None = None,
        dim: int | None = None,
    ) -> int:
        user_text, assistant_text = user_text.strip(), assistant_text.strip()
        if not user_text or not assistant_text:
            return 0
        def write(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO transcripts"
                " (speaker, user_text, assistant_text, emotion, topic, embedding, dim, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (speaker, user_text, assistant_text, emotion, topic, embedding, dim, time.time()),
            )
            return int(cur.lastrowid)
        return self._source_write(write)

    def transcript_embeddings(self) -> list[dict[str, Any]]:
        """embedding 付きの全会話ログ (意味検索の行列構築用)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, speaker, user_text, assistant_text, emotion, topic,"
                " embedding, dim, created_at FROM transcripts WHERE embedding IS NOT NULL"
            ).fetchall()
        return [dict(r) for r in rows]

    def all_transcripts(self) -> list[dict[str, Any]]:
        """全会話ログ (embedding なし運用のキーワード検索用)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, speaker, user_text, assistant_text, emotion, topic, created_at"
                " FROM transcripts"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_transcripts(self, limit: int = 6) -> list[dict[str, Any]]:
        """直近の会話ログを時系列順で返す。

        「それ」「さっきおすすめした物」のような参照表現は意味検索に
        内容語がほとんど無く、正しい会話を拾えない。全件走査ではなく
        SQLiteで新しい順に限定して取得し、LLMへは自然な時系列で渡す。
        """
        safe_limit = max(1, min(20, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, speaker, user_text, assistant_text, emotion, topic, created_at"
                " FROM transcripts ORDER BY created_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def transcripts_between(self, start_ts: float, end_ts: float, limit: int = 200) -> list[dict[str, Any]]:
        """期間指定の会話ログ (「昨日何話した?」等の日付質問用)。時系列順。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, speaker, user_text, assistant_text, emotion, topic, created_at"
                " FROM transcripts WHERE created_at >= ? AND created_at < ?"
                " ORDER BY created_at ASC LIMIT ?",
                (float(start_ts), float(end_ts), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def _source_signature(self) -> str:
        """Legacy diagnostic retained for old tests/tools; not freshness truth."""
        with self._lock:
            memory_max = self._conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM memories"
            ).fetchone()[0]
            transcript_max = self._conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM transcripts"
            ).fetchone()[0]
            page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
            free_pages = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        stat = self._path.stat() if self._path.exists() and str(self._path) != ":memory:" else None
        payload = json.dumps([
            int(memory_max), int(transcript_max), int(page_count), int(free_pages),
            int(stat.st_size) if stat else 0,
            int(stat.st_mtime_ns) if stat else 0,
        ], separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    def _source_revision(self) -> int:
        """Return the persistent revision owned by indexed source fields."""
        with self._lock:
            return int(self._conn.execute(
                "SELECT revision FROM memory_search_state WHERE singleton=1"
            ).fetchone()[0])

    @staticmethod
    def _embedding_bands(blob: bytes | None, dim: int | None) -> tuple[tuple[int, str], ...]:
        """Build exact + dense random-hyperplane LSH buckets.

        Fourteen independent 16-bit tables inspect every dimension.  Query-time
        Hamming-distance-two multiprobe admits the similarity range used by
        ``Mind`` while final cosine ranking remains canonical there.
        """
        return MemoryStore._embedding_bands_batch(((blob, dim),))[0]

    @staticmethod
    def _embedding_bands_batch(
        items: tuple[tuple[bytes | None, int | None], ...],
    ) -> list[tuple[tuple[int, str], ...]]:
        """Validate/normalise each raw vector once, then derive its buckets."""
        normalised_items: list[tuple[bytes | None, int | None]] = []
        for blob, dim in items:
            try:
                size = int(dim or 0)
            except (TypeError, ValueError):
                normalised_items.append((None, None))
                continue
            raw = _normalised_embedding_blob(blob, dim)
            normalised_items.append((raw, size if raw is not None else None))
        return MemoryStore._embedding_bands_from_normalised_batch(
            tuple(normalised_items)
        )

    @staticmethod
    def _embedding_bands_from_normalised_batch(
        items: tuple[tuple[bytes | None, int | None], ...],
    ) -> list[tuple[tuple[int, str], ...]]:
        """Derive buckets from already validated unit-float32 bytes."""
        import numpy as np

        output: list[tuple[tuple[int, str], ...]] = [()] * len(items)
        groups: dict[int, list[tuple[int, bytes]]] = {}
        for position, (blob, dim) in enumerate(items):
            try:
                size = int(dim or 0)
                raw = bytes(blob or b"")
            except (TypeError, ValueError):
                continue
            if size <= 0 or len(raw) != size * 4:
                continue
            groups.setdefault(size, []).append((position, raw))
        for size, group in groups.items():
            matrix = np.stack([
                np.frombuffer(raw, dtype="<f4") for _position, raw in group
            ])
            projections = matrix @ _ann_planes(size).T
            for row_index, (position, raw) in enumerate(group):
                bands: list[tuple[int, str]] = [
                    (0, "x:" + hashlib.sha256(raw).hexdigest()[:24])
                ]
                for table in range(_ANN_TABLES):
                    bucket = 0
                    start = table * _ANN_BITS_PER_TABLE
                    values = projections[
                        row_index, start:start + _ANN_BITS_PER_TABLE
                    ]
                    for bit, value in enumerate(values):
                        if float(value) >= 0.0:
                            bucket |= 1 << bit
                    bands.append((table + 1, f"h:{bucket:04x}"))
                output[position] = tuple(bands)
        return output

    @staticmethod
    @lru_cache(maxsize=4096)
    def _ann_probes(bucket: str, radius: int | None = None) -> tuple[str, ...]:
        """Bounded Hamming neighbours for one table."""
        if not str(bucket).startswith("h:"):
            return (str(bucket),)
        safe_radius = min(
            _ANN_PROBE_RADIUS,
            max(0, int(_ANN_PROBE_RADIUS if radius is None else radius)),
        )
        value = int(str(bucket)[2:], 16)
        probes = [str(bucket)]
        for distance in range(1, safe_radius + 1):
            for flipped in combinations(range(_ANN_BITS_PER_TABLE), distance):
                candidate = value
                for bit in flipped:
                    candidate ^= 1 << bit
                probes.append(f"h:{candidate:04x}")
        return tuple(probes[:_ANN_MAX_PROBES_PER_TABLE])

    def _prepare_search_index_path(self) -> None:
        """Reject path escape and detach linked files before opening sidecar."""
        if str(self._search_path) == ":memory:":
            return
        source_parent = self._path.parent.resolve(strict=False)
        side_parent = self._search_path.parent.resolve(strict=False)
        if side_parent != source_parent:
            raise RuntimeError("search index path escapes canonical directory")
        try:
            linked = self._search_path.is_symlink()
            if self._search_path.exists() and not linked:
                linked = self._search_path.stat().st_nlink > 1
            if linked:
                # unlink removes only this directory entry.  Never open and
                # mutate a target shared with a canonical or unrelated DB.
                self._search_path.unlink()
        except FileNotFoundError:
            return

    def _open_search_index(self) -> sqlite3.Connection:
        if self._search_conn is None:
            for attempt in range(2):
                self._prepare_search_index_path()
                before = None
                if str(self._search_path) != ":memory:" and self._search_path.exists():
                    stat = self._search_path.stat()
                    before = (int(stat.st_dev), int(stat.st_ino))
                conn = sqlite3.connect(str(self._search_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row

                unsafe = False
                if str(self._search_path) != ":memory:":
                    try:
                        stat = self._search_path.stat()
                        after = (int(stat.st_dev), int(stat.st_ino))
                        unsafe = (
                            self._search_path.is_symlink()
                            or int(stat.st_nlink) > 1
                            or (before is not None and before != after)
                        )
                    except FileNotFoundError:
                        unsafe = True

                invalid_schema = False
                if not unsafe:
                    try:
                        cursor = conn.execute(
                            "SELECT name,type,sql FROM sqlite_master"
                            " WHERE name IN ('memory_search_docs',"
                            "'memory_embedding_docs','transcript_embedding_docs')"
                        )
                        object_rows = cursor.fetchall()
                        cursor.close()
                        objects = {
                            str(row[0]): (str(row[1]), str(row[2] or ""))
                            for row in object_rows
                        }
                        fts = objects.get("memory_search_docs")
                        if fts is not None and (
                            fts[0] != "table" or "VIRTUAL TABLE" not in fts[1].upper()
                        ):
                            invalid_schema = True
                        for name in ("memory_embedding_docs", "transcript_embedding_docs"):
                            item = objects.get(name)
                            if item is not None and item[0] != "table":
                                invalid_schema = True
                    except sqlite3.Error:
                        invalid_schema = True

                # The preflight query above creates another scheduling window.
                # Sample identity/link count again immediately before any DDL.
                if not unsafe and str(self._search_path) != ":memory:":
                    try:
                        stat = self._search_path.stat()
                        unsafe = (
                            self._search_path.is_symlink()
                            or int(stat.st_nlink) > 1
                            or after != (int(stat.st_dev), int(stat.st_ino))
                        )
                    except FileNotFoundError:
                        unsafe = True

                if unsafe or invalid_schema:
                    object_rows = []
                    objects = {}
                    conn.close()
                    if str(self._search_path) != ":memory:":
                        try:
                            self._prepare_search_index_path()
                            with contextlib.suppress(FileNotFoundError):
                                self._search_path.unlink()
                        except OSError as exc:
                            # A Windows scanner/backup process may retain the
                            # file after close.  Convert that platform error to
                            # the normal derived-index fallback boundary.
                            raise sqlite3.DatabaseError(
                                "memory search sidecar replacement unavailable"
                            ) from exc
                    if attempt == 0:
                        continue
                    raise sqlite3.DatabaseError("unsafe or invalid memory search sidecar")

                try:
                    conn.executescript(
                        "CREATE TABLE IF NOT EXISTS search_metadata ("
                        " key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search_docs USING fts5("
                        " doc_type UNINDEXED, source_id UNINDEXED, terms,"
                        " persona_id UNINDEXED, scope UNINDEXED, status UNINDEXED,"
                        " created_at UNINDEXED);"
                        "CREATE TABLE IF NOT EXISTS memory_embedding_docs ("
                        " source_id INTEGER NOT NULL, band INTEGER NOT NULL,"
                        " bucket TEXT NOT NULL, dim INTEGER NOT NULL,"
                        " persona_id TEXT NOT NULL, scope TEXT NOT NULL,"
                        " status TEXT NOT NULL, created_at REAL NOT NULL,"
                        " PRIMARY KEY(source_id,band));"
                        "CREATE INDEX IF NOT EXISTS idx_memory_embedding_bucket"
                        " ON memory_embedding_docs(dim,band,bucket,status,persona_id);"
                        "CREATE TABLE IF NOT EXISTS transcript_embedding_docs ("
                        " source_id INTEGER NOT NULL, band INTEGER NOT NULL,"
                        " bucket TEXT NOT NULL, dim INTEGER NOT NULL,"
                        " created_at REAL NOT NULL, PRIMARY KEY(source_id,band));"
                        "CREATE INDEX IF NOT EXISTS idx_transcript_embedding_bucket"
                        " ON transcript_embedding_docs(dim,band,bucket);"
                    )
                    conn.commit()
                    self._search_conn = conn
                    break
                except sqlite3.Error:
                    conn.close()
                    raise
        return self._search_conn

    def rebuild_search_index(self) -> dict[str, int]:
        """Rebuild the disposable sidecar without changing source rows."""
        with self._lock:
            revision = int(self._conn.execute(
                "SELECT revision FROM memory_search_state WHERE singleton=1"
            ).fetchone()[0])
            index = self._open_search_index()
            index.execute("DELETE FROM memory_search_docs")
            index.execute("DROP INDEX IF EXISTS idx_memory_embedding_bucket")
            index.execute("DELETE FROM memory_embedding_docs")
            index.execute("DROP INDEX IF EXISTS idx_transcript_embedding_bucket")
            index.execute("DELETE FROM transcript_embedding_docs")
            memory_count = 0
            cursor = self._conn.execute(
                "SELECT id,text,COALESCE(persona_id,''),COALESCE(scope,''),"
                "COALESCE(status,'active'),created_at,embedding,dim FROM memories"
            )
            while True:
                memory_rows = cursor.fetchmany(1000)
                if not memory_rows:
                    break
                index.executemany(
                    "INSERT INTO memory_search_docs"
                    " (doc_type,source_id,terms,persona_id,scope,status,created_at)"
                    " VALUES ('memory',?,?,?,?,?,?)",
                    [
                        (
                            int(row[0]), " ".join(_search_terms(str(row[1]))),
                            str(row[2]), str(row[3]), str(row[4]), float(row[5]),
                        )
                        for row in memory_rows
                    ],
                )
                embedding_rows = []
                band_sets = self._embedding_bands_batch(tuple(
                    (row[6], row[7]) for row in memory_rows
                ))
                for row, bands in zip(memory_rows, band_sets):
                    for band, bucket in bands:
                        embedding_rows.append((
                            int(row[0]), band, bucket, int(row[7]),
                            str(row[2]), str(row[3]), str(row[4]), float(row[5]),
                        ))
                if embedding_rows:
                    index.executemany(
                        "INSERT INTO memory_embedding_docs"
                        " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        embedding_rows,
                    )
                memory_count += len(memory_rows)
            transcript_count = 0
            cursor = self._conn.execute(
                "SELECT id,user_text,assistant_text,created_at,embedding,dim FROM transcripts"
            )
            while True:
                transcript_rows = cursor.fetchmany(1000)
                if not transcript_rows:
                    break
                index.executemany(
                    "INSERT INTO memory_search_docs"
                    " (doc_type,source_id,terms,persona_id,scope,status,created_at)"
                    " VALUES ('transcript',?,?,'','','active',?)",
                    [
                        (
                            int(row[0]),
                            " ".join(_search_terms(f"{row[1]} {row[2]}")),
                            float(row[3]),
                        )
                        for row in transcript_rows
                    ],
                )
                transcript_embedding_rows = []
                band_sets = self._embedding_bands_batch(tuple(
                    (row[4], row[5]) for row in transcript_rows
                ))
                for row, bands in zip(transcript_rows, band_sets):
                    for band, bucket in bands:
                        transcript_embedding_rows.append((
                            int(row[0]), band, bucket, int(row[5]), float(row[3]),
                        ))
                if transcript_embedding_rows:
                    index.executemany(
                        "INSERT INTO transcript_embedding_docs"
                        " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                        transcript_embedding_rows,
                    )
                transcript_count += len(transcript_rows)
            index.execute(
                "INSERT OR REPLACE INTO search_metadata(key,value) VALUES ('source_revision',?)",
                (str(revision),),
            )
            index.execute(
                "INSERT OR REPLACE INTO search_metadata(key,value) VALUES ('schema_version',?)",
                (_SEARCH_INDEX_SCHEMA_VERSION,),
            )
            # Build the lookup index after the bulk load; maintaining it for
            # every row made a disposable 100k rebuild needlessly expensive.
            index.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_embedding_bucket"
                " ON memory_embedding_docs(dim,band,bucket,status,persona_id)"
            )
            index.execute(
                "CREATE INDEX IF NOT EXISTS idx_transcript_embedding_bucket"
                " ON transcript_embedding_docs(dim,band,bucket)"
            )
            index.commit()
        return {
            "memory_rows": memory_count,
            "transcript_rows": transcript_count,
        }

    def _ensure_search_index(self) -> None:
        with self._lock:
            # Source revision and sidecar metadata are sampled under the same
            # source lock.  Otherwise a writer can advance the revision in the
            # gap and one search observes a stale candidate set.
            revision = int(self._conn.execute(
                "SELECT revision FROM memory_search_state WHERE singleton=1"
            ).fetchone()[0])
            index = self._open_search_index()
            fts_columns = {
                str(row[1]) for row in index.execute(
                    "PRAGMA table_info(memory_search_docs)"
                ).fetchall()
            }
            embedding_columns = {
                str(row[1]) for row in index.execute(
                    "PRAGMA table_info(memory_embedding_docs)"
                ).fetchall()
            }
            transcript_embedding_columns = {
                str(row[1]) for row in index.execute(
                    "PRAGMA table_info(transcript_embedding_docs)"
                ).fetchall()
            }
            if not {
                "doc_type", "source_id", "terms", "persona_id", "scope",
                "status", "created_at",
            }.issubset(fts_columns) or not {
                "source_id", "band", "bucket", "dim", "persona_id", "scope",
                "status", "created_at",
            }.issubset(embedding_columns):
                raise sqlite3.DatabaseError("invalid memory search index schema")
            if not {
                "source_id", "band", "bucket", "dim", "created_at",
            }.issubset(transcript_embedding_columns):
                raise sqlite3.DatabaseError("invalid transcript search index schema")
            values = dict(index.execute(
                "SELECT key,value FROM search_metadata"
                " WHERE key IN ('source_revision','schema_version')"
            ).fetchall())
        if (str(values.get("source_revision", "")) != str(revision)
                or str(values.get("schema_version", ""))
                != _SEARCH_INDEX_SCHEMA_VERSION):
            self.rebuild_search_index()

    def _query_search_index(
        self, sql: str, args: tuple[Any, ...],
    ) -> list[sqlite3.Row]:
        """Query derived data, repairing it at most once on SQLite failure."""
        try:
            self._ensure_search_index()
            with self._lock:
                return self._open_search_index().execute(sql, args).fetchall()
        except sqlite3.Error:
            logger.warning("Memory検索indexを一度だけ再構築します", exc_info=True)
        if not self._repair_search_index():
            raise sqlite3.DatabaseError("memory search index repair unavailable")
        with self._lock:
            return self._open_search_index().execute(sql, args).fetchall()

    def drop_search_index(self) -> None:
        """Remove only the disposable sidecar; canonical records are untouched."""
        with self._lock:
            if self._search_conn is not None:
                self._search_conn.close()
                self._search_conn = None
            if str(self._search_path) != ":memory:":
                self._prepare_search_index_path()
                with contextlib.suppress(FileNotFoundError):
                    self._search_path.unlink()

    def _repair_search_index(self) -> bool:
        """Atomically replace only the derived sidecar, never canonical data."""
        with self._lock:
            try:
                self.drop_search_index()
                self.rebuild_search_index()
            except (OSError, sqlite3.Error, RuntimeError):
                logger.warning(
                    "Memory検索indexの安全な再構築を完了できませんでした",
                    exc_info=True,
                )
                if self._search_conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        self._search_conn.close()
                    self._search_conn = None
                return False
        return True

    def _memory_candidates_by_id(
        self,
        ids: list[int],
        *,
        statuses: tuple[str, ...],
        persona_id: str,
        allow_legacy_unscoped: bool,
        speaker_key: str = "",
        embedding_dim: int | None = None,
    ) -> list[dict[str, Any]]:
        """Hydrate sidecar IDs while rechecking canonical ACL and status."""
        if not ids:
            return []
        id_marks = ",".join("?" for _ in ids)
        status_marks = ",".join("?" for _ in statuses)
        query_sql = (
            "SELECT id,kind,text,importance,embedding,dim,created_at,occurred_at,access_count,"
            " information_type,event_type,confidence,status,superseded_by,"
            " speaker_key,support_count,contradiction_count,meta,persona_id,scope"
            f" FROM memories WHERE id IN ({id_marks})"
            f" AND COALESCE(status,'active') IN ({status_marks})"
        )
        source_args: list[Any] = [*ids, *statuses]
        if speaker_key:
            query_sql += " AND (speaker_key=? OR speaker_key='')"
            source_args.append(speaker_key)
        if persona_id:
            from neuro_voice.cognition.persona_scope import sql_scope_filter

            clause, scope_args = sql_scope_filter(
                persona_id,
                allow_legacy=allow_legacy_unscoped,
                column_persona="persona_id",
                column_scope="scope",
            )
            query_sql += f" AND {clause}"
            source_args.extend(scope_args)
        with self._lock:
            rows = self._conn.execute(query_sql, tuple(source_args)).fetchall()
        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            candidate = dict(row)
            if embedding_dim is not None:
                normalised = _normalised_embedding_blob(
                    candidate.get("embedding"),
                    candidate.get("dim"),
                    expected_dim=embedding_dim,
                )
                if normalised is None:
                    continue
                candidate["embedding"] = normalised
                candidate["dim"] = int(embedding_dim)
            by_id[int(candidate["id"])] = candidate
        return [by_id[item] for item in ids if item in by_id]

    def search_episodes(
        self,
        query: str,
        *,
        limit: int = 200,
        persona_id: str = "",
        allow_legacy_unscoped: bool = False,
        include_archived: bool = False,
        speaker_key: str = "",
    ) -> list[dict[str, Any]]:
        """Return bounded, ACL-filtered candidates from the derived index."""
        terms = _search_terms(query)
        if not terms:
            return []
        effective_persona = str(persona_id or "")
        effective_allow_legacy = bool(allow_legacy_unscoped)
        if not effective_persona and self._strict_persona:
            effective_persona = self._write_persona_id
            effective_allow_legacy = False
        safe_limit = max(1, min(1000, int(limit)))
        statuses = ("active", "low_priority", "archived") if include_archived else (
            "active", "low_priority",
        )
        try:
            expression = " OR ".join(f'"{term}"' for term in terms)
            marks = ",".join("?" for _ in statuses)
            sql = (
                "SELECT source_id FROM memory_search_docs"
                " WHERE memory_search_docs MATCH ? AND doc_type='memory'"
                f" AND status IN ({marks})"
            )
            args: list[Any] = [expression, *statuses]
            if effective_persona:
                from neuro_voice.cognition.persona_scope import sql_scope_filter

                clause, scope_args = sql_scope_filter(
                    effective_persona,
                    allow_legacy=effective_allow_legacy,
                    column_persona="persona_id",
                    column_scope="scope",
                )
                sql += f" AND {clause}"
                args.extend(scope_args)
            sql += " ORDER BY bm25(memory_search_docs), CAST(created_at AS REAL) DESC LIMIT ?"
            args.append(safe_limit)
            candidate_rows = self._query_search_index(sql, tuple(args))
            ids = [int(row[0]) for row in candidate_rows]
        except sqlite3.Error:
            logger.warning("Memory検索indexを利用できないためbounded recentへ退避", exc_info=True)
            return self.episodes(
                limit=safe_limit,
                statuses=statuses,
                speaker_key=speaker_key,
                persona_id=effective_persona,
                allow_legacy_unscoped=effective_allow_legacy,
            )
        return self._memory_candidates_by_id(
            ids,
            statuses=statuses,
            persona_id=effective_persona,
            allow_legacy_unscoped=effective_allow_legacy,
            speaker_key=speaker_key,
        )

    def search_episode_embeddings(
        self,
        embedding: bytes,
        *,
        dim: int,
        limit: int = 200,
        persona_id: str = "",
        allow_legacy_unscoped: bool = False,
        include_archived: bool = False,
        _logical_repair_attempted: bool = False,
    ) -> list[dict[str, Any]]:
        """Return bounded semantic candidates from the disposable sidecar.

        Buckets only nominate IDs.  Canonical ACL/status are checked again and
        ``Mind`` remains the owner of the final cosine similarity/ranking.
        """
        bands = self._embedding_bands(embedding, dim)
        if not bands:
            return []
        effective_persona = str(persona_id or "")
        effective_allow_legacy = bool(allow_legacy_unscoped)
        if not effective_persona and self._strict_persona:
            effective_persona = self._write_persona_id
            effective_allow_legacy = False
        statuses = ("active", "low_priority", "archived") if include_archived else (
            "active", "low_priority",
        )
        safe_limit = max(1, min(1000, int(limit)))
        exact_limit = min(_ANN_MAX_ROWS_PER_TABLE, safe_limit * 4)
        status_marks = ",".join("?" for _ in statuses)
        exact_sql = (
            "SELECT source_id,band,bucket FROM memory_embedding_docs"
            f" WHERE dim=? AND band=? AND bucket=? AND status IN ({status_marks})"
        )
        scope_args: tuple[Any, ...] = ()
        if effective_persona:
            from neuro_voice.cognition.persona_scope import sql_scope_filter

            clause, scope_args = sql_scope_filter(
                effective_persona,
                allow_legacy=effective_allow_legacy,
                column_persona="persona_id",
                column_scope="scope",
            )
            exact_sql += f" AND {clause}"
        # Deliberately no global ORDER BY here.  A common approximate bucket
        # must stop at its bound using the lookup index instead of sorting all
        # matching embeddings; final relevance ordering belongs to cosine.
        exact_sql += " LIMIT ?"

        def collect_ann_ids(
            selected: list[tuple[int, str]], *, radius: int, row_limit: int,
        ) -> list[tuple[int, tuple[tuple[int, str], ...]]]:
            scores: dict[int, int] = {}
            order: dict[int, int] = {}
            provenance: dict[int, set[tuple[int, str]]] = {}
            for offset in range(0, len(selected), 6):
                chunk = selected[offset:offset + 6]
                parts: list[str] = []
                args: list[Any] = []
                query_buckets: dict[int, str] = {}
                for band, bucket in chunk:
                    probes = self._ann_probes(bucket, radius)
                    probe_marks = ",".join("?" for _ in probes)
                    part = (
                        "SELECT source_id,band,bucket FROM memory_embedding_docs"
                        f" WHERE dim=? AND band=? AND bucket IN ({probe_marks})"
                        f" AND status IN ({status_marks})"
                    )
                    if effective_persona:
                        part += f" AND {clause}"
                    part += " LIMIT ?"
                    parts.append(f"SELECT * FROM ({part})")
                    args.extend((
                        int(dim), band, *probes, *statuses,
                        *scope_args, row_limit,
                    ))
                    query_buckets[int(band)] = bucket
                rows = self._query_search_index(
                    " UNION ALL ".join(parts), tuple(args)
                )
                for row in rows:
                    source_id = int(row[0])
                    query_bucket = query_buckets[int(row[1])]
                    distance = (
                        int(str(row[2])[2:], 16)
                        ^ int(query_bucket[2:], 16)
                    ).bit_count()
                    weight = radius + 1 - min(radius, distance)
                    scores[source_id] = scores.get(source_id, 0) + weight
                    order.setdefault(source_id, len(order))
                    provenance.setdefault(source_id, set()).add(
                        (int(row[1]), str(row[2]))
                    )
            ranked = sorted(
                scores, key=lambda item: (-scores[item], order[item])
            )[:safe_limit]
            return [
                (source_id, tuple(sorted(provenance[source_id])))
                for source_id in ranked
            ]

        def hydrate(
            nominations: list[tuple[int, tuple[tuple[int, str], ...]]],
        ) -> tuple[list[dict[str, Any]], bool]:
            candidate_ids = [source_id for source_id, _rows in nominations]
            rows = self._memory_candidates_by_id(
                candidate_ids,
                statuses=statuses,
                persona_id=effective_persona,
                allow_legacy_unscoped=effective_allow_legacy,
                embedding_dim=int(dim),
            )
            by_id = {int(row["id"]): row for row in rows}
            hydrated: list[dict[str, Any]] = []
            inconsistent = set(by_id) != set(candidate_ids)
            present = [
                (source_id, selected_rows, by_id[source_id])
                for source_id, selected_rows in nominations
                if source_id in by_id
            ]
            derived_sets = self._embedding_bands_from_normalised_batch(tuple(
                (candidate.get("embedding"), candidate.get("dim"))
                for _source_id, _selected_rows, candidate in present
            ))
            for (source_id, selected_rows, candidate), bands_for_candidate in zip(
                present, derived_sets
            ):
                derived = dict(bands_for_candidate)
                if any(derived.get(band) != bucket for band, bucket in selected_rows):
                    inconsistent = True
                    continue
                hydrated.append(candidate)
            return hydrated, inconsistent

        def repair_and_retry() -> list[dict[str, Any]]:
            logger.warning(
                "Memory semantic候補indexの論理不整合を一度だけ再構築します"
            )
            if not self._repair_search_index():
                return []
            return self.search_episode_embeddings(
                embedding,
                dim=dim,
                limit=limit,
                persona_id=persona_id,
                allow_legacy_unscoped=allow_legacy_unscoped,
                include_archived=include_archived,
                _logical_repair_attempted=True,
            )

        try:
            exact_band, exact_bucket = bands[0]
            exact_rows = self._query_search_index(
                exact_sql,
                (
                    int(dim), exact_band, exact_bucket, *statuses,
                    *scope_args, exact_limit,
                ),
            )
            if exact_rows:
                exact_candidates, inconsistent = hydrate(
                    [
                        (int(row[0]), ((int(row[1]), str(row[2])),))
                        for row in exact_rows
                    ]
                )
                if inconsistent and not _logical_repair_attempted:
                    return repair_and_retry()
                if exact_candidates:
                    return exact_candidates[:safe_limit]
                # A forged exact bucket whose canonical rows are all invalid
                # must not suppress the wider ANN tier after the one repair.
            # High-similarity turns use a small first tier.  Only a validated
            # canonical cosine >= .95 may skip the wider quality tier.
            query_blob = _normalised_embedding_blob(embedding, dim)
            if query_blob is None:
                return []
            fast_ids = collect_ann_ids(
                list(bands[1:1 + _ANN_FAST_TABLES]),
                radius=_ANN_FAST_PROBE_RADIUS,
                row_limit=_ANN_FAST_ROWS_PER_TABLE,
            )
            fast_rows, fast_inconsistent = hydrate(fast_ids)
            if fast_inconsistent and not _logical_repair_attempted:
                return repair_and_retry()
            if _candidate_similarity_at_least(
                fast_rows, query_blob, _ANN_FAST_ACCEPT_COSINE
            ):
                return fast_rows
            # The wider tier preserves recall near Mind's .72 acceptance
            # boundary.  Each table remains independently row-bounded.
            ids = collect_ann_ids(
                list(bands[1:]),
                radius=_ANN_PROBE_RADIUS,
                row_limit=_ANN_MAX_ROWS_PER_TABLE,
            )
        except sqlite3.Error:
            logger.warning("Semantic候補indexを利用できないため候補なし", exc_info=True)
            return []
        rows, inconsistent = hydrate(ids)
        if inconsistent and not _logical_repair_attempted:
            try:
                return repair_and_retry()
            except sqlite3.Error:
                logger.warning(
                    "Semantic候補indexの論理修復に失敗したため候補なし",
                    exc_info=True,
                )
                return []
        return rows

    def search_transcript_embeddings(
        self,
        embedding: bytes,
        *,
        dim: int,
        limit: int = 40,
        _logical_repair_attempted: bool = False,
    ) -> list[dict[str, Any]]:
        """Return bounded transcript ANN candidates from the derived sidecar."""
        bands = self._embedding_bands(embedding, dim)
        if not bands:
            return []
        safe_limit = max(1, min(400, int(limit)))
        exact_limit = min(_ANN_MAX_ROWS_PER_TABLE, safe_limit * 4)

        def collect_ann_ids(
            selected: list[tuple[int, str]], *, radius: int, row_limit: int,
        ) -> list[tuple[int, tuple[tuple[int, str], ...]]]:
            scores: dict[int, int] = {}
            order: dict[int, int] = {}
            provenance: dict[int, set[tuple[int, str]]] = {}
            for offset in range(0, len(selected), 6):
                chunk = selected[offset:offset + 6]
                parts: list[str] = []
                args: list[Any] = []
                query_buckets: dict[int, str] = {}
                for band, bucket in chunk:
                    probes = self._ann_probes(bucket, radius)
                    marks = ",".join("?" for _ in probes)
                    part = (
                        "SELECT source_id,band,bucket"
                        " FROM transcript_embedding_docs"
                        f" WHERE dim=? AND band=? AND bucket IN ({marks}) LIMIT ?"
                    )
                    parts.append(f"SELECT * FROM ({part})")
                    args.extend((int(dim), band, *probes, row_limit))
                    query_buckets[int(band)] = bucket
                rows = self._query_search_index(
                    " UNION ALL ".join(parts), tuple(args)
                )
                for row in rows:
                    source_id = int(row[0])
                    query_bucket = query_buckets[int(row[1])]
                    distance = (
                        int(str(row[2])[2:], 16)
                        ^ int(query_bucket[2:], 16)
                    ).bit_count()
                    scores[source_id] = scores.get(source_id, 0) + (
                        radius + 1 - min(radius, distance)
                    )
                    order.setdefault(source_id, len(order))
                    provenance.setdefault(source_id, set()).add(
                        (int(row[1]), str(row[2]))
                    )
            ranked = sorted(
                scores, key=lambda item: (-scores[item], order[item])
            )[:safe_limit]
            return [
                (source_id, tuple(sorted(provenance[source_id])))
                for source_id in ranked
            ]

        def hydrate(
            nominations: list[tuple[int, tuple[tuple[int, str], ...]]],
        ) -> tuple[list[dict[str, Any]], bool]:
            candidate_ids = [source_id for source_id, _rows in nominations]
            if not candidate_ids:
                return [], False
            marks = ",".join("?" for _ in candidate_ids)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id,speaker,user_text,assistant_text,emotion,topic,"
                    "embedding,dim,created_at FROM transcripts"
                    f" WHERE id IN ({marks}) AND embedding IS NOT NULL",
                    tuple(candidate_ids),
                ).fetchall()
            by_id: dict[int, dict[str, Any]] = {}
            for row in rows:
                candidate = dict(row)
                normalised = _normalised_embedding_blob(
                    candidate.get("embedding"),
                    candidate.get("dim"),
                    expected_dim=int(dim),
                )
                if normalised is None:
                    continue
                candidate["embedding"] = normalised
                candidate["dim"] = int(dim)
                by_id[int(candidate["id"])] = candidate
            hydrated: list[dict[str, Any]] = []
            inconsistent = set(by_id) != set(candidate_ids)
            present = [
                (source_id, selected_rows, by_id[source_id])
                for source_id, selected_rows in nominations
                if source_id in by_id
            ]
            derived_sets = self._embedding_bands_from_normalised_batch(tuple(
                (candidate.get("embedding"), candidate.get("dim"))
                for _source_id, _selected_rows, candidate in present
            ))
            for (source_id, selected_rows, candidate), bands_for_candidate in zip(
                present, derived_sets
            ):
                derived = dict(bands_for_candidate)
                if any(derived.get(band) != bucket for band, bucket in selected_rows):
                    inconsistent = True
                    continue
                hydrated.append(candidate)
            return hydrated, inconsistent

        def repair_and_retry() -> list[dict[str, Any]]:
            logger.warning(
                "Transcript semantic候補indexの論理不整合を一度だけ再構築します"
            )
            if not self._repair_search_index():
                return []
            return self.search_transcript_embeddings(
                embedding,
                dim=dim,
                limit=limit,
                _logical_repair_attempted=True,
            )

        try:
            exact = self._query_search_index(
                "SELECT source_id,bucket FROM transcript_embedding_docs"
                " WHERE dim=? AND band=? AND bucket=? LIMIT ?",
                (int(dim), bands[0][0], bands[0][1], exact_limit),
            )
            if exact:
                exact_candidates, inconsistent = hydrate(
                    [
                        (int(row[0]), ((int(bands[0][0]), str(row[1])),))
                        for row in exact
                    ]
                )
                if inconsistent and not _logical_repair_attempted:
                    return repair_and_retry()
                if exact_candidates:
                    return exact_candidates[:safe_limit]
                # Invalid forged exact hits fall through to bounded ANN after
                # the one derived-index repair instead of returning empty.
            query_blob = _normalised_embedding_blob(embedding, dim)
            if query_blob is None:
                return []
            fast_ids = collect_ann_ids(
                list(bands[1:1 + _ANN_FAST_TABLES]),
                radius=_ANN_FAST_PROBE_RADIUS,
                row_limit=_ANN_FAST_ROWS_PER_TABLE,
            )
            fast_rows, fast_inconsistent = hydrate(fast_ids)
            if fast_inconsistent and not _logical_repair_attempted:
                return repair_and_retry()
            if _candidate_similarity_at_least(
                fast_rows, query_blob, _ANN_FAST_ACCEPT_COSINE
            ):
                return fast_rows
            ids = collect_ann_ids(
                list(bands[1:]),
                radius=_ANN_PROBE_RADIUS,
                row_limit=_ANN_MAX_ROWS_PER_TABLE,
            )
        except sqlite3.Error:
            logger.warning("Transcript semantic候補indexを利用できません", exc_info=True)
            return []
        rows, inconsistent = hydrate(ids)
        if inconsistent and not _logical_repair_attempted:
            try:
                return repair_and_retry()
            except sqlite3.Error:
                logger.warning(
                    "Transcript semantic候補indexの論理修復に失敗しました",
                    exc_info=True,
                )
                return []
        return rows

    def search_transcripts(self, query: str, *, limit: int = 40) -> list[dict[str, Any]]:
        """Return bounded transcript candidates from the same derived index."""
        terms = _search_terms(query)
        if not terms:
            return []
        safe_limit = max(1, min(400, int(limit)))
        try:
            expression = " OR ".join(f'"{term}"' for term in terms)
            candidates = self._query_search_index(
                "SELECT source_id FROM memory_search_docs"
                " WHERE memory_search_docs MATCH ? AND doc_type='transcript'"
                " ORDER BY bm25(memory_search_docs), CAST(created_at AS REAL) DESC LIMIT ?",
                (expression, safe_limit),
            )
            ids = [int(row[0]) for row in candidates]
        except sqlite3.Error:
            logger.warning("Transcript検索indexを利用できないためrecentへ退避", exc_info=True)
            return self.recent_transcripts(limit=min(20, safe_limit))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,speaker,user_text,assistant_text,emotion,topic,embedding,dim,created_at"
                f" FROM transcripts WHERE id IN ({marks})",
                tuple(ids),
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    def prune_transcripts(self, retention_days: float, max_items: int = 8000) -> int:
        """Compatibility maintenance hook; retained source rows are immutable."""
        del retention_days, max_items
        return 0

    def delete_memory_explicit(
        self, memory_id: int, *, reason: ExplicitDeletionReason | str,
    ) -> bool:
        try:
            accepted = ExplicitDeletionReason(str(reason))
        except ValueError as exc:
            raise ValueError("explicit deletion reason required") from exc
        del accepted
        removed = self._source_write(
            lambda conn: conn.execute(
                "DELETE FROM memories WHERE id=?", (int(memory_id),),
            ).rowcount or 0
        )
        self._explicit_source_deletions += int(removed)
        return bool(removed)

    def delete_transcript_explicit(
        self, transcript_id: int, *, reason: ExplicitDeletionReason | str,
    ) -> bool:
        try:
            accepted = ExplicitDeletionReason(str(reason))
        except ValueError as exc:
            raise ValueError("explicit deletion reason required") from exc
        del accepted
        removed = self._source_write(
            lambda conn: conn.execute(
                "DELETE FROM transcripts WHERE id=?", (int(transcript_id),),
            ).rowcount or 0
        )
        self._explicit_source_deletions += int(removed)
        return bool(removed)

    #: **強く減衰させない記憶。** 明示された好み・約束・訂正・自分の失敗。
    #: ここを削ると、覚えている意味がいちばん大きいものから消える。
    _PROTECTED_EVENTS = ("preference", "correction", "promise")

    def prune(self, max_items: int = 2000) -> int:
        """Compatibility maintenance hook; ranking may decay, sources do not."""
        del max_items
        return 0
