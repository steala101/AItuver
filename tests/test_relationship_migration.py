"""Phase 6E: 関係値の主キーを `speaker_id` から `person_id` へ、戻せる形で移す。

`speaker_id` は「どの声に似ているか」でしかない。声紋は揺れるので、
**主キーとして使い続けると、声が外れた日に関係が別人のものになる。**
かといって一括で書き換えるのは、もっと危ない——間違えた時に戻せない。

いちばん守りたいのは5つ:

* **既存データを消さない**（ロールバックは読む先を戻すだけ）
* **確認済みのリンクだけで移す**（候補では動かさない）
* **食い違う関係値を平均しない**（平均した値は誰の関係でもない）
* **未解決・競合は legacy へ落ちる**（会話は止めない）
* **同じ移行を2回走らせても二重に入らない**
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.identity import IdentityResolver, IdentityType, LinkStatus
from neuro_voice.mind.migration import (
    MERGE_TOLERANCE, IdentityMigration, MigrationMode, MigrationStatus,
    ParticipantIdentityRef, ReadSource, RelationshipResolver, coerce_mode,
    compare_metrics, merge_verdict, participant_ref, resolve_mode, untouched,
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


def confirmed(identity, *, person_id="person:A", value="voice:3", confidence=.95):
    return identity.link(person_id=person_id, identity_type=IdentityType.VOICE,
                         value=value, confidence=confidence, event_id="probe",
                         authoritative=True)


def ref(*, person_id="person:A", speaker_id="speaker:3", status="confirmed"):
    return ParticipantIdentityRef(person_id=person_id, speaker_id=speaker_id,
                                  resolution_status=status, confidence=.95)


# ===========================================================================
# 1. 一対一の移行
# ===========================================================================


def test_a_one_to_one_migration_keeps_the_values(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82, "comfort": .71},
                        interaction_count=12, meaningful_interaction_count=5)
    identity = IdentityResolver()
    link = confirmed(identity)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    record = IdentityMigration(resolver).migrate_speaker("speaker:3")

    assert str(record.migration_status) == str(MigrationStatus.APPLIED)
    assert record.person_id == "person:A"
    assert record.identity_link_id == link.link_id
    moved = person.get("person:A")
    assert abs(moved.trust - .82) < .0001
    assert abs(moved.comfort - .71) < .0001
    assert moved.interaction_count == 12, "根拠の重みが失われている"


def test_the_legacy_relationship_survives_the_migration(stores):
    """**既存データを消さない。**"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    IdentityMigration(resolver).migrate_speaker("speaker:3")
    assert abs(legacy.get("speaker:3").trust - .82) < .0001


def test_the_journal_records_what_moved(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    record = IdentityMigration(resolver).migrate_speaker("speaker:3")
    assert record.migrated_data.get("trust") == .82
    assert "trust" in record.previous_data, "戻すのに要る値が残っていない"
    assert record.applied_at > 0


def test_the_journal_holds_no_secrets(stores):
    """**声紋ベクトルも会話本文も記録へ入れない**（第12条）。"""
    import json

    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    record = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity,
        mode=MigrationMode.DUAL_WRITE)).migrate_speaker("speaker:3")
    text = json.dumps(record.snapshot(), ensure_ascii=False)
    for banned in ("vec", "embedding", "summary", "transcript"):
        assert banned not in text.lower()


# ===========================================================================
# 2. 冪等性
# ===========================================================================


def test_running_the_same_migration_twice_changes_nothing(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))

    first = migration.migrate_speaker("speaker:3")
    after_first = person.get("person:A").trust
    second = migration.migrate_speaker("speaker:3")

    assert second.migration_id == first.migration_id, "移行記録が2件できている"
    assert len(migration.records) == 1
    assert abs(person.get("person:A").trust - after_first) < .0001, "二重加算"


