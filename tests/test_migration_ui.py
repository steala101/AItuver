"""Phase 6F: 移行を人が操作できるようにし、過去の記憶を人物単位で引く。

移行の仕組み自体は Phase 6E で作った。だが**コードからしか動かせず、
`person_primary` にしても過去の記憶は `speaker_id` のままだった**
——Discord と Local で同じ人と話しても、記憶が繋がらない。

いちばん守りたいのは4つ:

* **UIのボタンを隠すだけにしない**（判断はバックエンドがやり直す）
* **移行で会話を止めない**（別スレッド、1つずつ）
* **記憶を物理移行しない**（検索側で束ねるだけ）
* **取り消したリンクの記憶を、その人のものとして語らない**
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.identity import IdentityResolver, IdentityType
from neuro_voice.mind.migration import (
    ALLOWED_TRANSITIONS, MIN_SHADOW_READS, IdentityMigration, JobStatus,
    MigrationJobRunner, MigrationMode, MigrationStatus, ParticipantIdentityRef,
    RelationshipResolver, identity_scope,
)
from neuro_voice.mind.relationship import RelationshipStore


class Cfg:
    def __init__(self, **values):
        self._values = {"mind.relationship.enabled": True,
                        "mind.relationship.max_change_per_session": .50, **values}

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture()
def stores(tmp_path):
    cfg = Cfg()
    return (RelationshipStore(tmp_path / "legacy.json", cfg),
            RelationshipStore(tmp_path / "person.json", cfg))


def confirmed(identity, *, person_id="person:A", value="voice:3"):
    return identity.link(person_id=person_id, identity_type=IdentityType.VOICE,
                         value=value, confidence=.95, event_id="probe",
                         authoritative=True)


def ref(*, person_id="person:A", speaker_id="speaker:3", status="confirmed"):
    return ParticipantIdentityRef(person_id=person_id, speaker_id=speaker_id,
                                  resolution_status=status, confidence=.95)


def migration_at(stores, mode=MigrationMode.DISABLED, identity=None):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person,
                                    resolver=identity or IdentityResolver(),
                                    mode=mode)
    return resolver, IdentityMigration(resolver)


# ===========================================================================
# 1. 状態遷移
# ===========================================================================


@pytest.mark.parametrize("current,target", [
    (MigrationMode.DISABLED, MigrationMode.PERSON_PRIMARY),
    (MigrationMode.DISABLED, MigrationMode.DUAL_WRITE),
    (MigrationMode.SHADOW_READ, MigrationMode.PERSON_PRIMARY),
    (MigrationMode.LEGACY_ROLLBACK, MigrationMode.PERSON_PRIMARY),
])
def test_skipping_a_stage_is_refused(stores, current, target):
    """**飛び越しを拒む。** 差を一度も見ないまま正式採用にしない。"""
    _resolver, migration = migration_at(stores, current)
    verdict = migration.can_transition(target)
    assert not verdict.allowed
    assert verdict.reason.startswith("transition_not_allowed")


def test_the_ladder_goes_up_one_step(stores):
    resolver, migration = migration_at(stores)
    assert migration.can_transition(MigrationMode.SHADOW_READ).allowed
    resolver.set_mode(MigrationMode.SHADOW_READ)
    resolver.counters["shadow_compared"] = MIN_SHADOW_READS
    assert migration.can_transition(MigrationMode.DUAL_WRITE).allowed


def test_going_back_is_always_allowed(stores):
    """**逃げ道を条件付きにしない。**"""
    for mode in (MigrationMode.SHADOW_READ, MigrationMode.DUAL_WRITE,
                 MigrationMode.PERSON_PRIMARY):
        resolver, migration = migration_at(stores, mode)
        del resolver
        verdict = migration.can_transition(MigrationMode.LEGACY_ROLLBACK)
        assert verdict.allowed, mode


def test_the_transition_table_has_no_shortcuts():
    assert MigrationMode.PERSON_PRIMARY not in ALLOWED_TRANSITIONS[
        MigrationMode.DISABLED]
    assert MigrationMode.PERSON_PRIMARY not in ALLOWED_TRANSITIONS[
        MigrationMode.SHADOW_READ]
    assert MigrationMode.PERSON_PRIMARY in ALLOWED_TRANSITIONS[
        MigrationMode.DUAL_WRITE]


# ===========================================================================
# 2. 昇格の条件
# ===========================================================================


def test_dual_write_needs_enough_shadow_reads(stores):
    """**一度見ただけでは足りない。**"""
    resolver, migration = migration_at(stores, MigrationMode.SHADOW_READ)
    resolver.counters["shadow_compared"] = 1
    verdict = migration.can_transition(MigrationMode.DUAL_WRITE)
    assert not verdict.allowed
    assert any("shadow_reads" in item for item in verdict.blockers)


def test_person_primary_is_refused_while_writes_are_failing(stores):
    """**部分的失敗が残っているうちは昇格しない。**"""
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    resolver.counters["writes_person"] = 10
    resolver.counters["write_person_failed"] = 2
    verdict = migration.can_transition(MigrationMode.PERSON_PRIMARY)
    assert not verdict.allowed
    assert any("unresolved_write_failures" in item for item in verdict.blockers)


def test_person_primary_is_refused_while_the_values_differ(stores):
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    resolver.counters["writes_person"] = 10
    resolver.last_difference = {"trust": .3}
    verdict = migration.can_transition(MigrationMode.PERSON_PRIMARY)
    assert not verdict.allowed
    assert any("shadow_difference" in item for item in verdict.blockers)


def test_person_primary_is_refused_before_dual_write_ever_ran(stores):
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    del resolver
    verdict = migration.can_transition(MigrationMode.PERSON_PRIMARY)
    assert "dual_write_never_ran" in verdict.blockers


def test_person_primary_passes_when_everything_is_clean(stores):
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    resolver.counters["writes_person"] = 10
    assert migration.can_transition(MigrationMode.PERSON_PRIMARY).allowed


def test_a_conflict_alone_does_not_block_promotion(stores):
    """競合対象を legacy へ隔離できるなら、全体の昇格は止めない。"""
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    resolver.counters["writes_person"] = 10
    identity = IdentityResolver()
    resolver.identity = identity
    for index in range(4):
        identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                      value="voice:9", confidence=.95, event_id=f"a{index}")
    identity.link(person_id="person:B", identity_type=IdentityType.VOICE,
                  value="voice:9", confidence=.95, event_id="b", authoritative=True)
    migration.migrate_speaker("speaker:9")
    assert migration.can_transition(MigrationMode.PERSON_PRIMARY).allowed


# ===========================================================================
# 3. DRY RUN
# ===========================================================================


def test_a_dry_run_changes_nothing(stores):
    """**モードもリレーションも Link も触らない。**"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    resolver, migration = migration_at(stores, MigrationMode.DISABLED, identity)

    before_mode = resolver.mode
    before_links = len(identity.links)
    before_person = person.get("person:A").trust
    result = migration.dry_run(["speaker:3"])

    assert resolver.mode is before_mode
    assert len(identity.links) == before_links
    assert abs(person.get("person:A").trust - before_person) < .0001
    assert not migration.records, "記録まで作っている"
    assert result.migratable_relationships == 1
    assert result.expected_creates == 1


