"""所有者不明の記憶を1つのペルソナへ帰属させる移行。

**戻せない失敗が3つある。** ここはその3つを見る。

* 持ち主を**推測**して付ける（DB名だけで決める・別人格へ帰属させる）
* 旧記憶を**共有スコープへ上げる**（私的な内容が全ペルソナへ出る）
* 移行のついでに**本文や embedding が変わる**
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neuro_voice.mind.legacy_scope_migration import (
    DEFAULT_SCOPE, JobStatus, MigrationLedger, audit_all, audit_database,
    content_digest, migrate_database, rollback,
)
from neuro_voice.mind.store import MemoryStore

KNOWN = {"neuro": "ポッポ", "luna": "Luna"}


def _legacy_db(path: Path, rows: int = 3) -> None:
    """`persona_id` 列の無い古い DB。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " kind TEXT NOT NULL, text TEXT NOT NULL, importance INTEGER DEFAULT 3,"
        " embedding BLOB, dim INTEGER, source TEXT DEFAULT '',"
        " created_at REAL NOT NULL, status TEXT DEFAULT 'active');")
    for index in range(rows):
        conn.execute(
            "INSERT INTO memories (kind, text, embedding, dim, created_at)"
            " VALUES ('episode', ?, ?, 4, 1.0)",
            (f"古い記憶{index}", bytes([index, index + 1, index + 2, index + 3])))
    conn.commit()
    conn.close()


def _modern_db(path: Path, entries) -> None:
    store = MemoryStore(path)
    for text, owner in entries:
        store.add_episode(text, persona_id=owner, scope=DEFAULT_SCOPE if owner else "")
    if entries:
        store._conn.execute(
            "UPDATE memories SET persona_id = '' WHERE persona_id IS NULL")
        store._conn.commit()
    store.close()


@pytest.fixture()
def data(tmp_path: Path) -> Path:
    (tmp_path / "backups").mkdir()
    return tmp_path


@pytest.fixture()
def ledger(data: Path) -> MigrationLedger:
    return MigrationLedger(data / "legacy_scope_migrations.json")


# ---------------------------------------------------------------------------
# 監査
# ---------------------------------------------------------------------------


def test_an_unknown_persona_is_not_resolved(data: Path):
    """**DB名だけで持ち主を決めない。** 削除済みかもしれない。"""
    _legacy_db(data / "mind_ghost.db")
    audit = audit_database(data / "mind_ghost.db", KNOWN)
    assert not audit.resolved
    assert not audit.migratable
    assert "設定に無い" in audit.reason


def test_a_legacy_database_counts_everything_as_unowned(data: Path):
    _legacy_db(data / "mind_luna.db", rows=12)
    audit = audit_database(data / "mind_luna.db", KNOWN)
    assert audit.resolved
    assert not audit.has_persona_column
    assert audit.memories == 12
    assert audit.unscoped == 12


def test_audit_all_lists_every_database(data: Path):
    _legacy_db(data / "mind_luna.db")
    _legacy_db(data / "mind_ghost.db")
    names = {item.database_id for item in audit_all(data, KNOWN)}
    assert names == {"mind_luna.db", "mind_ghost.db"}


# ---------------------------------------------------------------------------
# 移行
# ---------------------------------------------------------------------------


def test_a_dry_run_changes_nothing(data: Path, ledger):
    _legacy_db(data / "mind_luna.db")
    job = migrate_database(audit_database(data / "mind_luna.db", KNOWN), ledger)
    assert str(job.status) == str(JobStatus.PLANNED)
    assert not ledger.jobs
    assert not audit_database(data / "mind_luna.db", KNOWN).has_persona_column


def test_a_legacy_database_gains_the_column_and_the_owner(data: Path, ledger):
    path = data / "mind_luna.db"
    _legacy_db(path, rows=12)
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False,
                           backup_dir=data / "backups")
    assert str(job.status) == str(JobStatus.COMPLETED)
    after = audit_database(path, KNOWN)
    assert after.has_persona_column
    assert after.unscoped == 0
    assert after.scoped == 12
    assert after.memories == 12


