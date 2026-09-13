"""Persona-scoped, multi-axis relationships with safe persistence.

Relationship state affects expression only.  It never changes factual, safety,
or ethical conclusions, and deliberately has no like/dislike aggregate score.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({
    "kindness", "empathy", "honesty", "consistency", "shared_interest",
    "thoughtful_disagreement", "respectful_boundary", "vulnerability", "support",
    "humor_connection", "dismissal", "disrespect", "hostility", "deception",
    "coercion", "boundary_violation", "mockery", "repeated_interruption",
    "manipulation", "unfair_blame", "apology", "repair_attempt",
    "behavior_improvement", "clarification",
})
_METRICS = (
    "trust", "psychological_safety", "respect", "interest", "comfort", "reciprocity",
    "caution", "unresolved_hurt", "repair_willingness", "social_fatigue", "irritation",
    "familiarity", "playfulness", "emotional_closeness", "conversational_sync",
    "uncertainty", "tension",
)
#: Metrics where a higher value means a *worse* relationship.  Merging two
#: profiles of one person must not import the strain from both halves.
_STRAIN = frozenset({
    "caution", "unresolved_hurt", "social_fatigue", "irritation",
    "uncertainty", "tension",
})
_POSITIVE = {"kindness", "empathy", "honesty", "consistency", "shared_interest",
             "thoughtful_disagreement", "respectful_boundary", "vulnerability", "support",
             "humor_connection"}
_REPAIR = {"apology", "repair_attempt", "behavior_improvement", "clarification"}
_WEIGHTS: dict[str, dict[str, float]] = {
    "kindness": {"comfort": .75, "psychological_safety": .45, "emotional_closeness": .25},
    "empathy": {"trust": .55, "comfort": .65, "reciprocity": .50, "emotional_closeness": .45},
    "honesty": {"trust": .80, "uncertainty": -.30}, "consistency": {"trust": .45, "conversational_sync": .25},
    "shared_interest": {"interest": .75, "comfort": .40, "playfulness": .22},
    "thoughtful_disagreement": {"respect": .65, "trust": .22, "conversational_sync": .18},
    "respectful_boundary": {"psychological_safety": .45, "respect": .25},
    "vulnerability": {"trust": .45, "psychological_safety": .35},
    "support": {"trust": .45, "comfort": .65, "emotional_closeness": .32}, "humor_connection": {"comfort": .55, "interest": .25, "playfulness": .65},
    "dismissal": {"comfort": -.60, "respect": -.45, "irritation": .40},
    "disrespect": {"respect": -.75, "psychological_safety": -.60, "caution": .45},
    "hostility": {"comfort": -.80, "psychological_safety": -.80, "caution": .65, "irritation": .60, "tension": .70},
    "deception": {"trust": -.85, "caution": .70, "uncertainty": .70}, "coercion": {"psychological_safety": -.85, "caution": .85, "tension": .65},
    "boundary_violation": {"psychological_safety": -.90, "unresolved_hurt": .75, "caution": .80},
    "mockery": {"comfort": -.70, "respect": -.65, "unresolved_hurt": .50},
    "repeated_interruption": {"social_fatigue": .55, "irritation": .40},
    "manipulation": {"trust": -.75, "caution": .70}, "unfair_blame": {"respect": -.50, "irritation": .50},
    "apology": {"irritation": -.70, "unresolved_hurt": -.40, "tension": -.40},
    "repair_attempt": {"repair_willingness": .50, "trust": .20, "tension": -.22},
    "behavior_improvement": {"trust": .35, "psychological_safety": .35, "comfort": .22, "conversational_sync": .18},
    "clarification": {"caution": -.25, "irritation": -.22, "uncertainty": -.40},
}


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _finite(v: Any, default: float) -> float:
    try:
        value = float(v)
        return _clamp(value) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


@dataclass
class RelationshipState:
    schema_version: int = 1
    speaker_id: str = ""
    trust: float = .50
    psychological_safety: float = .65
    respect: float = .50
    interest: float = .50
    comfort: float = .50
    reciprocity: float = .50
    caution: float = .10
    unresolved_hurt: float = .00
    repair_willingness: float = .80
    social_fatigue: float = .00
    irritation: float = .00
    familiarity: float = .10
    playfulness: float = .25
    emotional_closeness: float = .20
    conversational_sync: float = .35
    uncertainty: float = .35
    tension: float = .00
    interaction_count: int = 0
    meaningful_interaction_count: int = 0
    last_interaction_at: float | None = None
    last_updated_at: float | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        return data


@dataclass(frozen=True)
class RelationshipStyle:
    warmth: float
    openness: float
    playfulness: float
    directness: float
    guardedness: float
    boundary_strength: float
    repair_orientation: float
    label: str


class RelationshipStore:
    """Atomic JSON store for one persona's per-speaker relationship states."""

    def __init__(self, path: str | Path, cfg, persona_id: str = ""):
        self._path = Path(path)
        self._cfg = cfg
        self._lock = threading.RLock()
        self._states: dict[str, RelationshipState] = {}
        self._session_change: dict[str, float] = {}
        #: このファイルの持ち主（Phase 7D ⑥）。空なら従来どおり素通し。
        self._persona_id = str(persona_id or "")
        #: 他人のファイルだった。**読まないし、書かない。**
        self._foreign_owner = ""
        #: 持ち主の書かれていない旧ファイルを引き取ったか。
        self._adopted_legacy = False
        self._load()

    # ---------- 持ち主（Phase 7D ⑥） ----------

    @property
    def persona_id(self) -> str:
        return self._persona_id

    @property
    def readonly(self) -> bool:
        """他人のファイルを開いてしまった状態。**書き込みを止める。**

        読まないだけにして書き込みを許すと、次の `save()` が
        **相手の関係値を空で上書きする。** 読めないより悪い。
        """
        return bool(self._foreign_owner)

    @property
    def allow_foreign(self) -> bool:
        """互換 fallback。**既定は無効。**"""
        return bool(self._cfg.get(
            "mind.relationship.allow_foreign_persona_file", False))

    def ownership(self) -> dict[str, Any]:
        return {"persona_id": self._persona_id,
                "foreign_owner": self._foreign_owner,
                "adopted_legacy": self._adopted_legacy,
                "readonly": self.readonly, "path": self._path.name}

    def rollback_adoption(self) -> bool:
        """引き取りを取り消し、持ち主を書かない形へ戻す。

        値は動かしていないので、**ヘッダを消すだけで元に戻る。**
        """
        with self._lock:
            if not self._adopted_legacy:
                return False
            self._persona_id = ""
            self._adopted_legacy = False
        self.save()
        return True

    def _defaults(self) -> dict[str, float]:
        return {key: _finite(self._cfg.get(f"mind.relationship.defaults.{key}", default), default)
                for key, default in RelationshipState().__dict__.items() if key in _METRICS}

    def _new(self, speaker_id: str) -> RelationshipState:
        state = RelationshipState(speaker_id=str(speaker_id)[:120])
        for key, value in self._defaults().items():
            setattr(state, key, value)
        return state

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            entries = raw.get("relationships", {}) if isinstance(raw, dict) else {}
            if not isinstance(entries, dict):
                return
            owner = str(raw.get("persona_id", "") or "") if isinstance(raw, dict) else ""
            if self._persona_id and owner and owner != self._persona_id:
                # **他人の関係値。** 旧データを全ペルソナへ配らないための境界。
                if not self.allow_foreign:
                    self._foreign_owner = owner
                    logger.warning(
                        "他のペルソナの関係値ファイルなので読みません: %s "
                        "(持ち主=%s / いま=%s)", self._path.name, owner,
                        self._persona_id)
                    return
                logger.warning(
                    "互換設定により他ペルソナの関係値を読み込みます: %s (持ち主=%s)",
                    self._path.name, owner)
            elif self._persona_id and not owner and entries:
                # 持ち主の書かれていない旧ファイル。**ファイル名で既に
                # 分かれている**ので、引き取りであって配布ではない。
                # 冪等——一度ヘッダが付けば二度目は通らない。
                self._adopted_legacy = True
                logger.info("持ち主の無い関係値を引き取ります: %s → %s",
                            self._path.name, self._persona_id)
            for key, item in entries.items():
                if not isinstance(item, dict):
                    continue
                state = self._new(str(key))
                for metric in _METRICS:
                    setattr(state, metric, _finite(item.get(metric), getattr(state, metric)))
                state.interaction_count = max(0, int(item.get("interaction_count", 0) or 0))
                state.meaningful_interaction_count = max(0, int(item.get("meaningful_interaction_count", 0) or 0))
                state.last_interaction_at = self._timestamp(item.get("last_interaction_at"))
                state.last_updated_at = self._timestamp(item.get("last_updated_at"))
                events = item.get("recent_events", [])
                state.recent_events = [e for e in events if isinstance(e, dict)][-self._max_events:]
                self._states[state.speaker_id] = state
        except Exception:
            logger.warning("関係性データの読込に失敗。初期状態で継続します: %s", self._path)

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed > 0 else None
        except (TypeError, ValueError):
            return None

    @property
    def _max_events(self) -> int:
        return max(1, int(self._cfg.get("mind.relationship.max_recent_events", 20)))

    def get(self, speaker_id: str) -> RelationshipState:
        key = str(speaker_id or "unknown")[:120]
        with self._lock:
            state = self._states.setdefault(key, self._new(key))
            self._decay(state, time.time())
            return RelationshipState(**deepcopy(state.as_dict()))

    def import_state(
        self, speaker_id: str, metrics: dict[str, float], *,
        interaction_count: int = 0, meaningful_interaction_count: int = 0,
        last_interaction_at: float | None = None,
    ) -> dict[str, float]:
        """外から値を丸ごと入れる。**移行とロールバック専用。**

        `get()` は写しを返すので、返り値を書き換えても保存されない
        ——呼び出し側が共有状態を壊さないための作りで、そこは変えない。
        だが `speaker_id` から `person_id` への移行では、
        **既存の値をそのまま写す**必要がある。会話中の更新用（`apply_state_delta`）は
        セッション上限で削られるので使えない。

        **普段の会話からは呼ばないこと。** ここには上限が無い。
        """
        key = str(speaker_id or "unknown")[:120]
        applied: dict[str, float] = {}
        with self._lock:
            state = self._states.setdefault(key, self._new(key))
            for name, value in (metrics or {}).items():
                if name not in _METRICS:
                    continue
                applied[name] = _clamp(_finite(value, getattr(state, name, .5)))
                setattr(state, name, applied[name])
            if interaction_count:
                state.interaction_count = max(
                    int(state.interaction_count), int(interaction_count))
            if meaningful_interaction_count:
                state.meaningful_interaction_count = max(
                    int(state.meaningful_interaction_count),
                    int(meaningful_interaction_count))
            if last_interaction_at:
                state.last_interaction_at = max(
                    float(state.last_interaction_at or 0.0),
                    float(last_interaction_at))
            state.last_updated_at = time.time()
        self.save()
        return applied

    def observe(self, speaker_id: str, meaningful: bool = True) -> None:
        with self._lock:
            state = self._states.setdefault(str(speaker_id or "unknown")[:120], self._new(speaker_id))
            now = time.time()
            self._decay(state, now)
            state.interaction_count += 1
            state.meaningful_interaction_count += int(bool(meaningful))
            state.familiarity = _clamp(state.familiarity + (.004 if meaningful else .001))
            state.conversational_sync = _clamp(state.conversational_sync + (.002 if meaningful else 0.0))
            state.last_interaction_at = now
            state.last_updated_at = now
        self.save()

    def apply_events(self, events: list[dict[str, Any]]) -> list[str]:
        if not bool(self._cfg.get("mind.relationship.enabled", True)):
            return []
        changed: list[str] = []
        reflection_change: dict[str, float] = {}
        with self._lock:
            for raw in events[:12]:
                event = self._validated_event(raw)
                if event is None:
                    continue
                speaker_id = event["speaker_id"]
                state = self._states.setdefault(speaker_id, self._new(speaker_id))
                self._decay(state, time.time())
                novelty, repetition = self._event_factors(state, event)
                base = float(self._cfg.get("mind.relationship.max_change_per_event", .025))
                multiplier = (float(self._cfg.get("mind.relationship.positive_multiplier", 1.0)) if event["event_type"] in _POSITIVE
                              else float(self._cfg.get("mind.relationship.repair_multiplier", 1.1)) if event["event_type"] in _REPAIR
                              else float(self._cfg.get("mind.relationship.negative_multiplier", 1.2)))
                growth = max(0.0, float(self._cfg.get("mind.relationship.growth_rate", 1.0)))
                delta = min(base, base * event["intensity"] * event["confidence"] * growth * multiplier * novelty * repetition)
                reflection_remaining = max(0.0, float(self._cfg.get("mind.relationship.max_change_per_reflection", .05)) - reflection_change.get(speaker_id, 0.0))
                remaining = max(0.0, float(self._cfg.get("mind.relationship.max_change_per_session", .12)) - self._session_change.get(speaker_id, 0.0))
                delta = min(delta, remaining, reflection_remaining)
                if delta <= .0001:
                    continue
                for metric, weight in _WEIGHTS[event["event_type"]].items():
                    setattr(state, metric, _clamp(getattr(state, metric) + delta * weight))
                state.last_updated_at = time.time()
                event["delta"] = round(delta, 4)
                state.recent_events.append(event)
                state.recent_events = state.recent_events[-self._max_events:]
                self._session_change[speaker_id] = self._session_change.get(speaker_id, 0.0) + delta
                reflection_change[speaker_id] = reflection_change.get(speaker_id, 0.0) + delta
                changed.append(f"{speaker_id}:{event['event_type']}")
        if changed:
            self.save()
            logger.info("関係性を更新: %s", ", ".join(changed))
        return changed

    def apply_state_delta(
        self, speaker_id: str, dimension: str, delta: float, *, reason: str = "",
    ) -> float:
        """検算済みの差分を1軸へ入れる。**LLMのイベント経由ではない入口。**

        `apply_events` は内省LLMが出した `relationship_events` 専用で、
        `ActionOutcome` から関係を動かす道が無かった。ここはその道。

        **上限は二重に掛かる**——呼び出し側（`StateDeltaGate`）で1イベント分を
        絞ってから、ここでもセッション合計の残りを見る。片方だけだと、
        1ターンに何度も呼べば関係が動かせてしまう。
        """
        if not bool(self._cfg.get("mind.relationship.enabled", True)):
            return .0
        speaker = str(speaker_id or "").strip()[:120]
        if not speaker or speaker in {"unknown", "unknown_speaker"}:
            # **不明話者で特定の人の長期値を動かさない。**
            return .0
        if dimension not in _METRICS:
            return .0
        with self._lock:
            state = self._states.setdefault(speaker, self._new(speaker))
            self._decay(state, time.time())
            remaining = max(0.0, float(
                self._cfg.get("mind.relationship.max_change_per_session", .12)
            ) - self._session_change.get(speaker, 0.0))
            amount = max(-remaining, min(remaining, float(delta)))
            if abs(amount) <= .0001:
                return .0
            setattr(state, dimension, _clamp(getattr(state, dimension) + amount))
            state.last_updated_at = time.time()
            state.recent_events.append({
                "speaker_id": speaker, "event_type": "state_delta",
                "summary": str(reason)[:160], "delta": round(amount, 4),
                "dimension": dimension, "at": time.time(),
            })
            state.recent_events = state.recent_events[-self._max_events:]
            self._session_change[speaker] = self._session_change.get(speaker, 0.0) + abs(amount)
        self.save()
        return round(amount, 4)

    def _validated_event(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("event_type", "")).strip()
        speaker_id = str(raw.get("speaker_id", "")).strip()[:120]
        if kind not in EVENT_TYPES or not speaker_id:
            return None
        confidence = _finite(raw.get("confidence"), 0.0)
        if confidence < .55:
            return None
        return {"speaker_id": speaker_id, "event_type": kind,
                "intensity": _finite(raw.get("intensity"), 0.0), "confidence": confidence,
                "summary": str(raw.get("summary", "")).replace("\n", " ")[:160],
                "at": time.time()}

    def _event_factors(self, state: RelationshipState, event: dict[str, Any]) -> tuple[float, float]:
        same = [e for e in state.recent_events if e.get("event_type") == event["event_type"]]
        similar = [e for e in same if e.get("summary") and e.get("summary") == event.get("summary")]
        novelty = .15 if similar else (.55 if same else 1.0)
        repetition = min(1.5, 1.0 + .15 * len(same))
        return novelty, repetition

    def _decay(self, state: RelationshipState, now: float) -> None:
        then = float(state.last_updated_at or now)
        hours = max(0.0, (now - then) / 3600.0)
        if hours <= 0:
            return
        for metric, path, baseline in (
            ("irritation", "irritation_half_life_hours", 0.0),
            ("social_fatigue", "social_fatigue_half_life_hours", 0.0),
            ("caution", "caution_half_life_days", .10),
            ("unresolved_hurt", "unresolved_hurt_passive_half_life_days", 0.0),
            ("tension", "tension_half_life_hours", 0.0),
            ("uncertainty", "uncertainty_half_life_days", .25),
        ):
            half = float(self._cfg.get(f"mind.relationship.decay.{path}", 4.0 if "hours" in path else 14.0))
            if "days" in path:
                half *= 24
            factor = .5 ** (hours / max(.01, half))
            setattr(state, metric, _clamp(baseline + (getattr(state, metric) - baseline) * factor))
        state.last_updated_at = now

    def style(self, speaker_id: str) -> RelationshipStyle:
        s = self.get(speaker_id)
        warmth = _clamp((s.trust + s.comfort + s.psychological_safety) / 3)
        openness = _clamp(.45*s.trust + .35*s.psychological_safety + .20*s.comfort)
        guarded = _clamp(.55*s.caution + .30*s.unresolved_hurt + .15*(1-s.trust))
        boundary = _clamp(.45*s.caution + .35*s.unresolved_hurt + .20*s.irritation)
        if boundary >= .58: label = "boundary_needed"
        elif guarded >= .45: label = "cautious"
        elif warmth >= .68: label = "friendly"
        elif openness >= .58: label = "open"
        else: label = "neutral"
        return RelationshipStyle(warmth, openness, _clamp(.45*s.comfort + .30*s.interest + .25*s.playfulness),
                                 _clamp(.55*s.respect + .45*s.trust), guarded, boundary,
                                 _clamp(s.repair_willingness * (1 - .35*s.unresolved_hurt)), label)

    def prompt(self, speaker_id: str) -> str:
        style = self.style(speaker_id)
        if style.label == "boundary_needed":
            body = "最近のやり取りには傷つきと警戒がある。礼儀は保ちつつ、無理に親しげにせず、境界線を短く明確に伝える。改善された態度は認める。"
        elif style.label == "cautious":
            body = "この相手には興味や親しみがあっても、少し警戒と疲れが残っている。自己開示は控えめにし、落ち着いて率直に話す。"
        elif style.label in {"friendly", "open"}:
            body = "この相手には基本的な信頼と親しみを感じている。意見が違っても、遠慮しすぎず温かく率直に伝えられる。"
        else:
            body = "この相手とは中立で丁寧な関係にある。親切に話すが、関係を急いで決めつけない。"
        return ("【この相手との現在の関係】\n" + body + "\n"
                "関係性は伝え方だけの参考。事実・安全・倫理の判断や、同意・反対の結論は変えない。"
                "好意を示すためだけに同意せず、違うと思う時は理由を添えて自然に伝える。")

    def snapshot(self, speaker_id: str) -> dict[str, Any]:
        state = self.get(speaker_id)
        style = self.style(speaker_id)
        return {**state.as_dict(), "style": style.__dict__, "description": self.prompt(speaker_id).split("\n", 1)[1].split("\n", 1)[0]}

    def all_snapshots(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            keys = list(self._states)
        return {key: self.snapshot(key) for key in keys}

    def merge(self, source_id: str, target_id: str) -> bool:
        """Fold one speaker's relationship into another's.

        Used when two profiles turn out to be one person.  Every axis keeps the
        longer history: trust and closeness take the higher value, interaction
        counts add up.  Averaging would quietly demote a real friendship.
        """
        source_id, target_id = str(source_id), str(target_id)
        if source_id == target_id:
            return False
        with self._lock:
            source = self._states.get(source_id)
            if source is None:
                return False
            target = self._states.get(target_id)
            if target is None:
                source.speaker_id = target_id
                self._states[target_id] = source
                self._states.pop(source_id, None)
                self.save()
                return True
            for key in _METRICS:
                left = _finite(getattr(target, key, 0.0), 0.0)
                right = _finite(getattr(source, key, 0.0), 0.0)
                # A person is not more distrusted or more tiring just because
                # their second profile had a rough turn, so the strain metrics
                # take the lower value while the warmth metrics take the higher.
                setattr(target, key, min(left, right) if key in _STRAIN else max(left, right))
            for key in ("interaction_count", "meaningful_interaction_count"):
                setattr(target, key, int(getattr(target, key, 0)) + int(getattr(source, key, 0)))
            for key in ("last_interaction_at", "last_updated_at"):
                values = [
                    value for value in (getattr(target, key, None), getattr(source, key, None))
                    if value
                ]
                if values:
                    setattr(target, key, max(values))
            history = list(getattr(target, "recent_events", []) or [])
            history += list(getattr(source, "recent_events", []) or [])
            history.sort(key=lambda item: float((item or {}).get("at", 0.0) or 0.0))
            target.recent_events = history[-self._max_events:]
            self._states.pop(source_id, None)
        self.save()
        logger.info("関係性を統合: %s → %s", source_id, target_id)
        return True

    def reset(self, speaker_id: str, temporary_only: bool = False) -> None:
        with self._lock:
            state = self._states.setdefault(str(speaker_id), self._new(speaker_id))
            if temporary_only:
                state.irritation = state.social_fatigue = 0.0
            else:
                self._states[str(speaker_id)] = self._new(speaker_id)
        self.save()

    def save(self) -> None:
        if self.readonly:
            # **相手の関係値を空で上書きしない。** 読めないより悪い。
            logger.warning(
                "他ペルソナの関係値ファイルなので保存しません: %s (持ち主=%s)",
                self._path.name, self._foreign_owner)
            return
        with self._lock:
            data = {"schema_version": 2, "persona_id": self._persona_id,
                    "relationships": {k: v.as_dict() for k, v in self._states.items()}}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self._path.stem, suffix=".tmp", dir=self._path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            logger.exception("関係性データの保存に失敗: %s", self._path)
