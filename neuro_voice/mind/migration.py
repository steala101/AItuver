"""関係値の主キーを `speaker_id` から `person_id` へ、**戻せる形で**移す。

いま2つのID層が並走している:

```
SpeakerRegistry → speaker_id → 関係値の主キー（既存）
PersonIdentity  → person_id  → 接続と声紋を束ねる（Phase 6D で追加）
```

`speaker_id` は「どの声に似ているか」でしかない。声紋は揺れるので、
**主キーとして使い続けると、声が外れた日に関係が別人のものになる。**
かといって一括で書き換えるのは、もっと危ない——間違えた時に戻せない。

だからここは**移行そのものを1つの機能として**持つ:

* 5段階のモード（`DISABLED` → `SHADOW_READ` → `DUAL_WRITE` →
  `PERSON_PRIMARY`、いつでも `LEGACY_ROLLBACK`）
* 読み書きを1箇所へ集約（`RelationshipResolver`）
* 何をどう移したかの記録（`IdentityMigrationRecord`）
* **既存データは消さない。** ロールバックは読む先を戻すだけ

守ること:

* **`CONFIRMED` なリンクだけを自動移行に使う**（候補では動かさない）
* **値が食い違う複数の関係値を平均しない**——競合として止める
* **未解決・競合は legacy へ落ちる**。会話は止めない
"""
from __future__ import annotations

import json
import logging
import time
import uuid
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class MigrationMode(StrEnum):
    """どこまで移したか。**一度に1つだけ。**"""

    #: 従来どおり `speaker_id`。移行処理は何もしない。
    DISABLED = "disabled"
    #: legacy を正式採用しつつ、person 側も計算して**差だけ記録する**。
    SHADOW_READ = "shadow_read"
    #: 両方へ書く。読むのはまだ legacy。
    DUAL_WRITE = "dual_write"
    #: person 側を正式採用。解決できない時だけ legacy。
    PERSON_PRIMARY = "person_primary"
    #: person 側を止めて legacy へ戻す。**person のデータは消さない。**
    LEGACY_ROLLBACK = "legacy_rollback"


#: person 側へ書いてよいモード。
_WRITES_PERSON = frozenset({MigrationMode.DUAL_WRITE, MigrationMode.PERSON_PRIMARY})
#: person 側を読んでよいモード。
_READS_PERSON = frozenset({MigrationMode.PERSON_PRIMARY})


class MigrationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    #: 値が食い違って自動では決められない。**会話は止めない。**
    CONFLICT = "conflict"
    #: 片方だけ書けた。**成功扱いにしない。**
    PARTIAL = "partial"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


class ReadSource(StrEnum):
    LEGACY = "legacy"
    PERSON = "person"


# ---------------------------------------------------------------------------
# 参加者の指し方
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParticipantIdentityRef:
    """1人を指すのに要るものを**全部持つ**。

    `person_id` が決まらない時も **`speaker_id` を失わない。**
    落としてしまうと、解決できなかった相手の関係値が
    行き場を無くして「不明な誰か」へ混ざる。
    """

    person_id: str = ""
    speaker_id: str = ""
    transport_identity: str = ""
    resolution_status: str = "unknown"
    confidence: float = .0
    link_id: str = ""

    @property
    def resolved(self) -> bool:
        """人物として使ってよいか。**確認済みだけ。**"""
        return bool(self.person_id) and self.resolution_status in {
            "authoritative", "confirmed"}

    @property
    def conflicted(self) -> bool:
        return self.resolution_status == "conflicted"

    @property
    def legacy_key(self) -> str:
        return self.speaker_id or "unknown"

    def snapshot(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id, "speaker_id": self.speaker_id,
            "transport": self.transport_identity,
            "status": self.resolution_status,
            "confidence": round(float(self.confidence), 3),
            "link_id": self.link_id,
        }


def participant_ref(
    *, speaker_id: str = "", resolution=None, transport_identity: str = "",
) -> ParticipantIdentityRef:
    """解決結果と legacy の鍵から参照を作る。**両方を持たせる。**"""
    if resolution is None:
        return ParticipantIdentityRef(
            speaker_id=str(speaker_id or ""),
            transport_identity=str(transport_identity or ""))
    transport = getattr(resolution, "transport_identity", None)
    return ParticipantIdentityRef(
        person_id=str(getattr(resolution, "person_id", "") or ""),
        speaker_id=str(speaker_id or ""),
        transport_identity=(str(transport_identity or "")
                            or str(getattr(transport, "key", "") or "")),
        resolution_status=str(getattr(resolution, "resolution_status", "unknown")),
        confidence=float(getattr(resolution, "confidence", 0.0) or 0.0),
        link_id=str(getattr(resolution, "link_id", "") or ""))


# ---------------------------------------------------------------------------
# 過去データを人物単位で引くための範囲
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityScope:
    """1人を指す `speaker_id` の集まり。**過去データを移さずに横断する。**

    記憶は `speaker_id` で保存されている。人物単位で引きたいからといって
    **過去のレコードを書き換えるのは危ない**——間違えた時に戻せないし、
    元がどれだったかも消える。代わりに「この人物はどの `speaker_id` か」を
    その場で作って、検索側で束ねる。

    含めるのは**いま有効な確認済みリンクだけ**。候補・競合・取り消し・
    置き換え済みは入れない——誤ったリンクで拾った記憶を、
    その人のものとして語り続けることになる。
    """

    person_id: str = ""
    primary_speaker_id: str = ""
    confirmed_speaker_ids: tuple[str, ...] = ()
    transport_identity_ids: tuple[str, ...] = ()
    #: 取り消したので**外した**もの。何を外したかが読めるように残す。
    excluded_revoked_ids: tuple[str, ...] = ()
    resolution_status: str = "unknown"

    @property
    def resolved(self) -> bool:
        return bool(self.person_id) and self.resolution_status in {
            "authoritative", "confirmed"}

    @property
    def search_keys(self) -> tuple[str, ...]:
        """検索に使う鍵。**主が先頭**——同点なら今の声を優先したい。"""
        keys = [self.primary_speaker_id] if self.primary_speaker_id else []
        keys += [item for item in self.confirmed_speaker_ids
                 if item and item != self.primary_speaker_id]
        return tuple(keys)

    def snapshot(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "primary": self.primary_speaker_id,
            "speakers": list(self.confirmed_speaker_ids[:6]),
            "transports": list(self.transport_identity_ids[:4]),
            "excluded": list(self.excluded_revoked_ids[:4]),
            "status": self.resolution_status,
        }