def test_the_scope_is_private_not_shared(data: Path, ledger):
    """**共有へ上げない。** 分からないものを全員に見せる方向へ倒さない。

    ここで `DEFAULT_SCOPE` と比べてはいけない——定数を共有スコープへ
    書き換えるとテストも一緒に動いてしまい、**何も見ていないことになる**
    （故障注入で実際に素通りした）。リテラルで比べる。
    """
    from neuro_voice.cognition.persona_scope import SHARED_SCOPES

    path = data / "mind_luna.db"
    _legacy_db(path)
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    conn = sqlite3.connect(str(path))
    scopes = {row[0] for row in conn.execute("SELECT scope FROM memories")}
    conn.close()
    assert scopes == {"persona_private"}
    assert not (scopes & SHARED_SCOPES)
    assert DEFAULT_SCOPE not in SHARED_SCOPES


def test_the_content_never_changes(data: Path, ledger):
    """本文・embedding を触っていないことを、前後の指紋で確かめる。"""
    path = data / "mind_luna.db"
    _legacy_db(path, rows=12)
    conn = sqlite3.connect(str(path))
    before = content_digest(conn)
    conn.close()
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    conn = sqlite3.connect(str(path))
    assert content_digest(conn) == before
    conn.close()


def test_an_existing_owner_is_not_overwritten(data: Path, ledger):
    """既に持ち主のある行は触らない。

    持ち主だけを見ても分からない——同じペルソナへ帰属させる移行では、
    上書きしても値が変わらないので**素通りする**（故障注入で判明）。
    区別できる印として `scope` を見る。
    """
    path = data / "mind_neuro.db"
    _modern_db(path, [("持ち主あり", "neuro"), ("持ち主なし", "")])
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE memories SET scope = 'world_shared' WHERE id = 1")
    conn.commit()
    conn.close()

    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    conn = sqlite3.connect(str(path))
    rows = conn.execute(
        "SELECT persona_id, scope FROM memories ORDER BY id").fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["neuro", "neuro"]
    # **触っていない行のスコープは変わらない。**
    assert [row[1] for row in rows] == ["world_shared", "persona_private"]


def test_a_foreign_owner_stops_the_migration(data: Path, ledger):
    """**上書きは移行ではなく奪取。** 止める。"""
    path = data / "mind_neuro.db"
    _modern_db(path, [("他人のもの", "luna"), ("持ち主なし", "")])
    audit = audit_database(path, KNOWN)
    assert audit.foreign == 1
    job = migrate_database(audit, ledger, dry_run=False)
    assert str(job.status) == str(JobStatus.CONFLICT)
    conn = sqlite3.connect(str(path))
    owners = sorted(row[0] for row in conn.execute("SELECT persona_id FROM memories"))
    conn.close()
    assert owners == ["", "luna"]


def test_an_unresolved_database_is_blocked(data: Path, ledger):
    _legacy_db(data / "mind_ghost.db")
    job = migrate_database(audit_database(data / "mind_ghost.db", KNOWN),
                           ledger, dry_run=False)
    assert str(job.status) == str(JobStatus.BLOCKED)


def test_a_backup_is_written(data: Path, ledger):
    path = data / "mind_luna.db"
    _legacy_db(path)
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False,
                           backup_dir=data / "backups")
    assert job.backup_reference
    assert (data / "backups" / job.backup_reference).exists()


# ---------------------------------------------------------------------------
# 冪等性・再開・巻き戻し
# ---------------------------------------------------------------------------


def test_running_twice_does_not_duplicate(data: Path, ledger):
    path = data / "mind_luna.db"
    _legacy_db(path, rows=12)
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    second = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    assert str(second.status) == str(JobStatus.SKIPPED)
    assert audit_database(path, KNOWN).memories == 12