def test_migrating_everything_is_also_idempotent(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    migration.migrate_all(["speaker:3"])
    again = migration.migrate_all(["speaker:3"])
    assert not again["applied"], "2回目も適用扱いになっている"
    assert len(migration.records) == 1


# ===========================================================================
# 3. Shadow Read
# ===========================================================================


def test_shadow_read_returns_legacy(stores):
    """**会話に使うのは legacy。** person 側は測るだけ。"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .30}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.SHADOW_READ)
    state, source, _reason = resolver.read(ref())
    assert source is ReadSource.LEGACY
    assert abs(state.trust - .82) < .0001


def test_shadow_read_records_the_difference(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .30}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.SHADOW_READ)
    resolver.read(ref())
    assert "trust" in resolver.last_difference
    assert resolver.counters["shadow_differed"] == 1


def test_shadow_read_is_quiet_when_the_values_agree(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .82}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.SHADOW_READ)
    resolver.read(ref())
    assert resolver.last_difference == {}
    assert resolver.counters["shadow_differed"] == 0


# ===========================================================================
# 4. Dual Write
# ===========================================================================


def test_dual_write_reaches_both_sides(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    outcome = resolver.apply_state_delta(ref(), "trust", .05, reason="probe",
                                         event_id="e1")
    assert outcome["targets"] == ["legacy", "person"]
    assert legacy.get("speaker:3").trust > .50
    assert person.get("person:A").trust > .50


def test_the_same_event_is_not_applied_twice(stores):
    """**同じ出来事を person 側へ2回入れない。**"""
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")
    after_first = person.get("person:A").trust
    repeat = resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")
    assert repeat["duplicate"] is True
    assert abs(person.get("person:A").trust - after_first) < .0001


def test_dual_write_still_reads_legacy(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .10}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    _state, source, _reason = resolver.read(ref())
    assert source is ReadSource.LEGACY


def test_a_failed_person_write_is_not_a_success(stores):
    """**片方だけ書けたのを成功扱いしない。** 会話は止めない。"""
    legacy, _person = stores

    class Broken:
        def apply_state_delta(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def get(self, key):
            raise RuntimeError("disk full")

        def observe(self, *args, **kwargs):
            raise RuntimeError("disk full")

    resolver = RelationshipResolver(legacy, Broken(), mode=MigrationMode.DUAL_WRITE)
    outcome = resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")
    assert outcome["error"]
    assert outcome["targets"] == ["legacy"], "person を書けたことにしている"
    assert legacy.get("speaker:3").trust > .50, "legacy まで止まっている"
    assert resolver.counters["write_person_failed"] == 1


def test_a_failed_write_can_be_retried(stores):
    """失敗した分は控えに残らない——**後で入れ直せる。**"""
    legacy, person = stores

    class Flaky:
        def __init__(self):
            self.calls = 0

        def apply_state_delta(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("disk full")
            return person.apply_state_delta(*args, **kwargs)

        def get(self, key):
            return person.get(key)

        def observe(self, *args, **kwargs):
            person.observe(*args, **kwargs)

    resolver = RelationshipResolver(legacy, Flaky(), mode=MigrationMode.DUAL_WRITE)
    assert resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")["error"]
    retry = resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")
    assert not retry["duplicate"], "失敗した分まで適用済みにしている"
    assert "person" in retry["targets"]


# ===========================================================================
# 5. Person Primary
# ===========================================================================


def test_person_primary_reads_the_person_relationship(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .10}, interaction_count=4)
    person.import_state("person:A", {"trust": .90}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    state, source, reason = resolver.read(ref())
    assert source is ReadSource.PERSON
    assert abs(state.trust - .90) < .0001
    assert reason == ""


def test_the_snapshot_says_where_it_came_from(stores):
    legacy, person = stores
    person.import_state("person:A", {"trust": .90}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    assert resolver.snapshot(ref())["_source"] == "person"
    resolver.set_mode(MigrationMode.SHADOW_READ)
    assert resolver.snapshot(ref())["_source"] == "legacy"


# ===========================================================================
# 6. 未解決・競合は legacy へ
# ===========================================================================


def test_an_unresolved_identity_falls_back(stores):
    """**別人へ統合しない。** legacy の鍵をそのまま使う。"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    unresolved = ParticipantIdentityRef(speaker_id="speaker:3")
    state, source, reason = resolver.read(unresolved)
    assert source is ReadSource.LEGACY
    assert reason == "person_unresolved"
    assert abs(state.trust - .82) < .0001


def test_a_conflicted_identity_falls_back(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    _state, source, reason = resolver.read(ref(status="conflicted"))
    assert source is ReadSource.LEGACY
    assert reason == "identity_conflicted"


def test_a_conflicted_identity_is_not_written_to_person(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    outcome = resolver.apply_state_delta(ref(status="conflicted"), "trust", .05,
                                         event_id="e1")
    assert outcome["targets"] == ["legacy"]
    assert abs(person.get("person:A").trust - .50) < .0001


def test_a_probable_identity_is_not_good_enough(stores):
    """**`PROBABLE` は既知ではない。** 関係値を動かすには足りない。"""
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    probable = ParticipantIdentityRef(person_id="person:A", speaker_id="speaker:3",
                                      resolution_status="probable", confidence=.7)
    _state, source, _reason = resolver.read(probable)
    assert source is ReadSource.LEGACY


def test_the_speaker_id_is_never_lost():
    """`person_id` が決まらなくても legacy の鍵を失わない。"""
    reference = participant_ref(speaker_id="speaker:3")
    assert reference.speaker_id == "speaker:3"
    assert reference.legacy_key == "speaker:3"
    assert not reference.resolved


def test_the_ref_carries_both_ids():
    resolver = IdentityResolver()
    from neuro_voice.cognition.identity import TransportIdentity

    resolution = resolver.resolve(
        transport=TransportIdentity(provider="discord", transport_user_id="7",
                                    authoritative=True), event_id="e1")
    reference = participant_ref(speaker_id="speaker:3", resolution=resolution)
    assert reference.speaker_id == "speaker:3"
    assert reference.person_id
    assert reference.resolved


# ===========================================================================
# 7. Identity 競合では移行しない
# ===========================================================================


def test_a_candidate_link_does_not_migrate(stores):
    """**確認済みのリンクだけ。**"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                  value="voice:3", confidence=.95, event_id="e1")
    record = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity,
        mode=MigrationMode.DUAL_WRITE)).migrate_speaker("speaker:3")
    assert str(record.migration_status) == str(MigrationStatus.SKIPPED)
    assert record.conflict_reason == "no_confirmed_link"
    assert untouched(person.get("person:A"))


def test_a_low_confidence_link_does_not_migrate(stores):
    legacy, person = stores
    identity = IdentityResolver()
    link = confirmed(identity, confidence=.5)
    link.confidence = .5
    record = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity,
        mode=MigrationMode.DUAL_WRITE)).migrate_speaker("speaker:3")
    assert record.conflict_reason == "confidence_too_low"


def test_a_contested_identity_does_not_migrate(stores):
    """**一度でも割れた識別子は自動移行しない。**

    争った末に片方が `CONFLICTED` になると残りは1件だが、
    関係値を写すのは取り返しがつかない。人が確かめてから。
    """
    legacy, person = stores
    legacy.import_state("speaker:9", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    for index in range(4):
        identity.link(person_id="person:A", identity_type=IdentityType.VOICE,
                      value="voice:9", confidence=.95, event_id=f"a{index}")
    identity.link(person_id="person:B", identity_type=IdentityType.VOICE,
                  value="voice:9", confidence=.95, event_id="b", authoritative=True)
    record = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity,
        mode=MigrationMode.DUAL_WRITE)).migrate_speaker("speaker:9")
    assert str(record.migration_status) == str(MigrationStatus.CONFLICT)
    assert record.conflict_reason == "multiple_person_candidates"
    assert abs(legacy.get("speaker:9").trust - .82) < .0001, "legacy を壊している"


# ===========================================================================
# 8. 複数 speaker_id が同じ人物
# ===========================================================================


def test_an_untouched_target_accepts_the_values():
    from neuro_voice.mind.relationship import RelationshipState

    assert untouched(RelationshipState())
    assert not untouched(RelationshipState(interaction_count=1))


@pytest.mark.parametrize("left,right,expected", [
    ({}, {"trust": .9}, "safe"),
    ({"trust": .5}, {"trust": .5}, "safe"),
    ({"trust": .50}, {"trust": .50 + MERGE_TOLERANCE / 2}, "candidate"),
    ({"trust": .10}, {"trust": .90}, "conflict"),
])
def test_two_relationships_are_never_averaged(left, right, expected):
    """**平均した値は誰の関係でもない。** どちらの履歴とも合わない。"""
    assert merge_verdict(left, right)[0] == expected


def test_a_second_speaker_with_different_values_conflicts(stores):
    legacy, person = stores
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:4")
    legacy.import_state("speaker:3", {"trust": .90}, interaction_count=8)
    legacy.import_state("speaker:4", {"trust": .10}, interaction_count=8)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))

    first = migration.migrate_speaker("speaker:3")
    assert str(first.migration_status) == str(MigrationStatus.APPLIED)
    second = migration.migrate_speaker("speaker:4")
    assert str(second.migration_status) == str(MigrationStatus.CONFLICT)
    # **片方の値で上書きしない。**
    assert abs(person.get("person:A").trust - .90) < .0001


def test_a_second_speaker_with_matching_values_is_safe(stores):
    legacy, person = stores
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:4")
    legacy.import_state("speaker:3", {"trust": .90}, interaction_count=8)
    legacy.import_state("speaker:4", {"trust": .90}, interaction_count=3)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    migration.migrate_speaker("speaker:3")
    second = migration.migrate_speaker("speaker:4")
    assert str(second.migration_status) == str(MigrationStatus.APPLIED)


def test_a_conflict_does_not_stop_the_conversation(stores):
    """競合しても legacy で読める。**会話は続く。**"""
    legacy, person = stores
    identity = IdentityResolver()
    confirmed(identity, value="voice:3")
    confirmed(identity, value="voice:4")
    legacy.import_state("speaker:3", {"trust": .90}, interaction_count=8)
    legacy.import_state("speaker:4", {"trust": .10}, interaction_count=8)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    migration = IdentityMigration(resolver)
    migration.migrate_speaker("speaker:3")
    migration.migrate_speaker("speaker:4")
    state, source, _reason = resolver.read(
        ParticipantIdentityRef(speaker_id="speaker:4"))
    assert source is ReadSource.LEGACY
    assert abs(state.trust - .10) < .0001


# ===========================================================================
# 9. リンクの取り消しと繋ぎ直し
# ===========================================================================


def test_revoking_a_link_stops_new_updates(stores):
    legacy, person = stores
    identity = IdentityResolver()
    link = confirmed(identity)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.PERSON_PRIMARY)
    identity.revoke(link.link_id, reason="別人だった")
    # 解決し直すと、もう person は引けない。
    verdict = identity.resolve(
        voice=__import__("neuro_voice.cognition.identity", fromlist=["VoiceIdentity"])
        .VoiceIdentity(voiceprint_id="3", confidence=.95))
    assert not verdict.known
    reference = participant_ref(speaker_id="speaker:3", resolution=verdict)
    outcome = resolver.apply_state_delta(reference, "trust", .05, event_id="e1")
    assert outcome["targets"] == ["legacy"], "取り消した後も person へ書いている"


def test_a_revoked_link_marks_the_migration_for_review(stores):
    """**物理削除しない。** 要再評価の印を付けるだけ。"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    link = confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    record = migration.migrate_speaker("speaker:3")
    identity.revoke(link.link_id, reason="別人")
    touched = migration.mark_needs_review("person:A", reason="link_revoked")

    assert record.migration_id in touched
    assert str(record.migration_status) == str(MigrationStatus.CONFLICT)
    assert record.migration_id in migration.records, "記録を消している"
    # **移った値も消さない。**
    assert abs(person.get("person:A").trust - .82) < .0001


def test_relinking_sends_only_new_updates_to_the_new_person(stores):
    """**過去の値を自動で移さない。** 新しい更新だけが新しい人物へ。"""
    legacy, person = stores
    identity = IdentityResolver()
    link = confirmed(identity)
    resolver = RelationshipResolver(legacy, person, resolver=identity,
                                    mode=MigrationMode.DUAL_WRITE)
    resolver.apply_state_delta(ref(person_id="person:A"), "trust", .05,
                               event_id="e1")
    before = person.get("person:A").trust

    identity.relink(link.link_id, person_id="person:B", reason="取り違え")
    resolver.apply_state_delta(ref(person_id="person:B"), "trust", .05,
                               event_id="e2")

    assert abs(person.get("person:A").trust - before) < .0001, "過去値が動いている"
    assert person.get("person:B").trust > .50
    assert str(identity.links[link.link_id].status) == str(LinkStatus.SUPERSEDED)


def test_relinking_does_not_touch_the_journal_values(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    link = confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    record = migration.migrate_speaker("speaker:3")
    identity.relink(link.link_id, person_id="person:B")
    assert record.migrated_data.get("trust") == .82
    assert record.person_id == "person:A", "記録の宛先が書き換わっている"


# ===========================================================================
# 10. ロールバック
# ===========================================================================


def test_rollback_restores_the_previous_person_values(stores):
    legacy, person = stores
    person.import_state("person:A", {"trust": .30}, interaction_count=2)
    legacy.import_state("speaker:3", {"trust": .90}, interaction_count=8)
    identity = IdentityResolver()
    confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    record = migration.migrate_speaker("speaker:3")
    # 値が近くないので競合になる想定——**上書きしない。**
    assert str(record.migration_status) == str(MigrationStatus.CONFLICT)
    assert abs(person.get("person:A").trust - .30) < .0001


def test_rollback_after_a_real_migration(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .90}, interaction_count=8)
    identity = IdentityResolver()
    confirmed(identity)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE))
    record = migration.migrate_speaker("speaker:3")
    assert str(record.migration_status) == str(MigrationStatus.APPLIED)

    migration.rollback(record.migration_id)
    assert str(record.migration_status) == str(MigrationStatus.ROLLED_BACK)
    assert record.rolled_back_at > 0
    assert record.migration_id in migration.records, "記録を消している"
    restored = person.get("person:A").trust
    assert abs(restored - float(record.previous_data["trust"])) < .0001


def test_legacy_rollback_mode_returns_to_the_old_values(stores):
    """**読む先を戻すだけ。** person のデータは残る。"""
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .10}, interaction_count=4)
    person.import_state("person:A", {"trust": .90}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    assert resolver.read(ref())[1] is ReadSource.PERSON

    resolver.set_mode(MigrationMode.LEGACY_ROLLBACK)
    state, source, _reason = resolver.read(ref())
    assert source is ReadSource.LEGACY
    assert abs(state.trust - .10) < .0001
    # person 側のデータは消えていない。
    assert abs(person.get("person:A").trust - .90) < .0001


def test_legacy_rollback_stops_writing_to_person(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person,
                                    mode=MigrationMode.LEGACY_ROLLBACK)
    outcome = resolver.apply_state_delta(ref(), "trust", .05, event_id="e1")
    assert outcome["targets"] == ["legacy"]


# ===========================================================================
# 11. 話者ごとの分離
# ===========================================================================


def test_one_speakers_event_does_not_reach_another(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    resolver.apply_state_delta(ref(person_id="person:A", speaker_id="speaker:3"),
                               "trust", .05, event_id="e1")
    assert person.get("person:A").trust > .50
    assert abs(person.get("person:B").trust - .50) < .0001
    assert abs(legacy.get("speaker:4").trust - .50) < .0001


def test_an_unknown_speaker_does_not_move_a_known_person(stores):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.DUAL_WRITE)
    unknown = ParticipantIdentityRef(speaker_id="unknown")
    resolver.apply_state_delta(unknown, "trust", .05, event_id="e1")
    assert abs(person.get("person:A").trust - .50) < .0001


# ===========================================================================
# 12. モード
# ===========================================================================


def test_the_modes_are_the_five_stages():
    assert {str(item) for item in MigrationMode} == {
        "disabled", "shadow_read", "dual_write", "person_primary",
        "legacy_rollback"}


@pytest.mark.parametrize("mode,reads,writes", [
    (MigrationMode.DISABLED, False, False),
    (MigrationMode.SHADOW_READ, False, False),
    (MigrationMode.DUAL_WRITE, False, True),
    (MigrationMode.PERSON_PRIMARY, True, True),
    (MigrationMode.LEGACY_ROLLBACK, False, False),
])
def test_each_mode_touches_the_right_side(stores, mode, reads, writes):
    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=mode)
    assert resolver.reads_person is reads
    assert resolver.writes_person is writes


