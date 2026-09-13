"""Phase 6G: 落ちても続けられるようにし、同じ話を2回渡さない。

Phase 6F で移行を人が操作できるようにしたが、3つ残った:

* UI が関係値を**直接** legacy から読んでいた（`person_primary` で画面だけ古い）
* 作業の状態が**メモリにしか無く**、落ちると次に何をすべきか分からない
* 人物単位で引くと、**同じ話が2回** Planner へ渡る

いちばん守りたいのは4つ:

* **レコードを消さない**（渡す直前に絞るだけ）
* **勝手に再開しない**（中断は人に選ばせる）
* **中断を無視して新しい作業を始めない**
* **本人の発言と推測を1つにしない**
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.dedup import (
    TYPE_PRIORITY, deduplicate, fingerprint, normalise_text,
)
from neuro_voice.cognition.episodic import EpisodicMemory, MemoryStatus
from neuro_voice.cognition.identity import IdentityResolver, IdentityType
from neuro_voice.cognition.types import InformationType
from neuro_voice.mind.migration import (
    PROCESS_ID, IdentityMigration, JobStatus, MigrationJobRunner, MigrationMode,
    RelationshipResolver, job_from_row,
)
from neuro_voice.mind.relationship import RelationshipStore


class Cfg:
    def __init__(self, **values):
        self._values = {"mind.relationship.enabled": True,
                        "mind.relationship.max_change_per_session": .50, **values}

    def get(self, key, default=None):
        return self._values.get(key, default)


def store_at(tmp_path):
    from neuro_voice.mind.store import MemoryStore

    return MemoryStore(tmp_path / "mind.db")


def confirmed(identity, *, person_id="person:A", value="voice:3"):
    return identity.link(person_id=person_id, identity_type=IdentityType.VOICE,
                         value=value, confidence=.95, event_id="probe",
                         authoritative=True)


def runner_at(tmp_path, journal, keys=("speaker:3",)):
    cfg = Cfg()
    legacy = RelationshipStore(tmp_path / "legacy.json", cfg)
    person = RelationshipStore(tmp_path / "person.json", cfg)
    identity = IdentityResolver()
    for key in keys:
        legacy.import_state(key, {"trust": .82}, interaction_count=4)
        confirmed(identity, value=key.replace("speaker:", "voice:"))
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    migration = IdentityMigration(resolver, journal=journal)
    return MigrationJobRunner(migration, store=journal), migration


# ===========================================================================
# 1. UI の差し替え
# ===========================================================================


def test_the_ui_no_longer_reads_the_legacy_store_directly():
    """**`person_primary` で画面だけ古い値**、が起きないこと。"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    start = text.index("def status(self) -> dict[str, Any]:")
    body = text[start:start + 3000]
    assert '_relationships.snapshot(f"speaker:' not in body, "直読みが残っている"
    assert "_speaker_relationship_snapshot(" in body


def test_the_display_goes_through_the_resolver():
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    start = text.index("def _speaker_relationship_snapshot")
    body = text[start:start + 1400]
    assert "relationship_resolver()" in body
    assert "resolver.snapshot(" in body


def test_the_compatible_method_is_kept():
    """**互換のため消さない。**"""
    from neuro_voice.mind.mind import Mind

    assert hasattr(Mind, "relationship_status")
    assert hasattr(Mind, "speaker_status_rows")


def test_the_display_does_not_change_any_state():
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    start = text.index("def speaker_status_rows")
    body = text[start:start + 1600]
    for banned in ("apply_state_delta", "observe(", "link(", "revoke("):
        assert banned not in body, banned


def test_the_ui_shows_the_identity_beside_each_speaker():
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    assert 'sp["identity"]' in text


# ===========================================================================
# 2. Job の永続化
# ===========================================================================