def test_a_partial_run_resumes(data: Path, ledger):
    """途中で止まった後に足された行も、次の実行で拾う。"""
    path = data / "mind_neuro.db"
    _modern_db(path, [("一つ目", "")])
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    store = MemoryStore(path)
    store.add_episode("あとから増えた")  # 持ち主なしで追加
    store.close()
    assert audit_database(path, KNOWN).unscoped == 1
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    assert str(job.status) == str(JobStatus.COMPLETED)
    assert audit_database(path, KNOWN).unscoped == 0


def test_rollback_only_touches_the_rows_this_job_set(data: Path, ledger):
    """**移行後に別処理が変えた行は戻さない。**

    「戻す」つもりで新しい正しい値を消すのがいちばんまずい。
    """
    path = data / "mind_neuro.db"
    _modern_db(path, [("一つ目", ""), ("二つ目", "")])
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE memories SET persona_id = 'luna' WHERE id = 1")
    conn.commit()
    conn.close()

    rolled = rollback(job.migration_id, data, ledger)
    assert str(rolled.status) == str(JobStatus.ROLLED_BACK)
    conn = sqlite3.connect(str(path))
    owners = [row[0] for row in conn.execute(
        "SELECT persona_id FROM memories ORDER BY id")]
    conn.close()
    assert owners == ["luna", ""]


def test_rollback_needs_a_completed_job(data: Path, ledger):
    assert rollback("nope", data, ledger) is None


def test_the_ledger_survives_a_restart(data: Path, ledger):
    path = data / "mind_luna.db"
    _legacy_db(path)
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    reopened = MigrationLedger(data / "legacy_scope_migrations.json")
    stored = reopened.get(job.migration_id)
    assert stored is not None
    assert stored.target_persona_id == "luna"
    assert stored.affected_memory_ids
    assert stored.previous_persona_values


def test_a_neuro_database_is_never_assigned_to_luna(data: Path, ledger):
    path = data / "mind_neuro.db"
    _modern_db(path, [("ポッポの記憶", "")])
    job = migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    assert job.target_persona_id == "neuro"
    conn = sqlite3.connect(str(path))
    assert conn.execute(
        "SELECT count(*) FROM memories WHERE persona_id = 'luna'").fetchone()[0] == 0
    conn.close()


def test_reflections_are_migrated_too(data: Path, ledger):
    path = data / "mind_neuro.db"
    store = MemoryStore(path)
    store.save_reflection({"reflection_id": "r1", "statement": "短く答える"})
    store._conn.execute("UPDATE reflections SET persona_id = ''")
    store._conn.commit()
    store.close()
    assert audit_database(path, KNOWN).reflections_unscoped == 1
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    assert audit_database(path, KNOWN).reflections_unscoped == 0


def test_the_digest_notices_a_changed_embedding(data: Path):
    """指紋が本文と embedding を**実際に覆っている**こと。

    前後で同じ関数を使うだけだと、指紋を弱めても一致してしまう
    ——検証しているつもりで何も見ていない状態になる（故障注入で判明）。
    """
    path = data / "mind_luna.db"
    _legacy_db(path, rows=3)
    conn = sqlite3.connect(str(path))
    before = content_digest(conn)
    conn.execute("UPDATE memories SET embedding = x'ffffffff' WHERE id = 1")
    conn.commit()
    assert content_digest(conn) != before
    conn.execute("UPDATE memories SET embedding = x'00010203' WHERE id = 1")
    conn.execute("UPDATE memories SET text = '書き換えた' WHERE id = 2")
    conn.commit()
    assert content_digest(conn) != before
    conn.close()


def test_the_digest_ignores_the_owner_column(data: Path, ledger):
    """**持ち主を書き換えても指紋は変わらない。**

    変わってしまうと、正しい移行が毎回「本文が変わった」と判定されて
    ロールバックする。
    """
    path = data / "mind_neuro.db"
    _modern_db(path, [("記憶", "")])
    conn = sqlite3.connect(str(path))
    before = content_digest(conn)
    conn.close()
    migrate_database(audit_database(path, KNOWN), ledger, dry_run=False)
    conn = sqlite3.connect(str(path))
    assert content_digest(conn) == before
    conn.close()