def test_a_dry_run_reports_why_it_would_stop(stores):
    legacy, person = stores
    legacy.import_state("speaker:9", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                  value="voice:9", confidence=.95, event_id="e1")
    _resolver, migration = migration_at(stores, MigrationMode.DISABLED, identity)
    result = migration.dry_run(["speaker:9"])
    assert result.unresolved_speakers == 1
    assert any("no_confirmed_link" in item for item in result.blocked_reasons)
    del person


def test_a_dry_run_counts_updates_separately(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .82}, interaction_count=2)
    identity = IdentityResolver()
    confirmed(identity)
    _resolver, migration = migration_at(stores, MigrationMode.DISABLED, identity)
    result = migration.dry_run(["speaker:3"])
    assert result.expected_updates == 1
    assert result.expected_creates == 0


# ===========================================================================
# 4. Migration Job
# ===========================================================================


def test_the_job_runs_off_the_conversation_thread(stores):
    import threading

    legacy, _person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    _resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE, identity)
    runner = MigrationJobRunner(migration)
    here = threading.current_thread().ident
    seen: list[int] = []

    original = migration.migrate_speaker
    migration.migrate_speaker = lambda key: (
        seen.append(threading.current_thread().ident) or original(key))
    runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    assert seen and seen[0] != here, "会話のスレッドで走っている"


def test_a_double_click_does_not_start_two_jobs(stores):
    legacy, _person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    _resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE, identity)
    runner = MigrationJobRunner(migration)
    first = runner.start("migrate", ["speaker:3"])
    second = runner.start("migrate", ["speaker:3"])
    assert first.job_id == second.job_id
    runner.wait(5)
    assert len(runner.history) == 1