def test_a_job_is_written_to_the_database(tmp_path):
    journal = store_at(tmp_path)
    runner, _migration = runner_at(tmp_path, journal)
    job = runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    rows = journal.migration_jobs()
    assert rows and rows[0]["job_id"] == job.job_id
    assert rows[0]["status"] in {"completed", "completed_with_conflicts"}
    journal.close()


def test_the_job_row_holds_no_secrets(tmp_path):
    """**声紋も記憶本文も関係値も入れない**（第12条）。"""
    import json

    journal = store_at(tmp_path)
    runner, _migration = runner_at(tmp_path, journal)
    runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    rows = journal.migration_jobs()
    # 列名ではなく**中身**を見る。関係値も記憶本文も声紋も入っていないこと。
    allowed = {"job_id", "operation", "requested_mode", "previous_mode", "status",
               "progress_cursor", "processed_count", "success_count",
               "conflict_count", "failure_count", "process_id", "created_at",
               "started_at", "updated_at", "completed_at", "error_summary"}
    assert set(rows[0]) <= allowed, set(rows[0]) - allowed
    text = json.dumps([{k: v for k, v in row.items()} for row in rows],
                      ensure_ascii=False)
    for banned in ("ダイヤ", "0.82", "voiceprint", "embedding"):
        assert banned not in text, banned
    journal.close()


def test_progress_is_written_as_it_goes(tmp_path):
    """**1件ごとに残す。** 途中で落ちても、どこまで進んだかが分かる。"""
    journal = store_at(tmp_path)
    keys = tuple(f"speaker:{index}" for index in range(1, 5))
    runner, _migration = runner_at(tmp_path, journal, keys)
    runner.start("migrate", list(keys))
    runner.wait(5)
    row = journal.migration_jobs()[0]
    assert row["progress_cursor"] == keys[-1]
    assert row["processed_count"] == len(keys)
    journal.close()


# ===========================================================================
# 3. 起動時の復旧
# ===========================================================================


def test_a_crashed_job_is_found_at_startup(tmp_path):
    """前回のプロセスが残した `RUNNING` を、中断として拾う。"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "progress_cursor": "speaker:3", "processed_count": 50,
        "process_id": "前回の起動ID", "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)

    found = runner.recover()
    assert found is not None
    assert found.job_id == "old"
    assert str(found.status) == str(JobStatus.INTERRUPTED_RECOVERABLE)
    assert found.processed_count == 50
    journal.close()


def test_a_job_from_this_process_is_left_alone(tmp_path):
    """**同じプロセスで走っているものを中断扱いにしない。**"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "mine", "operation": "migrate", "status": "running",
        "process_id": PROCESS_ID, "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)
    assert runner.recover() is None
    journal.close()


def test_recovery_does_not_resume_by_itself(tmp_path):
    """**勝手に再開しない。** 続けるかどうかは人が決める。"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "processed_count": 2, "process_id": "前回", "created_at": 1000.0})
    runner, migration = runner_at(tmp_path, journal)
    runner.recover()
    assert not runner.busy
    assert not migration.counters["applied"], "勝手に移行している"
    journal.close()


def test_recovery_never_promotes_to_person_primary(tmp_path):
    """**起動しただけで正式採用にしない。**"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "person_primary", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, migration = runner_at(tmp_path, journal)
    runner.recover()
    assert migration._resolver.mode is MigrationMode.DUAL_WRITE
    journal.close()


def test_a_recovered_job_is_reported(tmp_path):
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "processed_count": 7, "progress_cursor": "speaker:7",
        "process_id": "前回", "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)
    runner.recover()
    status = runner.status()
    assert status["recovered"]["job_id"] == "old"
    assert status["recovered"]["resumable"] is True
    assert status["blocked_by"]["job_id"] == "old"
    journal.close()


# ===========================================================================
# 4. Resume
# ===========================================================================


