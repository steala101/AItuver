"""同じ話を2回渡さないための、**決定的な**重複判定。

人物単位で記憶を引くようになると、Discord で覚えたことと Local で
覚えたことが両方returnされる。中身が同じでも `memory_id` が違えば
別の記憶なので、そのまま Planner へ渡すと**同じ話を2回聞かせる**。

ここでやること:

* `memory_id` で落とす（同じレコードが複数経路から返った分）
* **正規化した内容**で落とす（別レコードだが同じ話）
* 残す1件を選ぶ

やらないこと:

* **レコードを消さない。** 渡す直前に絞るだけ
* **LLMを呼ばない。** 毎回の重複判定にモデルを使うと、遅いうえに
  同じ入力で違う答えが返る
* **似ているだけのものを勝手に消さない。** 近いものは印を付けて
  Trace へ出すだけで、判断は人に残す

出典が違うものを内容だけで1つにしないのは、
**「本人がそう言った」と「こちらがそう推測した」は別のこと**だから。
似た文になっていても、片方は事実で片方は当て推量になりうる。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.types import InformationType

#: 末尾の句読点の揺れ。**意味は変わらないので落とす。**
_TRAILING = "。．.、,！!？?　 \t\r\n"
_SPACES = re.compile(r"[\s　]+")


def normalise_text(value: Any) -> str:
    """内容を比べるための形へ。**決定的**——同じ入力なら必ず同じ結果。

    やるのは Unicode の正規化と、空白・大文字小文字・末尾句読点の統一だけ。
    語順を並べ替えたり同義語を寄せたりはしない——**そこから先は推測**で、
    推測で記憶を落とすと「言ったのに覚えていない」が起きる。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SPACES.sub(" ", text).strip()
    text = text.strip(_TRAILING)
    # ASCII の英字だけ小文字化する。**日本語の表記は触らない。**
    return "".join(ch.lower() if ch.isascii() and ch.isalpha() else ch
                   for ch in text)


def fingerprint(memory: Any) -> str:
    """内容の指紋。**同じ話なら同じ、違う出典なら違う。**

    出典（`information_type`）を入れているのは、
    **本人の発言と推測を1つにしないため。**

    **`participant_ids` は入れない。** 同じ話が Discord と Local で
    別々の `speaker_id` に紐づいて保存されているのが、まさに今回
    まとめたい形——ここに入れると指紋が必ず違ってしまい、
    重複排除が一件も効かなくなる（実際そうなった）。

    別人の記憶が混ざる心配は無い。ここへ来るのは**1人分の範囲**
    （`IdentityScope`）で集めたものだけで、範囲を作る側が
    確認済みのリンクしか入れていない。
    """
    summary = normalise_text(getattr(memory, "summary", ""))
    if not summary:
        return ""
    kind = str(getattr(memory, "information_type", "") or "")
    topics = ",".join(sorted(
        normalise_text(item) for item in
        (getattr(memory, "topic_ids", ()) or ())))
    payload = f"{kind}\x1f{summary}\x1f{topics}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: 残す1件を選ぶ時の優先順位。**本人が言ったことを最優先。**
#:
#: 推測が本人の発言を押しのけると、「そう言ったよね」と
#: 言われた覚えのないことを返すようになる。
TYPE_PRIORITY: dict[str, int] = {
    str(InformationType.USER_STATEMENT): 0,
    str(InformationType.SYSTEM_FACT): 1,
    str(InformationType.OBSERVATION): 2,
    str(InformationType.REFLECTION): 3,
    str(InformationType.INFERENCE): 4,
}
_UNKNOWN_PRIORITY = 9


def _rank(memory: Any) -> tuple:
    """小さいほど残す。"""
    status = str(getattr(memory, "status", "") or "")
    # **覆された記憶・置き換えられた記憶は後ろへ。**
    stale = 1 if status in {"contradicted", "superseded"} else 0
    kind = str(getattr(memory, "information_type", "") or "")
    return (
        stale,
        TYPE_PRIORITY.get(kind, _UNKNOWN_PRIORITY),
        -float(getattr(memory, "confidence", 0.0) or 0.0),
        -float(getattr(memory, "importance", 0.0) or 0.0),
        -float(getattr(memory, "last_verified_at", 0.0)
               or getattr(memory, "created_at", 0.0) or 0.0),
        int(getattr(memory, "memory_id", 0) or 0),
    )