def test_the_job_reports_conflicts_without_failing(stores):
    legacy, _person = stores
    identity = IdentityResolver()
    for index in range(4):
        identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                      value="voice:9", confidence=.95, event_id=f"a{index}")
    identity.link(person_id="person:B", identity_type=IdentityType.VOICE,
                  value="voice:9", confidence=.95, event_id="b", authoritative=True)
    legacy.import_state("speaker:9", {"trust": .82}, interaction_count=4)
    _resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE, identity)
    runner = MigrationJobRunner(migration)
    runner.start("migrate", ["speaker:9"])
    runner.wait(5)
    assert str(runner.current.status) == str(JobStatus.COMPLETED_WITH_CONFLICTS)
    assert runner.current.conflict_count == 1


def test_the_job_status_survives_a_ui_reload(stores):
    """**UI を開き直しても状態を取り直せる。**"""
    legacy, _person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    _resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE, identity)
    runner = MigrationJobRunner(migration)
    job = runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    # UI が再読込しても、同じ runner から取り直せる。
    status = runner.status()
    assert status["current"]["job_id"] == job.job_id
    assert not status["busy"]


def test_a_failing_job_does_not_raise(stores):
    """**移行が転んでも会話は続く**（第17条）。"""
    _resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)

    def explode(key):
        raise RuntimeError("disk full")

    migration.migrate_speaker = explode
    runner = MigrationJobRunner(migration)
    runner.start("migrate", ["speaker:3"])
    runner.wait(5)
    assert str(runner.current.status) == str(JobStatus.FAILED)
    assert "disk full" in runner.current.error_summary


def test_resync_clears_the_failure_count_but_not_the_difference(stores):
    """**「再同期した」で食い違いを消さない。**"""
    resolver, migration = migration_at(stores, MigrationMode.DUAL_WRITE)
    resolver.counters["write_person_failed"] = 3
    resolver.last_difference = {"trust": .3}
    result = migration.resync()
    assert result["cleared_failures"] == 3
    assert result["remaining_difference"] == 1
    assert resolver.counters["write_person_failed"] == 0


# ===========================================================================
# 5. IdentityScope
# ===========================================================================


def test_an_unresolved_person_gets_only_its_own_speaker():
    """**人物が分からないまま範囲を広げない。**"""
    scope = identity_scope(ParticipantIdentityRef(speaker_id="speaker:3"))
    assert scope.search_keys == ("speaker:3",)
    assert not scope.resolved


def test_a_resolved_person_collects_every_confirmed_speaker():
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    scope = identity_scope(ref(), identity)
    assert set(scope.confirmed_speaker_ids) == {"speaker:3", "speaker:7"}
    assert scope.primary_speaker_id == "speaker:3"
    assert scope.search_keys[0] == "speaker:3", "いま話している声が先頭でない"


def test_a_candidate_link_is_not_in_scope():
    """**候補では人物の範囲に入れない。**"""
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                  value="voice:8", confidence=.95, event_id="e1")
    scope = identity_scope(ref(), identity)
    assert "speaker:8" not in scope.confirmed_speaker_ids


def test_a_revoked_link_is_excluded_and_recorded():
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    stale = confirmed(identity, value="voice:9")
    identity.revoke(stale.link_id, reason="別人だった")
    scope = identity_scope(ref(), identity)
    assert "speaker:9" not in scope.confirmed_speaker_ids
    assert "speaker:9" in scope.excluded_revoked_ids


def test_a_transport_link_is_kept_apart():
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    identity.link(person_id="person:A", identity_type=IdentityType.TRANSPORT,
                  value="discord:7", confidence=1.0, authoritative=True)
    scope = identity_scope(ref(), identity)
    assert scope.transport_identity_ids == ("discord:7",)
    assert "discord:7" not in scope.confirmed_speaker_ids


def test_the_scope_holds_no_names():
    import json

    identity = IdentityResolver()
    confirmed(identity)
    assert "ジーレン" not in json.dumps(
        identity_scope(ref(), identity).snapshot(), ensure_ascii=False)


# ===========================================================================
# 6. 人物単位の記憶検索
# ===========================================================================


