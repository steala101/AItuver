"""誰なのかを、**3つの別々の問い**として持つ。

これまでは1つの `speaker_id` が3つの違うことを同時に意味していた:

* どの接続から来たか（Discord の user ID）
* どの声に似ているか（声紋の推定）
* 誰として覚えているか（関係値・記憶の持ち主）

同じ番号で扱っていると、**声紋が一度外れただけで関係値が別人へ移る**。
そして移った先が正しいかどうかを後から確かめる手がかりが残らない。

だから分ける:

```
TransportIdentity  接続元が保証する。Discord ID は疑いようがない。
                   ただし**誰の声かは言っていない。**
VoiceIdentity      声紋モデルの推定。誰の声かは言うが、**確実ではない。**
PersonIdentity     関係値・記憶・目標が参照する人物。**ここだけが正本。**
```

`IdentityLink` が3者を繋ぐ。**リンクは必ず取り消せる。**
一度の声紋一致で恒久的に統合しないのは、間違えた時に戻せなくなるから。
実際、取り違えは分離できない形で起きる。

**声紋ベクトルも音声そのものもここには入らない**（第12条）。
持つのはIDと数値だけ。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 3つのIdentity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransportIdentity:
    """接続元が保証する人物。**接続としては確実。**

    Discord の user ID がこれ。誰が繋いでいるかは疑いようがないが、
    **その人が今喋っているとは言っていない。**
    """

    provider: str
    transport_user_id: str
    display_name: str = ""
    #: 接続元が正本かどうか。Discord は true、推測経由は false。
    authoritative: bool = True

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.transport_user_id}"

    def snapshot(self) -> dict[str, Any]:
        # **表示名は入れない**（第12条）。
        return {"provider": self.provider, "key": self.key,
                "authoritative": self.authoritative}


@dataclass(frozen=True, slots=True)
class VoiceIdentity:
    """声紋モデルの推定。**誰の声かは言うが、確実ではない。**"""

    voiceprint_id: str
    confidence: float = .0
    model_version: str = ""

    @property
    def key(self) -> str:
        return f"voice:{self.voiceprint_id}"

    def snapshot(self) -> dict[str, Any]:
        return {"key": self.key, "confidence": round(float(self.confidence), 3),
                "model": self.model_version}


@dataclass(slots=True)
class PersonIdentity:
    """関係値・記憶・目標が参照する人物。**ここだけが正本。**

    `person_id` は接続にも声紋にも依存しない。Discord を抜けても、
    声が変わっても、同じ人は同じ `person_id` のまま。
    """

    person_id: str = field(default_factory=lambda: f"person:{uuid.uuid4().hex[:10]}")
    canonical_name: str = ""
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        return {"person_id": self.person_id, "chars": len(self.canonical_name)}


# ---------------------------------------------------------------------------
# リンク
# ---------------------------------------------------------------------------


class IdentityType(StrEnum):
    TRANSPORT = "transport"
    VOICE = "voice"


class LinkStatus(StrEnum):
    #: 根拠はあるが足りない。**まだ人物として扱わない。**
    CANDIDATE = "candidate"
    #: 十分な根拠が積み上がった。
    CONFIRMED = "confirmed"
    #: 別の正本と食い違った。**混ぜない。**
    CONFLICTED = "conflicted"
    #: 誤りと分かって取り消した。**消さずに残す。**
    REVOKED = "revoked"
    #: 新しいリンクへ置き換えた。
    SUPERSEDED = "superseded"


#: `CANDIDATE` から `CONFIRMED` へ上がるのに要る一致回数。
#:
#: **1回では上げない。** 声紋は数%揺れるので、たまたま高く出た1回で
#: 人物を確定すると、その後の関係値が全部別人へ流れる。
CONFIRM_SUPPORT = 3
#: 1回の一致として数えてよい確信の下限。
CONFIRM_CONFIDENCE = .72
#: これだけ食い違ったら `CONFLICTED` にする。
CONFLICT_LIMIT = 2


@dataclass(slots=True)
class IdentityLink:
    """人物と、接続または声紋の対応1件。**取り消せる。**"""

    person_id: str
    identity_type: IdentityType | str
    identity_value: str
    status: LinkStatus | str = LinkStatus.CANDIDATE
    confidence: float = .5
    source_event_ids: tuple[str, ...] = ()
    support_count: int = 0
    contradiction_count: int = 0
    model_version: str = ""
    #: 取り消した理由。**なぜ間違いと判断したかを残す。**
    revoked_reason: str = ""
    #: 置き換え先。追跡できるようにする。
    superseded_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    link_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def live(self) -> bool:
        return str(self.status) in {str(LinkStatus.CANDIDATE),
                                    str(LinkStatus.CONFIRMED)}

    @property
    def usable(self) -> bool:
        """人物の根拠として使ってよいか。**候補のうちは使わない。**"""
        return str(self.status) == str(LinkStatus.CONFIRMED)

    def snapshot(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id, "person_id": self.person_id,
            "type": str(self.identity_type), "value": self.identity_value,
            "status": str(self.status),
            "confidence": round(float(self.confidence), 3),
            "support": self.support_count,
            "contradictions": self.contradiction_count,
            "events": list(self.source_event_ids[-3:]),
            "revoked_reason": self.revoked_reason[:40],
            "superseded_by": self.superseded_by,
        }


# ---------------------------------------------------------------------------
# 解決
# ---------------------------------------------------------------------------


class ResolutionStatus(StrEnum):
    #: 接続元が保証している。**いちばん強い。**
    AUTHORITATIVE = "authoritative"
    #: 確認済みのリンクから引けた。
    CONFIRMED = "confirmed"
    #: 根拠が複数あるが確定していない。
    PROBABLE = "probable"
    #: 分からない。**既知の人物として扱わない。**
    UNKNOWN = "unknown"
    #: 正本と推定が食い違っている。
    CONFLICTED = "conflicted"


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """1つの出来事について「誰か」を1回だけ決めた結果。"""

    person_id: str = ""
    resolution_status: ResolutionStatus | str = ResolutionStatus.UNKNOWN
    confidence: float = .0
    transport_identity: TransportIdentity | None = None
    voice_identity: VoiceIdentity | None = None
    evidence: tuple[str, ...] = ()
    conflict_reason: str = ""
    link_id: str = ""

    @property
    def known(self) -> bool:
        """既知の人物として扱ってよいか。

        **`PROBABLE` は既知ではない。** 関係値を動かすには足りない。
        """
        return bool(self.person_id) and str(self.resolution_status) in {
            str(ResolutionStatus.AUTHORITATIVE), str(ResolutionStatus.CONFIRMED)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "person_id": self.person_id,
            "status": str(self.resolution_status),
            "confidence": round(float(self.confidence), 3),
            "transport": (self.transport_identity.snapshot()
                          if self.transport_identity else None),
            "voice": (self.voice_identity.snapshot()
                      if self.voice_identity else None),
            "evidence": list(self.evidence[:4]),
            "conflict": self.conflict_reason[:60],
            "link_id": self.link_id,
        }


class IdentityResolver:
    """**「誰か」を決める場所を1つにする。**

    優先順位（上から順に見て、最初に当たったところで決まる）:

    1. 正本の Transport Identity（Discord ID）
    2. 確認済みの Identity Link
    3. 根拠が複数ある Voice Identity → `PROBABLE`（**既知扱いにはしない**）
    4. 単発の声紋推定 → `PROBABLE`
    5. `UNKNOWN`

    **表示名だけで人物を決めない。** 表示名は変わるし重複する。
    """

    def __init__(self) -> None:
        self.people: dict[str, PersonIdentity] = {}
        self.links: dict[str, IdentityLink] = {}
        self.counters: dict[str, int] = {
            "resolved": 0, "unknown": 0, "conflicts": 0,
            "links_created": 0, "links_confirmed": 0, "links_revoked": 0,
        }

    # -- 人物 -----------------------------------------------------------

    def person(self, person_id: str) -> PersonIdentity | None:
        return self.people.get(person_id)

    def ensure_person(self, *, person_id: str = "", name: str = "") -> PersonIdentity:
        if person_id and person_id in self.people:
            record = self.people[person_id]
            if name and not record.canonical_name:
                record.canonical_name = str(name)[:60]
            return record
        record = PersonIdentity(canonical_name=str(name or "")[:60],
                                **({"person_id": person_id} if person_id else {}))
        self.people[record.person_id] = record
        return record

    # -- リンク ---------------------------------------------------------

    def _find(self, identity_type: IdentityType | str, value: str,
              *, live_only: bool = True) -> IdentityLink | None:
        for link in self.links.values():
            if (str(link.identity_type) == str(identity_type)
                    and link.identity_value == value
                    and (link.live or not live_only)):
                return link
        return None

    def links_for(self, person_id: str, *, live_only: bool = False) -> list[IdentityLink]:
        return [link for link in self.links.values()
                if link.person_id == person_id and (link.live or not live_only)]

    def link(
        self, *, person_id: str, identity_type: IdentityType | str, value: str,
        confidence: float = .5, event_id: str = "", model_version: str = "",
        authoritative: bool = False,
    ) -> IdentityLink:
        """根拠を1つ積む。**1回では確定しない。**

        `authoritative` は接続元が保証している場合だけ。その時は
        最初から `CONFIRMED`——Discord ID を3回待つ理由は無い。
        """
        existing = self._find(identity_type, value)
        if existing is not None and existing.person_id != person_id:
            # **正本と食い違った。** 勝手に付け替えない。
            existing.contradiction_count += 1
            existing.updated_at = time.time()
            if authoritative or existing.contradiction_count >= CONFLICT_LIMIT:
                existing.status = LinkStatus.CONFLICTED
            self.counters["conflicts"] += 1
            logger.info("Identityの食い違い: %s は %s か %s か",
                        value, existing.person_id, person_id)
            if not authoritative:
                return existing
            # 正本が言うなら、新しい方を作る。古い方は消さずに残す。
        if existing is not None and existing.person_id == person_id:
            existing.support_count += 1
            existing.confidence = max(existing.confidence, float(confidence))
            if event_id:
                existing.source_event_ids = (
                    *existing.source_event_ids, str(event_id))[-8:]
            existing.updated_at = time.time()
            if str(existing.status) == str(LinkStatus.CANDIDATE) and (
                    authoritative
                    or (existing.support_count >= CONFIRM_SUPPORT
                        and existing.confidence >= CONFIRM_CONFIDENCE)):
                existing.status = LinkStatus.CONFIRMED
                self.counters["links_confirmed"] += 1
            return existing
        link = IdentityLink(
            person_id=person_id, identity_type=identity_type, identity_value=value,
            status=LinkStatus.CONFIRMED if authoritative else LinkStatus.CANDIDATE,
            confidence=float(confidence), support_count=1,
            model_version=str(model_version),
            source_event_ids=((str(event_id),) if event_id else ()))
        self.links[link.link_id] = link
        self.counters["links_created"] += 1
        if authoritative:
            self.counters["links_confirmed"] += 1
        return link

    def revoke(self, link_id: str, *, reason: str = "") -> IdentityLink | None:
        """**消さずに取り消す。** 何を間違えたかが残らないと直せない。"""
        link = self.links.get(link_id)
        if link is None:
            return None
        link.status = LinkStatus.REVOKED
        link.revoked_reason = str(reason or "manual")[:80]
        link.updated_at = time.time()
        self.counters["links_revoked"] += 1
        return link

    def relink(self, link_id: str, *, person_id: str, reason: str = "") -> IdentityLink | None:
        """別の人物へ繋ぎ直す。**古いリンクは `SUPERSEDED` として残す。**

        **過去の会話や記憶は動かさない。** 動かすと、間違いだった時に
        二重に壊れる。過去は「不確かだったかもしれない」印を付けるだけで、
        今後のイベントが新しいリンクへ入るようにする。
        """
        old = self.links.get(link_id)
        if old is None:
            return None
        fresh = IdentityLink(
            person_id=person_id, identity_type=old.identity_type,
            identity_value=old.identity_value, status=LinkStatus.CANDIDATE,
            confidence=old.confidence, source_event_ids=old.source_event_ids,
            model_version=old.model_version)
        self.links[fresh.link_id] = fresh
        old.status = LinkStatus.SUPERSEDED
        old.superseded_by = fresh.link_id
        old.revoked_reason = str(reason or "relinked")[:80]
        old.updated_at = time.time()
        self.counters["links_created"] += 1
        return fresh

    def history_for(self, value: str) -> list[IdentityLink]:
        """その識別子に紐づいた全てのリンク。**取り消した分も含む。**"""
        return sorted(
            (link for link in self.links.values() if link.identity_value == value),
            key=lambda item: item.created_at)

    # -- 解決 -----------------------------------------------------------

    def resolve(
        self, *, transport: TransportIdentity | None = None,
        voice: VoiceIdentity | None = None, event_id: str = "",
    ) -> IdentityResolution:
        """この出来事は誰か。**判断はここ1箇所だけ。**"""
        evidence: list[str] = []

        # 1. 正本の接続。**いちばん強い。**
        if transport is not None and transport.authoritative:
            link = self._find(IdentityType.TRANSPORT, transport.key)
            if link is None:
                person = self.ensure_person(name=transport.display_name)
                link = self.link(
                    person_id=person.person_id, identity_type=IdentityType.TRANSPORT,
                    value=transport.key, confidence=1.0, event_id=event_id,
                    authoritative=True)
            evidence.append("transport_authoritative")
            conflict = ""
            if voice is not None:
                voice_link = self._find(IdentityType.VOICE, voice.key)
                if voice_link is not None and voice_link.person_id != link.person_id:
                    # **接続を優先する。** 声紋の方を食い違いとして記録し、
                    # 関係値は動かさない。
                    voice_link.contradiction_count += 1
                    voice_link.status = LinkStatus.CONFLICTED
                    voice_link.updated_at = time.time()
                    self.counters["conflicts"] += 1
                    conflict = "voice_disagrees_with_transport"
                    evidence.append(conflict)
                    logger.warning(
                        "声紋が接続と食い違う: transport=%s voice=%s（接続を優先）",
                        link.person_id, voice_link.person_id)
            self.counters["resolved"] += 1
            return IdentityResolution(
                person_id=link.person_id,
                resolution_status=ResolutionStatus.AUTHORITATIVE,
                confidence=1.0, transport_identity=transport,
                voice_identity=voice, evidence=tuple(evidence),
                conflict_reason=conflict, link_id=link.link_id)

        # 2. 確認済みのリンク。
        if voice is not None:
            # **食い違ったリンクも引く。** 見なかったことにして `UNKNOWN` を
            # 返すと、「分からない」と「矛盾している」が同じに見える。
            # 前者は根拠が無いだけだが、後者は**根拠が2つあって割れている**。
            link = self._find(IdentityType.VOICE, voice.key, live_only=False)
            if link is not None and str(link.status) in {
                    str(LinkStatus.REVOKED), str(LinkStatus.SUPERSEDED)}:
                # 取り消した根拠は使わない。**無かったことにする。**
                link = None
            if link is not None and link.usable:
                evidence.append("confirmed_voice_link")
                self.counters["resolved"] += 1
                return IdentityResolution(
                    person_id=link.person_id,
                    resolution_status=ResolutionStatus.CONFIRMED,
                    confidence=min(1.0, float(voice.confidence)),
                    voice_identity=voice, transport_identity=transport,
                    evidence=tuple(evidence), link_id=link.link_id)
            if link is not None and str(link.status) == str(LinkStatus.CONFLICTED):
                self.counters["conflicts"] += 1
                return IdentityResolution(
                    resolution_status=ResolutionStatus.CONFLICTED,
                    confidence=float(voice.confidence), voice_identity=voice,
                    transport_identity=transport,
                    evidence=("conflicted_voice_link",),
                    conflict_reason="voice_link_conflicted", link_id=link.link_id)
            # 3〜4. 候補どまり。**既知として扱わない。**
            if link is not None:
                evidence.append(
                    "multi_evidence_voice" if link.support_count > 1
                    else "single_voice_match")
                self.counters["unknown"] += 1
                return IdentityResolution(
                    person_id=link.person_id,
                    resolution_status=ResolutionStatus.PROBABLE,
                    confidence=float(voice.confidence), voice_identity=voice,
                    transport_identity=transport, evidence=tuple(evidence),
                    link_id=link.link_id)

        # 5. 分からない。
        self.counters["unknown"] += 1
        return IdentityResolution(
            resolution_status=ResolutionStatus.UNKNOWN,
            transport_identity=transport, voice_identity=voice,
            evidence=("no_usable_identity",))

    def note_voice_sample(
        self, *, transport: TransportIdentity, voice: VoiceIdentity,
        event_id: str = "",
    ) -> IdentityLink | None:
        """**接続が正本と分かっている音声**を、声紋の根拠として積む。

        Discord direct audio は「この user が喋った」が確実なので、
        その時の声紋を本人のものとして数えられる。ただし
        **1回では確定しない**（`CONFIRM_SUPPORT` 回要る）。
        """
        if not transport.authoritative:
            return None
        if float(voice.confidence) < CONFIRM_CONFIDENCE:
            # 弱い一致を根拠に積むと、CONFIRMED が意味を失う。
            return None
        resolution = self.resolve(transport=transport, event_id=event_id)
        if not resolution.person_id:
            return None
        return self.link(
            person_id=resolution.person_id, identity_type=IdentityType.VOICE,
            value=voice.key, confidence=float(voice.confidence),
            event_id=event_id, model_version=voice.model_version)

    def status(self) -> dict[str, Any]:
        """診断用。**名前も声紋も入れない**（第12条）。"""
        by_status: dict[str, int] = {}
        for link in self.links.values():
            key = str(link.status)
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "people": len(self.people),
            "links": len(self.links),
            "by_status": by_status,
            "counters": dict(self.counters),
        }


__all__ = [
    "CONFIRM_CONFIDENCE", "CONFIRM_SUPPORT", "CONFLICT_LIMIT",
    "IdentityLink", "IdentityResolution", "IdentityResolver", "IdentityType",
    "LinkStatus", "PersonIdentity", "ResolutionStatus", "TransportIdentity",
    "VoiceIdentity",
]