def test_resume_skips_what_is_already_done(tmp_path):
    """**完了済みを飛ばす。** 51件目から。"""
    journal = store_at(tmp_path)
    keys = [f"speaker:{index}" for index in range(1, 6)]
    runner, migration = runner_at(tmp_path, journal, tuple(keys))
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "progress_cursor": "speaker:2", "processed_count": 2,
        "process_id": "前回", "created_at": 1000.0})
    runner.recover()

    seen: list[str] = []
    original = migration.migrate_speaker
    migration.migrate_speaker = lambda key: (seen.append(key) or original(key))
    runner.resume(keys)
    runner.wait(5)

    assert seen == ["speaker:3", "speaker:4", "speaker:5"], seen
    assert runner.current.processed_count == 5
    journal.close()


def test_resume_does_not_apply_twice(tmp_path):
    journal = store_at(tmp_path)
    cfg = Cfg()
    legacy = RelationshipStore(tmp_path / "legacy.json", cfg)
    person = RelationshipStore(tmp_path / "person.json", cfg)
    identity = IdentityResolver()
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    confirmed(identity, value="voice:3")
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    migration = IdentityMigration(resolver, journal=journal)
    runner = MigrationJobRunner(migration, store=journal)

    runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    after_first = person.get("person:A").trust
    # 落ちたことにして、もう一度。
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "processed_count": 0, "process_id": "前回", "created_at": 900.0})
    runner.recover()
    runner.resume(["speaker:3"])
    runner.wait(5)
    assert abs(person.get("person:A").trust - after_first) < .0001, "二重適用"
    journal.close()


def test_the_start_kind_is_tracked(tmp_path):
    journal = store_at(tmp_path)
    runner, _migration = runner_at(tmp_path, journal)
    fresh = runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    assert fresh.start_kind == "new"

    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 900.0})
    runner.recover()
    resumed = runner.resume(["speaker:3"])
    runner.wait(5)
    assert resumed.start_kind == "resume"
    journal.close()


def test_resume_without_an_interrupted_job_does_nothing(tmp_path):
    journal = store_at(tmp_path)
    runner, _migration = runner_at(tmp_path, journal)
    assert runner.resume(["speaker:3"]) is None
    journal.close()


# ===========================================================================
# 5. DBを使った排他
# ===========================================================================