class FakeStore:
    """`speaker_key` で絞る所だけを真似た記憶ストア。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    def episodes(self, limit=50, speaker_key=""):
        self.calls.append((speaker_key, limit))
        found = [row for row in self.rows
                 if not speaker_key or row["speaker_key"] == speaker_key]
        return found[:limit]

    def touch(self, ids):
        return None


def row(memory_id, speaker_key, summary="ダイヤを掘った話"):
    return {
        "id": memory_id, "kind": "episode", "text": summary,
        "speaker_key": speaker_key, "importance": .8, "confidence": .9,
        "created_at": 1000.0 + memory_id, "last_used_at": 0.0,
        "meta": '{"participant_ids": ["' + speaker_key + '"]}',
    }


def engine_with(rows, **cfg):
    from neuro_voice.mind.episodes import EpisodicMemoryService

    store = FakeStore(rows)
    settings = Cfg(**{"memory.episodic_enabled": True,
                      "memory.retrieval_enabled": True, **cfg})
    return EpisodicMemoryService(store, settings), store


def test_a_person_search_spans_every_confirmed_speaker():
    engine, store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    scope = identity_scope(ref(), identity)
    result = engine.retrieve("さっき言ってたダイヤの話だけど", speaker_key="speaker:3",
                             scope=scope, scope_mode="person")
    assert result.retrieval_mode == "person"
    assert set(result.scope_speaker_ids) == {"speaker:3", "speaker:7"}
    assert {key for key, _limit in store.calls} >= {"speaker:3", "speaker:7"}


def test_the_same_memory_is_not_counted_twice():
    shared = row(1, "speaker:3")
    engine, _store = engine_with([shared, row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    # 両方の鍵で同じ行が返るストア。
    engine._store.episodes = lambda limit=50, speaker_key="": [shared][:limit]
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="person")
    assert result.deduplicated_count >= 1


def test_the_search_budget_does_not_grow_with_the_speaker_count():
    """**人数倍に増やさない。** 予算は同じ。"""
    engine, store = engine_with(
        [row(index, f"speaker:{index}") for index in range(1, 9)],
        **{"memory.scan_limit": 40})
    identity = IdentityResolver()
    for index in (3, 4, 5, 6):
        confirmed(identity, value=f"voice:{index}")
    engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                    scope=identity_scope(ref(), identity), scope_mode="person")
    per_call = [limit for _key, limit in store.calls]
    assert max(per_call) <= 40
    assert sum(per_call) <= 40 + 8, "合計の読み込み量が予算を超えている"


def test_the_returned_count_stays_within_the_limit():
    engine, _store = engine_with(
        [row(index, "speaker:3") for index in range(1, 20)],
        **{"memory.max_retrieved": 3})
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="person")
    assert len(result.scored) <= 3


def test_an_unresolved_identity_searches_only_its_own_memories():
    engine, store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    scope = identity_scope(ParticipantIdentityRef(speaker_id="speaker:3"))
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=scope, scope_mode="person")
    assert result.retrieval_mode == "legacy"
    assert {key for key, _limit in store.calls} == {"speaker:3"}


def test_a_revoked_link_removes_its_memories_from_the_person_search():
    engine, store = engine_with([row(1, "speaker:3"), row(2, "speaker:9")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    stale = confirmed(identity, value="voice:9")
    identity.revoke(stale.link_id, reason="別人だった")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="person")
    assert "speaker:9" not in {key for key, _limit in store.calls}
    assert "speaker:9" in result.excluded_revoked_ids


def test_shadow_mode_keeps_the_legacy_result():
    """**正式な結果は legacy。** 差だけ測る。"""
    engine, _store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="shadow")
    assert result.retrieval_mode == "shadow"
    assert result.legacy_result_count == 1
    assert result.person_result_count == 2
    assert result.scope_difference == 1


def test_legacy_mode_never_widens():
    engine, store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                    scope=identity_scope(ref(), identity), scope_mode="legacy")
    assert {key for key, _limit in store.calls} == {"speaker:3"}


def test_the_scope_snapshot_has_no_memory_text():
    import json

    engine, _store = engine_with([row(1, "speaker:3", "口座番号は1234-5678")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="person")
    assert "1234-5678" not in json.dumps(result.scope_snapshot(), ensure_ascii=False)


# ===========================================================================
# 7. 記憶を移していないこと
# ===========================================================================


def test_no_memory_rows_are_rewritten():
    """**過去のレコードを書き換えない。** 検索側で束ねるだけ。"""
    import inspect

    from neuro_voice.mind import episodes as module

    source = inspect.getsource(module.EpisodicMemoryService._across)
    for banned in ("UPDATE", "update(", "save(", "delete"):
        assert banned not in source, banned


def test_the_store_is_only_read_during_a_person_search():
    engine, store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    original = dict(store.rows[0])
    engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                    scope=identity_scope(ref(), identity), scope_mode="person")
    assert store.rows[0] == original


# ===========================================================================
# 8. UI
# ===========================================================================


def ui_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "ui"
            / "assets" / "index.html").read_text(encoding="utf-8")


def api_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "ui"
            / "webview_app.py").read_text(encoding="utf-8")


def test_the_ui_reuses_the_existing_diagnostics_panel():
    """**新しい大型管理画面は作らない。**"""
    text = ui_source()
    assert "migrationPanel(" in text
    assert 'id="migration-panel"' in text
    # 既存の診断オーバーレイの中に足していること。
    assert text.index("function renderDiagnostics") < text.index("function migrationPanel")


def test_diagnostics_can_be_filtered_by_name_key_detail_and_attention():
    """100件超の診断から、目当ての配線を画面内で絞り込める。"""
    text = ui_source()
    assert 'id="diagSearch"' in text
    assert 'id="diagAttention"' in text
    assert "function diagnosticSearchText" in text
    # 表示している情報のどれで検索しても届く。key を隠したままにしない。
    for field in ("item.key", "item.title", "item.detail", "item.impact", "item.flag", "layer"):
        assert field in text
    assert "visibleDiagnosticItems" in text
    assert 'id="diagSearchClear"' in text


def test_diagnostics_can_keep_a_local_favorites_list():
    """お気に入りは診断キーだけを端末内に保存し、本文や記憶を持ち出さない。"""
    text = ui_source()
    assert "DIAG_FAVORITES_STORAGE" in text
    assert "window.localStorage" in text
    assert 'id="diagFavorites"' in text
    assert "function toggleDiagnosticFavorite" in text
    assert "function bindDiagnosticFavorites" in text
    assert "data-diag-key" in text


def test_diagnostics_make_the_smoke_test_values_explicit():
    """一般フラグの長い一覧を読まなくても、実機確認の5値を読める。"""
    text = ui_source()
    assert "function diagnosticSmokeChecks" in text
    for key in ("memory.retrieval_enabled", "turn_integrity.persona_scope_enabled",
                "turn_integrity.turn_frame_enabled", "cognition.trace.enabled",
                "active_persona_id"):
        assert key in text, key
    api = api_source()
    assert 'payload["persona"]' in api
    assert '"active_persona_id"' in api
    # 表示するだけでなく、通常の診断検索と同じ入口へ載せる。
    assert "function visibleDiagnosticSmokeChecks" in text
    assert "diagnosticSearchText" in text
    assert 'check.key + " = " + value' in text
    for description in ("現在のペルソナに属する過去の記憶を検索する",
                        "他ペルソナの私的記憶を検索時点で除外する",
                        "重複の発生層を記録する",
                        "会話本文は残さない",
                        "設定上のペルソナID"):
        assert description in text


def test_the_ui_shows_every_required_number():
    text = ui_source()
    for label in ("対象話者", "確認済みLink", "未解決", "競合", "差分軸",
                  "部分的失敗"):
        assert label in text, label
    assert "最後の移行=" in text
    assert "戻せる" in text


def test_the_ui_asks_before_the_risky_operations():
    text = ui_source()
    start = text.index("async function runMigration")
    body = text[start:start + 1800]
    assert "needs_confirmation" in body
    assert "window.confirm" in body
    for label in ("現在:", "変更後:", "対象:", "競合:", "部分的失敗:", "戻し方:"):
        assert label in body, label


def test_the_ui_never_touches_the_database():
    """**UI から DB を直接変更しない。** 操作は1メソッドを通る。"""
    text = ui_source()
    for banned in ("sqlite", "INSERT ", "UPDATE ", "DELETE "):
        assert banned not in text, banned
    assert text.count("run_migration_operation") >= 2


def test_the_backend_revalidates_even_if_the_button_is_hidden():
    """**ボタンを隠すだけでは足りない。**"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    start = text.index("def request_migration")
    body = text[start:start + 2600]
    assert "can_transition(" in body
    assert "confirmed" in body