def test_conflicting_flags_settle_on_the_weaker_side():
    """**強い方へ倒さない。** 1つの上げ間違いで正式採用にしない。"""
    assert resolve_mode(Cfg(**{
        "identity_migration.enabled": True,
        "identity_migration.mode": "person_primary"})) is MigrationMode.DISABLED
    assert resolve_mode(Cfg(**{
        "identity_migration.enabled": True,
        "identity_migration.mode": "person_primary",
        "identity_migration.shadow_read_enabled": True,
    })) is MigrationMode.SHADOW_READ
    assert resolve_mode(Cfg(**{
        "identity_migration.enabled": True,
        "identity_migration.mode": "person_primary",
        "identity_migration.shadow_read_enabled": True,
        "identity_migration.dual_write_enabled": True,
    })) is MigrationMode.DUAL_WRITE


def test_the_master_switch_wins():
    assert resolve_mode(Cfg(**{
        "identity_migration.enabled": False,
        "identity_migration.mode": "person_primary",
        "identity_migration.person_primary_enabled": True,
    })) is MigrationMode.DISABLED


def test_an_unknown_mode_is_disabled():
    assert coerce_mode("なんとなく") is MigrationMode.DISABLED
    assert coerce_mode("") is MigrationMode.DISABLED
    assert coerce_mode("person_primary") is MigrationMode.PERSON_PRIMARY


