"""所有者の書かれていない古い記憶を、1つのペルソナへ帰属させる。

Phase 7D ⑤ で**これから書く記憶**には持ち主が付くようになった。だが
既に溜まっている行は空のまま——`persona_scope_enabled: true` にした
瞬間に、その全部が隠れる。実機では `mind_neuro.db` に 202件、
`mind_luna.db` に 12件あった。

ここでやらないこと（どれも戻せない失敗に直結する）:

* **DBファイル名だけで持ち主を決めない。** 設定に無いキーの DB は
  「誰のか分からない」であって「名前から明らか」ではない。止める。
* **旧記憶を共有スコープへ上げない。** 分からないものを全員に見せる
  方向へ倒すと、私的な内容が全ペルソナへ出る。戻せない。
* **持ち主が既に入っている行を触らない。** 別の持ち主なら CONFLICT で
  止める。上書きは「移行」ではなく「奪取」。
* **本文・要約・embedding を触らない。** 変えていないことを、
  前後のハッシュで確かめる。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 帰属させるスコープ。**共有へ上げない**（第12条）。
DEFAULT_SCOPE = "persona_private"
#: 移行履歴の置き場所。
LEDGER_NAME = "legacy_scope_migrations.json"
BACKUP_DIR = "backups/legacy_scope"


class JobStatus(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    COMPLETED = "completed"
    CONFLICT = "conflict"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------------------
# 監査
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatabaseAudit:
    """1つの DB について、移行前に分かること。"""

    path: Path
    database_id: str
    persona_id: str = ""
    #: 持ち主を**設定と照合できた**か。名前から推測しただけなら偽。
    resolved: bool = False
    reason: str = ""
    memories: int = 0
    has_persona_column: bool = False
    unscoped: int = 0
    scoped: int = 0
    foreign: int = 0
    reflections: int = 0
    reflections_unscoped: int = 0
    has_reflection_column: bool = False

    @property
    def migratable(self) -> bool:
        return bool(self.resolved and (self.unscoped or self.reflections_unscoped))

    def snapshot(self) -> dict[str, Any]:
        return {
            "database_id": self.database_id, "persona_id": self.persona_id,
            "resolved": self.resolved, "reason": self.reason,
            "memories": self.memories,
            "has_persona_column": self.has_persona_column,
            "unscoped": self.unscoped, "scoped": self.scoped,
            "foreign": self.foreign, "reflections": self.reflections,
            "reflections_unscoped": self.reflections_unscoped,
            "has_reflection_column": self.has_reflection_column,
        }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    with contextlib.suppress(sqlite3.Error):
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    return set()


def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    with contextlib.suppress(sqlite3.Error):
        return int(conn.execute(sql, args).fetchone()[0])
    return 0


def audit_database(path: Path, known_personas: dict[str, str]) -> DatabaseAudit:
    """1つの DB を調べる。**持ち主は設定と照合してから名乗らせる。**"""
    database_id = path.name
    key = database_id[len("mind_"):-len(".db")] if (
        database_id.startswith("mind_") and database_id.endswith(".db")) else ""
    if not key:
        return DatabaseAudit(path, database_id, reason="DB名が mind_<key>.db の形ではない")
    owners = [name for name, _ in known_personas.items() if name == key]
    if not owners:
        return DatabaseAudit(
            path, database_id, persona_id=key,
            reason="設定に無いペルソナ。削除済みか改名された可能性があり、持ち主を決められない")

    conn = sqlite3.connect(str(path))
    try:
        memory_columns = _columns(conn, "memories")
        reflection_columns = _columns(conn, "reflections")
        has_persona = "persona_id" in memory_columns
        has_reflection = "persona_id" in reflection_columns
        total = _count(conn, "SELECT count(*) FROM memories")
        reflections = _count(conn, "SELECT count(*) FROM reflections")
        if has_persona:
            unscoped = _count(
                conn, "SELECT count(*) FROM memories WHERE coalesce(persona_id,'') = ''")
            scoped = _count(
                conn, "SELECT count(*) FROM memories WHERE coalesce(persona_id,'') = ?", (key,))
            foreign = _count(
                conn,
                "SELECT count(*) FROM memories"
                " WHERE coalesce(persona_id,'') <> '' AND persona_id <> ?", (key,))
        else:
            # 列が無いなら**全部が持ち主不明**。列を足してから帰属させる。
            unscoped, scoped, foreign = total, 0, 0
        reflections_unscoped = (
            _count(conn, "SELECT count(*) FROM reflections"
                         " WHERE coalesce(persona_id,'') = ''")
            if has_reflection else reflections)
        return DatabaseAudit(
            path, database_id, persona_id=key, resolved=True,
            reason="設定のプリセットと一致",
            memories=total, has_persona_column=has_persona,
            unscoped=unscoped, scoped=scoped, foreign=foreign,
            reflections=reflections, reflections_unscoped=reflections_unscoped,
            has_reflection_column=has_reflection)
    finally:
        conn.close()


def audit_all(data_dir: Path, known_personas: dict[str, str]) -> list[DatabaseAudit]:
    return [audit_database(path, known_personas)
            for path in sorted(Path(data_dir).glob("mind_*.db"))]


# ---------------------------------------------------------------------------
# 移行の記録
# ---------------------------------------------------------------------------


@dataclass
class MigrationJob:
    migration_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    database_id: str = ""
    target_persona_id: str = ""
    affected_memory_ids: list[int] = field(default_factory=list)
    affected_reflection_ids: list[str] = field(default_factory=list)
    #: 移行前の値。**空だった行だけ**を戻すために持つ。
    previous_persona_values: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    status: JobStatus | str = JobStatus.PLANNED
    backup_reference: str = ""
    detail: str = ""
    content_digest: str = ""

    def snapshot(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["status"] = str(self.status)
        return data


class MigrationLedger:
    """移行履歴。**再実行と巻き戻しの正本。**"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._jobs: list[MigrationJob] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with contextlib.suppress(Exception):
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw.get("jobs", []):
                job = MigrationJob()
                for key, value in item.items():
                    if hasattr(job, key):
                        setattr(job, key, value)
                self._jobs.append(job)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1,
                   "jobs": [job.snapshot() for job in self._jobs]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path)

    def add(self, job: MigrationJob) -> MigrationJob:
        self._jobs.append(job)
        self.save()
        return job

    def get(self, migration_id: str) -> MigrationJob | None:
        return next((job for job in self._jobs
                     if job.migration_id == str(migration_id)), None)

    def completed_for(self, database_id: str) -> list[MigrationJob]:
        return [job for job in self._jobs
                if job.database_id == database_id
                and str(job.status) == str(JobStatus.COMPLETED)]

    @property
    def jobs(self) -> list[MigrationJob]:
        return list(self._jobs)