def test_the_api_refuses_while_the_ui_flag_is_down():
    text = api_source()
    start = text.index("def run_migration_operation")
    body = text[start:start + 900]
    assert "identity_migration.ui_enabled" in body
    assert "ui_disabled" in body


def test_the_ui_disables_buttons_while_a_job_runs():
    text = ui_source()
    assert "busy" in text and "disabled" in text
    start = text.index("async function runMigration")
    assert "button.disabled = true" in text[start:start + 600]


def test_the_ui_calls_local_counts_an_estimate():
    """**Local の人数を確定値として出さない。**"""
    assert "推定話者数" in ui_source()


def test_the_speaker_rows_show_the_resolution_state():
    text = ui_source()
    for label in ("Legacy Speaker", "Resolved Person", "Unknown", "Conflicted",
                  "Revoked"):
        assert label in text, label


# ===========================================================================
# 9. 機能フラグ
# ===========================================================================


def test_the_new_flags_default_to_off():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))
    section = data["identity_migration"]
    assert section["ui_enabled"] is False
    # **何も変えない操作だけは既定で許す。**
    assert section["dry_run_enabled"] is True
    memory = data["memory"]
    assert memory["identity_scope_retrieval_enabled"] is False
    assert memory["identity_scope_shadow_read_enabled"] is False


