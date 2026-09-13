"""Stage M: retained source records must not be deleted by maintenance."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from neuro_voice.mind.episodes import EpisodicMemoryService
from neuro_voice.mind.backup import BackupManager
from neuro_voice.mind.mind import Mind
from neuro_voice.mind.store import (
    ExplicitDeletionReason,
    MemoryStore,
    MemoryStoreMigrationRequired,
    MemoryWriteUnavailable,
    RetentionPolicy,
)
from neuro_voice.privacy import PrivacyManager


class _Cfg:
    def __init__(self, values: dict[str, object] | None = None):
        self.values = values or {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def _fingerprint(row: sqlite3.Row | tuple) -> str:
    values = tuple(row)
    payload = json.dumps(
        [value.hex() if isinstance(value, bytes) else value for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_fingerprints(store: MemoryStore) -> tuple[list[str], list[str]]:
    memories = store._conn.execute(
        "SELECT * FROM memories ORDER BY id"
    ).fetchall()
    transcripts = store._conn.execute(
        "SELECT * FROM transcripts ORDER BY id"
    ).fetchall()
    return ([ _fingerprint(row) for row in memories ],
            [ _fingerprint(row) for row in transcripts ])


def _near_vector_pair(dim: int = 384, cosine: float = .99985) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(907)
    target = rng.standard_normal(dim).astype(np.float32)
    target /= np.linalg.norm(target)
    noise = rng.standard_normal(dim).astype(np.float32)
    noise -= target * float(noise @ target)
    noise /= np.linalg.norm(noise)
    query = target * cosine + noise * np.sqrt(1.0 - cosine * cosine)
    query /= np.linalg.norm(query)
    return target.astype(np.float32), query.astype(np.float32)


def _seeded_vector_pair(seed: int, cosine: float) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    target = rng.standard_normal(384).astype(np.float32)
    target /= np.linalg.norm(target)
    noise = rng.standard_normal(384).astype(np.float32)
    noise -= target * float(noise @ target)
    noise /= np.linalg.norm(noise)
    query = target * cosine + noise * np.sqrt(1.0 - cosine * cosine)
    query /= np.linalg.norm(query)
    return target.astype(np.float32), query.astype(np.float32)


def _mind_for_semantic(store: MemoryStore, query_vector: np.ndarray) -> Mind:
    mind = object.__new__(Mind)
    mind._store = store
    mind._episodes = EpisodicMemoryService(
        store, _Cfg({"memory.write_enabled": False})
    )
    mind._embedder = SimpleNamespace(
        encode=lambda _texts, is_query=False: np.stack([query_vector]),
    )
    mind._top_k = 5
    mind._min_score = .2
    mind._transcript_enabled = True
    mind._transcript_top_k = 5
    return mind


def test_embedder_warmup_does_not_scan_all_persistent_embeddings():
    scans: list[str] = []
    mind = object.__new__(Mind)
    mind._embedder = SimpleNamespace(encode=lambda texts: np.ones((len(texts), 4)))
    mind._store = SimpleNamespace(
        all_embeddings=lambda: scans.append("memory") or [],
        transcript_embeddings=lambda: scans.append("transcript") or [],
    )
    mind._emb_cache = None
    mind._transcript_cache = None
    mind._transcript_enabled = True

    mind._warmup_embedder()

    assert scans == []


def test_default_policy_retains_memory_and_transcript_source_records(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode(
            "old retained fact", persona_id="b", scope="persona_private",
            meta={"summary": "safe summary", "source_event_ids": ["evt-old"]},
            embedding=b"\x01\x02\x03\x04", dim=1,
        )
        transcript_id = store.add_transcript("old user turn", "old assistant turn")
        old = time.time() - 3650 * 86400
        store._conn.execute("UPDATE memories SET created_at=? WHERE id=?", (old, memory_id))
        store._conn.execute("UPDATE transcripts SET created_at=? WHERE id=?", (old, transcript_id))
        store._conn.commit()
        before = _source_fingerprints(store)

        assert store.prune(max_items=0) == 0
        assert store.prune_transcripts(retention_days=0, max_items=0) == 0

        assert _source_fingerprints(store) == before
        assert store.retention_snapshot()["source_record_policy"] == "retain"
        assert store.retention_snapshot()["automatic_source_deletions"] == 0
    finally:
        store.close()


def test_retention_policy_is_explicit_not_a_huge_numeric_limit():
    policy = RetentionPolicy.from_config(_Cfg({
        "mind.retention.source_records": "retain",
        "mind.transcript.retention_policy": "retain",
    }))
    assert policy.source_records == "retain"
    assert policy.transcript_source_records == "retain"
    assert not hasattr(policy, "unlimited_max_items")


def test_write_disabled_service_does_not_enable_destructive_maintenance(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode("kept", persona_id="b", scope="persona_private")
        service = EpisodicMemoryService(store, _Cfg({
            "memory.episodic_enabled": True,
            "memory.write_enabled": False,
        }))
        service.bind_persona("b")
        assert service.write_enabled is False
        assert store.prune(max_items=0) == 0
        assert store.episodes(limit=5, persona_id="b")[0]["id"] == memory_id
    finally:
        store.close()


def test_write_disabled_episodic_retrieve_never_touches_canonical_source(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode(
            "計測用の合言葉は青い灯台", persona_id="b", scope="persona_private",
        )
        service = EpisodicMemoryService(store, _Cfg({
            "memory.episodic_enabled": True,
            "memory.retrieval_enabled": True,
            "memory.write_enabled": False,
        }))
        service.bind_persona("b")
        before = _source_fingerprints(store)

        result = service.retrieve("前に覚えてもらった計測用の合言葉を教えて")

        assert memory_id in {item.memory.memory_id for item in result.scored}
        assert _source_fingerprints(store) == before
    finally:
        store.close()


def test_write_disabled_mind_lexical_recall_never_touches_source(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode(
            "計測用の合言葉は青い灯台", persona_id="b", scope="persona_private",
        )
        store.bind_persona("b", strict=True)
        mind = object.__new__(Mind)
        mind._store = store
        mind._episodes = EpisodicMemoryService(store, _Cfg({"memory.write_enabled": False}))
        mind._top_k = 3
        mind._transcript_enabled = False
        mind._transcript_top_k = 0
        before = _source_fingerprints(store)

        rows, _ = mind._recall_keyword("前に覚えてもらった計測用の合言葉を教えて")

        assert memory_id in {row["id"] for row in rows}
        assert _source_fingerprints(store) == before
    finally:
        store.close()


def test_explicit_user_or_privacy_deletion_remains_available(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        user_deleted = store.add_episode("forget me", persona_id="b", scope="persona_private")
        privacy_deleted = store.add_transcript("private source", "private reply")
        assert store.delete_memory_explicit(
            user_deleted, reason=ExplicitDeletionReason.USER_REQUEST,
        ) is True
        assert store.delete_transcript_explicit(
            privacy_deleted, reason=ExplicitDeletionReason.PRIVACY_CORRECTION,
        ) is True
        assert store.counts()["total"] == 0
        assert store.all_transcripts() == []
        assert store.retention_snapshot()["explicit_source_deletions"] == 2
    finally:
        store.close()


def test_unlabelled_deletion_is_rejected(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add("keep me")
        with pytest.raises(ValueError):
            store.delete_memory_explicit(memory_id, reason="maintenance")
        assert store.counts()["total"] == 1
    finally:
        store.close()


def test_do_not_store_turn_never_reaches_memory_or_transcript_source(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    mind = object.__new__(Mind)
    mind._privacy = PrivacyManager(_Cfg())
    mind._privacy_context = {"group": False, "audience": set()}
    mind._current_speaker = None
    mind._speakers = None
    mind._personality = SimpleNamespace(on_turn=lambda **_kwargs: None)
    mind._dialogue = SimpleNamespace(record_response=lambda *_args, **_kwargs: None)
    mind._pending = []
    mind._store = store
    mind._episodes = EpisodicMemoryService(store, _Cfg({
        "memory.episodic_enabled": True,
        "memory.write_enabled": True,
    }))
    try:
        mind.record_turn("この秘密は保存しないで", "了解", source="local")

        assert store.counts()["total"] == 0
        assert store.all_transcripts() == []
        assert mind._pending == []
    finally:
        store.close()


def test_disk_full_marks_store_read_only_without_deleting_old_records(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        existing = store.add_episode("existing", persona_id="b", scope="persona_private")
        before = _source_fingerprints(store)
        store._conn.execute("PRAGMA query_only=ON")

        with pytest.raises(MemoryWriteUnavailable):
            store.add_episode("new write", persona_id="b", scope="persona_private")

        assert store.storage_health()["state"] == "read_only"
        assert store.storage_health()["last_error"] == "sqlite_write_failed"
        assert _source_fingerprints(store) == before
        assert store.episodes(limit=5, persona_id="b")[0]["id"] == existing
        with pytest.raises(MemoryWriteUnavailable):
            store.add("another write")
    finally:
        store.close()


def test_real_sqlite_full_preserves_sources_and_they_survive_restart(tmp_path):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    existing = store.add_episode(
        "existing after full", persona_id="b", scope="persona_private",
    )
    before = _source_fingerprints(store)
    page_count = int(store._conn.execute("PRAGMA page_count").fetchone()[0])
    store._conn.execute(f"PRAGMA max_page_count={page_count}")

    with pytest.raises(MemoryWriteUnavailable) as caught:
        store.add_episode(
            "x" * 2_000_000, persona_id="b", scope="persona_private",
        )

    assert isinstance(caught.value.__cause__, sqlite3.Error)
    assert getattr(caught.value.__cause__, "sqlite_errorname", "") == "SQLITE_FULL"
    assert store.storage_health()["state"] == "read_only"
    assert _source_fingerprints(store) == before
    store.close()

    reopened = MemoryStore(path)
    try:
        assert reopened.episodes(
            limit=5, persona_id="b", allow_legacy_unscoped=False,
        )[0]["id"] == existing
        assert reopened.storage_health()["state"] == "writable"
    finally:
        reopened.close()


def test_storage_health_warns_below_twenty_percent_without_deleting_sources(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=1_000, used=850, free=150),
    )
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    store = MemoryStore(
        data_dir / "mind.db",
        capacity_probe_paths={"data": data_dir, "backup": backup_dir},
    )
    try:
        store.add_episode(
            "retained memory", persona_id="b", scope="persona_private",
        )
        store.add_transcript("retained transcript", "retained reply")
        before = _source_fingerprints(store)

        health = store.storage_health()

        assert health["state"] == "writable"
        assert health["capacity"]["state"] == "warning"
        assert health["capacity"]["free_percent"] == 15.0
        assert health["capacity"]["warning_threshold_percent"] == 20.0
        assert health["capacity"]["measurement_path"]
        assert health["capacity"]["probe_failures"] == 0
        assert {target for volume in health["capacity"]["volumes"]
                for target in volume["targets"]} >= {
            "canonical", "sidecar", "data", "backup",
        }
        assert store.retention_snapshot()["automatic_source_deletions"] == 0
        assert _source_fingerprints(store) == before
    finally:
        store.close()


def test_storage_health_compares_unrounded_free_percent_at_warning_boundary(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=100_000, used=80_001, free=19_999),
    )
    store = MemoryStore(tmp_path / "mind.db")
    try:
        capacity = store.storage_health()["capacity"]

        assert capacity["free_percent"] == 20.0
        assert capacity["warning_threshold_percent"] == 20.0
        assert capacity["state"] == "warning"
    finally:
        store.close()


def test_storage_warning_threshold_is_configurable_through_mind_store_factory(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=1_000, used=850, free=150),
    )
    mind = object.__new__(Mind)
    mind._cfg = _Cfg({
        "mind.storage.warning_free_percent": 10,
        "mind.backup.dir": str(tmp_path / "external-backups"),
    })
    mind._data_dir = tmp_path / "data"

    store = mind._open_persona_store("b")
    try:
        capacity = store.storage_health()["capacity"]
        assert capacity["state"] == "ok"
        assert capacity["free_percent"] == 15.0
        assert capacity["warning_threshold_percent"] == 10.0
        assert "backup" in {
            target for volume in capacity["volumes"]
            for target in volume["targets"]
        }
    finally:
        store.close()


def test_storage_probe_failure_is_unknown_and_does_not_disable_writes(
    tmp_path, monkeypatch,
):
    def unavailable(_path):
        raise OSError("disk probe unavailable")

    monkeypatch.setattr(shutil, "disk_usage", unavailable)
    store = MemoryStore(tmp_path / "mind.db")
    try:
        existing = store.add_episode(
            "existing", persona_id="b", scope="persona_private",
        )
        before = _source_fingerprints(store)

        health = store.storage_health()

        assert health["state"] == "writable"
        assert health["last_error"] == ""
        assert health["capacity"]["state"] == "unknown"
        assert health["capacity"]["free_percent"] is None
        assert health["capacity"]["warning_threshold_percent"] == 20.0
        assert health["capacity"]["measurement_path"]
        assert health["capacity"]["probe_failures"] >= 1
        assert _source_fingerprints(store) == before
        assert store.add_episode(
            "write remains enabled", persona_id="b", scope="persona_private",
        ) > existing
        assert store.retention_snapshot()["automatic_source_deletions"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["touch", "reinforce", "mark", "upgrade"])
def test_existing_source_mutations_share_the_read_only_failure_boundary(tmp_path, mutation):
    store = MemoryStore(tmp_path / f"{mutation}.db")
    try:
        memory_id = store.add_episode(
            "canonical source", persona_id="b", scope="persona_private",
        )
        before = _source_fingerprints(store)
        store._conn.execute("PRAGMA query_only=ON")
        operations = {
            "touch": lambda: store.touch([memory_id]),
            "reinforce": lambda: store.reinforce_episode(memory_id),
            "mark": lambda: store.mark_episode(memory_id, "archived"),
            "upgrade": lambda: store.upgrade_episode(
                memory_id, information_type="user_statement", confidence=.9,
            ),
        }

        with pytest.raises(MemoryWriteUnavailable):
            operations[mutation]()

        health = store.storage_health()
        assert health["state"] == "read_only"
        assert health["last_error"] == "sqlite_write_failed"
        assert health["capacity"]["state"] in {"ok", "warning", "unknown"}
        assert _source_fingerprints(store) == before
        with pytest.raises(MemoryWriteUnavailable):
            store.touch([memory_id])
    finally:
        store.close()


def test_read_only_transition_is_checked_after_acquiring_the_write_lock(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    first_inside = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_executed = threading.Event()
    errors: list[type[BaseException]] = []

    def first_operation(_conn):
        first_inside.set()
        assert release_first.wait(timeout=2)
        raise sqlite3.OperationalError("database or disk is full")

    def run_first():
        try:
            store._source_write(first_operation)
        except BaseException as exc:  # test captures both worker outcomes
            errors.append(type(exc))

    def run_second():
        second_started.set()
        try:
            store._source_write(lambda _conn: second_executed.set())
        except BaseException as exc:
            errors.append(type(exc))

    first = threading.Thread(target=run_first)
    second = threading.Thread(target=run_second)
    first.start()
    assert first_inside.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    time.sleep(.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    try:
        assert not first.is_alive() and not second.is_alive()
        assert not second_executed.is_set()
        assert errors.count(MemoryWriteUnavailable) == 2
    finally:
        store.close()


def test_bounded_persona_search_can_reach_an_old_matching_memory(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        old_id = store.add_episode(
            "計測用の合言葉は青い灯台", persona_id="b", scope="persona_private",
        )
        store._conn.execute("UPDATE memories SET created_at=1 WHERE id=?", (old_id,))
        for index in range(300):
            store.add_episode(
                f"recent filler {index}", persona_id="b", scope="persona_private",
            )
        store.add_episode(
            "計測用の合言葉は別物", persona_id="c", scope="persona_private",
        )

        rows = store.search_episodes(
            "前に覚えてもらった計測用の合言葉を教えて",
            limit=8,
            persona_id="b",
            allow_legacy_unscoped=False,
            include_archived=True,
        )
        assert old_id in {row["id"] for row in rows}
        assert len(rows) <= 8
        assert {row["persona_id"] for row in rows} == {"b"}
    finally:
        store.close()


def test_persona_switch_b_to_c_to_b_never_crosses_the_acl(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        b_id = store.add_episode(
            "共有されない合言葉は青い灯台", persona_id="b",
            scope="persona_private",
        )
        store.bind_persona("c", strict=True)
        c_id = store.add_episode(
            "共有されない合言葉は赤い灯台", persona_id="c",
            scope="persona_private",
        )

        c_rows = store.search_episodes(
            "共有されない合言葉", limit=8, include_archived=True,
        )
        assert {row["id"] for row in c_rows} == {c_id}

        store.bind_persona("b", strict=True)
        b_rows = store.search_episodes(
            "共有されない合言葉", limit=8, include_archived=True,
        )
        assert {row["id"] for row in b_rows} == {b_id}
    finally:
        store.close()


def test_canonical_persona_acl_rejects_a_poisoned_derived_index(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        c_id = store.add_episode(
            "機密の合言葉は赤い灯台", persona_id="c", scope="persona_private",
        )
        store.rebuild_search_index()
        with store._lock:
            index = store._open_search_index()
            index.execute(
                "UPDATE memory_search_docs SET persona_id='b'"
                " WHERE doc_type='memory' AND source_id=?",
                (c_id,),
            )
            index.commit()

        rows = store.search_episodes(
            "機密の合言葉", limit=8, persona_id="b",
            allow_legacy_unscoped=False, include_archived=True,
        )

        assert rows == []
    finally:
        store.close()


def test_canonical_status_recheck_rejects_poisoned_archived_candidate(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode(
            "archive only secret", persona_id="b", scope="persona_private",
            status="archived",
        )
        store.rebuild_search_index()
        with store._lock:
            index = store._open_search_index()
            index.execute(
                "UPDATE memory_search_docs SET status='active'"
                " WHERE doc_type='memory' AND source_id=?",
                (memory_id,),
            )
            index.commit()

        rows = store.search_episodes(
            "archive only secret", limit=8, persona_id="b",
            allow_legacy_unscoped=False, include_archived=False,
        )

        assert rows == []
    finally:
        store.close()


def test_episodic_recall_calls_bounded_index_for_an_old_memory(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        old_id = store.add_episode(
            "計測用の合言葉は青い灯台", persona_id="b", scope="persona_private",
        )
        store._conn.execute("UPDATE memories SET created_at=1 WHERE id=?", (old_id,))
        for index in range(60):
            store.add_episode(
                f"unrelated filler {index}", persona_id="b", scope="persona_private",
            )
        service = EpisodicMemoryService(store, _Cfg({
            "memory.retrieval_enabled": True,
            "memory.write_enabled": False,
            "memory.scan_limit": 20,
            "memory.max_retrieved": 3,
        }))
        service.bind_persona("b")

        result = service.retrieve("前に覚えてもらった計測用の合言葉を教えて")

        assert old_id in {item.memory.memory_id for item in result.scored}
        assert result.legacy_result_count <= 20
    finally:
        store.close()


def test_legacy_keyword_recall_uses_bounded_store_candidates(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.bind_persona("b", strict=True)
        target = store.add_episode(
            "機器名はオーロラ", persona_id="b", scope="persona_private",
        )
        mind = object.__new__(Mind)
        mind._store = store
        mind._episodes = EpisodicMemoryService(store, _Cfg({"memory.write_enabled": False}))
        mind._top_k = 3
        mind._transcript_enabled = False
        mind._transcript_top_k = 0
        monkeypatch.setattr(
            store, "all_texts",
            lambda: (_ for _ in ()).throw(AssertionError("unbounded source materialization")),
        )

        rows, transcripts = mind._recall_keyword("前の機器名を教えて")

        assert [row["id"] for row in rows] == [target]
        assert transcripts == []
    finally:
        store.close()


def test_rebuildable_index_is_derived_and_does_not_change_sources(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        store.add_episode("古い機器名はオーロラ", persona_id="b", scope="persona_private")
        store.add_transcript("以前の話", "覚えているよ")
        before = _source_fingerprints(store)

        first = store.rebuild_search_index()
        store.drop_search_index()
        second = store.rebuild_search_index()

        assert first["memory_rows"] == second["memory_rows"] == 1
        assert first["transcript_rows"] == second["transcript_rows"] == 1
        assert _source_fingerprints(store) == before
    finally:
        store.close()


def test_index_freshness_tracks_add_and_explicit_delete_across_restart(tmp_path):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        stale_id = store.add_episode(
            "消す対象は古い羅針盤", persona_id="b", scope="persona_private",
        )
        store.rebuild_search_index()
        fresh_id = store.add_episode(
            "追加対象は新しい六分儀", persona_id="b", scope="persona_private",
        )
        assert fresh_id in {row["id"] for row in store.search_episodes(
            "追加対象は新しい六分儀", limit=8, persona_id="b",
            include_archived=True,
        )}
        assert store.delete_memory_explicit(
            stale_id, reason=ExplicitDeletionReason.USER_REQUEST,
        ) is True
    finally:
        store.close()

    reopened = MemoryStore(path)
    try:
        assert stale_id not in {row["id"] for row in reopened.search_episodes(
            "消す対象は古い羅針盤", limit=8, persona_id="b",
            include_archived=True,
        )}
        assert {row["id"] for row in reopened.search_episodes(
            "追加対象は新しい六分儀", limit=8, persona_id="b",
            include_archived=True,
        )} == {fresh_id}
    finally:
        reopened.close()


def test_restart_repairs_a_corrupt_derived_index_without_losing_old_recall(tmp_path):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        old_id = store.add_episode(
            "復旧対象の合言葉は青い灯台", persona_id="b", scope="persona_private",
        )
        store._conn.execute("UPDATE memories SET created_at=1 WHERE id=?", (old_id,))
        store._conn.commit()
        for index in range(40):
            store.add_episode(
                f"recent filler {index}", persona_id="b", scope="persona_private",
            )
        store.rebuild_search_index()
        index_path = store._search_path
    finally:
        store.close()
    index_path.write_bytes(b"not a sqlite database")

    reopened = MemoryStore(path)
    try:
        rows = reopened.search_episodes(
            "復旧対象の合言葉", limit=8, persona_id="b",
            allow_legacy_unscoped=False, include_archived=True,
        )
        assert old_id in {row["id"] for row in rows}
    finally:
        reopened.close()


def test_restart_repairs_valid_sqlite_with_wrong_fts_schema_once(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        old_id = store.add_episode(
            "schema repair target", persona_id="b", scope="persona_private",
        )
        store.rebuild_search_index()
        index_path = store._search_path
        source_revision = store._source_revision()
    finally:
        store.close()
    index_path.unlink()
    wrong = sqlite3.connect(index_path)
    try:
        wrong.execute("CREATE TABLE search_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        wrong.execute(
            "INSERT INTO search_metadata VALUES('source_revision',?)",
            (str(source_revision),),
        )
        wrong.execute("INSERT INTO search_metadata VALUES('schema_version','8')")
        wrong.execute("CREATE TABLE memory_search_docs(source_id INTEGER)")
        wrong.commit()
    finally:
        wrong.close()

    reopened = MemoryStore(path)
    rebuilds = 0
    original = reopened.rebuild_search_index

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original()

    monkeypatch.setattr(reopened, "rebuild_search_index", counted_rebuild)
    try:
        rows = reopened.search_episodes(
            "schema repair target", limit=8, persona_id="b", include_archived=True,
        )
        assert old_id in {row["id"] for row in rows}
        assert rebuilds == 1
    finally:
        reopened.close()


def test_touch_does_not_invalidate_index_in_process_or_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    memory_id = store.add_episode(
        "touch freshness target", persona_id="b", scope="persona_private",
    )
    store.rebuild_search_index()
    rebuilds = 0
    original = store.rebuild_search_index

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original()

    monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)
    store.touch([memory_id])
    assert store.search_episodes(
        "touch freshness target", limit=8, persona_id="b",
    )
    assert rebuilds == 0
    store.close()

    reopened = MemoryStore(path)
    original_reopened = reopened.rebuild_search_index

    def counted_reopened():
        nonlocal rebuilds
        rebuilds += 1
        return original_reopened()

    monkeypatch.setattr(reopened, "rebuild_search_index", counted_reopened)
    try:
        assert reopened.search_episodes(
            "touch freshness target", limit=8, persona_id="b",
        )
        assert rebuilds == 0
    finally:
        reopened.close()


def test_indexed_status_update_invalidates_sidecar_but_nonindexed_upgrade_does_not(
    tmp_path, monkeypatch,
):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        memory_id = store.add_episode(
            "status revision target", persona_id="b", scope="persona_private",
        )
        store.rebuild_search_index()
        rebuilds = 0
        original = store.rebuild_search_index

        def counted_rebuild():
            nonlocal rebuilds
            rebuilds += 1
            return original()

        monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)
        store.upgrade_episode(
            memory_id, information_type="user_statement", confidence=.9,
        )
        assert store.search_episodes(
            "status revision target", limit=8, persona_id="b",
        )
        assert rebuilds == 0

        store.mark_episode(memory_id, "archived")
        assert store.search_episodes(
            "status revision target", limit=8, persona_id="b",
            include_archived=False,
        ) == []
        assert rebuilds == 1
    finally:
        store.close()


def test_semantic_candidate_index_recalls_old_vocabulary_mismatch_without_full_scan(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target_vector = np.zeros(32, dtype=np.float32)
        target_vector[0] = 1.0
        query_vector = target_vector.copy()
        query_vector[1] = .02
        target_id = store.add_episode(
            "自動車が好き", persona_id="b", scope="persona_private",
            embedding=target_vector.tobytes(), dim=32,
        )
        store._conn.execute("UPDATE memories SET created_at=1 WHERE id=?", (target_id,))
        store._conn.commit()
        for index in range(80):
            vector = np.zeros(32, dtype=np.float32)
            vector[(index % 31) + 1] = 1.0
            store.add_episode(
                f"unrelated filler {index}", persona_id="b", scope="persona_private",
                embedding=vector.tobytes(), dim=32,
            )
        store.bind_persona("b", strict=True)
        mind = object.__new__(Mind)
        mind._store = store
        mind._episodes = EpisodicMemoryService(store, _Cfg({"memory.write_enabled": False}))
        mind._embedder = SimpleNamespace(
            encode=lambda _texts, is_query=False: np.stack([query_vector]),
        )
        mind._top_k = 3
        mind._min_score = .2
        mind._transcript_enabled = False
        mind._transcript_top_k = 0
        monkeypatch_target = store.all_embeddings
        store.all_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full embedding scan is forbidden")
        )
        before = _source_fingerprints(store)

        rows, transcripts = mind._recall_semantic("クルマの好み")

        assert target_id in {row["id"] for row in rows}
        assert transcripts == []
        assert _source_fingerprints(store) == before
        store.all_embeddings = monkeypatch_target
    finally:
        store.close()


def test_semantic_ann_survives_high_cosine_quantization_boundary_crossing(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        dim = 384
        target = np.full(dim, .1, dtype=np.float32)
        query = target.copy()
        for band in range(1, 5):
            index = (band * 7) % dim
            target[index] = .049
            query[index] = .051
        cosine = float(target @ query / (np.linalg.norm(target) * np.linalg.norm(query)))
        assert cosine > .99985
        target_id = store.add_episode(
            "自動車が好き", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=dim,
        )
        store.bind_persona("b", strict=True)
        mind = _mind_for_semantic(store, query)
        mind._transcript_enabled = False
        store.all_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full embedding scan is forbidden")
        )
        rows, _ = mind._recall_semantic("クルマの好み")

        assert target_id in {row["id"] for row in rows}
    finally:
        store.close()


def test_semantic_ann_does_not_lose_old_target_behind_300_legacy_bucket_collisions(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target, query = _near_vector_pair()
        sampled = {
            (band * 7 + offset * 13) % 384
            for band in range(1, 5)
            for offset in range(16)
        }
        collider = -query.copy()
        for index in sampled:
            collider[index] = query[index]
        for index in range(300):
            store.add_episode(
                f"legacy bucket collision {index}", persona_id="b",
                scope="persona_private", embedding=collider.tobytes(), dim=384,
            )
        target_id = store.add_episode(
            "自動車が好き", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=384,
        )
        store._conn.execute("UPDATE memories SET created_at=1 WHERE id=?", (target_id,))
        store._conn.commit()
        store.bind_persona("b", strict=True)
        mind = _mind_for_semantic(store, query)
        mind._transcript_enabled = False
        store.all_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full embedding scan is forbidden")
        )

        rows, _ = mind._recall_semantic("クルマの好み")

        assert target_id in {row["id"] for row in rows}
    finally:
        store.close()


def test_ann_fixed_seed_candidate_recall_gate():
    minimum_hits = {0.72: 950, 0.80: 980, 0.90: 995, 0.95: 1000, 0.99: 1000}
    for cosine, minimum in minimum_hits.items():
        pairs = [_seeded_vector_pair(index, cosine) for index in range(1000)]
        target_bands = MemoryStore._embedding_bands_batch(tuple(
            (target.tobytes(), 384) for target, _query in pairs
        ))
        query_bands = MemoryStore._embedding_bands_batch(tuple(
            (query.tobytes(), 384) for _target, query in pairs
        ))
        hits = sum(
            any(
                target_bucket in MemoryStore._ann_probes(query_bucket)
                for (_target_band, target_bucket), (_query_band, query_bucket)
                in zip(target[1:], query[1:])
            )
            for target, query in zip(target_bands, query_bands)
        )
        assert hits >= minimum, (cosine, hits, minimum)


def test_cosine_point_nine_reaches_memory_and_transcript_through_mind(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        memory_id = store.add_episode(
            "自動車が好き", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=384,
        )
        transcript_id = store.add_transcript(
            "自動車が好き", "覚えておく", embedding=target.tobytes(), dim=384,
        )
        store.bind_persona("b", strict=True)
        mind = _mind_for_semantic(store, query)
        mind._min_score = .72
        store.all_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full memory embedding scan is forbidden")
        )
        store.transcript_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full transcript embedding scan is forbidden")
        )

        memories, transcripts = mind._recall_semantic("クルマの好み")

        assert memory_id in {row["id"] for row in memories}
        assert transcript_id in {row["id"] for row in transcripts}
    finally:
        store.close()


def test_high_cosine_semantic_query_uses_bounded_fast_probe_for_both_sources(
    tmp_path, monkeypatch,
):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target, query = _near_vector_pair(cosine=.99985)
        memory_id = store.add_episode(
            "fast memory", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=384,
        )
        transcript_id = store.add_transcript(
            "fast transcript", "answer", embedding=target.tobytes(), dim=384,
        )
        store.bind_persona("b", strict=True)
        calls = 0
        original = store._query_search_index

        def counted(sql, args):
            nonlocal calls
            calls += 1
            return original(sql, args)

        monkeypatch.setattr(store, "_query_search_index", counted)

        memories = store.search_episode_embeddings(
            query.tobytes(), dim=384, limit=80, persona_id="b",
        )
        transcripts = store.search_transcript_embeddings(
            query.tobytes(), dim=384, limit=40,
        )

        assert memory_id in {row["id"] for row in memories}
        assert transcript_id in {row["id"] for row in transcripts}
        assert calls <= 4
    finally:
        store.close()


def test_transcript_semantic_ann_recalls_old_vocabulary_mismatch_without_full_scan(tmp_path):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target, query = _near_vector_pair()
        target_id = store.add_transcript(
            "自動車が好き", "覚えておく", embedding=target.tobytes(), dim=384,
        )
        store._conn.execute(
            "UPDATE transcripts SET created_at=? WHERE id=?",
            (time.time() - 86400.0, target_id),
        )
        store._conn.commit()
        for index in range(100):
            filler = np.roll(target, index + 1).astype(np.float32)
            store.add_transcript(
                f"unrelated transcript {index}", "irrelevant",
                embedding=filler.tobytes(), dim=384,
            )
        mind = _mind_for_semantic(store, query)
        store.transcript_embeddings = lambda: (_ for _ in ()).throw(
            AssertionError("full transcript embedding scan is forbidden")
        )

        ids, rows, matrix = mind._transcript_matrix("クルマの好み", query)
        recalled = mind._search_transcripts(query, "クルマの好み")

        assert target_id in ids
        assert matrix.shape[0] == len(rows) <= 40
        assert target_id in {row["id"] for row in recalled}
    finally:
        store.close()


def test_semantic_candidates_reject_invalid_canonical_vectors_for_memory_and_transcript(
    tmp_path,
):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        valid = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        invalid_vectors = {
            "dim_mismatch": (valid.tobytes(), 3),
            "truncated": (valid[:3].tobytes(), 4),
            "nan": (np.array([np.nan, 0.0, 0.0, 0.0], dtype=np.float32).tobytes(), 4),
            "inf": (np.array([np.inf, 0.0, 0.0, 0.0], dtype=np.float32).tobytes(), 4),
            "zero": (np.zeros(4, dtype=np.float32).tobytes(), 4),
        }
        invalid_memory_ids = []
        invalid_transcript_ids = []
        for repeat in range(18):
            for name, (blob, dim) in invalid_vectors.items():
                invalid_memory_ids.append(store.add_episode(
                    f"invalid memory {name} {repeat}", persona_id="b",
                    scope="persona_private", embedding=blob, dim=dim,
                ))
                invalid_transcript_ids.append(store.add_transcript(
                    f"invalid transcript {name} {repeat}", "answer",
                    embedding=blob, dim=dim,
                ))
        valid_memory = store.add_episode(
            "valid memory", persona_id="b", scope="persona_private",
            embedding=valid.tobytes(), dim=4,
        )
        valid_transcript = store.add_transcript(
            "valid transcript", "answer", embedding=valid.tobytes(), dim=4,
        )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        exact_band, exact_bucket = store._embedding_bands(valid.tobytes(), 4)[0]
        store._search_conn.execute(
            "DELETE FROM memory_embedding_docs WHERE band=?", (exact_band,)
        )
        store._search_conn.execute(
            "DELETE FROM transcript_embedding_docs WHERE band=?", (exact_band,)
        )
        for source_id in invalid_memory_ids:
            store._search_conn.execute(
                "INSERT OR REPLACE INTO memory_embedding_docs"
                " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (source_id, exact_band, exact_bucket, 4, "b", "persona_private", "active", 1.0),
            )
        for source_id in invalid_transcript_ids:
            store._search_conn.execute(
                "INSERT OR REPLACE INTO transcript_embedding_docs"
                " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                (source_id, exact_band, exact_bucket, 4, 1.0),
            )
        store._search_conn.execute(
            "INSERT INTO memory_embedding_docs"
            " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (valid_memory, exact_band, exact_bucket, 4, "b", "persona_private", "active", 1.0),
        )
        store._search_conn.execute(
            "INSERT INTO transcript_embedding_docs"
            " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
            (valid_transcript, exact_band, exact_bucket, 4, 1.0),
        )
        store._search_conn.commit()

        memory_rows = store.search_episode_embeddings(
            valid.tobytes(), dim=4, limit=80, persona_id="b",
        )
        transcript_rows = store.search_transcript_embeddings(
            valid.tobytes(), dim=4, limit=40,
        )

        assert {row["id"] for row in memory_rows} == {valid_memory}
        assert {row["id"] for row in transcript_rows} == {valid_transcript}
        assert all(
            np.isfinite(np.frombuffer(row["embedding"], dtype=np.float32)).all()
            and np.isclose(np.linalg.norm(
                np.frombuffer(row["embedding"], dtype=np.float32)
            ), 1.0)
            for row in [*memory_rows, *transcript_rows]
        )
    finally:
        store.close()


@pytest.mark.parametrize("source,limit", [("memory", 80), ("transcript", 40)])
def test_semantic_repair_prevents_invalid_approximate_rows_from_consuming_final_cap(
    tmp_path, monkeypatch, source, limit,
):
    store = MemoryStore(tmp_path / f"{source}.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        if source == "memory":
            valid_id = store.add_episode(
                "valid approximate memory", persona_id="b", scope="persona_private",
                embedding=target.tobytes(), dim=384,
            )
            invalid_ids = [
                store.add_episode(
                    f"invalid approximate memory {index}", persona_id="b",
                    scope="persona_private", embedding=np.zeros(384, dtype=np.float32).tobytes(),
                    dim=384,
                )
                for index in range(limit)
            ]
        else:
            valid_id = store.add_transcript(
                "valid approximate transcript", "answer",
                embedding=target.tobytes(), dim=384,
            )
            invalid_ids = [
                store.add_transcript(
                    f"invalid approximate transcript {index}", "answer",
                    embedding=np.zeros(384, dtype=np.float32).tobytes(), dim=384,
                )
                for index in range(limit)
            ]
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        query_bands = store._embedding_bands(query.tobytes(), 384)[1:]
        for source_id in invalid_ids:
            for band, bucket in query_bands:
                if source == "memory":
                    store._search_conn.execute(
                        "INSERT OR REPLACE INTO memory_embedding_docs"
                        " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (source_id, band, bucket, 384, "b", "persona_private", "active", 1.0),
                    )
                else:
                    store._search_conn.execute(
                        "INSERT OR REPLACE INTO transcript_embedding_docs"
                        " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                        (source_id, band, bucket, 384, 1.0),
                    )
        store._search_conn.commit()
        rebuilds = 0
        original_rebuild = store.rebuild_search_index

        def counted_rebuild():
            nonlocal rebuilds
            rebuilds += 1
            return original_rebuild()

        monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)

        if source == "memory":
            rows = store.search_episode_embeddings(
                query.tobytes(), dim=384, limit=limit, persona_id="b",
            )
        else:
            rows = store.search_transcript_embeddings(
                query.tobytes(), dim=384, limit=limit,
            )

        assert valid_id in {row["id"] for row in rows}
        assert rebuilds == 1
    finally:
        store.close()


@pytest.mark.parametrize("source,limit", [("memory", 80), ("transcript", 40)])
def test_invalid_exact_sidecar_hit_repairs_once_then_falls_through_to_full_ann(
    tmp_path, monkeypatch, source, limit,
):
    store = MemoryStore(tmp_path / f"{source}.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        invalid = np.zeros(384, dtype=np.float32).tobytes()
        if source == "memory":
            valid_id = store.add_episode(
                "valid full tier memory", persona_id="b", scope="persona_private",
                embedding=target.tobytes(), dim=384,
            )
            invalid_id = store.add_episode(
                "invalid exact memory", persona_id="b", scope="persona_private",
                embedding=invalid, dim=384,
            )
        else:
            valid_id = store.add_transcript(
                "valid full tier transcript", "answer",
                embedding=target.tobytes(), dim=384,
            )
            invalid_id = store.add_transcript(
                "invalid exact transcript", "answer", embedding=invalid, dim=384,
            )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        exact_band, exact_bucket = store._embedding_bands(query.tobytes(), 384)[0]
        if source == "memory":
            store._search_conn.execute(
                "INSERT OR REPLACE INTO memory_embedding_docs"
                " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (invalid_id, exact_band, exact_bucket, 384, "b", "persona_private", "active", 1.0),
            )
        else:
            store._search_conn.execute(
                "INSERT OR REPLACE INTO transcript_embedding_docs"
                " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                (invalid_id, exact_band, exact_bucket, 384, 1.0),
            )
        store._search_conn.commit()
        rebuilds = 0
        original_rebuild = store.rebuild_search_index

        def counted_rebuild():
            nonlocal rebuilds
            rebuilds += 1
            return original_rebuild()

        monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)

        if source == "memory":
            rows = store.search_episode_embeddings(
                query.tobytes(), dim=384, limit=limit, persona_id="b",
            )
        else:
            rows = store.search_transcript_embeddings(
                query.tobytes(), dim=384, limit=limit,
            )

        assert valid_id in {row["id"] for row in rows}
        assert invalid_id not in {row["id"] for row in rows}
        assert rebuilds == 1
    finally:
        store.close()


@pytest.mark.parametrize("source,limit", [("memory", 80), ("transcript", 40)])
def test_well_formed_wrong_vector_approximate_rows_trigger_one_sidecar_repair(
    tmp_path, monkeypatch, source, limit,
):
    store = MemoryStore(tmp_path / f"{source}.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        wrong = (-query).astype(np.float32)
        if source == "memory":
            valid_id = store.add_episode(
                "valid provenance memory", persona_id="b", scope="persona_private",
                embedding=target.tobytes(), dim=384,
            )
            wrong_ids = [
                store.add_episode(
                    f"wrong provenance memory {index}", persona_id="b",
                    scope="persona_private", embedding=wrong.tobytes(), dim=384,
                )
                for index in range(limit)
            ]
        else:
            valid_id = store.add_transcript(
                "valid provenance transcript", "answer",
                embedding=target.tobytes(), dim=384,
            )
            wrong_ids = [
                store.add_transcript(
                    f"wrong provenance transcript {index}", "answer",
                    embedding=wrong.tobytes(), dim=384,
                )
                for index in range(limit)
            ]
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        for source_id in wrong_ids:
            for band, bucket in store._embedding_bands(query.tobytes(), 384)[1:]:
                if source == "memory":
                    store._search_conn.execute(
                        "INSERT OR REPLACE INTO memory_embedding_docs"
                        " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (source_id, band, bucket, 384, "b", "persona_private", "active", 1.0),
                    )
                else:
                    store._search_conn.execute(
                        "INSERT OR REPLACE INTO transcript_embedding_docs"
                        " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                        (source_id, band, bucket, 384, 1.0),
                    )
        store._search_conn.commit()
        rebuilds = 0
        original_rebuild = store.rebuild_search_index

        def counted_rebuild():
            nonlocal rebuilds
            rebuilds += 1
            return original_rebuild()

        monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)

        rows = (
            store.search_episode_embeddings(
                query.tobytes(), dim=384, limit=limit, persona_id="b",
            )
            if source == "memory"
            else store.search_transcript_embeddings(
                query.tobytes(), dim=384, limit=limit,
            )
        )

        assert valid_id in {row["id"] for row in rows}
        assert not ({row["id"] for row in rows} & set(wrong_ids))
        assert rebuilds == 1
    finally:
        store.close()


@pytest.mark.parametrize("source,limit", [("memory", 80), ("transcript", 40)])
def test_well_formed_wrong_vector_exact_hit_repairs_then_reaches_full_ann(
    tmp_path, monkeypatch, source, limit,
):
    store = MemoryStore(tmp_path / f"{source}.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        wrong = (-query).astype(np.float32)
        if source == "memory":
            valid_id = store.add_episode(
                "valid full provenance memory", persona_id="b",
                scope="persona_private", embedding=target.tobytes(), dim=384,
            )
            wrong_id = store.add_episode(
                "wrong exact provenance memory", persona_id="b",
                scope="persona_private", embedding=wrong.tobytes(), dim=384,
            )
        else:
            valid_id = store.add_transcript(
                "valid full provenance transcript", "answer",
                embedding=target.tobytes(), dim=384,
            )
            wrong_id = store.add_transcript(
                "wrong exact provenance transcript", "answer",
                embedding=wrong.tobytes(), dim=384,
            )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        exact_band, exact_bucket = store._embedding_bands(query.tobytes(), 384)[0]
        if source == "memory":
            store._search_conn.execute(
                "INSERT OR REPLACE INTO memory_embedding_docs"
                " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (wrong_id, exact_band, exact_bucket, 384, "b", "persona_private", "active", 1.0),
            )
        else:
            store._search_conn.execute(
                "INSERT OR REPLACE INTO transcript_embedding_docs"
                " (source_id,band,bucket,dim,created_at) VALUES (?,?,?,?,?)",
                (wrong_id, exact_band, exact_bucket, 384, 1.0),
            )
        store._search_conn.commit()
        rebuilds = 0
        original_rebuild = store.rebuild_search_index

        def counted_rebuild():
            nonlocal rebuilds
            rebuilds += 1
            return original_rebuild()

        monkeypatch.setattr(store, "rebuild_search_index", counted_rebuild)

        rows = (
            store.search_episode_embeddings(
                query.tobytes(), dim=384, limit=limit, persona_id="b",
            )
            if source == "memory"
            else store.search_transcript_embeddings(
                query.tobytes(), dim=384, limit=limit,
            )
        )

        assert valid_id in {row["id"] for row in rows}
        assert wrong_id not in {row["id"] for row in rows}
        assert rebuilds == 1
    finally:
        store.close()


@pytest.mark.parametrize("source,limit", [("memory", 80), ("transcript", 40)])
def test_exact_provenance_normalises_canonical_vector_only_once_without_repair(
    tmp_path, monkeypatch, source, limit,
):
    rng = np.random.default_rng(55)
    vector = np.zeros(384, dtype=np.float32)
    vector[:4] = rng.standard_normal(4).astype(np.float32)
    once = (
        vector.astype(np.float64) / np.linalg.norm(vector.astype(np.float64))
    ).astype(np.float32)
    twice = (
        once.astype(np.float64) / np.linalg.norm(once.astype(np.float64))
    ).astype(np.float32)
    assert not np.array_equal(once, twice)
    store = MemoryStore(tmp_path / f"{source}.db")
    try:
        if source == "memory":
            target_id = store.add_episode(
                "single normalization exact memory", persona_id="b",
                scope="persona_private", embedding=vector.tobytes(), dim=384,
            )
        else:
            target_id = store.add_transcript(
                "single normalization exact transcript", "answer",
                embedding=vector.tobytes(), dim=384,
            )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        before = _source_fingerprints(store)
        repairs = 0
        original_repair = store._repair_search_index

        def counted_repair():
            nonlocal repairs
            repairs += 1
            return original_repair()

        monkeypatch.setattr(store, "_repair_search_index", counted_repair)

        first = (
            store.search_episode_embeddings(
                vector.tobytes(), dim=384, limit=limit, persona_id="b",
            )
            if source == "memory"
            else store.search_transcript_embeddings(
                vector.tobytes(), dim=384, limit=limit,
            )
        )
        second = (
            store.search_episode_embeddings(
                vector.tobytes(), dim=384, limit=limit, persona_id="b",
            )
            if source == "memory"
            else store.search_transcript_embeddings(
                vector.tobytes(), dim=384, limit=limit,
            )
        )

        assert target_id in {row["id"] for row in first}
        assert target_id in {row["id"] for row in second}
        assert repairs == 0
        assert _source_fingerprints(store) == before
    finally:
        store.close()


def test_semantic_repair_excludes_parallel_search_from_intermediate_sidecar(
    tmp_path, monkeypatch,
):
    store = MemoryStore(tmp_path / "mind.db")
    allow_repair = threading.Event()
    repair_gap = threading.Event()
    competitor_opened = threading.Event()
    try:
        target, query = _seeded_vector_pair(2, .90)
        valid_id = store.add_episode(
            "concurrent repair target", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=384,
        )
        wrong_id = store.add_episode(
            "concurrent wrong exact", persona_id="b", scope="persona_private",
            embedding=(-query).astype(np.float32).tobytes(), dim=384,
        )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        exact_band, exact_bucket = store._embedding_bands(query.tobytes(), 384)[0]
        store._search_conn.execute(
            "INSERT OR REPLACE INTO memory_embedding_docs"
            " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (wrong_id, exact_band, exact_bucket, 384, "b", "persona_private", "active", 1.0),
        )
        store._search_conn.commit()
        original_prepare = store._prepare_search_index_path
        original_open = store._open_search_index
        paused = False

        def paused_prepare():
            nonlocal paused
            if threading.current_thread().name == "repair" and not paused:
                paused = True
                repair_gap.set()
                assert allow_repair.wait(3.0)
            return original_prepare()

        def observed_open():
            if threading.current_thread().name == "competitor":
                competitor_opened.set()
            return original_open()

        monkeypatch.setattr(store, "_prepare_search_index_path", paused_prepare)
        monkeypatch.setattr(store, "_open_search_index", observed_open)
        results: dict[str, list[dict]] = {}
        failures: list[BaseException] = []

        def search(name):
            try:
                results[name] = store.search_episode_embeddings(
                    query.tobytes(), dim=384, limit=80, persona_id="b",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        repair = threading.Thread(target=search, args=("repair",), name="repair")
        competitor = threading.Thread(
            target=search, args=("competitor",), name="competitor"
        )
        repair.start()
        assert repair_gap.wait(3.0)
        competitor.start()
        assert not competitor_opened.wait(.25)
        allow_repair.set()
        repair.join(5.0)
        competitor.join(5.0)

        assert not repair.is_alive() and not competitor.is_alive()
        assert failures == []
        assert valid_id in {row["id"] for row in results["repair"]}
        assert valid_id in {row["id"] for row in results["competitor"]}
    finally:
        allow_repair.set()
        store.close()


def test_semantic_repair_permission_error_is_safe_and_keeps_canonical_source(
    tmp_path, monkeypatch,
):
    store = MemoryStore(tmp_path / "mind.db")
    try:
        target, query = _seeded_vector_pair(2, .90)
        valid_id = store.add_episode(
            "permission repair target", persona_id="b", scope="persona_private",
            embedding=target.tobytes(), dim=384,
        )
        wrong_id = store.add_episode(
            "permission wrong exact", persona_id="b", scope="persona_private",
            embedding=(-query).astype(np.float32).tobytes(), dim=384,
        )
        store.bind_persona("b", strict=True)
        store.rebuild_search_index()
        exact_band, exact_bucket = store._embedding_bands(query.tobytes(), 384)[0]
        store._search_conn.execute(
            "INSERT OR REPLACE INTO memory_embedding_docs"
            " (source_id,band,bucket,dim,persona_id,scope,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (wrong_id, exact_band, exact_bucket, 384, "b", "persona_private", "active", 1.0),
        )
        store._search_conn.commit()
        original_unlink = Path.unlink

        def denied_unlink(path, *args, **kwargs):
            if path == store._search_path:
                raise PermissionError("synthetic locked sidecar")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", denied_unlink)

        rows = store.search_episode_embeddings(
            query.tobytes(), dim=384, limit=80, persona_id="b",
        )

        assert rows == []
        assert {valid_id, wrong_id}.issubset({
            row["id"] for row in store.episodes(
                limit=20, persona_id="b", allow_legacy_unscoped=False,
            )
        })
    finally:
        store.close()


def test_malformed_sidecar_permission_error_falls_back_without_source_loss(
    tmp_path, monkeypatch,
):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    memory_id = store.add_episode(
        "locked malformed sidecar target", persona_id="b", scope="persona_private",
    )
    store.rebuild_search_index()
    sidecar = store._search_path
    store.close()
    sidecar.unlink()
    with sqlite3.connect(sidecar) as wrong:
        wrong.execute("CREATE VIEW memory_search_docs AS SELECT 1 AS source_id")
        wrong.commit()
    reopened = MemoryStore(path)
    original_unlink = Path.unlink

    def denied_unlink(candidate, *args, **kwargs):
        if candidate == sidecar:
            raise PermissionError("synthetic Windows lock")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    try:
        rows = reopened.search_episodes(
            "locked malformed sidecar target", limit=8, persona_id="b",
        )

        assert memory_id in {row["id"] for row in rows}
        assert reopened.counts()["total"] == 1
    finally:
        reopened.close()


def test_valid_sidecar_view_schema_is_unlinked_and_rebuilt_once(tmp_path, monkeypatch):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        old_id = store.add_episode(
            "view schema repair target", persona_id="b", scope="persona_private",
        )
        store.rebuild_search_index()
        sidecar = store._search_path
    finally:
        store.close()
    sidecar.unlink()
    wrong = sqlite3.connect(sidecar)
    try:
        wrong.execute("CREATE TABLE decoy(source_id INTEGER)")
        wrong.execute("CREATE VIEW memory_search_docs AS SELECT source_id FROM decoy")
        wrong.commit()
    finally:
        wrong.close()
    unlink_count = 0
    original_unlink = Path.unlink

    def counted_unlink(candidate, *args, **kwargs):
        nonlocal unlink_count
        if candidate == sidecar:
            unlink_count += 1
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", counted_unlink)

    reopened = MemoryStore(path)
    rebuilds = 0
    original = reopened.rebuild_search_index

    def counted_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        return original()

    monkeypatch.setattr(reopened, "rebuild_search_index", counted_rebuild)
    try:
        rows = reopened.search_episodes(
            "view schema repair target", limit=8, persona_id="b",
            include_archived=True,
        )
        assert old_id in {row["id"] for row in rows}
        assert rebuilds == 1
        assert unlink_count == 1
    finally:
        reopened.close()


class _ConnectionProxy:
    def __init__(self, connection, closed: list[bool]):
        self._connection = connection
        self._closed = closed

    def __getattr__(self, name):
        return getattr(self._connection, name)

    @property
    def row_factory(self):
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._connection.row_factory = value

    def close(self):
        self._closed.append(True)
        return self._connection.close()


def _query_only_connect(monkeypatch, canonical: Path, closed: list[bool]):
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if Path(str(path)) == canonical:
            connection.execute("PRAGMA query_only=ON")
            return _ConnectionProxy(connection, closed)
        return connection

    monkeypatch.setattr("neuro_voice.mind.store.sqlite3.connect", connect)


def test_fully_migrated_query_only_startup_falls_back_to_read_only_retrieval(
    tmp_path, monkeypatch,
):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    memory_id = store.add_episode(
        "read only startup target", persona_id="b", scope="persona_private",
    )
    store.close()
    closed: list[bool] = []
    _query_only_connect(monkeypatch, path, closed)

    reopened = MemoryStore(path)
    try:
        health = reopened.storage_health()
        assert health["state"] == "read_only"
        assert health["last_error"] == "sqlite_read_only"
        assert health["capacity"]["state"] in {"ok", "warning", "unknown"}
        assert memory_id in {row["id"] for row in reopened.search_episodes(
            "read only startup target", limit=8, persona_id="b",
        )}
        with pytest.raises(MemoryWriteUnavailable):
            reopened.touch([memory_id])
    finally:
        reopened.close()


def test_query_only_old_schema_raises_safe_migration_error_and_closes_connection(
    tmp_path, monkeypatch,
):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memories(id INTEGER PRIMARY KEY, text TEXT)")
        connection.commit()
    finally:
        connection.close()
    closed: list[bool] = []
    _query_only_connect(monkeypatch, path, closed)

    with pytest.raises(MemoryStoreMigrationRequired, match="migration required"):
        MemoryStore(path)

    assert closed == [True]


def test_hardlinked_sidecar_is_replaced_without_modifying_its_target(tmp_path):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        memory_id = store.add_episode(
            "hardlink safety target", persona_id="b", scope="persona_private",
        )
        external = tmp_path / "other.db"
        with sqlite3.connect(external) as conn:
            conn.execute("CREATE TABLE protected(value TEXT)")
            conn.execute("INSERT INTO protected VALUES('unchanged')")
            conn.commit()
        before = hashlib.sha256(external.read_bytes()).hexdigest()
        os.link(external, store._search_path)

        rows = store.search_episodes(
            "hardlink safety target", limit=8, persona_id="b",
        )

        assert memory_id in {row["id"] for row in rows}
        assert hashlib.sha256(external.read_bytes()).hexdigest() == before
        assert not os.path.samefile(external, store._search_path)
    finally:
        store.close()


def test_sidecar_link_swap_between_check_and_connect_is_detached(
    tmp_path, monkeypatch,
):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        memory_id = store.add_episode(
            "connect race safety target", persona_id="b", scope="persona_private",
        )
        external = tmp_path / "protected.db"
        connection = sqlite3.connect(external)
        try:
            connection.execute("CREATE TABLE protected(value TEXT)")
            connection.execute("INSERT INTO protected VALUES('unchanged')")
            connection.commit()
        finally:
            connection.close()
        before = hashlib.sha256(external.read_bytes()).hexdigest()
        real_connect = sqlite3.connect
        swapped = False

        def connect(candidate, *args, **kwargs):
            nonlocal swapped
            if Path(str(candidate)) == store._search_path and not swapped:
                swapped = True
                os.link(external, store._search_path)
            return real_connect(candidate, *args, **kwargs)

        monkeypatch.setattr("neuro_voice.mind.store.sqlite3.connect", connect)

        rows = store.search_episodes(
            "connect race safety target", limit=8, persona_id="b",
        )

        assert swapped is True
        assert memory_id in {row["id"] for row in rows}
        assert hashlib.sha256(external.read_bytes()).hexdigest() == before
        assert not os.path.samefile(external, store._search_path)
    finally:
        store.close()


def test_symlinked_sidecar_is_replaced_without_modifying_its_target(tmp_path):
    path = tmp_path / "mind.db"
    store = MemoryStore(path)
    try:
        memory_id = store.add_episode(
            "symlink safety target", persona_id="b", scope="persona_private",
        )
        external = tmp_path / "other.db"
        with sqlite3.connect(external) as conn:
            conn.execute("CREATE TABLE protected(value TEXT)")
            conn.execute("INSERT INTO protected VALUES('unchanged')")
            conn.commit()
        try:
            store._search_path.symlink_to(external)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege is unavailable")
            raise
        before = hashlib.sha256(external.read_bytes()).hexdigest()

        rows = store.search_episodes(
            "symlink safety target", limit=8, persona_id="b",
        )

        assert memory_id in {row["id"] for row in rows}
        assert hashlib.sha256(external.read_bytes()).hexdigest() == before
        assert not store._search_path.is_symlink()
    finally:
        store.close()


def test_search_sidecar_path_escape_is_rejected_before_open(tmp_path):
    store = MemoryStore(tmp_path / "inside" / "mind.db")
    try:
        store._search_path = tmp_path / "outside.search-index"
        with pytest.raises(RuntimeError, match="search index path escapes"):
            store._prepare_search_index_path()
    finally:
        store.close()


def test_backup_failure_is_not_recorded_as_success(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = MemoryStore(data_dir / "mind_b.db")
    store.add_episode("retained", persona_id="b", scope="persona_private")
    store.close()
    manager = BackupManager(data_dir)

    def fail_copy(_src, _dst):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(manager, "_copy_sqlite", fail_copy)
    with pytest.raises(OSError, match="synthetic backup failure"):
        manager.create_backup(reason="manual")

    assert manager._last_signature is None
    assert manager.list_backups() == []
    assert list((data_dir / "backups").glob("*.tmp")) == []


def test_mind_constructs_both_persona_stores_with_the_same_policy_owner():
    source = (Path("neuro_voice/mind/mind.py")).read_text(encoding="utf-8")
    assert source.count("self._open_persona_store(") == 2
    assert source.count("retention_policy=RetentionPolicy.from_config") == 1


def test_synthetic_benchmark_reports_bounded_counts_without_production_data():
    from tools.benchmark_memory_retention import run_benchmark

    report = run_benchmark(counts=(1000,), rounds=3)

    assert report["synthetic"] is True
    assert report["counts"] == [1000]
    assert report["rounds"] == 3
    assert "OS RSS" in report["ram_measurement_note"]
    assert "SQLite" in report["ram_measurement_note"]
    assert "only 3 query rounds" in report["percentile_note"]
    assert "only 9 query rounds" not in report["percentile_note"]
    assert "exploratory" in report["percentile_note"].lower()
    sample = report["samples"][0]
    assert sample["fast_target_cosine"] == .99985
    assert sample["full_target_cosine"] == .90
    assert sample["fast_semantic_path"] == "Mind._recall_semantic"
    assert sample["full_semantic_path"] == "Mind._recall_semantic"
    assert sample["fast_probe_tier"] == "4-table-hamming-1"
    assert sample["full_probe_tier"] == "14-table-hamming-2"
    assert sample["fast_probe_tier"] != sample["full_probe_tier"]
    assert sample["ram_measurement_scope"] == "dataset_build_index_and_queries"
    assert sample["source_rows_before"] == sample["source_rows_after"] == 1000
    assert sample["transcript_rows_before"] == sample["transcript_rows_after"] == 1000
    assert sample["lexical_candidate_limit"] == 40
    assert sample["lexical_candidate_count"] <= 40
    for tier in ("fast", "full"):
        assert sample[f"{tier}_memory_candidate_limit"] == 80
        assert 1 <= sample[f"{tier}_memory_candidate_count"] <= 80
        assert sample[f"{tier}_transcript_candidate_limit"] == 40
        assert 1 <= sample[f"{tier}_transcript_candidate_count"] <= 40
        assert sample[f"{tier}_final_memory_limit"] == 5
        assert 1 <= sample[f"{tier}_final_memory_count"] <= 5
        assert sample[f"{tier}_final_transcript_limit"] == 5
        assert 1 <= sample[f"{tier}_final_transcript_count"] <= 5
    assert sample["query_p50_ms"] >= 0
    assert sample["query_p95_ms"] >= sample["query_p50_ms"]
    for tier in ("fast", "full"):
        assert sample[f"{tier}_semantic_query_p50_ms"] >= 0
        assert (
            sample[f"{tier}_semantic_query_p95_ms"]
            >= sample[f"{tier}_semantic_query_p50_ms"]
        )
        assert sample[f"{tier}_transcript_query_p50_ms"] >= 0
        assert (
            sample[f"{tier}_transcript_query_p95_ms"]
            >= sample[f"{tier}_transcript_query_p50_ms"]
        )
    assert "semantic_query_p95_ms" not in sample
    assert "transcript_query_p95_ms" not in sample
    assert sample["startup_ms"] >= 0
    assert sample["index_rebuilds_during_recall"] == 0
    assert sample["index_rebuilds_after_touch_restart"] == 0
    assert sample["peak_ram_bytes"] >= 0
    assert sample["index_bytes"] >= 0