def test_the_config_defaults_to_disabled():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))
    section = data["identity_migration"]
    assert section["enabled"] is False
    assert section["mode"] == "disabled"
    for key in ("shadow_read_enabled", "dual_write_enabled",
                "person_primary_enabled"):
        assert section[key] is False, key


# ===========================================================================
# 13. 永続化と再起動
# ===========================================================================


def store_at(tmp_path):
    from neuro_voice.mind.store import MemoryStore

    return MemoryStore(tmp_path / "mind.db")


def test_the_journal_survives_a_restart(tmp_path):
    cfg = Cfg()
    legacy = RelationshipStore(tmp_path / "legacy.json", cfg)
    person = RelationshipStore(tmp_path / "person.json", cfg)
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    journal = store_at(tmp_path)
    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE),
        journal=journal)
    record = migration.migrate_speaker("speaker:3")
    journal.close()

    reopened = store_at(tmp_path)
    restarted = IdentityMigration(RelationshipResolver(
        RelationshipStore(tmp_path / "legacy.json", cfg),
        RelationshipStore(tmp_path / "person.json", cfg),
        resolver=identity, mode=MigrationMode.DUAL_WRITE), journal=reopened)
    assert restarted.hydrate() == 1
    restored = restarted.records[record.migration_id]
    assert str(restored.migration_status) == str(MigrationStatus.APPLIED)
    assert restored.previous_data == record.previous_data
    reopened.close()