# ---------------------------------------------------------------------------
# 移行
# ---------------------------------------------------------------------------


def content_digest(conn: sqlite3.Connection) -> str:
    """本文・要約・embedding の指紋。**移行の前後で変わらないこと。**"""
    digest = hashlib.sha256()
    for row in conn.execute(
            "SELECT id, text, coalesce(embedding, x''), coalesce(dim, -1)"
            " FROM memories ORDER BY id"):
        digest.update(str(row[0]).encode("utf-8"))
        digest.update(str(row[1]).encode("utf-8"))
        digest.update(bytes(row[2] or b""))
        digest.update(str(row[3]).encode("utf-8"))
    return digest.hexdigest()[:32]


def _ensure_columns(path: Path) -> None:
    """旧スキーマへ列を足す。**既存の移行基盤をそのまま使う。**

    `MemoryStore` を開くだけで `_migrate_episode_columns()` と
    `_migrate_reflection_columns()` が走る。ここで別の DDL を書くと、
    正本が2つになって必ず食い違う。
    """
    from neuro_voice.mind.store import MemoryStore

    store = MemoryStore(path)
    with contextlib.suppress(Exception):
        store.close()


def migrate_database(audit: DatabaseAudit, ledger: MigrationLedger, *,
                     dry_run: bool = True, backup_dir: Path | None = None,
                     scope: str = DEFAULT_SCOPE) -> MigrationJob:
    """1つの DB を1つのペルソナへ帰属させる。

    **空の行だけ**が対象。既に持ち主がいる行は触らない。別の持ち主が
    入っていたら CONFLICT で止める——上書きは移行ではない。
    """
    job = MigrationJob(database_id=audit.database_id,
                       target_persona_id=audit.persona_id,
                       started_at=time.time())
    if not audit.resolved:
        job.status = JobStatus.BLOCKED
        job.detail = audit.reason or "持ち主を決められない"
        return job if dry_run else ledger.add(job)
    if audit.foreign:
        job.status = JobStatus.CONFLICT
        job.detail = f"別のペルソナが持ち主の行が {audit.foreign} 件ある"
        return job if dry_run else ledger.add(job)
    if not audit.unscoped and not audit.reflections_unscoped:
        job.status = JobStatus.SKIPPED
        job.detail = "持ち主不明の行が無い"
        job.completed_at = time.time()
        return job if dry_run else ledger.add(job)

    if dry_run:
        job.status = JobStatus.PLANNED
        job.detail = (f"記憶 {audit.unscoped} 件 / 仮説 "
                      f"{audit.reflections_unscoped} 件を {audit.persona_id} へ")
        return job

    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        reference = backup_dir / f"{audit.database_id}.{job.migration_id}.bak"
        shutil.copy2(audit.path, reference)
        job.backup_reference = str(reference.name)

    _ensure_columns(audit.path)
    conn = sqlite3.connect(str(audit.path))
    try:
        before = content_digest(conn)
        before_total = _count(conn, "SELECT count(*) FROM memories")
        job.content_digest = before
        # **再確認する。** 監査から実行までの間に増えている可能性がある。
        live_foreign = _count(
            conn, "SELECT count(*) FROM memories"
                  " WHERE coalesce(persona_id,'') <> '' AND persona_id <> ?",
            (audit.persona_id,))
        if live_foreign:
            job.status = JobStatus.CONFLICT
            job.detail = f"実行直前に別ペルソナの行を {live_foreign} 件検出"
            return ledger.add(job)

        rows = conn.execute(
            "SELECT id FROM memories WHERE coalesce(persona_id,'') = ''").fetchall()
        job.affected_memory_ids = [int(row[0]) for row in rows]
        reflection_rows = conn.execute(
            "SELECT reflection_id FROM reflections"
            " WHERE coalesce(persona_id,'') = ''").fetchall()
        job.affected_reflection_ids = [str(row[0]) for row in reflection_rows]
        job.previous_persona_values = {
            str(memory_id): "" for memory_id in job.affected_memory_ids}

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE memories SET persona_id = ?,"
            " scope = CASE WHEN coalesce(scope,'') = '' THEN ? ELSE scope END"
            " WHERE coalesce(persona_id,'') = ''",
            (audit.persona_id, scope))
        conn.execute(
            "UPDATE reflections SET persona_id = ?,"
            " scope = CASE WHEN coalesce(scope,'') = '' THEN ? ELSE scope END"
            " WHERE coalesce(persona_id,'') = ''",
            (audit.persona_id, scope))

        after_total = _count(conn, "SELECT count(*) FROM memories")
        left = _count(conn, "SELECT count(*) FROM memories"
                            " WHERE coalesce(persona_id,'') = ''")
        after = content_digest(conn)
        if after_total != before_total or left or after != before:
            conn.execute("ROLLBACK")
            job.status = JobStatus.FAILED
            job.detail = (f"検証に失敗（件数 {before_total}→{after_total} / "
                          f"残り {left} / 指紋 {'一致' if after == before else '不一致'}）")
            return ledger.add(job)
        conn.execute("COMMIT")
        job.status = JobStatus.COMPLETED
        job.completed_at = time.time()
        job.detail = (f"記憶 {len(job.affected_memory_ids)} 件 / 仮説 "
                      f"{len(job.affected_reflection_ids)} 件を {audit.persona_id} へ")
        logger.info("所有者不明の記憶を移行: %s → %s (%s)",
                    audit.database_id, audit.persona_id, job.detail)
        return ledger.add(job)
    except Exception as error:  # pragma: no cover - 想定外
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        job.status = JobStatus.FAILED
        job.detail = f"{type(error).__name__}: {error}"
        logger.exception("移行に失敗: %s", audit.database_id)
        return ledger.add(job)
    finally:
        conn.close()


