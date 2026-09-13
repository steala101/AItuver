"""Persistent, privacy-safe temporal self and continuous voice affect.

This module stores only operational timing and aggregate affect values.  It
never stores transcript text, raw audio, or a hidden reasoning trace.  Wall
clock is used for calendar/absence calculations; monotonic time is used only
for the currently running process session.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _utc_now() -> float:
    return time.time()


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    boot_id: str
    started_at: float
    ended_at: float | None = None
    last_heartbeat_at: float = 0.0
    duration_seconds: float = 0.0
    shutdown_type: str = "UNKNOWN"  # CLEAN / CRASHED / FORCED / UNKNOWN
    discord_joined_at: float | None = None
    discord_left_at: float | None = None
    conversation_count: int = 0
    active_seconds: float = 0.0


@dataclass(slots=True)
class AffectState:
    """Slow mood plus event-driven emotion; all values remain bounded."""
    joy: float = 0.0
    warmth: float = 0.0
    calm: float = 0.15
    concern: float = 0.0
    sadness: float = 0.0
    frustration: float = 0.0
    loneliness: float = 0.0
    surprise: float = 0.0
    mood: str = "neutral"
    last_updated_at: float = 0.0


class TemporalSelf:
    """A persona-scoped lifetime/session tracker and affect controller."""

    VERSION = 1
    _AFFECT_DELTAS = {
        "joy": {"joy": .16, "warmth": .06, "calm": .02},
        "fun": {"joy": .13, "warmth": .03},
        "sad": {"sadness": .12, "calm": -.03},
        "angry": {"concern": .08, "frustration": .05, "calm": -.05},
        "surprised": {"surprise": .13, "joy": .02},
        "warm": {"warmth": .10, "calm": .04},
    }

    def __init__(self, path: str | Path, cfg, *, persona_key: str) -> None:
        self._path = Path(path)
        self._cfg = cfg
        self._persona_key = persona_key
        self._zone = ZoneInfo(str(cfg.get("temporal_self.timezone", "Asia/Tokyo") or "Asia/Tokyo"))
        self._heartbeat_interval = max(5.0, float(cfg.get("temporal_self.heartbeat_interval_seconds", 30)))
        self._max_sessions = max(10, int(cfg.get("temporal_self.max_recent_sessions", 60)))
        self._max_delta = max(.01, min(.5, float(cfg.get("affect.max_event_delta", .25))))
        self._attack = _clip(float(cfg.get("voice_affect.attack_rate", .30)), 0.01, 1.0)
        self._release = _clip(float(cfg.get("voice_affect.release_rate", .15)), 0.01, 1.0)
        self._voice_max_delta = _clip(float(cfg.get("voice_affect.max_delta_per_utterance", .20)), .01, 1.0)
        self._data: dict[str, Any] = self._load()
        self._boot_monotonic = time.monotonic()
        self._voice_by_source: dict[str, Any] = {}
        self._start_session()

    def _load(self) -> dict[str, Any]:
        default = {
            "version": self.VERSION, "persona_key": self._persona_key,
            "first_initialized_at": _utc_now(), "first_successful_boot_at": 0.0,
            "last_seen_alive_at": 0.0, "last_clean_shutdown_at": 0.0,
            "total_uptime_seconds": 0.0, "total_inactive_seconds": 0.0,
            "active_calendar_days": [], "sessions": [], "affect": asdict(AffectState()),
        }
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                default.update(loaded)
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt enrichment file never prevents the assistant booting.
            pass
        return default

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def _start_session(self) -> None:
        now = _utc_now()
        sessions = self._data.setdefault("sessions", [])
        # An unfinished previous session means the process did not cleanly exit.
        if sessions and sessions[-1].get("ended_at") is None:
            previous = sessions[-1]
            previous["ended_at"] = float(previous.get("last_heartbeat_at") or previous.get("started_at") or now)
            previous["shutdown_type"] = "CRASHED"
            previous["duration_seconds"] = max(0.0, previous["ended_at"] - float(previous.get("started_at") or now))
        last_seen = float(self._data.get("last_seen_alive_at") or 0.0)
        if last_seen and now > last_seen:
            self._data["total_inactive_seconds"] = float(self._data.get("total_inactive_seconds") or 0.0) + (now - last_seen)
            self._apply_offline_decay(now - last_seen)
        self._session = SessionRecord(uuid.uuid4().hex, uuid.uuid4().hex, now, last_heartbeat_at=now)
        sessions.append(asdict(self._session))
        self._data["sessions"] = sessions[-self._max_sessions:]
        self._data["first_successful_boot_at"] = float(self._data.get("first_successful_boot_at") or now)
        self._data["last_seen_alive_at"] = now
        self._active_day(now)
        self._save()

    def heartbeat(self) -> None:
        now = _utc_now()
        if now - self._session.last_heartbeat_at < self._heartbeat_interval:
            return
        self._session.last_heartbeat_at = now
        self._session.duration_seconds = max(0.0, time.monotonic() - self._boot_monotonic)
        self._update_current_session()
        self._data["last_seen_alive_at"] = now
        self._save()

    def close(self, shutdown_type: str = "CLEAN") -> None:
        now = _utc_now()
        self._session.ended_at = now
        self._session.shutdown_type = shutdown_type if shutdown_type in {"CLEAN", "FORCED", "CRASHED"} else "UNKNOWN"
        self._session.duration_seconds = max(0.0, time.monotonic() - self._boot_monotonic)
        self._data["total_uptime_seconds"] = float(self._data.get("total_uptime_seconds") or 0.0) + self._session.duration_seconds
        self._data["last_seen_alive_at"] = now
        if self._session.shutdown_type == "CLEAN":
            self._data["last_clean_shutdown_at"] = now
        self._update_current_session()
        self._save()

    def observe_activity(self, *, source: str = "local", discord: bool = False) -> None:
        self._session.conversation_count += 1
        self._session.active_seconds = max(self._session.active_seconds, time.monotonic() - self._boot_monotonic)
        if discord and self._session.discord_joined_at is None:
            self._session.discord_joined_at = _utc_now()
        self._active_day(_utc_now())
        self._update_current_session()

    def note_discord_left(self) -> None:
        self._session.discord_left_at = _utc_now()
        self._update_current_session()

    def observe_affect(self, label: str | None, *, intensity: float = 1.0, reason: str = "conversation") -> AffectState:
        state = self._affect()
        label = str(label or "neutral").lower()
        scale = _clip(intensity, 0.0, 1.0)
        for key, delta in self._AFFECT_DELTAS.get(label, {}).items():
            setattr(state, key, _clip(getattr(state, key) + max(-self._max_delta, min(self._max_delta, delta * scale))))
        # emotion is temporary, mood is deliberately slower and does not flip each turn.
        state.calm = _clip(state.calm + (.015 if label in {"neutral", "calm", "warm"} else -.005))
        state.mood = self._mood_for(state)
        state.last_updated_at = _utc_now()
        self._data["affect"] = asdict(state)
        return state

    def smooth_style(self, base_style, *, source: str, generation_id: str | None = None):
        """Return a bounded, gradual SpeechStyle without mutating TTS state."""
        from neuro_voice.tts.style import SpeechStyle
        state = self._affect()
        mood_delivery = {"bright": "lively", "warm": "warm", "low": "soft", "tense": "serious"}.get(state.mood, base_style.delivery)
        target = SpeechStyle(
            emotion=base_style.emotion, delivery=mood_delivery,
            speed=_clip(base_style.speed + (.035 * state.joy) - (.04 * state.sadness), .75, 1.25),
            pitch=_clip(base_style.pitch + (.018 * state.joy) - (.014 * state.sadness), -.12, .12),
            intonation=_clip(base_style.intonation + (.15 * state.joy) - (.11 * state.sadness) - (.08 * state.frustration), .70, 1.35),
            volume=_clip(base_style.volume + (.05 * state.joy) - (.05 * state.sadness), .70, 1.20),
            pre_phoneme=base_style.pre_phoneme, post_phoneme=base_style.post_phoneme,
        )
        previous = self._voice_by_source.get(source)
        if previous is None:
            rendered = target
        else:
            rate = self._attack if target.intonation >= previous.intonation else self._release
            def step(old: float, new: float) -> float:
                raw = old + (new - old) * rate
                return old + max(-self._voice_max_delta, min(self._voice_max_delta, raw - old))
            rendered = SpeechStyle(target.emotion, target.delivery, step(previous.speed, target.speed),
                                   step(previous.pitch, target.pitch), step(previous.intonation, target.intonation),
                                   step(previous.volume, target.volume), target.pre_phoneme, target.post_phoneme)
        self._voice_by_source[source] = rendered
        return rendered

    def prompt_context(self) -> str:
        now = _utc_now()
        zone_now = datetime.fromtimestamp(now, self._zone)
        ret = self.return_context()
        affect = self._affect()
        return (
            f"【時間的自己認識】現在は{zone_now:%Y-%m-%d %H:%M} ({self._zone.key})。"
            f"この稼働は{self._human_duration(time.monotonic() - self._boot_monotonic)}。"
            f"前回から{self._human_duration(ret['inactive_duration'])}空いている。"
            f"現在の気分傾向は{affect.mood}。日時・不在期間を根拠なく言い換えず、確信がない時は断定しない。"
        )

    def return_context(self) -> dict[str, Any]:
        sessions = self._completed_sessions()
        now = _utc_now()
        last_end = float(sessions[-1].get("ended_at") or 0.0) if sessions else 0.0
        gap = max(0.0, now - last_end) if last_end else 0.0
        gaps = [max(0.0, float(b.get("started_at", 0)) - float(a.get("ended_at", 0)))
                for a, b in zip(sessions, sessions[1:]) if a.get("ended_at")]
        expected = statistics.median(gaps[-20:]) if len(gaps) >= 5 else 0.0
        return {"inactive_duration": gap, "expected_inactive_duration": expected,
                "gap_ratio": (gap / expected) if expected > 0 else None,
                "cadence_confidence": "high" if len(sessions) >= 15 else "low" if len(sessions) < 5 else "medium",
                "is_unusual_gap": bool(expected and gap > expected * 2.5)}

    def snapshot(self) -> dict[str, Any]:
        now = _utc_now()
        first = float(self._data.get("first_successful_boot_at") or now)
        sessions = self._completed_sessions()
        durations = [float(item.get("duration_seconds") or 0.0) for item in sessions]
        return {"calendar_age_seconds": max(0.0, now - first), "current_session_uptime": time.monotonic() - self._boot_monotonic,
                "total_uptime_seconds": float(self._data.get("total_uptime_seconds") or 0.0),
                "total_inactive_seconds": float(self._data.get("total_inactive_seconds") or 0.0),
                "session_count": len(sessions) + 1, "active_calendar_days": len(self._data.get("active_calendar_days") or []),
                "median_session_duration": statistics.median(durations) if durations else 0.0,
                "return_context": self.return_context(), "affect": asdict(self._affect())}

    def _affect(self) -> AffectState:
        raw = self._data.get("affect") or {}
        allowed = {key: raw.get(key) for key in AffectState.__dataclass_fields__ if key in raw}
        return AffectState(**allowed)

    def _apply_offline_decay(self, seconds: float) -> None:
        state = self._affect()
        half_life = max(60.0, float(self._cfg.get("affect.mood_decay_half_life_hours", 12)) * 3600)
        factor = .5 ** (max(0.0, seconds) / half_life)
        for key in ("joy", "warmth", "concern", "sadness", "frustration", "surprise"):
            setattr(state, key, getattr(state, key) * factor)
        # Absence can add a small, capped longing, never a fabricated grievance.
        state.loneliness = min(float(self._cfg.get("affect.absence_max_loneliness", .65)), state.loneliness * factor + min(.08, seconds / 86400 * .02))
        state.mood, state.last_updated_at = self._mood_for(state), _utc_now()
        self._data["affect"] = asdict(state)

    def _mood_for(self, state: AffectState) -> str:
        if state.frustration + state.concern > .50: return "tense"
        if state.sadness + state.loneliness > .55: return "low"
        if state.joy > .35: return "bright"
        if state.warmth > .28: return "warm"
        return "neutral"

    def _active_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, self._zone).date().isoformat()
        days = list(self._data.get("active_calendar_days") or [])
        if day not in days: days.append(day)
        self._data["active_calendar_days"] = days[-366:]

    def _completed_sessions(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._data.get("sessions") or [] if isinstance(item, dict) and item.get("ended_at")]

    def _update_current_session(self) -> None:
        sessions = self._data.get("sessions") or []
        if sessions: sessions[-1] = asdict(self._session)

    @staticmethod
    def _human_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 90: return "たった今"
        if seconds < 3600: return f"約{round(seconds / 60)}分"
        if seconds < 86400: return f"約{round(seconds / 3600)}時間"
        return f"約{round(seconds / 86400)}日"