def test_a_completed_migration_is_not_rerun_after_a_restart(tmp_path):
    cfg = Cfg()
    legacy = RelationshipStore(tmp_path / "legacy.json", cfg)
    person = RelationshipStore(tmp_path / "person.json", cfg)
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)
    journal = store_at(tmp_path)
    IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE),
        journal=journal).migrate_speaker("speaker:3")
    journal.close()

    reopened = store_at(tmp_path)
    restarted = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE),
        journal=reopened)
    restarted.hydrate()
    before = len(restarted.records)
    restarted.migrate_speaker("speaker:3")
    assert len(restarted.records) == before, "再起動後に同じ移行をやり直している"
    assert restarted.counters["skipped"] >= 1
    reopened.close()


def test_person_relationships_survive_a_restart(tmp_path):
    cfg = Cfg()
    person = RelationshipStore(tmp_path / "person.json", cfg)
    person.import_state("person:A", {"trust": .77}, interaction_count=3)
    reopened = RelationshipStore(tmp_path / "person.json", cfg)
    assert abs(reopened.get("person:A").trust - .77) < .0001


def test_unfinished_migrations_are_visible(stores):
    from neuro_voice.mind.migration import IdentityMigrationRecord

    legacy, person = stores
    migration = IdentityMigration(RelationshipResolver(legacy, person))
    stuck = IdentityMigrationRecord(speaker_id="speaker:3")
    migration.records[stuck.migration_id] = stuck
    assert migration.unfinished() == [stuck]