def test_an_interrupted_job_blocks_new_work(tmp_path):
    """**中断を無視して新しい作業を始めない。**"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)
    runner.recover()

    same = runner.start("migrate", ["speaker:3"])
    assert same.job_id == "old", "中断中なのに新しい作業を始めている"
    journal.close()


def test_a_running_job_blocks_new_work(tmp_path):
    journal = store_at(tmp_path)
    runner, _migration = runner_at(tmp_path, journal)
    first = runner.start("migrate", ["speaker:3"])
    second = runner.start("migrate", ["speaker:3"])
    assert first.job_id == second.job_id
    runner.wait(5)
    journal.close()


def test_rollback_gives_up_the_interrupted_job(tmp_path):
    """戻す時は諦める。**残すと「途中で止まっている」と言い続ける。**"""
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)
    runner.recover()
    cancelled = runner.cancel_recovered(reason="legacy_rollback")
    assert str(cancelled.status) == str(JobStatus.CANCELLED)
    assert runner.blocked is None
    journal.close()


def test_a_job_row_round_trips():
    from neuro_voice.mind.migration import MigrationJob

    job = MigrationJob(operation="migrate", progress_cursor="speaker:7",
                       processed_count=7, success_count=6, conflict_count=1)
    restored = job_from_row(job.row())
    assert restored.job_id == job.job_id
    assert restored.progress_cursor == "speaker:7"
    assert restored.processed_count == 7
    assert restored.conflict_count == 1


# ===========================================================================
# 6. Mind から見た復旧
# ===========================================================================


class FakeMind:
    """`request_migration` の判断だけを確かめるための最小の殻。"""

    def __init__(self, runner, migration, resolver):
        from neuro_voice.mind.mind import Mind

        self._real = Mind.__new__(Mind)
        self._real._migration_jobs = runner
        self._real._identity_migration = migration
        self._real._relationship_resolver = resolver
        self._real._identity_runtime = None
        self._real._cfg = Cfg()
        self._real._relationships = resolver.legacy
        self._real.migration_speaker_keys = lambda: ["speaker:3"]
        self._real.relationship_resolver = lambda: resolver
        self._real.identity_migration = lambda: migration
        self._real.migration_job_runner = lambda: runner
        self._real._ensure_person_store = lambda: resolver

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_backend_refuses_new_work_while_interrupted(tmp_path):
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, migration = runner_at(tmp_path, journal)
    runner.recover()
    mind = FakeMind(runner, migration, migration._resolver)

    result = mind.request_migration("migrate", confirmed=True)
    assert result["ok"] is False
    assert result["reason"] == "interrupted_job_pending"
    journal.close()


def test_the_backend_allows_rollback_while_interrupted(tmp_path):
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, migration = runner_at(tmp_path, journal)
    runner.recover()
    mind = FakeMind(runner, migration, migration._resolver)

    result = mind.request_migration("legacy_rollback", confirmed=True)
    assert result["ok"] is True
    assert result["mode"] == "legacy_rollback"
    assert runner.blocked is None
    journal.close()


def test_resume_needs_confirmation(tmp_path):
    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "process_id": "前回", "created_at": 1000.0})
    runner, migration = runner_at(tmp_path, journal)
    runner.recover()
    mind = FakeMind(runner, migration, migration._resolver)
    assert mind.request_migration("resume")["reason"] == "needs_confirmation"
    journal.close()


# ===========================================================================
# 7. Content Fingerprint
# ===========================================================================


def memory(memory_id, summary, kind=InformationType.USER_STATEMENT, **kwargs):
    return EpisodicMemory(memory_id=memory_id, summary=summary,
                          information_type=kind, **kwargs)


@pytest.mark.parametrize("left,right", [
    ("ダイヤを掘った", "ダイヤを掘った。"),
    ("ダイヤを掘った", " ダイヤを掘った  "),
    ("Minecraft をやった", "minecraft をやった"),
    ("ダイヤ を  掘った", "ダイヤ を 掘った"),
    ("ダイヤを掘った", "ダイヤを掘った！"),
])
def test_harmless_differences_are_normalised_away(left, right):
    assert normalise_text(left) == normalise_text(right)


def test_meaningful_differences_survive():
    assert normalise_text("ダイヤを掘った") != normalise_text("鉄を掘った")
    # **日本語の表記は触らない。** 勝手に寄せない。
    assert normalise_text("ダイヤ") != normalise_text("だいや")


def test_the_same_content_gets_the_same_fingerprint():
    assert fingerprint(memory(1, "ダイヤを掘った")) == fingerprint(
        memory(2, "ダイヤを掘った。"))


def test_a_different_source_gets_a_different_fingerprint():
    """**本人が言ったことと、こちらの推測は別。**"""
    said = memory(1, "ダイヤを掘った", InformationType.USER_STATEMENT)
    guessed = memory(2, "ダイヤを掘った", InformationType.INFERENCE)
    assert fingerprint(said) != fingerprint(guessed)


def test_an_empty_summary_has_no_fingerprint():
    assert fingerprint(memory(1, "")) == ""


def test_the_fingerprint_is_deterministic():
    first = fingerprint(memory(1, "ダイヤを掘った"))
    for _ in range(20):
        assert fingerprint(memory(1, "ダイヤを掘った")) == first


# ===========================================================================
# 8. 重複排除
# ===========================================================================


def test_the_same_memory_id_is_returned_once():
    same = memory(1, "ダイヤを掘った")
    result = deduplicate([same, same, same])
    assert result.count_after == 1
    assert result.id_duplicates_removed == 2


def test_the_same_content_with_different_ids_is_returned_once():
    """**別レコードだが同じ話。** 1件だけ渡す。"""
    result = deduplicate([memory(1, "ダイヤを掘った", created_at=1000.0),
                          memory(2, "ダイヤを掘った。", created_at=900.0)])
    assert result.count_after == 1
    assert result.fingerprint_duplicates_removed == 1
    # 新しく確かめられた方を残す。落とした方はIDだけ残す。
    assert result.memories[0].memory_id == 1
    assert result.duplicate_memory_ids == [2]


def test_the_original_records_are_untouched():
    """**物理削除しない。** 渡す直前に絞るだけ。"""
    rows = [memory(1, "ダイヤを掘った"), memory(2, "ダイヤを掘った。")]
    before = [item.memory_id for item in rows]
    deduplicate(rows)
    assert [item.memory_id for item in rows] == before
    assert len(rows) == 2


def test_a_statement_and_an_inference_are_both_kept():
    """**似ていても出典が違えば統合しない。**"""
    result = deduplicate([
        memory(1, "ダイヤを掘った", InformationType.USER_STATEMENT),
        memory(2, "ダイヤを掘った", InformationType.INFERENCE)])
    assert result.count_after == 2
    assert result.fingerprint_duplicates_removed == 0


def test_the_user_statement_wins_over_the_others():
    """**本人が言ったことを残す。** 推測が押しのけない。"""
    result = deduplicate([
        memory(1, "ダイヤを掘った", InformationType.OBSERVATION),
        memory(2, "ダイヤを掘った", InformationType.OBSERVATION,
               confidence=.9)])
    assert result.memories[0].memory_id == 2
    assert TYPE_PRIORITY[str(InformationType.USER_STATEMENT)] < TYPE_PRIORITY[
        str(InformationType.INFERENCE)]


def test_a_contradicted_memory_loses():
    result = deduplicate([
        memory(1, "ダイヤを掘った", confidence=.9,
               status=MemoryStatus.CONTRADICTED),
        memory(2, "ダイヤを掘った", confidence=.5)])
    assert result.memories[0].memory_id == 2


def test_the_more_confident_one_wins():
    result = deduplicate([memory(1, "ダイヤを掘った", confidence=.4),
                          memory(2, "ダイヤを掘った", confidence=.9)])
    assert result.memories[0].memory_id == 2


def test_the_order_is_kept():
    """順位付けの並びを崩さない。"""
    rows = [memory(index, f"話{index}") for index in range(1, 6)]
    assert [item.memory_id
            for item in deduplicate(rows).memories] == [1, 2, 3, 4, 5]


def test_near_duplicates_are_flagged_not_removed():
    """**似ているだけのものを勝手に消さない。**"""
    result = deduplicate([
        memory(1, "きのうダイヤを10個掘ってきたよ"),
        memory(2, "きのうダイヤを10個掘ってきたね")])
    assert result.count_after == 2, "近いだけで落としている"
    assert result.possible_near_duplicates


def test_the_snapshot_holds_no_text():
    import json

    result = deduplicate([memory(1, "口座番号は1234-5678"),
                          memory(2, "口座番号は1234-5678。")])
    assert "1234-5678" not in json.dumps(result.snapshot(), ensure_ascii=False)


def test_no_llm_is_called():
    """**毎回の重複判定にモデルを使わない。**"""
    import inspect

    from neuro_voice.cognition import dedup as module

    source = inspect.getsource(module)
    # 説明文でLLMに触れるのは構わない。**呼んでいないこと**を見る。
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith(("#", "*", '"""')))
    for banned in ("ollama", "openai", "generate(", "embed(", "await "):
        assert banned not in code.lower(), banned
    assert "hashlib" in code, "決定的なハッシュを使っていない"