def rollback(job_id: str, data_dir: Path, ledger: MigrationLedger) -> MigrationJob | None:
    """このジョブが**空から設定した行だけ**を空へ戻す。

    移行後に別の処理が持ち主を変えた行は触らない。「戻す」つもりで
    新しい正しい値を消すのがいちばんまずい。
    """
    job = ledger.get(job_id)
    if job is None or str(job.status) != str(JobStatus.COMPLETED):
        return None
    path = Path(data_dir) / job.database_id
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("BEGIN")
        for memory_id, previous in job.previous_persona_values.items():
            conn.execute(
                "UPDATE memories SET persona_id = ? WHERE id = ? AND persona_id = ?",
                (previous, int(memory_id), job.target_persona_id))
        for reflection_id in job.affected_reflection_ids:
            conn.execute(
                "UPDATE reflections SET persona_id = ''"
                " WHERE reflection_id = ? AND persona_id = ?",
                (reflection_id, job.target_persona_id))
        conn.execute("COMMIT")
    finally:
        conn.close()
    job.status = JobStatus.ROLLED_BACK
    job.detail = f"{len(job.previous_persona_values)} 件を空へ戻した"
    ledger.save()
    return job


__all__ = [
    "BACKUP_DIR", "DEFAULT_SCOPE", "LEDGER_NAME", "DatabaseAudit", "JobStatus",
    "MigrationJob", "MigrationLedger", "audit_all", "audit_database",
    "content_digest", "migrate_database", "rollback",
]
