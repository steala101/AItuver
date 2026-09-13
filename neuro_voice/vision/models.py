"""Structured, privacy-safe visual observations."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class VisualObservation:
    observation_id: str = field(default_factory=lambda: uuid4().hex)
    summary: str = ""
    people: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    visible_text: list[str] = field(default_factory=list)
    scene_changes: list[str] = field(default_factory=list)
    confidence: float | None = None
    captured_at: float = field(default_factory=time.time)
    analyzed_at: float = field(default_factory=time.time)
    source_name: str = ""
    scene_type: str = ""
    importance: float = 0.0
    frame_hash: str = ""
    processing_time_ms: int = 0
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    conversation_relevance: str = "none"
    # Optional game fields.  Keeping them on the shared observation model lets
    # ordinary screen/camera analysis remain backward compatible.
    game: str = ""
    player_state: list[str] = field(default_factory=list)
    player_state_details: dict[str, Any] = field(default_factory=dict)
    notable_events: list[str] = field(default_factory=list)
    suggested_help: str = ""
    commentary_worthy: bool = False

    @classmethod
    def from_model_text(cls, text: str, *, captured_at: float | None = None) -> "VisualObservation":
        """Parse constrained JSON and retain malformed output as a short summary."""
        raw = (text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Common streaming-model damage: prose around the JSON object or a
            # trailing comma before a closing bracket. Repair only those
            # deterministic cases; otherwise retain a bounded fallback text.
            start, end = raw.find("{"), raw.rfind("}")
            candidate = raw[start:end + 1] if 0 <= start < end else raw
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                data = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                return cls(summary=raw[:800], captured_at=captured_at or time.time())
        if not isinstance(data, dict):
            return cls(summary=raw[:800], captured_at=captured_at or time.time())

        def values(key: str) -> list[str]:
            value = data.get(key) or []
            if not isinstance(value, list):
                return []
            return [str(item).strip()[:160] for item in value if str(item).strip()][:12]

        raw_player_state = data.get("player_state") or {}
        player_state_details = raw_player_state if isinstance(raw_player_state, dict) else {}
        if player_state_details:
            player_state = [
                f"{key}: {value}" for key, value in player_state_details.items()
                if value not in (None, "", [], {})
            ][:16]
        else:
            player_state = values("player_state")

        confidence = data.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        importance = data.get("importance", 0.0)
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.0
        relevance = str(data.get("conversation_relevance", "none")).lower()
        if relevance not in {"none", "low", "medium", "high"}:
            relevance = "none"
        return cls(
            summary=str(data.get("summary", "")).strip()[:800],
            people=values("people"), objects=values("objects"), actions=values("actions"),
            visible_text=values("visible_text"), scene_changes=values("scene_changes"),
            confidence=confidence, captured_at=captured_at or time.time(),
            analyzed_at=time.time(),
            conversation_relevance=relevance,
            game=str(data.get("game", "")).strip()[:80],
            player_state=player_state,
            player_state_details={str(key): value for key, value in player_state_details.items()},
            notable_events=values("notable_events"),
            suggested_help=str(data.get("suggested_help", "")).strip()[:300],
            commentary_worthy=bool(data.get("commentary_worthy", False)),
            scene_type=str(data.get("scene_type", "")).strip()[:80],
            importance=importance,
        )

    def prompt_context(self) -> str:
        lines = [f"- {self.summary}"] if self.summary else []
        if self.actions:
            lines.append("- 動作: " + "、".join(self.actions))
        if self.scene_changes:
            lines.append("- 変化: " + "、".join(self.scene_changes))
        if self.objects:
            lines.append("- 物体: " + "、".join(self.objects))
        if self.visible_text:
            lines.append("- 見える文字: " + "、".join(self.visible_text))
        if self.game:
            lines.append("- ゲーム: " + self.game)
        if self.player_state:
            lines.append("- プレイヤー状態: " + "、".join(self.player_state))
        if self.notable_events:
            lines.append("- 重要イベント: " + "、".join(self.notable_events))
        if self.suggested_help:
            lines.append("- サポート候補: " + self.suggested_help)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class VisualEvent:
    event_id: str
    event_type: str
    summary: str
    occurred_at: float
    importance: float
    confidence: float
    signature: str
    observation_id: str


@dataclass(slots=True)
class PerceptionMemory:
    memory_seconds: float = 30.0
    current_scene: VisualObservation | None = None
    recent_events: list[VisualEvent] = field(default_factory=list)
    recent_observations: list[VisualObservation] = field(default_factory=list)
    flow_summary: str = ""
    last_injected_at: float | None = None
    _recent_signatures: dict[str, float] = field(default_factory=dict, repr=False)

    @staticmethod
    def _signature(text: str) -> str:
        return re.sub(r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+", "", text.casefold())[:200]

    @staticmethod
    def _event_importance(text: str, fallback: float) -> float:
        lowered = text.casefold()
        if any(word in lowered for word in (
            "死亡", "死ん", "you died", "爆発", "explosion", "瀕死", "critical health",
        )):
            return 1.0
        if any(word in lowered for word in (
            "攻撃", "襲われ", "戦闘", "敵", "ゾンビ", "creeper", "lava", "落下",
            "ダイヤ", "diamond", "エラー", "error",
        )):
            return max(.75, fallback)
        return max(.35, fallback)

    def _prune(self, now: float) -> None:
        cutoff = now - max(1.0, float(self.memory_seconds))
        self.recent_observations[:] = [
            item for item in self.recent_observations if float(item.captured_at or now) >= cutoff
        ]
        self.recent_events[:] = [item for item in self.recent_events if item.occurred_at >= cutoff]
        for signature, seen_at in tuple(self._recent_signatures.items()):
            if seen_at < cutoff:
                self._recent_signatures.pop(signature, None)

    def record(self, observation: VisualObservation) -> list[VisualEvent]:
        previous = self.current_scene
        self.current_scene = observation
        self.recent_observations.append(observation)
        now = float(observation.captured_at or time.time())
        self._prune(now)
        created: list[VisualEvent] = []
        changes = [*observation.scene_changes, *observation.notable_events]
        if not changes and previous is not None and previous.summary and observation.summary:
            if self._signature(previous.summary) != self._signature(observation.summary):
                changes = [f"{previous.summary[:100]} → {observation.summary[:140]}"]
        for text in dict.fromkeys(item.strip() for item in changes if item.strip()):
            signature = self._signature(text)
            if not signature or signature in self._recent_signatures:
                continue
            importance = self._event_importance(text, observation.importance)
            event = VisualEvent(
                event_id=uuid4().hex,
                event_type="visual_change",
                summary=text[:240],
                occurred_at=now,
                importance=importance,
                confidence=float(observation.confidence or 0.0),
                signature=signature,
                observation_id=observation.observation_id,
            )
            self.recent_events.append(event)
            self._recent_signatures[signature] = now
            created.append(event)
        if self.recent_events:
            self.flow_summary = " → ".join(item.summary for item in self.recent_events[-5:])[:800]
        return created

    def get_current_state(self) -> VisualObservation | None:
        return self.current_scene

    def get_recent_events(self, seconds: float = 15.0, *, now: float | None = None) -> list[VisualEvent]:
        now = time.time() if now is None else float(now)
        self._prune(now)
        cutoff = now - max(0.0, float(seconds))
        return [item for item in self.recent_events if item.occurred_at >= cutoff]

    def get_rolling_summary(self, seconds: float = 30.0, *, now: float | None = None) -> str:
        events = self.get_recent_events(seconds, now=now)
        if events:
            return " → ".join(item.summary for item in events[-6:])[:800]
        return self.current_scene.summary[:400] if self.current_scene is not None else ""

    def timeline_prompt_context(
        self, *, now: float | None = None, latest_frame_at: float | None = None,
        motion_score: float | None = None,
    ) -> str | None:
        """Compact current state plus chronological evidence for conversation.

        The timestamps make stale semantic inference explicit.  The model must
        not silently treat a 20-second-old observation as the current frame.
        """
        current = self.current_scene
        if current is None:
            return None
        now = time.time() if now is None else float(now)
        state_age = max(0.0, now - float(current.captured_at or now))
        frame_age = None if latest_frame_at is None else max(0.0, now - float(latest_frame_at))
        lines = [
            "ゲーム映像の継続認識状態:",
            f"- 意味解析済み状態（{state_age:.1f}秒前のフレーム）:",
            current.prompt_context() or "- 詳細なし",
        ]
        events = self.get_recent_events(min(30.0, self.memory_seconds), now=now)
        if events:
            lines.append("- 直近の出来事: " + " → ".join(item.summary for item in events[-6:]))
        if self.flow_summary:
            lines.append("- 直近の流れ: " + self.flow_summary)
        if frame_age is not None:
            motion = "不明" if motion_score is None else f"{max(0.0, motion_score):.2f}"
            lines.append(f"- OBS最新フレーム: {frame_age:.1f}秒前 / 画面変化量={motion}")
        lines.append(
            "最新フレーム取得時刻と意味解析時刻は別である。意味解析後の変化を勝手に補完せず、"
            "ユーザーが今の状況を説明した場合はその発言を最新情報として優先する。"
        )
        return "\n".join(lines)

    def get_context_for_dialogue(
        self, query: str, *, now: float | None = None,
        latest_frame_at: float | None = None, motion_score: float | None = None,
        max_chars: int = 1600,
    ) -> str | None:
        current = self.current_scene
        if current is None:
            return None
        normalized = re.sub(r"\s+", "", query or "")
        visual_terms = (
            "画面", "見える", "映って", "今どう", "今何", "今どこ", "今の状況",
            "さっき", "何が起き", "敵", "体力", "死ん", "どうしたら", "どうすれば",
            "見つからない", "採掘", "掘って", "マイクラ", "Minecraft", "やば", "危な",
        )
        explicit = any(term in normalized for term in visual_terms)
        if not explicit:
            return None
        context = self.timeline_prompt_context(
            now=now, latest_frame_at=latest_frame_at, motion_score=motion_score,
        )
        return context[:max(200, int(max_chars))] if context else None


@dataclass(slots=True)
class VideoSegment:
    start_s: float
    end_s: float
    observation: VisualObservation


@dataclass(slots=True)
class VideoAnalysis:
    summary: str
    timeline: list[VideoSegment] = field(default_factory=list)
    detected_actions: list[str] = field(default_factory=list)
    detected_objects: list[str] = field(default_factory=list)
    visible_text: list[str] = field(default_factory=list)