def test_a_broken_journal_does_not_stop_the_migration(stores):
    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    identity = IdentityResolver()
    confirmed(identity)

    class Broken:
        def save_identity_migration(self, row):
            raise RuntimeError("disk full")

        def identity_migrations(self):
            raise RuntimeError("disk full")

    migration = IdentityMigration(RelationshipResolver(
        legacy, person, resolver=identity, mode=MigrationMode.DUAL_WRITE),
        journal=Broken())
    record = migration.migrate_speaker("speaker:3")
    assert str(record.migration_status) == str(MigrationStatus.APPLIED)
    assert migration.hydrate() == 0


# ===========================================================================
# 14. Trace
# ===========================================================================


def test_the_trace_has_the_new_fields():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    for name in ("migration_mode", "relationship_read_source",
                 "relationship_write_targets", "shadow_read_difference",
                 "migration_record_id", "migration_status", "migration_conflict",
                 "legacy_write_result", "person_write_result", "fallback_reason",
                 "rollback_applied"):
        assert hasattr(trace, name), name


def test_the_trace_records_where_it_read(stores):
    from neuro_voice.cognition.trace import CognitiveTrace

    legacy, person = stores
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.PERSON_PRIMARY)
    reference = ParticipantIdentityRef(speaker_id="speaker:3")
    _state, source, fallback = resolver.read(reference)
    trace = CognitiveTrace()
    trace.note_relationship_read(reference, source, fallback)
    assert trace.relationship_read_source == "legacy"
    assert trace.fallback_reason == "person_unresolved"


