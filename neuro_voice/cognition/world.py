"""**いまどうなっているか。** 過去に何が起きたかとは別物。

Phase 3 のエピソード記憶は「過去に何が起きたか」を持つ。ここが持つのは
「**現在どうなっているか**」——誰がいて、何が見えていて、何をしている最中か。

同じDBレコードへ押し込まない。性質が違うため:

* 記憶は**増えていく**。世界状態は**古くなって消える**
* 記憶は確信が上がっていく。世界状態は**放っておくと確信が落ちる**
* 記憶は「本当にあったこと」。世界状態は「たぶん今もそう」

**完全な世界シミュレーションではない。** 会話とゲームと共有タスクを
続けて理解するのに要る分だけ、根拠つきで持つ。全画面のオブジェクトを
永久に溜めるようなことはしない。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.types import InformationType


class ItemStatus(StrEnum):
    """いまその情報をどう扱うか。**「知らない」と「無い」を混ぜない。**"""

    ACTIVE = "active"
    #: 古くなった。**まだ在るかもしれないが、根拠として使わない。**
    STALE = "stale"
    UNCERTAIN = "uncertain"
    CONTRADICTED = "contradicted"
    COMPLETED = "completed"
    REMOVED = "removed"


#: 根拠として使ってよい状態。
USABLE = frozenset({ItemStatus.ACTIVE})


class Presence(StrEnum):
    """在るかどうかの度合い。**見失っただけで「無い」と言わない。**

    画面から消えたのは、視界の外へ出たのか、無くなったのか。区別しないと
    「さっきの作業台が無くなった」と誤って断言する。
    """

    VISIBLE = "visible"
    #: 見えていないが、在ると考えるのが自然。
    INFERRED = "inferred"
    #: 直前まで在った。
    RECENTLY_SEEN = "recently_seen"
    #: 無いことを確かめた。
    CONFIRMED_ABSENT = "confirmed_absent"
    UNKNOWN = "unknown"


class EntityType(StrEnum):
    PARTICIPANT = "participant"
    GAME_CHARACTER = "game_character"
    OBJECT = "object"
    LOCATION = "location"
    TASK_TARGET = "task_target"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class DeltaOperation(StrEnum):
    ADD = "add"
    UPDATE = "update"
    CONFIRM = "confirm"
    CONTRADICT = "contradict"
    MARK_STALE = "mark_stale"
    COMPLETE = "complete"
    REMOVE = "remove"


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


# ---------------------------------------------------------------------------
# 対象
# ---------------------------------------------------------------------------

#: 既知の対象へ寄せてよい確からしさ。**これ未満は別物として持つ。**
#:
#: 低い確信で「たぶんあの人だ」と統合すると、別人の情報が混ざる。
#: 混ざった後で分離するのは、最初から分けておくよりずっと難しい。
MERGE_CONFIDENCE = .75


@dataclass(slots=True)
class WorldEntity:
    """人・物・場所・ゲーム上の対象を1つの形で。"""

    canonical_name: str
    entity_type: EntityType | str = EntityType.UNKNOWN
    aliases: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    presence: Presence | str = Presence.VISIBLE
    confidence: float = .6
    source_event_ids: tuple[str, ...] = ()
    first_observed_at: float = field(default_factory=time.monotonic)
    last_observed_at: float = field(default_factory=time.monotonic)
    status: ItemStatus | str = ItemStatus.ACTIVE
    entity_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def matches(self, name: str) -> bool:
        value = str(name or "").strip().lower()
        if not value:
            return False
        return value in {self.canonical_name.lower(),
                         *(item.lower() for item in self.aliases)}

    def snapshot(self) -> dict[str, Any]:
        """トレース用。**属性の中身は入れない**（第12条）。"""
        return {
            "entity_id": self.entity_id,
            "type": str(self.entity_type),
            "name": self.canonical_name[:40],
            "presence": str(self.presence),
            "confidence": round(float(self.confidence), 2),
            "status": str(self.status),
            "attributes": len(self.attributes),
        }


def resolve_entity(
    name: str, known: list[WorldEntity] | tuple[WorldEntity, ...], *,
    confidence: float = .6, entity_type: EntityType | str = EntityType.UNKNOWN,
) -> WorldEntity | None:
    """既知の対象へ寄せてよいか。**寄せられないなら None。**

    確信が足りないものを勝手に統合しない。「たぶんあの人」で統合すると、
    別人の情報が混ざり、後から分離できなくなる。
    """
    if _clamp(confidence) < MERGE_CONFIDENCE:
        return None
    for entity in known:
        if str(entity.status) in {str(ItemStatus.REMOVED)}:
            continue
        if entity.matches(name):
            # 種類が食い違うなら別物。人と場所を同じ名前でまとめない。
            if (str(entity_type) != str(EntityType.UNKNOWN)
                    and str(entity.entity_type) != str(EntityType.UNKNOWN)
                    and str(entity.entity_type) != str(entity_type)):
                continue
            return entity
    return None


# ---------------------------------------------------------------------------
# 事実
# ---------------------------------------------------------------------------

#: 述語ごとの寿命（秒）。**すべてを永久に現在状態として残さない。**
#:
#: HPは数秒で古くなるが、「いまMinecraftをやっている」はセッション中もつ。
#: 一律にすると、どちらかが必ずおかしくなる。
FACT_TTL: dict[str, float] = {
    "hp": 8.0,
    "position": 10.0,
    "visible": 15.0,
    "speaking": 6.0,
    "holding": 60.0,
    "nearby": 45.0,
    "playing": 3600.0,
    "searching_for": 600.0,
    "activity": 1800.0,
}
_DEFAULT_TTL = 120.0


@dataclass(slots=True)
class WorldFact:
    """「いまこうなっている」1つ。**出典と鮮度を必ず持つ。**"""

    subject_id: str
    predicate: str
    value: Any = True
    confidence: float = .6
    source_type: InformationType | str = InformationType.OBSERVATION
    source_event_ids: tuple[str, ...] = ()
    observed_at: float = field(default_factory=time.monotonic)
    last_verified_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    status: ItemStatus | str = ItemStatus.ACTIVE
    fact_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        if self.expires_at is None:
            self.expires_at = self.observed_at + FACT_TTL.get(
                self.predicate, _DEFAULT_TTL)

    @property
    def key(self) -> str:
        return f"{self.subject_id}:{self.predicate}"

    def stale(self, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return self.expires_at is not None and moment > self.expires_at

    def usable(self, *, now: float | None = None) -> bool:
        """根拠として使ってよいか。**古い事実で断定しない。**"""
        return str(self.status) in {str(x) for x in USABLE} and not self.stale(now=now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject_id,
            "predicate": self.predicate,
            "confidence": round(float(self.confidence), 2),
            "status": str(self.status),
            "source": str(self.source_type),
            "events": list(self.source_event_ids[:3]),
        }


#: これ未満の確信の事実は、断定の根拠にしない。
FACT_CONFIDENCE_FLOOR = .55
#: 既存の事実を覆すのに要る確信の差。**僅差では覆さない。**
CONTRADICTION_MARGIN = .15


@dataclass(slots=True)
class WorldStateDelta:
    """状況の変化1つ。**LLMに現在世界を直接書かせないための型。**"""

    operation: DeltaOperation | str
    target_id: str = ""
    field_name: str = ""
    previous_value: Any = None
    proposed_value: Any = None
    applied_value: Any = None
    confidence: float = .6
    reason: str = ""
    source_event_ids: tuple[str, ...] = ()
    expires_at: float | None = None
    rejected_reason: str = ""
    delta_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def applied(self) -> bool:
        return not self.rejected_reason

    def snapshot(self) -> dict[str, Any]:
        return {
            "operation": str(self.operation),
            "target": self.target_id,
            "field": self.field_name,
            "confidence": round(float(self.confidence), 2),
            "applied": self.applied,
            "rejected": self.rejected_reason,
            "reason": self.reason[:60],
            "events": list(self.source_event_ids[:3]),
        }


# ---------------------------------------------------------------------------
# 現在状況
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GroundedWorldState:
    """いまの状況。**短い構造だけ。巨大な知識グラフは作らない。**"""

    version: int = 0
    updated_at: float = field(default_factory=time.monotonic)
    active_scene: str = ""
    entities: dict[str, WorldEntity] = field(default_factory=dict)
    facts: dict[str, WorldFact] = field(default_factory=dict)
    active_topics: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    recent_changes: list[WorldStateDelta] = field(default_factory=list)
    state_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # -- 読む -----------------------------------------------------------

    def participants(self) -> list[WorldEntity]:
        return [item for item in self.entities.values()
                if str(item.entity_type) == str(EntityType.PARTICIPANT)
                and str(item.status) != str(ItemStatus.REMOVED)]

    def fact(self, subject_id: str, predicate: str) -> WorldFact | None:
        return self.facts.get(f"{subject_id}:{predicate}")

    def usable_facts(self, *, now: float | None = None) -> list[WorldFact]:
        """根拠として使える事実だけ。**古いものは含めない。**"""
        return [item for item in self.facts.values() if item.usable(now=now)]

    def stale_facts(self, *, now: float | None = None) -> list[WorldFact]:
        return [item for item in self.facts.values() if item.stale(now=now)]

    def find(self, name: str) -> WorldEntity | None:
        for entity in self.entities.values():
            if entity.matches(name):
                return entity
        return None

    # -- 手入れ ---------------------------------------------------------

    def sweep(self, *, now: float | None = None, max_entities: int = 40) -> list[str]:
        """古い事実へ印を付け、見失った対象の在り方を下げる。

        **消さずに落とす。** 「見えなくなった」と「無くなった」は違う。
        """
        moment = time.monotonic() if now is None else now
        marked: list[str] = []
        for fact in self.facts.values():
            if fact.stale(now=moment) and str(fact.status) == str(ItemStatus.ACTIVE):
                fact.status = ItemStatus.STALE
                marked.append(fact.fact_id)
        for entity in self.entities.values():
            age = moment - entity.last_observed_at
            if str(entity.presence) == str(Presence.VISIBLE) and age > 20.0:
                entity.presence = Presence.RECENTLY_SEEN
            elif str(entity.presence) == str(Presence.RECENTLY_SEEN) and age > 120.0:
                # **在ると推測はするが、見えてはいない。**
                entity.presence = Presence.INFERRED
        # 全画面のオブジェクトを永久に溜めない。
        if len(self.entities) > max_entities:
            ordered = sorted(self.entities.values(), key=lambda x: x.last_observed_at)
            for entity in ordered[:len(self.entities) - max_entities]:
                self.entities.pop(entity.entity_id, None)
        return marked

    def summary(self, *, now: float | None = None) -> dict[str, Any]:
        """トレース用。**画面全文も音声全文も入れない**（第12条）。"""
        moment = time.monotonic() if now is None else now
        return {
            "version": self.version,
            "scene": self.active_scene[:40],
            "entities": len(self.entities),
            "participants": len(self.participants()),
            "facts_usable": len(self.usable_facts(now=moment)),
            "facts_stale": len(self.stale_facts(now=moment)),
            "topics": list(self.active_topics[:4]),
            "questions": len(self.unresolved_questions),
            "recent_deltas": [item.snapshot() for item in self.recent_changes[-6:]],
        }


# ---------------------------------------------------------------------------
# 差分の適用
# ---------------------------------------------------------------------------


class WorldStateGate:
    """**現在世界を書き換えてよいかを決める唯一の場所。**

    LLM が「たぶんこうなっている」と言っても、ここを通らなければ状態は
    変わらない。確信・矛盾・鮮度をここで見る。
    """

    def __init__(
        self, *, confidence_floor: float = FACT_CONFIDENCE_FLOOR,
        contradiction_margin: float = CONTRADICTION_MARGIN,
    ) -> None:
        self.confidence_floor = float(confidence_floor)
        self.contradiction_margin = float(contradiction_margin)

    def observe_entity(
        self, state: GroundedWorldState, name: str, *,
        entity_type: EntityType | str = EntityType.UNKNOWN,
        confidence: float = .6, event_ids: tuple[str, ...] = (),
        attributes: dict[str, Any] | None = None, now: float | None = None,
    ) -> tuple[WorldEntity, WorldStateDelta]:
        """対象を1つ観測する。**確信が足りなければ別物として持つ。**"""
        moment = time.monotonic() if now is None else now
        existing = resolve_entity(
            name, list(state.entities.values()),
            confidence=confidence, entity_type=entity_type)
        if existing is not None:
            existing.last_observed_at = moment
            existing.presence = Presence.VISIBLE
            existing.confidence = max(existing.confidence, _clamp(confidence))
            existing.source_event_ids = tuple(dict.fromkeys(
                (*existing.source_event_ids, *event_ids)))[:8]
            if attributes:
                existing.attributes.update(attributes)
            delta = WorldStateDelta(
                operation=DeltaOperation.CONFIRM, target_id=existing.entity_id,
                confidence=_clamp(confidence), reason="seen_again",
                source_event_ids=tuple(event_ids))
            state.recent_changes.append(delta)
            state.version += 1
            return existing, delta
        entity = WorldEntity(
            canonical_name=str(name)[:60],
            # **確信が足りないなら UNKNOWN のまま持つ。**
            entity_type=entity_type if _clamp(confidence) >= MERGE_CONFIDENCE
            else EntityType.UNKNOWN,
            confidence=_clamp(confidence), source_event_ids=tuple(event_ids),
            attributes=dict(attributes or {}),
            first_observed_at=moment, last_observed_at=moment,
            status=ItemStatus.ACTIVE if _clamp(confidence) >= self.confidence_floor
            else ItemStatus.UNCERTAIN,
        )
        state.entities[entity.entity_id] = entity
        delta = WorldStateDelta(
            operation=DeltaOperation.ADD, target_id=entity.entity_id,
            proposed_value=entity.canonical_name, applied_value=entity.canonical_name,
            confidence=_clamp(confidence), reason="new_entity",
            source_event_ids=tuple(event_ids))
        state.recent_changes.append(delta)
        state.version += 1
        del state.recent_changes[:-24]
        return entity, delta

    def observe_fact(
        self, state: GroundedWorldState, subject_id: str, predicate: str,
        value: Any = True, *, confidence: float = .6,
        source_type: InformationType | str = InformationType.OBSERVATION,
        event_ids: tuple[str, ...] = (), ttl: float | None = None,
        authoritative: bool = False, now: float | None = None,
    ) -> WorldStateDelta:
        """事実を1つ観測する。**矛盾は僅差では覆さない。**

        `authoritative` は「この出どころが正本」という印（第5条）。

        矛盾の余裕は**推測**のためにある——映像やLLMが「たぶんメニュー」と
        言っただけで今の画面を塗り替えられては困る。だが持ち主がはっきり
        している状態は推測ではない。Discordの接続が「もう居ない」と言うなら
        居ないし、ゲームのセッション管理が「爆弾は終わった」と言うなら
        終わっている。これらを確信の大小で覆すと、**退出が反映されない**。
        実際そうなった（VCから出た人がずっと居ることになっていた）。
        """
        moment = time.monotonic() if now is None else now
        key = f"{subject_id}:{predicate}"
        existing = state.facts.get(key)
        expires = moment + (ttl if ttl is not None
                            else FACT_TTL.get(predicate, _DEFAULT_TTL))

        if existing is None:
            fact = WorldFact(
                subject_id=subject_id, predicate=predicate, value=value,
                confidence=_clamp(confidence), source_type=source_type,
                source_event_ids=tuple(event_ids), observed_at=moment,
                last_verified_at=moment, expires_at=expires,
                status=ItemStatus.ACTIVE if _clamp(confidence) >= self.confidence_floor
                else ItemStatus.UNCERTAIN)
            state.facts[key] = fact
            delta = WorldStateDelta(
                operation=DeltaOperation.ADD, target_id=fact.fact_id,
                field_name=predicate, proposed_value=value, applied_value=value,
                confidence=_clamp(confidence), reason="new_fact",
                source_event_ids=tuple(event_ids), expires_at=expires)
        elif existing.value == value:
            # 同じことをまた見た。**鮮度が戻り、確信が少し上がる。**
            existing.last_verified_at = moment
            existing.expires_at = expires
            existing.confidence = min(1.0, existing.confidence + .05)
            if str(existing.status) in {str(ItemStatus.STALE), str(ItemStatus.UNCERTAIN)}:
                existing.status = ItemStatus.ACTIVE
            delta = WorldStateDelta(
                operation=DeltaOperation.CONFIRM, target_id=existing.fact_id,
                field_name=predicate, previous_value=existing.value,
                proposed_value=value, applied_value=value,
                confidence=existing.confidence, reason="confirmed",
                source_event_ids=tuple(event_ids), expires_at=expires)
        elif (not authoritative
              and _clamp(confidence) < existing.confidence + self.contradiction_margin):
            # **僅差では覆さない。** 代わりに古い方の確信を下げて不確かにする。
            existing.status = ItemStatus.UNCERTAIN
            existing.confidence = max(.1, existing.confidence - .1)
            delta = WorldStateDelta(
                operation=DeltaOperation.MARK_STALE, target_id=existing.fact_id,
                field_name=predicate, previous_value=existing.value,
                proposed_value=value, confidence=_clamp(confidence),
                reason="conflicting_but_not_convincing",
                rejected_reason="below_contradiction_margin",
                source_event_ids=tuple(event_ids))
        else:
            # 覆った。**古い方は消さずに印を付ける。**
            previous = existing.value
            existing.status = ItemStatus.CONTRADICTED
            fact = WorldFact(
                subject_id=subject_id, predicate=predicate, value=value,
                confidence=_clamp(confidence), source_type=source_type,
                source_event_ids=tuple(event_ids), observed_at=moment,
                last_verified_at=moment, expires_at=expires,
                status=ItemStatus.ACTIVE if _clamp(confidence) >= self.confidence_floor
                else ItemStatus.UNCERTAIN)
            state.facts[key] = fact
            delta = WorldStateDelta(
                # 持ち主が言い直したのは「矛盾」ではなく、ただの更新。
                operation=(DeltaOperation.UPDATE if authoritative
                           else DeltaOperation.CONTRADICT),
                target_id=fact.fact_id,
                field_name=predicate, previous_value=previous,
                proposed_value=value, applied_value=value,
                confidence=_clamp(confidence),
                reason=("authoritative_update" if authoritative
                        else "contradicted_with_confidence"),
                source_event_ids=tuple(event_ids), expires_at=expires)
        state.recent_changes.append(delta)
        del state.recent_changes[:-24]
        state.version += 1
        state.updated_at = moment
        return delta

    def lost_sight_of(
        self, state: GroundedWorldState, entity_id: str, *,
        confirmed_absent: bool = False, now: float | None = None,
    ) -> WorldStateDelta:
        """見えなくなった。**無くなったとは言わない。**

        `confirmed_absent` は「無いことを確かめた」時だけ。
        画面から外れただけで不存在を断定すると、すぐに嘘をつく。
        """
        moment = time.monotonic() if now is None else now
        entity = state.entities.get(entity_id)
        if entity is None:
            return WorldStateDelta(
                operation=DeltaOperation.REMOVE, target_id=entity_id,
                rejected_reason="unknown_entity")
        previous = str(entity.presence)
        entity.presence = (Presence.CONFIRMED_ABSENT if confirmed_absent
                           else Presence.RECENTLY_SEEN)
        delta = WorldStateDelta(
            operation=DeltaOperation.REMOVE if confirmed_absent else DeltaOperation.MARK_STALE,
            target_id=entity_id, field_name="presence",
            previous_value=previous, applied_value=str(entity.presence),
            confidence=.9 if confirmed_absent else .5,
            reason="confirmed_absent" if confirmed_absent else "out_of_view",
        )
        state.recent_changes.append(delta)
        state.version += 1
        state.updated_at = moment
        return delta


__all__ = [
    "CONTRADICTION_MARGIN", "FACT_CONFIDENCE_FLOOR", "FACT_TTL",
    "MERGE_CONFIDENCE", "USABLE", "DeltaOperation", "EntityType",
    "GroundedWorldState", "ItemStatus", "Presence", "WorldEntity", "WorldFact",
    "WorldStateDelta", "WorldStateGate", "resolve_entity",
]