@dataclass(slots=True)
class DedupResult:
    """絞り込みの結果。**落とした分もIDだけ残す。**"""

    memories: list[Any] = field(default_factory=list)
    count_before: int = 0
    id_duplicates_removed: int = 0
    fingerprint_duplicates_removed: int = 0
    duplicate_memory_ids: list[int] = field(default_factory=list)
    #: 似ているが同じとは言い切れないもの。**落としていない。**
    possible_near_duplicates: list[int] = field(default_factory=list)

    @property
    def count_after(self) -> int:
        return len(self.memories)

    def snapshot(self) -> dict[str, Any]:
        """Trace用。**本文は入れない**（第12条）。IDと件数だけ。"""
        return {
            "before": self.count_before,
            "after": self.count_after,
            "by_id": self.id_duplicates_removed,
            "by_fingerprint": self.fingerprint_duplicates_removed,
            "duplicate_ids": list(self.duplicate_memory_ids[:8]),
            "near": list(self.possible_near_duplicates[:6]),
        }


def deduplicate(memories) -> DedupResult:
    """Planner へ渡す直前に絞る。**元のレコードは触らない。**

    順番は保つ——`rank()` が付けた並びを崩さない。
    """
    items = list(memories or ())
    result = DedupResult(count_before=len(items))
    seen_ids: set[int] = set()
    by_fingerprint: dict[str, Any] = {}
    order: list[Any] = []
    for memory in items:
        memory_id = int(getattr(memory, "memory_id", 0) or 0)
        if memory_id and memory_id in seen_ids:
            result.id_duplicates_removed += 1
            result.duplicate_memory_ids.append(memory_id)
            continue
        if memory_id:
            seen_ids.add(memory_id)
        mark = fingerprint(memory)
        if not mark:
            order.append(memory)
            continue
        rival = by_fingerprint.get(mark)
        if rival is None:
            by_fingerprint[mark] = memory
            order.append(memory)
            continue
        # **同じ話が2件。** 残す方を選び、落とした方はIDだけ残す。
        result.fingerprint_duplicates_removed += 1
        if _rank(memory) < _rank(rival):
            by_fingerprint[mark] = memory
            order[order.index(rival)] = memory
            result.duplicate_memory_ids.append(
                int(getattr(rival, "memory_id", 0) or 0))
        else:
            result.duplicate_memory_ids.append(memory_id)
    result.memories = order
    result.possible_near_duplicates = _near_duplicates(order)
    return result


#: 似ていると見なす語の重なり。**落とすためではなく、気づくため。**
NEAR_DUPLICATE_OVERLAP = .85


def _near_duplicates(memories) -> list[int]:
    """似ているが指紋は違うもの。**印を付けるだけで落とさない。**

    出典が違う組は数えない——`USER_STATEMENT` と `INFERENCE` が
    似ているのは**当たり前**で、それは重複ではない。
    """
    found: list[int] = []
    seen: list[tuple[str, set[str], int]] = []
    for memory in memories:
        summary = normalise_text(getattr(memory, "summary", ""))
        if len(summary) < 8:
            continue
        kind = str(getattr(memory, "information_type", "") or "")
        words = {summary[index:index + 2] for index in range(len(summary) - 1)}
        if not words:
            continue
        for other_kind, other_words, other_id in seen:
            if other_kind != kind:
                continue
            overlap = len(words & other_words) / max(1, len(words | other_words))
            if overlap >= NEAR_DUPLICATE_OVERLAP:
                found.append(other_id)
                break
        seen.append((kind, words, int(getattr(memory, "memory_id", 0) or 0)))
    return found


__all__ = [
    "NEAR_DUPLICATE_OVERLAP", "TYPE_PRIORITY", "DedupResult", "deduplicate",
    "fingerprint", "normalise_text",
]