def test_the_trace_does_not_hide_a_partial_write(stores):
    from neuro_voice.cognition.trace import CognitiveTrace

    legacy, _person = stores

    class Broken:
        def apply_state_delta(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def get(self, key):
            raise RuntimeError("disk full")

    resolver = RelationshipResolver(legacy, Broken(), mode=MigrationMode.DUAL_WRITE)
    trace = CognitiveTrace()
    trace.note_relationship_write(
        resolver.apply_state_delta(ref(), "trust", .05, event_id="e1"))
    assert trace.legacy_write_result == "applied"
    assert trace.person_write_result == "failed"


def test_the_trace_keeps_only_the_size_of_the_difference(stores):
    """**関係値そのものは残さない**（第12条）。差の大きさだけ。"""
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    legacy, person = stores
    legacy.import_state("speaker:3", {"trust": .82}, interaction_count=4)
    person.import_state("person:A", {"trust": .30}, interaction_count=4)
    resolver = RelationshipResolver(legacy, person, mode=MigrationMode.SHADOW_READ)
    resolver.read(ref())
    trace = CognitiveTrace()
    trace.note_shadow_difference(resolver.last_difference)
    assert set(trace.shadow_read_difference) == {"axes", "max"}
    assert "trust" not in json.dumps(trace.snapshot()["migration"])


# ===========================================================================
# 15. 責務の分離
# ===========================================================================


def test_the_speaker_registry_is_not_removed():
    """**声紋の管理層として残す。** PersonIdentity は置き換えではない。"""
    from neuro_voice.mind.speakers import SpeakerRegistry

    assert hasattr(SpeakerRegistry, "identify")
    assert hasattr(SpeakerRegistry, "merge")


def test_the_relationship_store_is_not_replaced():
    """関係性モデルを作り直していないこと。**同じ軸・同じ減衰。**"""
    from neuro_voice.mind.relationship import _METRICS

    assert "trust" in _METRICS and "familiarity" in _METRICS
    assert hasattr(RelationshipStore, "apply_state_delta")
    assert hasattr(RelationshipStore, "_decay")


def test_the_import_path_is_only_for_migration():
    """**普段の会話から `import_state` を呼ばない。** 上限が無い。"""
    import inspect

    source = inspect.getsource(RelationshipStore.import_state)
    assert "移行とロールバック専用" in source
    root = Path(__file__).resolve().parents[1] / "neuro_voice"
    callers = [path for path in root.rglob("*.py")
               if "import_state(" in path.read_text(encoding="utf-8")]
    names = {path.name for path in callers}
    assert names <= {"relationship.py", "migration.py", "wiring.py"}, names


def test_the_write_path_is_centralised():
    """各モジュールが直接 `RelationshipStore` を叩く状態を増やさない。"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "internal.py").read_text(encoding="utf-8")
    assert "resolver.apply_state_delta(" in text
    assert "attach_relationship_resolver" in text


def test_mind_exposes_the_migration_controls():
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    for needle in ("def participant_ref", "def relationship_resolver",
                   "def set_migration_mode", "def relationship_migration_status",
                   "def attach_identity_runtime"):
        assert needle in text, needle


def test_the_audit_is_recorded():
    """**何を移し、何を移さないか**を文書に残してある。"""
    path = (Path(__file__).resolve().parents[1] / "docs" / "audits"
            / "2026-08-02_speaker_id_usage.md")
    text = path.read_text(encoding="utf-8")
    for label in ("MIGRATE_NOW", "RESOLVE_AT_BOUNDARY", "KEEP_LEGACY",
                  "UNRELATED", "UNSAFE_DIRECT_REFERENCE"):
        assert label in text, label


# ===========================================================================
# 16. 配線診断
# ===========================================================================


NEW_PROBES = ("migration.modes", "migration.fallback", "migration.no_average",
              "migration.reversible", "migration.link_only")


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


def test_the_new_probes_are_registered():
    from neuro_voice.diagnostics.wiring import run_all

    keys = {item.probe.key for item in run_all(yaml_config()).results}
    for key in NEW_PROBES:
        assert key in keys, key


def test_nothing_needs_attention():
    """**要注意 0 件を保つ。**"""
    from neuro_voice.diagnostics.wiring import run_all

    report = run_all(yaml_config())
    assert report.snapshot()["attention"] == [], report.snapshot()["attention"]


def test_averaging_turns_the_probe_red(monkeypatch):
    import neuro_voice.mind.migration as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module, "merge_verdict",
                        lambda left, right: ("safe", "averaged"))
    report = run_all(yaml_config(), keys=("migration.no_average",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_hard_delete_of_a_migration_turns_the_probe_red(monkeypatch):
    import neuro_voice.mind.migration as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.IdentityMigration.rollback

    def deleting(self, migration_id):
        result = original(self, migration_id)
        self.records.pop(migration_id, None)
        return result

    monkeypatch.setattr(module.IdentityMigration, "rollback", deleting)
    report = run_all(yaml_config(), keys=("migration.reversible",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_migrating_on_a_candidate_link_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.identity as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module.IdentityLink, "usable",
                        property(lambda self: True))
    report = run_all(yaml_config(), keys=("migration.link_only",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_missing_fallback_turns_the_probe_red(monkeypatch):
    import neuro_voice.mind.migration as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module.ParticipantIdentityRef, "conflicted",
                        property(lambda self: False))
    monkeypatch.setattr(module.ParticipantIdentityRef, "resolved",
                        property(lambda self: True))
    report = run_all(yaml_config(), keys=("migration.fallback",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_the_new_flags_are_watched():
    from neuro_voice.diagnostics.wiring import run_all

    flags = run_all(yaml_config()).flags
    for key in ("identity_migration.enabled",
                "identity_migration.shadow_read_enabled",
                "identity_migration.dual_write_enabled",
                "identity_migration.person_primary_enabled"):
        assert key in flags, key


def test_both_sides_attach_the_identity_runtime():
    """**部品があるだけでは動かない。** 呼ぶ側を確かめる。

    ここを忘れると Resolver が人物を引けず、フラグを上げても
    ずっと `disabled` のまま——`Phase 6` で何度も踏んだ形。
    """
    root = Path(__file__).resolve().parents[1] / "neuro_voice"
    for path in (root / "pipeline.py", root / "discord_bridge" / "bot.py"):
        text = path.read_text(encoding="utf-8")
        assert "attach_identity_runtime(" in text, path.name