def test_the_memory_scope_follows_the_migration_stage():
    """関係値が legacy なのに記憶だけ人物単位、を作らない。"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    start = text.index("def _memory_scope_mode")
    body = text[start:start + 1400]
    assert "MigrationMode.DISABLED" in body
    assert "LEGACY_ROLLBACK" in body
    assert "PERSON_PRIMARY" in body


# ===========================================================================
# 10. Trace
# ===========================================================================


def test_the_trace_has_the_new_fields():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    for name in ("migration_ui_operation", "migration_transition_from",
                 "migration_transition_to", "migration_job_id",
                 "migration_job_status", "migration_promotion_gate",
                 "migration_rejection_reason", "identity_scope_person_id",
                 "identity_scope_speaker_ids", "memory_retrieval_mode",
                 "legacy_memory_result_count", "person_scope_result_count",
                 "deduplicated_memory_count", "revoked_ids_excluded",
                 "speaker_status_resolution"):
        assert hasattr(trace, name), name


def test_the_trace_records_the_memory_scope():
    from neuro_voice.cognition.trace import CognitiveTrace

    engine, _store = engine_with([row(1, "speaker:3"), row(2, "speaker:7")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    result = engine.retrieve("さっきのダイヤの話", speaker_key="speaker:3",
                             scope=identity_scope(ref(), identity),
                             scope_mode="person")
    trace = CognitiveTrace()
    trace.note_memory_scope(result)
    assert trace.memory_retrieval_mode == "person"
    assert set(trace.identity_scope_speaker_ids) == {"speaker:3", "speaker:7"}
    assert trace.person_scope_result_count == 2


def test_the_trace_records_a_refused_operation():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    trace.note_migration_operation(
        "person_primary",
        {"ok": False, "reason": "preconditions_not_met",
         "blockers": ["unresolved_write_failures:2"]})
    assert trace.migration_ui_operation == "person_primary"
    assert trace.migration_rejection_reason == "preconditions_not_met"
    assert "unresolved_write_failures:2" in trace.migration_promotion_gate


def test_the_trace_holds_no_memory_text():
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    engine, _store = engine_with([row(1, "speaker:3", "口座番号は1234-5678")])
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:7")
    trace = CognitiveTrace()
    trace.note_memory_scope(engine.retrieve(
        "さっきの話", speaker_key="speaker:3",
        scope=identity_scope(ref(), identity), scope_mode="person"))
    assert "1234-5678" not in json.dumps(trace.snapshot(), ensure_ascii=False)


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


def test_the_transition_probe_is_registered():
    from neuro_voice.diagnostics.wiring import run_all

    keys = {item.probe.key for item in run_all(yaml_config()).results}
    assert "migration.transitions" in keys
    assert "migration.memory_scope" in keys


def test_a_shortcut_transition_turns_the_probe_red(monkeypatch):
    import neuro_voice.mind.migration as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setitem(module.ALLOWED_TRANSITIONS, module.MigrationMode.DISABLED,
                        frozenset({module.MigrationMode.PERSON_PRIMARY}))
    report = run_all(yaml_config(), keys=("migration.transitions",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_including_revoked_links_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.identity as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module.IdentityLink, "usable", property(lambda self: True))
    report = run_all(yaml_config(), keys=("migration.memory_scope",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)