# ===========================================================================
# 9. 検索経路での重複排除
# ===========================================================================


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def episodes(self, limit=50, speaker_key=""):
        found = [row for row in self.rows
                 if not speaker_key or row["speaker_key"] == speaker_key]
        return found[:limit]

    def touch(self, ids):
        return None


def row(memory_id, speaker_key, summary="ダイヤを掘った話", kind="user_statement"):
    return {
        "id": memory_id, "kind": "episode", "text": summary,
        "speaker_key": speaker_key, "importance": .8, "confidence": .9,
        "created_at": 1000.0 + memory_id, "last_used_at": 0.0,
        "meta": ('{"participant_ids": ["' + speaker_key + '"],'
                 ' "information_type": "' + kind + '"}'),
    }


def engine_with(rows, **cfg):
    from neuro_voice.mind.episodes import EpisodicMemoryService

    settings = Cfg(**{"memory.episodic_enabled": True,
                      "memory.retrieval_enabled": True, **cfg})
    return EpisodicMemoryService(FakeStore(rows), settings)


def scope_for(identity, speaker="speaker:3"):
    from neuro_voice.mind.migration import ParticipantIdentityRef, identity_scope

    return identity_scope(ParticipantIdentityRef(
        person_id="person:A", speaker_id=speaker,
        resolution_status="confirmed", confidence=.95), identity)


