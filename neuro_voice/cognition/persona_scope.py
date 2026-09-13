"""どの人格のものか。**「所有者不明だから共有」にしない。**

実機で、ペルソナを変えた後に旧ペルソナ固有と思われる反応が出た。
調べると、分離できている所とできていない所が混在していた。

```
DB ファイル      mind_<persona>.db      → 分かれている
関係値ファイル    relationships_<p>.json → 分かれている
会話履歴          ConversationManager    → **分かれていない**
Planner キャッシュ                        → **鍵に人格が入っていない**
進行中の非同期処理                        → **切替後に届いても止まらない**
```

物理ファイルが分かれていても、**メモリ上に残ったものが漏れる**。
ここは「何を共有し、何を分離するか」をコード側に置く場所。

守りたいのは3つ:

* **所有者が分からない情報を、全ペルソナへ公開しない**（隔離が既定）
* **切替は設定値1個の書き換えではなく、状態遷移**
* **切替前に始まった処理の結果を、切替後へ入れない**
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 所有者が書かれていない古いデータの扱い。
#:
#: **共有ではなく隔離。** 「たぶん誰のでもないから全員に見せてよい」で
#: 始めると、実際には誰かの私的な記憶だったものが全ペルソナへ出る。
#: 逆（見えなさすぎる）は「覚えていない」で済むが、こちらは戻せない。
LEGACY_UNSCOPED = "legacy_unscoped"


class PersonaScope(StrEnum):
    """その情報を、誰が見てよいか。"""

    #: ツール定義・安全規則。**人格に関係なく同じ。**
    GLOBAL_SYSTEM = "global_system"
    #: いま目の前で起きている客観的な事実（ゲーム状態など）。
    WORLD_SHARED = "world_shared"
    #: 「これは覚えておいて、どの子でもいい」と**明示的に**共有された事実。
    EXPLICIT_SHARED = "explicit_shared"
    #: そのペルソナ固有の設定・口調・知識。
    PERSONA_PRIVATE = "persona_private"
    #: そのペルソナとユーザーの関係。
    PERSONA_USER_RELATIONSHIP = "persona_user_relationship"
    #: このセッション限りの方針・感情。
    SESSION_PERSONA = "session_persona"


#: ペルソナを越えて読んでよいスコープ。**この3つだけ。**
SHARED_SCOPES: frozenset[str] = frozenset({
    str(PersonaScope.GLOBAL_SYSTEM),
    str(PersonaScope.WORLD_SHARED),
    str(PersonaScope.EXPLICIT_SHARED),
})
#: 持ち主のペルソナだけが読んでよいスコープ。
PRIVATE_SCOPES: frozenset[str] = frozenset({
    str(PersonaScope.PERSONA_PRIVATE),
    str(PersonaScope.PERSONA_USER_RELATIONSHIP),
    str(PersonaScope.SESSION_PERSONA),
})

#: 既定のスコープ。**分からないものは私的側。**
DEFAULT_SCOPE = PersonaScope.PERSONA_PRIVATE

#: 情報の種類 → 推奨スコープ。呼び出し側が迷わないための表。
RECOMMENDED_SCOPE: dict[str, PersonaScope] = {
    "tool_definition": PersonaScope.GLOBAL_SYSTEM,
    "safety_rule": PersonaScope.GLOBAL_SYSTEM,
    "game_state": PersonaScope.WORLD_SHARED,
    "world_fact": PersonaScope.WORLD_SHARED,
    "shared_fact": PersonaScope.EXPLICIT_SHARED,
    "persona_setting": PersonaScope.PERSONA_PRIVATE,
    "persona_knowledge": PersonaScope.PERSONA_PRIVATE,
    # **会話戦略の反省は、その子の学びであって共通の規則ではない。**
    "reflection": PersonaScope.PERSONA_PRIVATE,
    "conversation_strategy": PersonaScope.PERSONA_PRIVATE,
    "self_failure": PersonaScope.PERSONA_PRIVATE,
    "relationship": PersonaScope.PERSONA_USER_RELATIONSHIP,
    "session_mood": PersonaScope.SESSION_PERSONA,
}


def scope_of(value: Any) -> PersonaScope | None:
    try:
        return PersonaScope(str(value))
    except ValueError:
        return None


def recommended_scope(kind: str) -> PersonaScope:
    """**知らない種類は私的側。** 共有を既定にしない。"""
    return RECOMMENDED_SCOPE.get(str(kind or ""), DEFAULT_SCOPE)


# ---------------------------------------------------------------------------
# いまの人格
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaContext:
    """いま誰として話しているか。**世代まで持つ。**

    `persona_id` だけだと、A→B→A と戻った時に「切替前の A」と
    「切替後の A」が区別できない。進行中だった処理が戻ってきた時に
    受け入れてよいかを決められるよう、`epoch` を持たせてある。
    """

    persona_id: str = ""
    persona_version: str = "1"
    #: 切替のたびに増える。**単調増加**。
    persona_epoch: int = 0

    @property
    def key(self) -> str:
        return f"{self.persona_id}@{self.persona_version}#{self.persona_epoch}"

    def matches(self, other: Any) -> bool:
        """この結果を受け入れてよいか。**両方が一致した時だけ。**"""
        if other is None:
            return False
        return (str(getattr(other, "persona_id", "")) == self.persona_id
                and int(getattr(other, "persona_epoch", -1)) == self.persona_epoch)

    def snapshot(self) -> dict[str, Any]:
        return {"persona_id": self.persona_id,
                "persona_version": self.persona_version,
                "persona_epoch": self.persona_epoch}


def cache_key(*parts: Any, persona: PersonaContext) -> str:
    """人格ごとに分かれたキャッシュ鍵。

    **`persona_id` だけでは足りない。** 設定を編集して version が
    上がっても、epoch が進んでも、古い口調のプロンプトが残る。
    3つとも入れる。
    """
    payload = "|".join(str(item) for item in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{persona.key}|{digest}"


# ---------------------------------------------------------------------------
# 読んでよいか
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScopeVerdict:
    allowed: bool
    reason: str = ""

    @property
    def rejected_as_legacy(self) -> bool:
        return self.reason == LEGACY_UNSCOPED


def readable(
    *, owner_persona_id: Any, scope: Any, active_persona_id: str,
    allow_legacy: bool = False,
) -> ScopeVerdict:
    """この記憶を、いまの人格が読んでよいか。

    **プロンプトを作る時に外すのでは遅い。** 検索の候補に入った時点で
    embedding もランキングも別人格のものに引っ張られる。ここは
    DB の検索条件と候補生成の両方から呼ぶ。
    """
    owner = str(owner_persona_id or "").strip()
    scope_value = str(scope or "").strip()

    if scope_value in SHARED_SCOPES:
        return ScopeVerdict(True, scope_value)
    if not scope_value and not owner:
        # **所有者もスコープも無い古いデータ。**
        if allow_legacy:
            return ScopeVerdict(True, "legacy_allowed_by_config")
        return ScopeVerdict(False, LEGACY_UNSCOPED)
    if not owner:
        # スコープはあるが持ち主が分からない私的情報。隔離側。
        return ScopeVerdict(False, LEGACY_UNSCOPED)
    if owner == str(active_persona_id):
        return ScopeVerdict(True, scope_value or str(DEFAULT_SCOPE))
    return ScopeVerdict(False, "cross_persona")


def sql_scope_filter(active_persona_id: str, *, allow_legacy: bool = False,
                     column_persona: str = "persona_id",
                     column_scope: str = "scope") -> tuple[str, list[Any]]:
    """DB の検索条件。**取ってから捨てるのではなく、取らない。**

    全件取ってから絞る形だと、件数の上限（`LIMIT`）を別人格の記憶が
    先に埋めてしまう——結果として、自分の記憶が1件も出てこないのに
    「漏れてはいない」という状態になる。
    """
    shared = ",".join("?" for _ in SHARED_SCOPES)
    clause = (f"(({column_persona} = ?)"
              f" OR ({column_scope} IN ({shared})))")
    args: list[Any] = [str(active_persona_id), *sorted(SHARED_SCOPES)]
    if allow_legacy:
        clause = (f"({clause} OR (COALESCE({column_persona},'') = ''"
                  f" AND COALESCE({column_scope},'') = ''))")
    return clause, args


# ---------------------------------------------------------------------------
# 切替
# ---------------------------------------------------------------------------


class SwitchState(StrEnum):
    IDLE = "idle"
    REQUESTED = "switch_requested"
    DRAINING = "draining"
    COMPLETED = "switch_completed"


@dataclass(slots=True)
class SwitchRecord:
    from_persona: str
    to_persona: str
    from_epoch: int
    to_epoch: int
    state: SwitchState | str = SwitchState.REQUESTED
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    invalidated_caches: tuple[str, ...] = ()
    dropped_inflight: int = 0
    switch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def snapshot(self) -> dict[str, Any]:
        return {
            "switch_id": self.switch_id, "from": self.from_persona,
            "to": self.to_persona, "from_epoch": self.from_epoch,
            "to_epoch": self.to_epoch, "state": str(self.state),
            "caches": list(self.invalidated_caches),
            "dropped_inflight": self.dropped_inflight,
        }


class PersonaSwitch:
    """ペルソナ切替を**状態遷移**として持つ。

    設定値を1個書き換えて終わりにすると、切替の途中で届いた結果を
    誰も止められない。**旧ペルソナの遅い返事を、新しい声で読み上げる**
    のが一番まずい形。
    """

    def __init__(self, persona_id: str = "", persona_version: str = "1") -> None:
        self._context = PersonaContext(str(persona_id), str(persona_version), 0)
        self._state: SwitchState = SwitchState.IDLE
        self._history: list[SwitchRecord] = []
        self._invalidators: list[Any] = []
        self._lock = threading.RLock()

    @property
    def context(self) -> PersonaContext:
        with self._lock:
            return self._context

    @property
    def state(self) -> SwitchState:
        return self._state

    @property
    def switching(self) -> bool:
        return self._state in {SwitchState.REQUESTED, SwitchState.DRAINING}

    @property
    def history(self) -> list[SwitchRecord]:
        return list(self._history[-8:])

    def on_invalidate(self, handler: Any) -> None:
        """切替で捨てるキャッシュを登録する。**名前を返すこと。**"""
        self._invalidators.append(handler)

    def switch(self, persona_id: str, *, persona_version: str = "1",
               drain: Any = None) -> SwitchRecord:
        """切り替える。**新規処理を止めてから世代を進める。**"""
        with self._lock:
            previous = self._context
            record = SwitchRecord(
                from_persona=previous.persona_id, to_persona=str(persona_id),
                from_epoch=previous.persona_epoch,
                to_epoch=previous.persona_epoch + 1)
            self._state = SwitchState.REQUESTED
            self._history.append(record)
            del self._history[:-8]

        # 1. 走っているものを終わらせる／失効させる。
        self._state = SwitchState.DRAINING
        record.state = SwitchState.DRAINING
        if drain is not None:
            try:
                record.dropped_inflight = int(drain() or 0)
            except Exception:                           # noqa: BLE001
                record.dropped_inflight = 0

        # 2. 世代を進める。**ここから旧世代の結果は受け付けない。**
        with self._lock:
            self._context = PersonaContext(
                str(persona_id), str(persona_version), record.to_epoch)

        # 3. 人格固有のキャッシュを捨てる。
        names: list[str] = []
        for handler in self._invalidators:
            try:
                name = handler(previous, self._context)
            except Exception:                           # noqa: BLE001
                name = None
            if name:
                names.append(str(name))
        record.invalidated_caches = tuple(names)

        record.state = SwitchState.COMPLETED
        record.completed_at = time.time()
        self._state = SwitchState.COMPLETED
        return record

    def accepts(self, stamped: Any) -> bool:
        """切替前に始まった結果を受け入れてよいか。

        **切替中は何も受け入れない。** 途中の状態で書き込むと、
        新旧どちらの持ち物か決められないものが残る。
        """
        if self.switching:
            return False
        return self.context.matches(stamped)


@dataclass(frozen=True, slots=True)
class PersonaStamp:
    """非同期の結果に貼る札。**始めた時の世代を覚えておく。**"""

    persona_id: str = ""
    persona_epoch: int = 0
    issued_at: float = field(default_factory=time.time)

    @classmethod
    def of(cls, context: PersonaContext) -> "PersonaStamp":
        return cls(persona_id=context.persona_id,
                   persona_epoch=context.persona_epoch)


__all__ = [
    "DEFAULT_SCOPE", "LEGACY_UNSCOPED", "PRIVATE_SCOPES", "RECOMMENDED_SCOPE",
    "SHARED_SCOPES", "PersonaContext", "PersonaScope", "PersonaStamp",
    "PersonaSwitch", "ScopeVerdict", "SwitchRecord", "SwitchState",
    "cache_key", "readable", "recommended_scope", "scope_of",
    "sql_scope_filter",
]
