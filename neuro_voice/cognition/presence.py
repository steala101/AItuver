"""いま誰が居るのか。**「分かっている人数」と「たぶんの人数」を分ける。**

Discord には参加者一覧がある——接続が「この3人が居る」と言うなら3人居る。
Local のマイクには一覧が無い。聞こえた声から推し量るしかない。

この2つを同じ数字にすると、**推測が事実の顔をして出てくる**。
「今3人だよね」と言い切って、実は同じ人が2回喋っただけ、という壊れ方をする。

だから別々に持つ:

```
authoritative_transport_count   接続が保証する人数。**確定。**
estimated_unique_person_count   声から推した人数。**確定ではない。**
```

もう1つ守ること: **声が聞こえないだけでは「帰った」と言わない。**
黙っているだけかもしれない。接続が切れた時だけ `LEFT` にする。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PresenceStatus(StrEnum):
    #: 接続が保証している、または直前まで喋っていた。
    PRESENT = "present"
    #: 居ると考えるのが自然だが、確かめていない。
    PROBABLY_PRESENT = "probably_present"
    UNKNOWN = "unknown"
    #: **接続が切れた時だけ。** 静かなだけでここへ落とさない。
    LEFT = "left"
    #: しばらく何も無い。居ないとは言っていない。
    STALE = "stale"


class PresenceSource(StrEnum):
    #: 接続元。**正本。**
    TRANSPORT = "transport"
    #: 声が聞こえた。**推測。**
    VOICE = "voice"
    #: 復元した。**確かめ直すまで断定しない。**
    RESTORED = "restored"


#: これだけ何も無ければ `STALE`。**`LEFT` にはしない。**
QUIET_BEFORE_STALE = 600.0
#: 声だけで居ると見なす時間。
VOICE_PRESENCE_WINDOW = 300.0


@dataclass(slots=True)
class PresenceEntry:
    """1人ぶん。"""

    person_id: str
    transport_identity: str = ""
    voice_identity: str = ""
    presence_status: PresenceStatus | str = PresenceStatus.UNKNOWN
    presence_source: PresenceSource | str = PresenceSource.VOICE
    confidence: float = .5
    joined_at: float = .0
    last_activity_at: float = .0
    left_at: float = .0

    @property
    def here(self) -> bool:
        return str(self.presence_status) in {str(PresenceStatus.PRESENT),
                                             str(PresenceStatus.PROBABLY_PRESENT)}

    @property
    def certain(self) -> bool:
        """**接続が保証しているか。** 声だけの推測は含まない。"""
        return (str(self.presence_source) == str(PresenceSource.TRANSPORT)
                and str(self.presence_status) == str(PresenceStatus.PRESENT))

    def snapshot(self) -> dict[str, Any]:
        # **表示名も声紋も入れない**（第12条）。
        return {
            "person_id": self.person_id,
            "transport": self.transport_identity,
            "voice": self.voice_identity,
            "status": str(self.presence_status),
            "source": str(self.presence_source),
            "confidence": round(float(self.confidence), 2),
            "joined_at": round(self.joined_at, 1),
            "last_activity_at": round(self.last_activity_at, 1),
        }


@dataclass(frozen=True, slots=True)
class PresenceSummary:
    """人数の見立て。**2つの数を混ぜない。**"""

    authoritative_transport_count: int = 0
    observed_voice_count: int = 0
    estimated_unique_person_count: int = 0
    known_person_ids: tuple[str, ...] = ()
    unknown_voice_count: int = 0
    confidence: float = .0
    updated_at: float = field(default_factory=time.time)

    @property
    def certain(self) -> bool:
        """人数を言い切ってよいか。

        **接続の一覧があるだけでは足りない。** 一覧は「繋いでいる人」を
        保証するが、部屋に他の誰かが居ないことまでは保証しない。
        身元の分からない声が1つでも聞こえていたら、
        「いま3人だよね」とは言えない。
        """
        return self.authoritative_transport_count > 0 and not self.unknown_voice_count

    def snapshot(self) -> dict[str, Any]:
        return {
            "authoritative": self.authoritative_transport_count,
            "voices": self.observed_voice_count,
            "estimated": self.estimated_unique_person_count,
            "known": list(self.known_person_ids[:6]),
            "unknown_voices": self.unknown_voice_count,
            "confidence": round(float(self.confidence), 2),
            "certain": self.certain,
        }


class PresenceRegistry:
    """接続・声・世界状態の参加者を1つに束ねる。

    **接続の言うことと、声から推したことを、別の欄に入れておく。**
    混ぜた瞬間に「たぶん」が「確定」として出てくる。
    """

    def __init__(self, *, session_id: str = "") -> None:
        self.session_id = str(session_id)
        self.entries: dict[str, PresenceEntry] = {}
        self.updated_at: float = .0
        #: 聞こえた声のうち、誰か分からなかったもの。
        self._unknown_voices: dict[str, float] = {}
        self.counters: dict[str, int] = {
            "transport_joins": 0, "transport_leaves": 0,
            "voice_observations": 0, "unknown_voices": 0, "stale": 0,
        }

    # -- 接続（正本） ---------------------------------------------------

    def note_transport(
        self, *, person_id: str, transport_identity: str, present: bool,
        now: float | None = None,
    ) -> PresenceEntry:
        """接続の出入り。**ここだけが `LEFT` を確定できる。**"""
        moment = time.monotonic() if now is None else now
        entry = self.entries.get(person_id)
        if entry is None:
            entry = PresenceEntry(person_id=person_id)
            self.entries[person_id] = entry
        entry.transport_identity = str(transport_identity)
        entry.presence_source = PresenceSource.TRANSPORT
        entry.confidence = 1.0
        if present:
            if not entry.here:
                entry.joined_at = moment
            entry.presence_status = PresenceStatus.PRESENT
            entry.left_at = .0
            entry.last_activity_at = moment
            self.counters["transport_joins"] += 1
        else:
            entry.presence_status = PresenceStatus.LEFT
            entry.left_at = moment
            self.counters["transport_leaves"] += 1
        self.updated_at = moment
        return entry

    # -- 声（推測） -----------------------------------------------------

    def note_voice(
        self, *, voice_key: str, person_id: str = "", known: bool = False,
        confidence: float = .5, now: float | None = None,
    ) -> PresenceEntry | None:
        """声が聞こえた。

        **誰か分からない声を既知の人物として数えない。** 同じマイクから
        別人が喋ることはある。分からないなら分からないまま数える。
        """
        moment = time.monotonic() if now is None else now
        self.counters["voice_observations"] += 1
        self.updated_at = moment
        if not known or not person_id:
            self._unknown_voices[str(voice_key)] = moment
            self.counters["unknown_voices"] += 1
            return None
        entry = self.entries.get(person_id)
        if entry is None:
            entry = PresenceEntry(person_id=person_id, joined_at=moment)
            self.entries[person_id] = entry
        entry.voice_identity = str(voice_key)
        entry.last_activity_at = moment
        # **接続が居ると言っているなら、それを声で格下げしない。**
        if str(entry.presence_source) != str(PresenceSource.TRANSPORT):
            entry.presence_status = PresenceStatus.PRESENT
            entry.presence_source = PresenceSource.VOICE
            entry.confidence = max(entry.confidence, float(confidence))
        return entry

    def restore(self, entries) -> int:
        """再起動後の復元。**居るとは断定しない。**

        接続し直して一覧を見るまでは `UNKNOWN`。前回 `PRESENT` だった
        からといって、今も居るとは限らない。
        """
        restored = 0
        for item in entries or ():
            person_id = str(item.get("person_id", "") or "")
            if not person_id:
                continue
            self.entries[person_id] = PresenceEntry(
                person_id=person_id,
                transport_identity=str(item.get("transport_identity", "") or ""),
                voice_identity=str(item.get("voice_identity", "") or ""),
                presence_status=PresenceStatus.UNKNOWN,
                presence_source=PresenceSource.RESTORED,
                confidence=.3,
                joined_at=float(item.get("joined_at", 0.0) or 0.0))
            restored += 1
        return restored

    # -- 時間 -----------------------------------------------------------

    def sweep(self, *, now: float | None = None,
              quiet: float = QUIET_BEFORE_STALE) -> list[str]:
        """しばらく何も無い人へ印を付ける。**`LEFT` にはしない。**"""
        moment = time.monotonic() if now is None else now
        marked: list[str] = []
        for entry in self.entries.values():
            if not entry.here:
                continue
            # **接続が居ると言っている間は、黙っていても居る。**
            if str(entry.presence_source) == str(PresenceSource.TRANSPORT):
                continue
            if entry.last_activity_at and moment - entry.last_activity_at > quiet:
                entry.presence_status = PresenceStatus.STALE
                marked.append(entry.person_id)
                self.counters["stale"] += 1
        self._unknown_voices = {
            key: at for key, at in self._unknown_voices.items()
            if moment - at <= VOICE_PRESENCE_WINDOW}
        self.updated_at = moment
        return marked

    # -- 見立て ---------------------------------------------------------

    def summary(self, *, now: float | None = None) -> PresenceSummary:
        moment = time.monotonic() if now is None else now
        authoritative = [item for item in self.entries.values() if item.certain]
        voices = {item.voice_identity for item in self.entries.values()
                  if item.voice_identity and item.here}
        unknown = len([at for at in self._unknown_voices.values()
                       if moment - at <= VOICE_PRESENCE_WINDOW])
        here = [item for item in self.entries.values() if item.here]
        # **推測の人数。** 確定した人 + 誰か分からない声の本数。
        # 分からない声を1人ずつ数えるのは上振れするが、
        # **「居るのに数えない」より「居るかも」で持つ方が安全。**
        estimated = len(here) + unknown
        confidence = 1.0 if authoritative and not unknown else (
            .4 if unknown else .7 if here else .0)
        return PresenceSummary(
            authoritative_transport_count=len(authoritative),
            observed_voice_count=len(voices) + unknown,
            estimated_unique_person_count=estimated,
            known_person_ids=tuple(sorted(item.person_id for item in here)),
            unknown_voice_count=unknown,
            confidence=confidence, updated_at=moment)

    def present_ids(self) -> tuple[str, ...]:
        return tuple(sorted(item.person_id for item in self.entries.values()
                            if item.here))

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "entries": [item.snapshot() for item in self.entries.values()][:8],
            "summary": self.summary(now=now).snapshot(),
            "counters": dict(self.counters),
            "updated_at": round(self.updated_at, 1),
        }


__all__ = [
    "QUIET_BEFORE_STALE", "VOICE_PRESENCE_WINDOW",
    "PresenceEntry", "PresenceRegistry", "PresenceSource", "PresenceStatus",
    "PresenceSummary",
]