def test_the_same_story_from_two_speakers_is_passed_once():
    """**Discord と Local で覚えた同じ話**を2回渡さない。"""
    engine = engine_with([row(1, "speaker:3", "ダイヤを10個掘った"),
                          row(2, "speaker:7", "ダイヤを10個掘った。")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=scope_for(identity), scope_mode="person")
    assert result.dedup.fingerprint_duplicates_removed == 1
    assert len({item.memory.memory_id for item in result.scored}) == len(result.scored)


def test_the_dedup_result_is_reported():
    engine = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=scope_for(identity), scope_mode="person")
    assert result.dedup is not None
    assert result.dedup.count_before >= result.dedup.count_after


def test_the_budget_still_holds_after_dedup():
    engine = engine_with(
        [row(index, f"speaker:{3 if index % 2 else 7}", f"話{index}")
         for index in range(1, 20)], **{"memory.max_retrieved": 3})
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきの話", speaker_key="speaker:3",
                             scope=scope_for(identity), scope_mode="person")
    assert len(result.scored) <= 3


def test_dedup_also_runs_in_legacy_mode():
    """**人物単位でなくても効く。** 同じ話は1件。"""
    engine = engine_with([row(1, "speaker:3", "ダイヤを掘った"),
                          row(2, "speaker:3", "ダイヤを掘った。")])
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3")
    assert result.dedup.fingerprint_duplicates_removed == 1


# ===========================================================================
# 10. Trace
# ===========================================================================


def test_the_trace_has_the_new_fields():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    for name in ("speaker_status_ui_source", "migration_job_id",
                 "migration_job_persisted", "migration_job_recovered",
                 "migration_job_resume_from_cursor",
                 "migration_job_recovery_action",
                 "memory_result_count_before_dedup",
                 "memory_id_duplicates_removed",
                 "memory_fingerprint_duplicates_removed",
                 "memory_result_count_after_dedup", "duplicate_memory_ids"):
        assert hasattr(trace, name), name


def test_the_trace_records_the_dedup():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    trace.note_dedup(deduplicate([memory(1, "ダイヤを掘った", created_at=1000.0),
                                  memory(2, "ダイヤを掘った。", created_at=900.0)]))
    assert trace.memory_result_count_before_dedup == 2
    assert trace.memory_result_count_after_dedup == 1
    assert trace.memory_fingerprint_duplicates_removed == 1
    assert trace.duplicate_memory_ids == [2]


def test_the_trace_records_the_recovery(tmp_path):
    from neuro_voice.cognition.trace import CognitiveTrace

    journal = store_at(tmp_path)
    journal.save_migration_job({
        "job_id": "old", "operation": "migrate", "status": "running",
        "progress_cursor": "speaker:7", "process_id": "前回",
        "created_at": 1000.0})
    runner, _migration = runner_at(tmp_path, journal)
    job = runner.recover()
    trace = CognitiveTrace()
    trace.note_job(job, persisted=True, recovered=True, action="resume")
    assert trace.migration_job_recovered is True
    assert trace.migration_job_resume_from_cursor == "speaker:7"
    assert trace.migration_job_recovery_action == "resume"
    journal.close()