def _speaker_key(voice_value: str) -> str:
    """声紋リンクの値 (`voice:3`) → 関係値・記憶の鍵 (`speaker:3`)。"""
    text = str(voice_value or "")
    if text.startswith("voice:"):
        return f"speaker:{text.split(':', 1)[1]}"
    return text


def identity_scope(ref: ParticipantIdentityRef, resolver=None) -> IdentityScope:
    """その人物に紐づく `speaker_id` を集める。

    **解決できていないなら、今の `speaker_id` だけ。** 人物が分からない
    まま範囲を広げると、別人の過去を自分のものとして語り出す。
    """
    primary = ref.legacy_key if ref.speaker_id else ""
    if resolver is None or not ref.resolved:
        return IdentityScope(
            person_id=ref.person_id, primary_speaker_id=primary,
            confirmed_speaker_ids=((primary,) if primary else ()),
            transport_identity_ids=((ref.transport_identity,)
                                    if ref.transport_identity else ()),
            resolution_status=ref.resolution_status)
    speakers: list[str] = []
    transports: list[str] = []
    excluded: list[str] = []
    for link in resolver.links.values():
        if link.person_id != ref.person_id:
            continue
        value = str(link.identity_value)
        if str(link.identity_type) == "transport":
            if link.usable:
                transports.append(value)
            continue
        key = _speaker_key(value)
        if link.usable:
            speakers.append(key)
        else:
            # **候補も取り消しも入れない。** 何を外したかは残す。
            excluded.append(key)
    if primary and primary not in speakers:
        # いま話している声は、リンクが未確認でも自分の分として引く。
        speakers.insert(0, primary)
    return IdentityScope(
        person_id=ref.person_id, primary_speaker_id=primary,
        confirmed_speaker_ids=tuple(dict.fromkeys(speakers)),
        transport_identity_ids=tuple(dict.fromkeys(transports)),
        excluded_revoked_ids=tuple(dict.fromkeys(
            item for item in excluded if item not in speakers)),
        resolution_status=ref.resolution_status)


# ---------------------------------------------------------------------------
# 移行の記録
# ---------------------------------------------------------------------------

#: 移行に使ってよいリンクの確からしさ。**候補では動かさない。**
MIGRATION_MIN_CONFIDENCE = .75
#: 2つの関係値を「同じもの」と見なす差。これを超えたら競合。
#:
#: 平均しないのは、**平均した値が誰の関係でもなくなる**から。
#: 信頼 .9 の人と .3 の人を混ぜた .6 は、どちらの履歴とも合わない。
MERGE_TOLERANCE = .12


@dataclass(slots=True)
class IdentityMigrationRecord:
    """1件の移行。**戻すのに要るものだけを持つ。**

    `previous_data` はロールバック用の最小限のスナップショット。
    **声紋ベクトルも会話本文も入れない**（第12条）。
    """

    speaker_id: str
    person_id: str = ""
    identity_link_id: str = ""
    source_relationship_id: str = ""
    target_relationship_id: str = ""
    previous_data: dict[str, Any] = field(default_factory=dict)
    migrated_data: dict[str, Any] = field(default_factory=dict)
    migration_status: MigrationStatus | str = MigrationStatus.PENDING
    conflict_reason: str = ""
    created_at: float = field(default_factory=time.time)
    applied_at: float = .0
    rolled_back_at: float = .0
    migration_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def done(self) -> bool:
        return str(self.migration_status) == str(MigrationStatus.APPLIED)

    def snapshot(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "speaker_id": self.speaker_id, "person_id": self.person_id,
            "link_id": self.identity_link_id,
            "status": str(self.migration_status),
            "conflict": self.conflict_reason[:60],
            "applied_at": round(self.applied_at, 1),
            "rolled_back_at": round(self.rolled_back_at, 1),
            "dimensions": len(self.migrated_data),
        }


def _metrics_of(state) -> dict[str, float]:
    """関係値の数値軸だけを取り出す。**イベント本文は入れない。**"""
    from neuro_voice.mind.relationship import _METRICS

    return {name: round(float(getattr(state, name, 0.0)), 4) for name in _METRICS}


