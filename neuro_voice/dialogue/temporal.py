"""Persona-scoped wall-clock and topic-time awareness for dialogue prompts."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _compact(text: str) -> str:
    return re.sub(r"[\s、。！？!?・,.\-]+", "", str(text or "").lower())


class TimeAwareness:
    """Keep just enough persisted timing data for natural temporal references.

    The store contains timestamps and short topic labels only.  It does not
    duplicate transcripts or become a second conversation-history database.
    """

    def __init__(self, path: str | Path, cfg):
        self._path = Path(path)
        zone_name = str(cfg.get("time_awareness.timezone", "Asia/Tokyo") or "Asia/Tokyo")
        try:
            self._zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            self._zone = ZoneInfo("UTC")
            zone_name = "UTC"
        self._zone_name = zone_name
        self._max_topics = max(8, int(cfg.get("time_awareness.max_topics", 48)))
        self._last_interaction_at = 0.0
        self._topics: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._last_interaction_at = float(raw.get("last_interaction_at") or 0.0)
            items = raw.get("topics") or []
            self._topics = [
                {"topic": str(item.get("topic") or "")[:120], "at": float(item.get("at") or 0.0)}
                for item in items if isinstance(item, dict) and str(item.get("topic") or "").strip()
            ][-self._max_topics:]
        except FileNotFoundError:
            pass
        except Exception:
            # Time awareness is enrichment; a corrupt file must never block a turn.
            self._last_interaction_at, self._topics = 0.0, []

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"last_interaction_at": self._last_interaction_at, "topics": self._topics[-self._max_topics:]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def record_turn(self, topic: str) -> None:
        now = time.time()
        self._last_interaction_at = now
        topic = " ".join(str(topic or "").split())[:120]
        if not topic:
            self.save()
            return
        key = _compact(topic)
        for item in reversed(self._topics):
            if _compact(item["topic"]) == key:
                item["at"] = now
                self.save()
                return
        self._topics.append({"topic": topic, "at": now})
        del self._topics[:-self._max_topics]
        self.save()

    def prompt_context(self, current_topic: str = "") -> str:
        now = time.time()
        local = datetime.fromtimestamp(now, self._zone)
        parts = [
            "【時間の状況】",
            f"現在は {local:%Y年%m月%d日} {self._weekday(local)}曜 {local:%H:%M}（{self._period(local.hour)}、{self._zone_name}）です。",
        ]
        if self._last_interaction_at > 0:
            parts.append(f"前回の会話から {self._relative(self._last_interaction_at, now)}です。")
        match = self._similar_topic(current_topic)
        if match is not None:
            parts.append(
                f"現在の話題に近い過去の話題「{match['topic'][:72]}」は {self._relative(float(match['at']), now)}に話しました。"
            )
        parts.append(
            "時間帯や経過時間が会話に関係するときだけ自然に触れる。時刻を毎回わざわざ言わない。"
            "時刻・日付が不確かな記憶を推測で断定せず、この情報を基準に『さっき』『昨日』『この前』を使い分ける。"
        )
        return "\n".join(parts)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        local = datetime.fromtimestamp(now, self._zone)
        return {
            "now": local.isoformat(timespec="minutes"), "timezone": self._zone_name,
            "period": self._period(local.hour),
            "last_interaction": self._relative(self._last_interaction_at, now) if self._last_interaction_at else None,
        }

    def relative_at(self, timestamp: float) -> str:
        """Format a persisted record timestamp relative to the current wall clock."""
        return self._relative(float(timestamp), time.time())

    def _similar_topic(self, current: str) -> dict[str, Any] | None:
        current = _compact(current)
        if len(current) < 3:
            return None
        grams = {current[i:i + 2] for i in range(len(current) - 1)}
        best: tuple[float, dict[str, Any]] | None = None
        for item in self._topics:
            candidate = _compact(item["topic"])
            other = {candidate[i:i + 2] for i in range(len(candidate) - 1)}
            if not other:
                continue
            score = len(grams & other) / max(1, len(grams | other))
            if score >= 0.34 and (best is None or score > best[0]):
                best = (score, item)
        return best[1] if best else None

    def _relative(self, then: float, now: float) -> str:
        seconds = max(0.0, now - then)
        if seconds < 90:
            return "たった今"
        if seconds < 90 * 60:
            return f"約{round(seconds / 60)}分前"
        if seconds < 22 * 3600:
            return f"約{round(seconds / 3600)}時間前"
        date = datetime.fromtimestamp(then, self._zone)
        today = datetime.fromtimestamp(now, self._zone).date()
        if (today - date.date()).days == 1:
            return f"昨日 {date:%H:%M}ごろ"
        if (today - date.date()).days < 7:
            return f"{(today - date.date()).days}日前"
        return f"{date:%m月%d日}"

    @staticmethod
    def _weekday(value: datetime) -> str:
        return "月火水木金土日"[value.weekday()]

    @staticmethod
    def _period(hour: int) -> str:
        if 5 <= hour < 10:
            return "朝"
        if 10 <= hour < 12:
            return "午前"
        if 12 <= hour < 17:
            return "昼"
        if 17 <= hour < 21:
            return "夜"
        return "深夜・早朝"