def test_the_trace_keeps_ids_not_text():
    """**落とした記憶のIDだけ。本文は入れない**（第12条）。"""
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    trace.note_dedup(deduplicate([memory(1, "口座番号は1234-5678"),
                                  memory(2, "口座番号は1234-5678。")]))
    text = json.dumps(trace.snapshot(), ensure_ascii=False)
    assert "1234-5678" not in text
    assert trace.snapshot()["scope"]["dedup"]["duplicate_ids"]


# ===========================================================================
# 11. 配線診断
# ===========================================================================


def yaml_config():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))

    class YamlCfg:
        def get(self, key, default=None):
            current = data
            for part in str(key).split("."):
                if not isinstance(current, dict):
                    return default
                current = current.get(part)
                if current is None:
                    return default
            return current

    return YamlCfg()


def test_nothing_needs_attention():
    from neuro_voice.diagnostics.wiring import run_all

    report = run_all(yaml_config())
    assert report.snapshot()["attention"] == [], report.snapshot()["attention"]


def test_the_dedup_probe_is_registered():
    from neuro_voice.diagnostics.wiring import run_all

    keys = {item.probe.key for item in run_all(yaml_config()).results}
    assert "memory.dedup" in keys
    assert "migration.recovery" in keys


def test_merging_across_sources_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.dedup as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    # 出典を無視した指紋にする。
    monkeypatch.setattr(module, "fingerprint",
                        lambda memory: module.normalise_text(
                            getattr(memory, "summary", "")))
    report = run_all(yaml_config(), keys=("memory.dedup",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_auto_resume_turns_the_probe_red(monkeypatch):
    import neuro_voice.mind.migration as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.MigrationJobRunner.recover

    def eager(self):
        job = original(self)
        if job is not None:
            self.current = job
            job.status = module.JobStatus.RUNNING
        return job

    monkeypatch.setattr(module.MigrationJobRunner, "recover", eager)
    report = run_all(yaml_config(), keys=("migration.recovery",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


# ===========================================================================
# 12. UI
# ===========================================================================


def ui_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "ui"
            / "assets" / "index.html").read_text(encoding="utf-8")


def test_the_ui_reports_an_interrupted_migration():
    text = ui_source()
    assert "前回の移行が中断された" in text
    assert "移行の記録は残っている" in text


def test_the_ui_offers_only_resume_and_rollback_while_interrupted():
    text = ui_source()
    start = text.index("const stuck =")
    body = text[start:start + 1200]
    assert '"中断した移行を再開"' in body
    assert '"従来へ戻す"' in body
    # 中断中は新しい作業を出さない。
    assert body.index("if (stuck)") < body.index("dry_run")


def test_the_probes_do_not_touch_the_disk():
    """**3秒ごとに回る点検で、一時ファイルを作らない。**

    Phase 6G で `migration.recovery` を足した時、一時ディレクトリと
    SQLite ファイルを毎回作っていて、点検全体が 291ms になった
    （上限 250ms）。点検が会話の邪魔をしては本末転倒。
    """
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "diagnostics"
            / "wiring.py").read_text(encoding="utf-8")
    for key in ("_migration_recovery", "_world_persistence"):
        start = text.index(f"def {key}")
        body = text[start:start + 3000]
        assert 'MemoryStore(":memory:")' in body, key


def test_the_sweep_stays_within_its_budget():
    from neuro_voice.diagnostics.wiring import run_all

    run_all(yaml_config())          # import の分を先に払う
    report = run_all(yaml_config())
    assert report.total_ms < 250, f"{report.total_ms:.1f}ms"