def compare_metrics(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    """軸ごとの差。**同じなら空。**"""
    return {key: round(float(right.get(key, 0.0)) - float(value), 4)
            for key, value in left.items()
            if abs(float(right.get(key, 0.0)) - float(value)) > .0001}


def untouched(state) -> bool:
    """まだ誰とも関わっていない関係値か。

    **既定値で埋まっている状態は「空」。** 新しく作った `RelationshipState` は
    信頼 .5 などの初期値を持つので、値の比較だけ見ると
    「移行元と食い違っている」ように見える——実際そう誤判定した。
    やり取りの回数と最終更新で「一度でも動いたか」を見る。
    """
    if state is None:
        return True
    return (not int(getattr(state, "interaction_count", 0) or 0)
            and not getattr(state, "last_updated_at", None)
            and not getattr(state, "recent_events", None))


def merge_verdict(left: dict[str, float], right: dict[str, float]) -> tuple[str, str]:
    """2つの関係値を1つにしてよいか。**平均はしない。**

    返り値は `(判定, 理由)`。判定は `safe` / `candidate` / `conflict`。
    """
    if not left or not right:
        # 片方が空なら、ある方をそのまま使える。
        return ("safe", "one_side_empty")
    difference = compare_metrics(left, right)
    if not difference:
        return ("safe", "identical")
    worst = max(abs(value) for value in difference.values())
    if worst <= MERGE_TOLERANCE:
        # 近いが同じではない。**人が見てから決める。**
        return ("candidate", f"close_within_{MERGE_TOLERANCE}")
    return ("conflict", f"differs_by_{round(worst, 3)}")


# ---------------------------------------------------------------------------
# 読み書きの1箇所
# ---------------------------------------------------------------------------


class RelationshipResolver:
    """関係値の読み書きを**ここだけ**に集める。

    各モジュールが直接 `RelationshipStore` を叩いていると、
    移行の段階を変えるたびに全箇所を直すことになり、必ず1つ忘れる。
    忘れた場所は**古い鍵のまま動き続ける**ので、気づくのが遅れる。
    """

    def __init__(
        self, legacy, person=None, *, resolver=None, mode: MigrationMode | str = MigrationMode.DISABLED,
    ) -> None:
        #: 既存の `RelationshipStore`（`speaker_id` 主キー）。**消さない。**
        self.legacy = legacy
        #: person 主キーの `RelationshipStore`。**同じクラス・同じ軸・同じ減衰。**
        self.person = person
        #: `IdentityResolver`。`speaker_id` → `person_id` を引く。
        self.identity = resolver
        self.mode = MigrationMode(str(mode)) if mode else MigrationMode.DISABLED
        #: 差分の記録（`SHADOW_READ` 用）。**値そのものは持ちすぎない。**
        self.last_difference: dict[str, float] = {}
        #: 二重適用を防ぐ、適用済みイベントIDの控え。
        self._applied_events: set[str] = set()
        self.counters: dict[str, int] = {
            "reads_legacy": 0, "reads_person": 0, "shadow_compared": 0,
            "shadow_differed": 0, "writes_legacy": 0, "writes_person": 0,
            "write_person_failed": 0, "fallbacks": 0, "duplicate_writes": 0,
        }
        self.last_error: str = ""

    # -- モード ---------------------------------------------------------

    def set_mode(self, mode: MigrationMode | str) -> MigrationMode:
        """**一度に1つだけ。** 矛盾した状態を作らせない。"""
        self.mode = MigrationMode(str(mode))
        return self.mode

    @property
    def writes_person(self) -> bool:
        return self.mode in _WRITES_PERSON and self.person is not None

    @property
    def reads_person(self) -> bool:
        return self.mode in _READS_PERSON and self.person is not None

    # -- 読み -----------------------------------------------------------

    def _person_state(self, ref: ParticipantIdentityRef):
        if self.person is None or not ref.resolved:
            return None
        return self.person.get(ref.person_id)

    def read(self, ref: ParticipantIdentityRef) -> tuple[Any, ReadSource, str]:
        """関係値を1つ返す。**どこから読んだかも一緒に返す。**

        返り値は `(状態, 出どころ, 落ちた理由)`。
        理由が空でないなら、person 側を使えなかったということ。
        """
        fallback = ""
        if self.reads_person:
            if ref.conflicted:
                # **人物が割れている間は、既存の関係値を使う。**
                fallback = "identity_conflicted"
            elif not ref.resolved:
                fallback = "person_unresolved"
            else:
                state = self._person_state(ref)
                if state is not None:
                    self.counters["reads_person"] += 1
                    return (state, ReadSource.PERSON, "")
                fallback = "person_relationship_missing"
        elif self.mode is MigrationMode.SHADOW_READ:
            self._compare(ref)
        if fallback:
            self.counters["fallbacks"] += 1
        self.counters["reads_legacy"] += 1
        return (self.legacy.get(ref.legacy_key), ReadSource.LEGACY, fallback)

    def snapshot(self, ref: ParticipantIdentityRef) -> dict[str, Any]:
        """既存 `RelationshipStore.snapshot` と同じ形で返す。"""
        state, source, fallback = self.read(ref)
        store = self.person if source is ReadSource.PERSON else self.legacy
        key = ref.person_id if source is ReadSource.PERSON else ref.legacy_key
        data = store.snapshot(key)
        data["_source"] = str(source)
        if fallback:
            data["_fallback"] = fallback
        del state
        return data

    def _compare(self, ref: ParticipantIdentityRef) -> dict[str, float]:
        """legacy と person の差を測る。**会話には使わない。**"""
        self.last_difference = {}
        if self.person is None or not ref.resolved:
            return {}
        self.counters["shadow_compared"] += 1
        difference = compare_metrics(
            _metrics_of(self.legacy.get(ref.legacy_key)),
            _metrics_of(self.person.get(ref.person_id)))
        if difference:
            self.counters["shadow_differed"] += 1
            self.last_difference = difference
            logger.info("関係値の差 (shadow): speaker=%s person=%s 軸=%d",
                        ref.speaker_id, ref.person_id, len(difference))
        return difference

    def shadow_difference(self, ref: ParticipantIdentityRef) -> dict[str, float]:
        return self._compare(ref)

    # -- 書き -----------------------------------------------------------

    def apply_state_delta(
        self, ref: ParticipantIdentityRef, dimension: str, delta: float, *,
        reason: str = "", event_id: str = "",
    ) -> dict[str, Any]:
        """差分を1軸へ入れる。**モードに応じた宛先へ、一度ずつ。**

        `event_id` は二重適用を防ぐ鍵。同じ出来事が2回来ても、
        person 側へ2回入れない（legacy 側は既存の実装が持つ）。
        """
        result: dict[str, Any] = {
            "targets": [], "legacy": None, "person": None,
            "duplicate": False, "error": "",
        }
        marker = f"{event_id}:{ref.person_id}:{dimension}" if event_id else ""
        if marker and marker in self._applied_events:
            self.counters["duplicate_writes"] += 1
            result["duplicate"] = True
            return result
        # legacy は常に更新する。**移行中に既存の記録を止めない。**
        # 止めると、途中でロールバックした時に空白期間ができる。
        result["legacy"] = self.legacy.apply_state_delta(
            ref.legacy_key, dimension, delta, reason=reason)
        result["targets"].append("legacy")
        self.counters["writes_legacy"] += 1
        if self.writes_person and ref.resolved and not ref.conflicted:
            try:
                result["person"] = self.person.apply_state_delta(
                    ref.person_id, dimension, delta, reason=reason)
                result["targets"].append("person")
                self.counters["writes_person"] += 1
            except Exception as error:  # noqa: BLE001 — 会話を止めない（第17条）
                # **片方だけ書けたのを成功扱いしない。**
                logger.exception("person側の関係値更新に失敗")
                self.counters["write_person_failed"] += 1
                self.last_error = f"{type(error).__name__}: {error}"[:120]
                result["error"] = self.last_error
        if marker and not result["error"]:
            self._applied_events.add(marker)
            if len(self._applied_events) > 512:
                self._applied_events = set(list(self._applied_events)[-256:])
        return result

    def observe(self, ref: ParticipantIdentityRef, meaningful: bool = True) -> None:
        """やり取りがあったことを数える。"""
        self.legacy.observe(ref.legacy_key, meaningful)
        if self.writes_person and ref.resolved and not ref.conflicted:
            with_person = self.person
            if with_person is not None:
                with_person.observe(ref.person_id, meaningful)

    # -- 状況 -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """診断用。**関係値そのものは出さない**（第12条）。"""
        return {
            "mode": str(self.mode),
            "reads_person": self.reads_person,
            "writes_person": self.writes_person,
            "person_store": self.person is not None,
            "counters": dict(self.counters),
            "last_difference_axes": len(self.last_difference),
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# 移行そのもの
# ---------------------------------------------------------------------------


class IdentityMigration:
    """`speaker_id` の関係値を `person_id` へ写す。**冪等で、戻せる。**"""

    def __init__(self, resolver: RelationshipResolver, *, journal=None) -> None:
        self._resolver = resolver
        #: 記録の保存先（`MemoryStore`）。無くても動くが残らない。
        self._journal = journal
        self.records: dict[str, IdentityMigrationRecord] = {}
        self.counters: dict[str, int] = {
            "applied": 0, "skipped": 0, "conflicts": 0, "rolled_back": 0,
        }

    # -- 引き当て -------------------------------------------------------

    def _usable_link(self, speaker_key: str):
        """移行に使ってよいリンク。**確認済み・高確信のものだけ。**

        複数の人物が候補に挙がったら `None` を返す——
        **僅差だからという理由で一方へ寄せない。**
        """
        identity = self._resolver.identity
        if identity is None:
            return None, "no_resolver"
        value = _voice_key(speaker_key)
        related = [item for item in identity.links.values()
                   if item.identity_value == value]
        # **一度でも割れた識別子は自動移行しない。**
        #
        # 争った末に片方が `CONFLICTED` になると、残るのは1件だけなので
        # 「候補は1人」に見える。だが関係値を写すのは取り返しがつかない
        # ——争った履歴がある相手は、人が確かめてからにする。
        if any(str(item.status) == "conflicted" for item in related):
            return None, "multiple_person_candidates"
        candidates = [item for item in related if item.usable]
        if not candidates:
            return None, "no_confirmed_link"
        people = {item.person_id for item in candidates}
        if len(people) > 1:
            # **人物が割れている。** 自動では決めない。
            return None, "multiple_person_candidates"
        link = candidates[0]
        if float(link.confidence) < MIGRATION_MIN_CONFIDENCE:
            return None, "confidence_too_low"
        return link, ""

    def record_for(self, speaker_key: str) -> IdentityMigrationRecord | None:
        return next((item for item in self.records.values()
                     if item.speaker_id == speaker_key), None)

    # -- 一対一 ---------------------------------------------------------

    def migrate_speaker(self, speaker_key: str) -> IdentityMigrationRecord:
        """1人ぶんを写す。**同じものを2回作らない。**"""
        existing = self.record_for(speaker_key)
        if existing is not None and existing.done:
            self.counters["skipped"] += 1
            return existing
        link, reason = self._usable_link(speaker_key)
        if link is None:
            record = existing or IdentityMigrationRecord(speaker_id=speaker_key)
            record.migration_status = (
                MigrationStatus.CONFLICT if reason == "multiple_person_candidates"
                else MigrationStatus.SKIPPED)
            record.conflict_reason = reason
            self.records[record.migration_id] = record
            self.counters["conflicts" if reason == "multiple_person_candidates"
                          else "skipped"] += 1
            return record
        person_store = self._resolver.person
        if person_store is None:
            record = existing or IdentityMigrationRecord(speaker_id=speaker_key)
            record.migration_status = MigrationStatus.SKIPPED
            record.conflict_reason = "no_person_store"
            self.records[record.migration_id] = record
            return record

        source = self._resolver.legacy.get(speaker_key)
        source_metrics = _metrics_of(source)
        target = person_store.get(link.person_id)
        target_metrics = _metrics_of(target)
        record = existing or IdentityMigrationRecord(speaker_id=speaker_key)
        record.person_id = link.person_id
        record.identity_link_id = link.link_id
        record.source_relationship_id = speaker_key
        record.target_relationship_id = link.person_id
        # **戻すのに要る分だけ。** 会話本文もイベント履歴も入れない。
        record.previous_data = dict(target_metrics)

        # **まだ何も入っていない相手なら、そのまま写せる。**
        # 既定値で埋まっているだけの状態を「食い違い」と読むと、
        # 一対一の移行が最初の1件から通らない（実際そうなった）。
        verdict, why = (("safe", "target_untouched") if untouched(target)
                        else merge_verdict(target_metrics, source_metrics))
        if verdict == "conflict":
            # **平均しない。** 値が違う2つの関係を1つにできない。
            record.migration_status = MigrationStatus.CONFLICT
            record.conflict_reason = why
            self.records[record.migration_id] = record
            self.counters["conflicts"] += 1
            self._persist(record)
            logger.warning("関係値の移行を保留（競合）: %s → %s (%s)",
                           speaker_key, link.person_id, why)
            return record

        # **`get()` は写しを返す。** 書き換えても保存されないので、
        # 移行専用の入口（`import_state`）を通す。会話中の更新用は
        # セッション上限で削られるので、丸ごと写す用途には使えない。
        applied = person_store.import_state(
            link.person_id, source_metrics,
            interaction_count=int(source.interaction_count),
            meaningful_interaction_count=int(source.meaningful_interaction_count),
            last_interaction_at=float(source.last_interaction_at or 0.0))
        record.migrated_data = applied
        record.migration_status = MigrationStatus.APPLIED
        record.conflict_reason = "" if verdict == "safe" else why
        record.applied_at = time.time()
        record.rolled_back_at = .0
        self.records[record.migration_id] = record
        self.counters["applied"] += 1
        self._persist(record)
        return record

    def migrate_all(self, speaker_keys=()) -> dict[str, Any]:
        """まとめて写す。**失敗した分は記録に残して続ける。**

        `applied` は**この呼び出しで実際に動いた分**だけ。既に済んでいた
        ものを毎回そこへ入れると、2回目も仕事をしたように見える。
        """
        out = {"applied": [], "conflicts": [], "skipped": []}
        for key in speaker_keys or ():
            before = self.record_for(str(key))
            was_done = before is not None and before.done
            record = self.migrate_speaker(str(key))
            status = str(record.migration_status)
            if was_done:
                out["skipped"].append(record.migration_id)
            elif status == str(MigrationStatus.APPLIED):
                out["applied"].append(record.migration_id)
            elif status == str(MigrationStatus.CONFLICT):
                out["conflicts"].append(record.migration_id)
            else:
                out["skipped"].append(record.migration_id)
        return out

    # -- 戻す -----------------------------------------------------------

    def rollback(self, migration_id: str) -> IdentityMigrationRecord | None:
        """person 側を移行前の値へ戻す。**記録は消さない。**"""
        record = self.records.get(migration_id)
        person_store = self._resolver.person
        if record is None or person_store is None or not record.done:
            return record
        person_store.import_state(record.person_id, dict(record.previous_data))
        record.migration_status = MigrationStatus.ROLLED_BACK
        record.rolled_back_at = time.time()
        self.counters["rolled_back"] += 1
        self._persist(record)
        return record

    def mark_needs_review(self, person_id: str, *, reason: str) -> list[str]:
        """リンクが変わった時に、写した分へ印を付ける。

        **物理削除しない。** 消すと、繋ぎ直しがまた誤りだった時に
        元の値へ戻せなくなる。
        """
        touched: list[str] = []
        for record in self.records.values():
            if record.person_id == person_id and record.done:
                record.migration_status = MigrationStatus.CONFLICT
                record.conflict_reason = str(reason)[:60]
                touched.append(record.migration_id)
                self._persist(record)
        if touched:
            self.counters["conflicts"] += len(touched)
        return touched

    # -- 記録 -----------------------------------------------------------

    def _persist(self, record: IdentityMigrationRecord) -> None:
        if self._journal is None:
            return
        try:
            self._journal.save_identity_migration({
                "migration_id": record.migration_id,
                "speaker_id": record.speaker_id, "person_id": record.person_id,
                "identity_link_id": record.identity_link_id,
                "source_relationship_id": record.source_relationship_id,
                "target_relationship_id": record.target_relationship_id,
                "previous_data": dict(record.previous_data),
                "migrated_data": dict(record.migrated_data),
                "migration_status": str(record.migration_status),
                "conflict_reason": record.conflict_reason,
                "created_at": record.created_at, "applied_at": record.applied_at,
                "rolled_back_at": record.rolled_back_at,
            })
        except Exception:  # noqa: BLE001 — 記録の失敗で会話を止めない
            logger.exception("移行記録の保存に失敗")

    def hydrate(self) -> int:
        """記録を戻す。**完了済みをもう一度走らせない。**"""
        if self._journal is None:
            return 0
        try:
            rows = self._journal.identity_migrations()
        except Exception:  # noqa: BLE001
            logger.exception("移行記録の読み込みに失敗")
            return 0
        for row in rows:
            record = IdentityMigrationRecord(
                speaker_id=str(row.get("speaker_id", "")),
                person_id=str(row.get("person_id", "") or ""),
                identity_link_id=str(row.get("identity_link_id", "") or ""),
                source_relationship_id=str(row.get("source_relationship_id", "") or ""),
                target_relationship_id=str(row.get("target_relationship_id", "") or ""),
                previous_data=dict(row.get("previous_data") or {}),
                migrated_data=dict(row.get("migrated_data") or {}),
                migration_status=str(row.get("migration_status", "pending")),
                conflict_reason=str(row.get("conflict_reason", "") or ""),
                created_at=float(row.get("created_at", 0.0) or 0.0),
                applied_at=float(row.get("applied_at", 0.0) or 0.0),
                rolled_back_at=float(row.get("rolled_back_at", 0.0) or 0.0),
                migration_id=str(row.get("migration_id", "")))
            self.records[record.migration_id] = record
        return len(rows)

    def unfinished(self) -> list[IdentityMigrationRecord]:
        """途中で終わっているもの。**再試行するか legacy へ戻す。**"""
        return [item for item in self.records.values()
                if str(item.migration_status) in {str(MigrationStatus.PENDING),
                                                  str(MigrationStatus.PARTIAL)}]

    # -- 段階を上げてよいか (Phase 6F) -----------------------------------

    def dry_run(self, speaker_keys=()) -> MigrationDryRunResult:
        """**何も変えずに**、移せるかどうかだけ見る。

        モードもリレーションも Identity Link も触らない。
        押す前に「何件動いて、何件止まるか」が分かるようにする。
        """
        identity = self._resolver.identity
        person_store = self._resolver.person
        keys = [str(item) for item in (speaker_keys or ()) if item]
        blocked: list[str] = []
        confirmed = unresolved = conflicting = 0
        migratable = conflicts = creates = updates = 0
        for key in keys:
            link, reason = self._usable_link(key)
            if link is None:
                if reason == "multiple_person_candidates":
                    conflicting += 1
                else:
                    unresolved += 1
                blocked.append(f"{key}:{reason}")
                continue
            confirmed += 1
            if person_store is None:
                blocked.append(f"{key}:no_person_store")
                continue
            target = person_store.get(link.person_id)
            source_metrics = _metrics_of(self._resolver.legacy.get(key))
            if untouched(target):
                migratable += 1
                creates += 1
                continue
            verdict, why = merge_verdict(_metrics_of(target), source_metrics)
            if verdict == "conflict":
                conflicts += 1
                blocked.append(f"{key}:{why}")
            else:
                migratable += 1
                updates += 1
        if identity is None:
            blocked.append("no_identity_resolver")
        if person_store is None:
            blocked.append("no_person_store")
        return MigrationDryRunResult(
            total_speakers=len(keys), confirmed_links=confirmed,
            unresolved_speakers=unresolved, conflicting_links=conflicting,
            migratable_relationships=migratable, relationship_conflicts=conflicts,
            expected_creates=creates, expected_updates=updates,
            blocked_reasons=tuple(dict.fromkeys(blocked))[:8],
            # 競合があっても shadow は始められる——**読むだけだから。**
            safe_to_start_shadow=(identity is not None and person_store is not None))

    def can_transition(self, target: MigrationMode | str) -> TransitionVerdict:
        """段階を変えてよいか。**UIのボタンではなく、ここが決める。**"""
        current = self._resolver.mode
        wanted = coerce_mode(target)
        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if wanted is current:
            return TransitionVerdict(True, "already_in_mode")
        if wanted not in allowed:
            # **飛び越しを拒む。** 差を見ないまま正式採用にしない。
            return TransitionVerdict(
                False, f"transition_not_allowed:{current}->{wanted}",
                (f"allowed:{','.join(sorted(str(x) for x in allowed))}",))
        blockers = list(self._blockers_for(wanted))
        if blockers:
            return TransitionVerdict(False, "preconditions_not_met",
                                     tuple(blockers))
        return TransitionVerdict(True, f"{current}->{wanted}")

    def _blockers_for(self, wanted: MigrationMode):
        """その段階へ上がるのを止める理由。**戻す方向は止めない。**"""
        resolver = self._resolver
        if wanted in {MigrationMode.LEGACY_ROLLBACK, MigrationMode.DISABLED}:
            # 戻すのはいつでもできる。**逃げ道を条件付きにしない。**
            return ()
        blockers: list[str] = []
        if resolver.person is None:
            blockers.append("no_person_store")
        if resolver.identity is None:
            blockers.append("no_identity_resolver")
        if wanted is MigrationMode.SHADOW_READ:
            return tuple(blockers)
        if wanted is MigrationMode.DUAL_WRITE:
            if resolver.counters["shadow_compared"] < MIN_SHADOW_READS:
                blockers.append(
                    f"shadow_reads:{resolver.counters['shadow_compared']}"
                    f"/{MIN_SHADOW_READS}")
            if resolver.counters["write_person_failed"]:
                blockers.append(
                    f"unresolved_write_failures:"
                    f"{resolver.counters['write_person_failed']}")
        if wanted is MigrationMode.PERSON_PRIMARY:
            if not resolver.counters["writes_person"]:
                blockers.append("dual_write_never_ran")
            if resolver.counters["write_person_failed"]:
                # **部分的失敗が残っているうちは昇格しない。**
                blockers.append(
                    f"unresolved_write_failures:"
                    f"{resolver.counters['write_person_failed']}")
            if resolver.last_difference:
                blockers.append(f"shadow_difference:{len(resolver.last_difference)}")
            if self.unfinished():
                blockers.append(f"unfinished_migrations:{len(self.unfinished())}")
        return tuple(blockers)

    def resync(self) -> dict[str, Any]:
        """食い違いを測り直して、失敗の数え上げを戻す。

        **勝手に値を合わせない。** 差がまだあるなら残す——
        「再同期した」で消していい食い違いではない。
        """
        resolver = self._resolver
        before = resolver.counters["write_person_failed"]
        resolver.counters["write_person_failed"] = 0
        resolver.last_error = ""
        return {"cleared_failures": before,
                "remaining_difference": len(resolver.last_difference)}

    def status(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for record in self.records.values():
            key = str(record.migration_status)
            by_status[key] = by_status.get(key, 0) + 1
        last = max(self.records.values(), key=lambda item: item.applied_at,
                   default=None)
        return {
            "records": len(self.records),
            "by_status": by_status,
            "unfinished": len(self.unfinished()),
            "counters": dict(self.counters),
            "last_migration_id": last.migration_id if last else "",
            "last_applied_at": round(last.applied_at, 1) if last else 0.0,
            "can_rollback": any(item.done for item in self.records.values()),
        }


# ---------------------------------------------------------------------------
# 段階を上げてよいか (Phase 6F)
#
# **UIのボタンを隠すだけでは足りない。** 押せなくしても、別の経路から
# 呼べば通ってしまう。判断はここに置き、UIはこれを呼ぶだけにする。
# ---------------------------------------------------------------------------

#: 許される遷移。**飛び越しは無い。**
#:
#: `disabled → person_primary` を許すと、差を一度も見ないまま
#: person 側が正式採用になる。そこで初めて食い違いに気づいても、
#: どのターンから間違っていたのかが分からない。
ALLOWED_TRANSITIONS: dict[MigrationMode, frozenset[MigrationMode]] = {
    MigrationMode.DISABLED: frozenset({MigrationMode.SHADOW_READ}),
    MigrationMode.SHADOW_READ: frozenset({
        MigrationMode.DUAL_WRITE, MigrationMode.DISABLED,
        MigrationMode.LEGACY_ROLLBACK}),
    MigrationMode.DUAL_WRITE: frozenset({
        MigrationMode.PERSON_PRIMARY, MigrationMode.LEGACY_ROLLBACK}),
    MigrationMode.PERSON_PRIMARY: frozenset({MigrationMode.LEGACY_ROLLBACK}),
    #: 戻した後は、もう一度下から。**いきなり person へ戻さない。**
    MigrationMode.LEGACY_ROLLBACK: frozenset({
        MigrationMode.DISABLED, MigrationMode.SHADOW_READ}),
}

#: `SHADOW_READ` を抜けるのに要る観測回数。**一度見ただけでは足りない。**
MIN_SHADOW_READS = 20


@dataclass(frozen=True, slots=True)
class TransitionVerdict:
    """段階を変えてよいか。**駄目な理由を必ず持つ。**"""

    allowed: bool
    reason: str = ""
    blockers: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "blockers": list(self.blockers[:6])}


@dataclass(frozen=True, slots=True)
class MigrationDryRunResult:
    """実際には何も変えずに、移せるかどうかだけ見る。"""

    total_speakers: int = 0
    confirmed_links: int = 0
    unresolved_speakers: int = 0
    conflicting_links: int = 0
    migratable_relationships: int = 0
    relationship_conflicts: int = 0
    expected_creates: int = 0
    expected_updates: int = 0
    blocked_reasons: tuple[str, ...] = ()
    safe_to_start_shadow: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_speakers": self.total_speakers,
            "confirmed_links": self.confirmed_links,
            "unresolved_speakers": self.unresolved_speakers,
            "conflicting_links": self.conflicting_links,
            "migratable": self.migratable_relationships,
            "relationship_conflicts": self.relationship_conflicts,
            "creates": self.expected_creates,
            "updates": self.expected_updates,
            "blocked": list(self.blocked_reasons[:6]),
            "safe_to_start_shadow": self.safe_to_start_shadow,
        }


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    #: 動いたが、決められない分が残った。**失敗ではないが完了でもない。**
    COMPLETED_WITH_CONFLICTS = "completed_with_conflicts"
    FAILED = "failed"
    #: プロセスが落ちて途中で止まった。**再開できる。**
    INTERRUPTED_RECOVERABLE = "interrupted_recoverable"
    CANCELLED = "cancelled"


#: このプロセスの起動ID。**「まだ生きているか」を見分けるため。**
#:
#: 起動IDが違うのに `RUNNING` のまま残っているなら、それは前回落ちた分。
#: 時刻だけで判断すると、長い移行と落ちた移行を区別できない。
PROCESS_ID = uuid.uuid4().hex[:12]

#: 中断とみなすまでの無応答時間。**同じプロセス内での保険。**
STALE_JOB_SECONDS = 300.0


@dataclass(slots=True)
class MigrationJob:
    """1回の移行作業。**会話とは別の場所で走る。**"""

    operation: str
    status: JobStatus | str = JobStatus.PENDING
    requested_mode: str = ""
    previous_mode: str = ""
    requested_at: float = field(default_factory=time.time)
    started_at: float = .0
    updated_at: float = .0
    completed_at: float = .0
    progress: float = .0
    #: どこまで進んだか。**再開はここから。**
    progress_cursor: str = ""
    processed_count: int = 0
    success_count: int = 0
    conflict_count: int = 0
    failure_count: int = 0
    error_summary: str = ""
    process_id: str = PROCESS_ID
    #: 新規 / 再開 / 再試行 / 戻し。**どれで始まったかを残す。**
    start_kind: str = "new"
    #: 何に対する何の作業か。**同一性の正本**（Phase 7B）。
    operation_key: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def running(self) -> bool:
        return str(self.status) in {str(JobStatus.PENDING), str(JobStatus.RUNNING)}

    @property
    def resumable(self) -> bool:
        return str(self.status) == str(JobStatus.INTERRUPTED_RECOVERABLE)

    @property
    def blocks_new_work(self) -> bool:
        """**新しい作業を始めてよいか。** 中断中も止める。"""
        return self.running or self.resumable

    def row(self) -> dict[str, Any]:
        """保存する形。**声紋も記憶本文も関係値も入れない**（第12条）。"""
        return {
            "job_id": self.job_id, "operation": self.operation,
            "requested_mode": self.requested_mode,
            "previous_mode": self.previous_mode,
            "status": str(self.status), "progress_cursor": self.progress_cursor,
            "processed_count": self.processed_count,
            "success_count": self.success_count,
            "conflict_count": self.conflict_count,
            "failure_count": self.failure_count,
            "process_id": self.process_id,
            "created_at": self.requested_at, "started_at": self.started_at,
            "updated_at": self.updated_at or time.time(),
            "completed_at": self.completed_at,
            "error_summary": self.error_summary,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "operation": self.operation,
            "status": str(self.status), "progress": round(self.progress, 2),
            "cursor": self.progress_cursor,
            "processed": self.processed_count, "success": self.success_count,
            "conflicts": self.conflict_count, "failures": self.failure_count,
            "error": self.error_summary[:80],
            "start_kind": self.start_kind,
            "resumable": self.resumable,
            "requested_at": round(self.requested_at, 1),
            "completed_at": round(self.completed_at, 1),
            "detail": dict(self.detail),
        }


def job_from_row(row) -> MigrationJob:
    """保存した行から戻す。"""
    return MigrationJob(
        operation=str(row.get("operation", "")),
        status=str(row.get("status", "pending")),
        requested_mode=str(row.get("requested_mode", "") or ""),
        previous_mode=str(row.get("previous_mode", "") or ""),
        requested_at=float(row.get("created_at", 0.0) or 0.0),
        started_at=float(row.get("started_at", 0.0) or 0.0),
        updated_at=float(row.get("updated_at", 0.0) or 0.0),
        completed_at=float(row.get("completed_at", 0.0) or 0.0),
        progress_cursor=str(row.get("progress_cursor", "") or ""),
        processed_count=int(row.get("processed_count", 0) or 0),
        success_count=int(row.get("success_count", 0) or 0),
        conflict_count=int(row.get("conflict_count", 0) or 0),
        failure_count=int(row.get("failure_count", 0) or 0),
        error_summary=str(row.get("error_summary", "") or ""),
        process_id=str(row.get("process_id", "") or ""),
        job_id=str(row.get("job_id", "")))


class MigrationJobRunner:
    """移行を**別スレッドで1つずつ**走らせる。

    音声の経路で同期実行すると、移行の間ずっと応答が止まる。
    危険警告も出ない。**会話を止めない**のが先（第17条）。

    同時に2つ走らせないのは、両方が同じ関係値を書くから。
    二重送信も1つ目に合流させる——UIのボタン連打で2重移行しない。
    """

    def __init__(self, migration: "IdentityMigration", *, store=None) -> None:
        self._migration = migration
        #: 作業の状態を残す先（`MemoryStore`）。**無くても動くが残らない。**
        self._store = store
        self._lock = threading.Lock()
        self._thread: Any = None
        #: 作業の同一性 → その作業。**「連打で2件」を防ぐ正本。**
        self._by_key: dict[str, MigrationJob] = {}
        self.current: MigrationJob | None = None
        self.history: list[MigrationJob] = []
        self.recovered: MigrationJob | None = None

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    @property
    def blocked(self) -> MigrationJob | None:
        """新しい作業を止めているもの。**中断中も止める。**"""
        for job in (self.current, self.recovered):
            if job is not None and job.blocks_new_work:
                return job
        return None

    # -- 保存と復旧 -----------------------------------------------------

    def _persist(self, job: MigrationJob) -> None:
        if self._store is None:
            return
        job.updated_at = time.time()
        try:
            self._store.save_migration_job(job.row())
        except Exception:  # noqa: BLE001 — 記録の失敗で会話を止めない
            logger.exception("移行ジョブの保存に失敗")

    def recover(self) -> MigrationJob | None:
        """起動時に、前回落ちた作業を拾う。

        **勝手に再開しない。** 「途中で止まっている」と言えるようにする
        だけで、続けるかどうかは人が決める。
        """
        if self._store is None:
            return None
        try:
            # **起動IDが違う `RUNNING` は、前回落ちた分。**
            self._store.interrupt_stale_jobs(PROCESS_ID)
            rows = self._store.migration_jobs(limit=20)
        except Exception:  # noqa: BLE001
            logger.exception("移行ジョブの復旧に失敗")
            return None
        self.history = [job_from_row(row) for row in reversed(rows)][-12:]
        for job in reversed(self.history):
            if job.resumable:
                self.recovered = job
                logger.info("前回の移行が中断されている: %s (%d件処理済み)",
                            job.job_id, job.processed_count)
                return job
        return None

    # -- 開始 -----------------------------------------------------------

    @staticmethod
    def operation_key(operation: str, speaker_keys=()) -> str:
        """同じ作業かどうかの正本。

        **時刻でも呼ばれた回数でもなく、何に対する何の作業か**で決める。
        「連打で2件」を防ぐのに `busy` だけを見ると、1件目が終わった
        直後の2回目が通る——押した人には1回のつもりでも2件走る。
        """
        targets = ",".join(sorted(str(item) for item in (speaker_keys or ())))
        return f"{str(operation)}|{targets}"

    def start(self, operation: str, speaker_keys=(), *, resume_of=None,
              start_kind: str = "new") -> MigrationJob:
        """開始する。**同じ作業が既にあるなら、それをそのまま返す。**

        取得と作成を1つのロックの中でやる（compare-and-set）。
        「無ければ作る」を2回に分けると、その隙間で2件できる。
        """
        key = self.operation_key(operation, speaker_keys)
        with self._lock:
            blocked = self.blocked
            if blocked is not None and resume_of is None:
                return blocked
            if resume_of is None:
                existing = self._by_key.get(key)
                if existing is not None:
                    # **同じ作業。** 走っていても、たった今終わっていても、
                    # 押した人にとっては同じ1回。新しく作らない。
                    return existing
            job = resume_of or MigrationJob(operation=str(operation))
            job.operation_key = key
            self._by_key[key] = job
            job.start_kind = str(start_kind)
            job.status = JobStatus.PENDING
            job.process_id = PROCESS_ID
            self.current = job
            if job not in self.history:
                self.history.append(job)
            del self.history[:-12]
            if self.recovered is job:
                self.recovered = None
        self._persist(job)
        thread = threading.Thread(
            target=self._run, args=(job, tuple(speaker_keys or ())),
            name=f"migration-{job.job_id}", daemon=True)
        self._thread = thread
        thread.start()
        return job

    def resume(self, speaker_keys=()) -> MigrationJob | None:
        """中断した作業を続ける。**完了済みは飛ばす。**"""
        job = self.recovered
        if job is None or not job.resumable:
            return None
        return self.start(job.operation, speaker_keys, resume_of=job,
                          start_kind="resume")

    def cancel_recovered(self, *, reason: str = "rolled_back") -> MigrationJob | None:
        """中断した作業を諦める。**戻す時に呼ぶ。**"""
        job = self.recovered
        if job is None:
            return None
        job.status = JobStatus.CANCELLED
        job.error_summary = str(reason)[:80]
        job.completed_at = time.time()
        self._persist(job)
        self.recovered = None
        return job

    def _run(self, job: MigrationJob, speaker_keys) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or time.time()
        job.process_id = PROCESS_ID
        self._persist(job)
        # **再開なら、済んだところまで飛ばす。**
        pending = list(speaker_keys)
        if job.progress_cursor and job.progress_cursor in pending:
            pending = pending[pending.index(job.progress_cursor) + 1:]
        total = max(1, len(speaker_keys))
        done = job.processed_count
        try:
            for key in pending:
                record = self._migration.migrate_speaker(str(key))
                done += 1
                job.processed_count = done
                job.progress_cursor = str(key)
                status = str(record.migration_status)
                if status == str(MigrationStatus.APPLIED):
                    job.success_count += 1
                elif status == str(MigrationStatus.CONFLICT):
                    job.conflict_count += 1
                job.progress = min(1.0, done / total)
                # **1件ごとに残す。** 途中で落ちても、どこまで進んだかが分かる。
                self._persist(job)
            job.status = (JobStatus.COMPLETED_WITH_CONFLICTS if job.conflict_count
                          else JobStatus.COMPLETED)
        except Exception as error:  # noqa: BLE001 — 会話は止めない（第17条）
            logger.exception("移行ジョブが失敗")
            job.status = JobStatus.FAILED
            job.failure_count += 1
            job.error_summary = f"{type(error).__name__}: {error}"[:120]
        finally:
            job.progress = 1.0
            job.completed_at = time.time()
            self._persist(job)

    def wait(self, timeout: float = 10.0) -> MigrationJob | None:
        """テストと終了処理のために待つ。**普段の会話からは呼ばない。**"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.current

    def status(self) -> dict[str, Any]:
        """UI再起動後もここから状態を取り直せる。"""
        blocked = self.blocked
        return {
            "busy": self.busy,
            "blocked_by": blocked.snapshot() if blocked else None,
            "recovered": self.recovered.snapshot() if self.recovered else None,
            "current": self.current.snapshot() if self.current else None,
            "history": [item.snapshot() for item in self.history[-4:]],
            "process_id": PROCESS_ID,
        }


def _voice_key(speaker_key: str) -> str:
    """関係値の鍵 (`speaker:3`) → 声紋リンクの値 (`voice:3`)。"""
    text = str(speaker_key or "")
    if text.startswith("speaker:"):
        return f"voice:{text.split(':', 1)[1]}"
    return text


def coerce_mode(value: Any) -> MigrationMode:
    """設定から読んだ文字列をモードへ。**知らない値は `DISABLED`。**"""
    try:
        return MigrationMode(str(value or "").strip().lower())
    except ValueError:
        logger.warning("知らない移行モード: %r（disabled として扱う）", value)
        return MigrationMode.DISABLED


def resolve_mode(cfg) -> MigrationMode:
    """設定からモードを1つ決める。**矛盾したフラグを同時に立てさせない。**

    個別フラグ（`shadow_read_enabled` 等）は、`mode` と食い違ったら
    **弱い方を採る**。強い方へ倒すと、間違えて上げた1つのフラグで
    person 側が正式採用になる。
    """
    get = getattr(cfg, "get", None) or (lambda _key, default=None: default)
    if not bool(get("identity_migration.enabled", False)):
        return MigrationMode.DISABLED
    mode = coerce_mode(get("identity_migration.mode", "disabled"))
    if mode is MigrationMode.PERSON_PRIMARY and not bool(
            get("identity_migration.person_primary_enabled", False)):
        mode = MigrationMode.DUAL_WRITE
    if mode is MigrationMode.DUAL_WRITE and not bool(
            get("identity_migration.dual_write_enabled", False)):
        mode = MigrationMode.SHADOW_READ
    if mode is MigrationMode.SHADOW_READ and not bool(
            get("identity_migration.shadow_read_enabled", False)):
        mode = MigrationMode.DISABLED
    return mode


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "ALLOWED_TRANSITIONS", "MERGE_TOLERANCE", "MIGRATION_MIN_CONFIDENCE",
    "MIN_SHADOW_READS",
    "IdentityMigration", "IdentityMigrationRecord", "IdentityScope", "JobStatus",
    "MigrationDryRunResult", "MigrationJob", "MigrationJobRunner",
    "MigrationMode", "MigrationStatus", "ParticipantIdentityRef", "ReadSource",
    "RelationshipResolver", "TransitionVerdict",
    "coerce_mode", "compare_metrics", "identity_scope", "merge_verdict",
    "participant_ref", "resolve_mode", "untouched",
]
