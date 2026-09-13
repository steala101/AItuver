"""Append-only activity ledger and canonical-state guard.

This intentionally contains no model call and executes no protocol supplied code.  The
model may choose an action, but only a checked proposal can become an event or be
spoken as a game fact.  The first protocol is Japanese word-chain (shiritori); the
ledger/snapshot/proposal interfaces are deliberately activity-neutral.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import json
import logging
import re
from typing import Any
from uuid import uuid4

from neuro_voice.activity.japanese_reading import (
    JapaneseReadingService,
    first_kana,
    last_kana,
    normalize_kana as _normalise_kana,
)

logger = logging.getLogger(__name__)


class ValidationResult(str, Enum):
    VALID = "VALID"
    INVALID_RULE = "INVALID_RULE"
    INVALID = "INVALID_RULE"  # compatibility alias
    WRONG_TURN = "WRONG_TURN"
    UNCLEAR_AUDIO = "UNCLEAR_AUDIO"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    UNCERTAIN = "UNCLEAR_AUDIO"  # compatibility alias
    STALE_INPUT = "STALE_INPUT"
    STALE = "STALE_INPUT"  # compatibility alias
    STATE_CONFLICT = "STATE_CONFLICT"
    CONFLICTED = "STATE_CONFLICT"  # compatibility alias
    SYSTEM_ERROR = "SYSTEM_ERROR"


class ActivityInputIntent(str, Enum):
    GAME_MOVE = "GAME_MOVE"
    CORRECTION = "CORRECTION"
    META_QUESTION = "META_QUESTION"
    RULE_QUESTION = "RULE_QUESTION"
    ACTIVITY_CONTROL = "ACTIVITY_CONTROL"
    NORMAL_CONVERSATION = "NORMAL_CONVERSATION"
    UNCERTAIN = "UNCERTAIN"


class FactType(str, Enum):
    CANONICAL_FACT = "CANONICAL_FACT"
    DERIVED_FACT = "DERIVED_FACT"
    RULE = "RULE"
    INTERPRETATION = "INTERPRETATION"
    HYPOTHESIS = "HYPOTHESIS"
    PREFERENCE = "PREFERENCE"
    EMOTION = "EMOTION"
    MEMORY_REFLECTION = "MEMORY_REFLECTION"


@dataclass(slots=True)
class FactRecord:
    fact_id: str
    activity_session_id: str
    fact_type: str
    key: str
    value: Any
    confidence: float
    provenance: str
    source_event_ids: list[str]
    created_at: str
    privacy_scope: str = "conversation"
    immutable: bool = True


@dataclass(slots=True)
class ActivityEvent:
    event_id: str
    activity_session_id: str
    event_type: str
    actor_id: str
    utterance_id: str | None
    occurred_at: str
    payload: dict[str, Any]
    provenance: str = "CODE_DERIVED"
    confidence: float = 1.0
    privacy_scope: str = "conversation"
    supersedes_event_id: str | None = None


@dataclass(slots=True)
class ActionProposal:
    proposal_id: str
    activity_session_id: str
    based_on_state_version: int
    actor_id: str
    action_type: str
    proposed_value: dict[str, Any]
    spoken_reaction: str = ""
    generation_id: str | None = None


@dataclass(slots=True)
class CanonicalWord:
    """Display form and a single comparison key for every participant's word."""
    raw_text: str
    spoken_form: str
    normalized_kana: str
    comparison_key: str
    first_kana: str
    last_kana: str
    source_actor_id: str
    source_utterance_id: str | None = None
    confidence: float = 1.0


@dataclass(slots=True)
class ActivityOutcome:
    handled: bool
    reply: str | None = None
    result: ValidationResult | None = None
    reason: str | None = None
    state_version: int | None = None
    input_intent: str = ActivityInputIntent.NORMAL_CONVERSATION.value
    requires_assistant_move: bool = False


_START_RE = re.compile(r"(?:\u3057\u308a\u3068\u308a|\u5c3b\u53d6\u308a).*(?:\u3057\u3088\u3046|\u3084\u308d\u3046|\u3057\u3088|\u958b\u59cb|\u59cb\u3081)|(?:\u3057\u308a\u3068\u308a|\u5c3b\u53d6\u308a)$")
_STOP_RE = re.compile(r"(?:\u3057\u308a\u3068\u308a|\u5c3b\u53d6\u308a).*(?:\u3084\u3081|\u7d42\u308f)|(?:\u3082\u3046)?(?:\u3084\u3081\u3088\u3046|\u7d42\u308f\u308a\u306b\u3057\u3088\u3046)")
_KANA_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fc]{2,24}")
_META_MARKERS = ("\u9055\u3046", "\u3061\u304c\u3046", "\u3058\u3083\u306a\u3044", "\u3058\u3083\u306a\u304f\u3066", "\u3067\u306f\u306a\u304f\u3066", "\u30eb\u30fc\u30eb", "\u9593\u9055", "\u5f85\u3063\u3066", "\u3055\u3063\u304d", "\u306a\u3093\u3067", "\u3082\u3046\u4e00\u56de")
_CONTROL_MARKERS = ("\u7d42\u308f\u308a", "\u7d42\u4e86", "\u3084\u3081\u3088\u3046", "\u4e2d\u65ad", "\u6b62\u3081\u3088\u3046", "\u30ea\u30bb\u30c3\u30c8", "\u3084\u308a\u76f4\u3057", "\u518d\u958b", "\u7d9a\u3051\u3088\u3046")
_CORRECTION_MARKERS = ("\u9055\u3046", "\u3061\u304c\u3046", "\u8a00\u3063\u305f", "\u3058\u3083\u306a\u3044", "\u3058\u3083\u306a\u304f\u3066", "\u3067\u306f\u306a\u304f\u3066", "\u9593\u9055", "\u6b63\u3057\u304f\u306f", "\u3055\u3063\u304d", "\u4ffa\u306e\u756a", "\u79c1\u306e\u756a", "\u4e8c\u56de\u76ee", "2\u56de\u76ee", "\u3082\u3046\u8a00\u3063\u305f", "\u305d\u308c\u4f7f\u3063\u305f", "\u91cd\u8907")
_META_QUESTION_MARKERS = ("\u8003\u3048\u76f4\u3057\u305f", "\u3069\u3046\u306a\u3063\u3066\u308b", "\u8ab0\u306e\u756a", "\u805e\u3053\u3048\u305f", "\u78ba\u8a8d\u3057\u305f", "\u5206\u304b\u3063\u305f")
_RULE_QUESTION_MARKERS = ("\u6fc1\u70b9", "\u56fa\u6709\u540d\u8a5e", "\u30eb\u30fc\u30eb", "\u30fc\u306f\u3069\u3046")
_CONFIRM_RE = re.compile(r"^(?:うん|うんうん|はい|そう|そうだよ|合ってる|あってる|それで合ってる|正しい|そういうこと)[。!！\s]*$")
_REJECT_RE = re.compile(r"^(?:いや|違う|ちがう|違います|そうじゃない|合ってない|あってない)[。!！\s]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivityStateManager:
    """One active activity, persisted as append-only events plus a snapshot.

    The JSON snapshot is a cache only. :meth:`rebuild` deterministically derives it
    from accepted events, making correction and corrupted-cache recovery safe.
    """

    protocol_id = "word_chain_ja_v1"

    def __init__(self, path: str | Path, cfg):
        self._path = Path(path)
        self._enabled = bool(cfg.get("fact_grounding.enabled", True))
        self._input_router_enabled = bool(cfg.get("fact_grounding.input_router_enabled", True))
        self._max_identical_guards = max(1, int(cfg.get("fact_grounding.max_identical_guard_responses", 1)))
        self._max_ai_reproposals = max(0, int(cfg.get("fact_grounding.ai_move_max_reproposals", 1)))
        self._max_events = max(20, int(cfg.get("fact_grounding.max_state_history", 200)))
        self._data: dict[str, Any] = {"version": 1, "events": [], "facts": [], "snapshot": None}
        self._guard_counts: dict[tuple[str, int, str], int] = {}
        self._data.setdefault("turn_attempts", {})
        configured_readings = cfg.get("fact_grounding.japanese_readings", {})
        self._reading = JapaneseReadingService(
            configured_readings if isinstance(configured_readings, dict) else None,
        )
        self._load()
        self.rebuild(save=False)

    @property
    def active(self) -> bool:
        return bool(self._enabled and self._snapshot and self._snapshot.get("status") in {
            "ACTIVE", "WAITING_FOR_FIRST_MOVE", "WAITING_FOR_HUMAN",
            "WAITING_FOR_AI", "REPAIRING", "NEEDS_REPAIR",
        })

    @property
    def _snapshot(self) -> dict[str, Any] | None:
        raw = self._data.get("snapshot")
        return raw if isinstance(raw, dict) else None

    def _load(self) -> None:
        try:
            if self._path.exists():
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(loaded)
        except Exception:
            logger.exception("Activity ledger load failed; preserving a new empty ledger")

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Activity ledger save failed")

    def _append(self, event_type: str, actor_id: str, payload: dict[str, Any], *, utterance_id: str | None = None, provenance: str = "CODE_DERIVED", confidence: float = 1.0, supersedes: str | None = None) -> ActivityEvent:
        snapshot = self._snapshot or {}
        event = ActivityEvent(uuid4().hex, str(snapshot.get("activity_session_id") or uuid4().hex), event_type, actor_id, utterance_id, _now(), payload, provenance, confidence, supersedes_event_id=supersedes)
        events = self._data.setdefault("events", [])
        events.append(asdict(event))
        if len(events) > self._max_events:
            # A compacted ledger never drops events of an active session.
            self._data["events"] = events[-self._max_events:]
        return event

    def _record_fact(self, kind: FactType, key: str, value: Any, event: ActivityEvent, *, provenance: str = "CODE_DERIVED") -> None:
        if kind not in {FactType.CANONICAL_FACT, FactType.DERIVED_FACT, FactType.RULE}:
            return
        fact = FactRecord(uuid4().hex, event.activity_session_id, kind.value, key, value, 1.0, provenance, [event.event_id], _now())
        facts = self._data.setdefault("facts", [])
        facts.append(asdict(fact))
        if len(facts) > self._max_events * 2:
            self._data["facts"] = facts[-self._max_events * 2:]

    def start_shiritori(self, actor_id: str, *, utterance_id: str | None = None) -> ActivityOutcome:
        if self.active:
            return ActivityOutcome(True, "今の遊びを終えてから始めよう。", ValidationResult.CONFLICTED, "active_session")
        session_id = uuid4().hex
        self._data["snapshot"] = {
            "activity_session_id": session_id, "activity_type": "word_chain", "activity_name": "日本語しりとり",
            "protocol_id": self.protocol_id, "status": "WAITING_FOR_FIRST_MOVE", "participants": [actor_id, "assistant"],
            # Activity identity is transport/user ID (not a display name or voiceprint).
            "current_actor_id": actor_id, "turn_number": 1,
            "rules": {"no_terminal_n": True, "no_duplicates": True, "allow_proper_nouns": False},
            "public_state": {"previous_word": None, "required_kana": None, "used_words": []},
            "state_version": 0, "integrity_status": "VALID", "last_event_id": None, "updated_at": _now(),
        }
        event = self._append("ACTIVITY_STARTED", actor_id, {"protocol_id": self.protocol_id, "rules": self._snapshot["rules"]}, utterance_id=utterance_id, provenance="STT_FINAL")
        self._record_fact(FactType.RULE, "word_chain_rules", self._snapshot["rules"], event)
        self.rebuild()
        return ActivityOutcome(True, "いいね、しりとりしよう。先に言葉をどうぞ。", ValidationResult.VALID,
                               state_version=self._snapshot["state_version"], input_intent=ActivityInputIntent.ACTIVITY_CONTROL.value)

    def handle_final_input(self, text: str, actor_id: str, *, source: str, utterance_id: str | None = None, is_final: bool = True) -> ActivityOutcome:
        """Accept only final, speaker-resolved input. Partial input never reaches ledger."""
        if not self._enabled or not is_final:
            return ActivityOutcome(False)
        text = str(text or "").strip()
        if not text:
            return ActivityOutcome(False)
        if not self.active:
            if _START_RE.search(text):
                return self.start_shiritori(actor_id, utterance_id=utterance_id)
            return ActivityOutcome(False)
        if self._snapshot.get("protocol_id") != self.protocol_id:
            return ActivityOutcome(False)
        if self._snapshot.get("integrity_status") != "VALID":
            intent = (
                self._route_input(text)
                if self._input_router_enabled
                else ActivityInputIntent.META_QUESTION
            )
            self._append(
                "INPUT_OBSERVED", actor_id,
                {"text": text, "intent": intent.value},
                utterance_id=utterance_id, provenance="STT_FINAL",
            )
            if intent is ActivityInputIntent.ACTIVITY_CONTROL:
                return self._handle_control(text, actor_id, utterance_id)
            if intent is ActivityInputIntent.CORRECTION:
                return self._repair_or_defer(text, actor_id, utterance_id)
            return ActivityOutcome(
                True,
                "遊びの状態に矛盾が見つかったので、次の手は確定できないよ。"
                "訂正する内容を一語で教えるか、いったん終了してやり直してね。",
                ValidationResult.STATE_CONFLICT,
                "activity_integrity_requires_repair",
                int(self._snapshot.get("state_version") or 0),
                intent.value,
            )
        pending = self._latest_pending_move(actor_id)
        if pending is not None and _CONFIRM_RE.fullmatch(text):
            pending_text = str((pending.get("payload") or {}).get("text") or "")
            self._append(
                "PENDING_MOVE_CONFIRMED", actor_id,
                {"pending_event_id": pending.get("event_id"), "text": pending_text},
                utterance_id=utterance_id, provenance="USER_CONFIRMED",
            )
            return self._submit_word(
                pending_text, actor_id, utterance_id,
                intent=ActivityInputIntent.GAME_MOVE,
                observed_event_id=str(pending.get("event_id") or ""),
            )
        if pending is not None and _REJECT_RE.fullmatch(text):
            self._resolve_pending_move(pending, actor_id, utterance_id, "user_rejected")
            return ActivityOutcome(
                True, "わかった。今の候補は使わないね。出したかった言葉を一語で教えて。",
                ValidationResult.UNCLEAR_AUDIO, "pending_move_rejected",
                self._snapshot["state_version"], ActivityInputIntent.CORRECTION.value,
            )
        intent = self._route_input(text) if self._input_router_enabled else ActivityInputIntent.GAME_MOVE
        self._append("INPUT_OBSERVED", actor_id, {"text": text, "intent": intent.value}, utterance_id=utterance_id, provenance="STT_FINAL")
        if intent is ActivityInputIntent.ACTIVITY_CONTROL:
            return self._handle_control(text, actor_id, utterance_id)
        if intent is ActivityInputIntent.CORRECTION:
            return self._repair_or_defer(text, actor_id, utterance_id)
        if intent is ActivityInputIntent.META_QUESTION:
            return self._meta_question_outcome(intent)
        if intent is ActivityInputIntent.RULE_QUESTION:
            return ActivityOutcome(True, "今は、同じ言葉はなしで、最後のかなからつなぐルールだよ。「ん」で終わったら負け。",
                                   ValidationResult.VALID, "rule_question", self._snapshot["state_version"], intent.value)
        if intent is ActivityInputIntent.NORMAL_CONVERSATION:
            return ActivityOutcome(False, result=ValidationResult.VALID, state_version=self._snapshot["state_version"], input_intent=intent.value)
        if intent is ActivityInputIntent.UNCERTAIN:
            self._append(
                "PENDING_MOVE_CREATED", actor_id, {"text": text},
                utterance_id=utterance_id, provenance="STT_FINAL", confidence=.55,
            )
            return ActivityOutcome(True, f"「{text}」をしりとりの一手として出した、で合ってる？", ValidationResult.UNCLEAR_AUDIO,
                                   "uncertain_input", self._snapshot["state_version"], intent.value)
        return self._submit_word(text, actor_id, utterance_id, intent=intent)

    def _route_input(self, text: str) -> ActivityInputIntent:
        compact = re.sub(r"[\s\u3002\u3001!?！？]", "", text)
        if _STOP_RE.search(text) or any(marker in compact for marker in _CONTROL_MARKERS):
            return ActivityInputIntent.ACTIVITY_CONTROL
        if any(marker in compact for marker in _RULE_QUESTION_MARKERS):
            return ActivityInputIntent.RULE_QUESTION
        if any(marker in compact for marker in _META_QUESTION_MARKERS):
            return ActivityInputIntent.META_QUESTION
        if any(marker in compact for marker in _CORRECTION_MARKERS):
            return ActivityInputIntent.CORRECTION
        _, word = self._extract_word(text)
        if word and len(compact) <= 16:
            return ActivityInputIntent.GAME_MOVE
        if len(compact) > 16:
            return ActivityInputIntent.NORMAL_CONVERSATION
        return ActivityInputIntent.UNCERTAIN

    def _handle_control(self, text: str, actor_id: str, utterance_id: str | None) -> ActivityOutcome:
        compact = re.sub(r"\s", "", text)
        if any(marker in compact for marker in ("\u30ea\u30bb\u30c3\u30c8", "\u3084\u308a\u76f4\u3057", "\u6700\u521d\u304b\u3089")):
            self._append("ACTIVITY_ENDED", actor_id, {"reason": "restart"}, utterance_id=utterance_id, provenance="STT_FINAL")
            self.rebuild()
            return self.start_shiritori(actor_id, utterance_id=utterance_id)
        self._append("ACTIVITY_ENDED", actor_id, {"reason": "user_requested"}, utterance_id=utterance_id, provenance="STT_FINAL")
        self.rebuild()
        return ActivityOutcome(True, "わかった。しりとりはいったん終わりにしよう。", ValidationResult.VALID,
                               state_version=self._snapshot["state_version"], input_intent=ActivityInputIntent.ACTIVITY_CONTROL.value)

    def _meta_question_outcome(self, intent: ActivityInputIntent) -> ActivityOutcome:
        state = self._snapshot or {}
        public = state.get("public_state") or {}
        current = state.get("current_actor_id")
        if current == "assistant":
            # The pending assistant turn is retried in this very response; no fake
            # background promise and no user-turn guard are emitted.
            return ActivityOutcome(False, result=ValidationResult.VALID, reason="assistant_turn_retry",
                                   state_version=state.get("state_version"), input_intent=intent.value,
                                   requires_assistant_move=True)
        required = public.get("required_kana")
        reply = f"今はあなたの番だよ。" + (f"「{required}」からお願い。" if required else "最初の言葉をどうぞ。")
        return ActivityOutcome(True, reply, ValidationResult.VALID, "state_answer", state.get("state_version"), intent.value)

    def _repair_or_defer(self, text: str, actor_id: str, utterance_id: str | None) -> ActivityOutcome:
        # Do not invent a replacement state. Record the request, rebuild the ledger,
        # and ask for the concrete word if it cannot be unambiguously repaired.
        event = self._append("CORRECTION_REQUESTED", actor_id, {"text": text}, utterance_id=utterance_id, provenance="STT_FINAL")
        pending_move = self._latest_pending_move(actor_id)
        corrected_surface, corrected_reading = self._correction_candidate(text)
        if pending_move is not None:
            if corrected_reading:
                self._resolve_pending_move(
                    pending_move, actor_id, utterance_id, "replaced_by_correction",
                    replacement=corrected_surface,
                )
                result = self._submit_word(
                    corrected_surface, actor_id, utterance_id,
                    intent=ActivityInputIntent.GAME_MOVE,
                    observed_event_id=str(pending_move.get("event_id") or ""),
                )
                if result.result is ValidationResult.VALID:
                    result.reason = "pending_move_corrected"
                return result
            self._resolve_pending_move(
                pending_move, actor_id, utterance_id, "correction_without_replacement",
            )
            return ActivityOutcome(
                True, "了解。直前の候補は取り消したよ。正しい言葉を一語で教えて。",
                ValidationResult.UNCLEAR_AUDIO, "correction_requires_word",
                self._snapshot["state_version"], ActivityInputIntent.CORRECTION.value,
            )
        correction_parts = re.split(r"(?:\u3058\u3083\u306a\u304f\u3066|\u3067\u306f\u306a\u304f\u3066|\u3058\u3083\u306a\u3044\u3001|\u3067\u306f\u306a\u3044\u3001)", text, maxsplit=1)
        words = []
        if len(correction_parts) == 2:
            for part in correction_parts:
                matches = _KANA_RE.findall(part)
                if matches:
                    words.append(_normalise_kana(matches[-1]))
        latest = next(
            (item for item in reversed(self._data.get("events", []))
             if item.get("event_type") in {"USER_INPUT_ACCEPTED", "ACTION_COMMITTED", "AI_MOVE_COMMITTED"}),
            None,
        )
        duplicate_correction = any(marker in text for marker in ("\u4e8c\u56de\u76ee", "2\u56de\u76ee", "\u3082\u3046\u8a00\u3063\u305f", "\u305d\u308c\u4f7f\u3063\u305f", "\u91cd\u8907"))
        if latest is not None and latest.get("event_type") in {"AI_MOVE_COMMITTED", "ACTION_COMMITTED"} and duplicate_correction:
            payload = latest.get("payload") or {}
            key = str(payload.get("word_key") or _normalise_kana(str(payload.get("word") or "")))
            if key and self._word_used_before_event(key, str(latest.get("event_id") or "")):
                self._append("AI_MOVE_INVALIDATED", actor_id, {
                    "invalidated_event_id": latest.get("event_id"), "reason": "word_already_used",
                    "correction_utterance_id": utterance_id, "candidate_key": key,
                }, utterance_id=utterance_id, provenance="USER_CONFIRMED", supersedes=str(latest.get("event_id") or ""))
                self.rebuild()
                repaired = self._snapshot or {}
                attempt = self._attempt_state(repaired, "assistant")
                if key not in attempt["rejected_candidate_keys"]:
                    attempt["rejected_candidate_keys"].append(key)
                    attempt["rejection_reasons"].append("word_already_used")
                self.save()
                return ActivityOutcome(False, result=ValidationResult.VALID, reason="erroneous_commit_rolled_back",
                                       state_version=repaired.get("state_version"), input_intent=ActivityInputIntent.CORRECTION.value,
                                       requires_assistant_move=True)
        # A common recovery path: STT final was already observed but the initial
        # validation/output path failed. Re-run that exact final input now, before
        # saying that we checked it. This avoids a deadlocked "think again" loop.
        pending = next(
            (item for item in reversed(self._data.get("events", []))
             if item.get("event_type") == "INPUT_OBSERVED"
             and item.get("actor_id") == actor_id
             and (item.get("payload") or {}).get("intent") == ActivityInputIntent.GAME_MOVE.value
             and str((item.get("payload") or {}).get("text") or "").strip()),
            None,
        )
        if pending is not None and (self._snapshot or {}).get("current_actor_id") == actor_id:
            pending_text = str((pending.get("payload") or {}).get("text") or "")
            retry = self._submit_word(pending_text, actor_id, pending.get("utterance_id"),
                                      intent=ActivityInputIntent.GAME_MOVE, observed_event_id=str(pending.get("event_id") or ""))
            if retry.result is ValidationResult.VALID and not retry.handled:
                retry.reason = "revalidated_pending_final"
                return retry
        # A correction is safe to apply only when it names both the current final
        # word and its replacement. Earlier-turn corrections could invalidate many
        # later moves, so they intentionally stay in confirmation mode for now.
        if latest is not None and len(words) >= 2:
            old = str((latest.get("payload") or {}).get("word") or "")
            replacement = words[-1]
            if words[0] == old and replacement != old and latest.get("event_id"):
                self._append(
                    "CORRECTION_APPLIED", actor_id,
                    {"word": replacement, "surface": replacement}, utterance_id=utterance_id,
                    provenance="USER_CONFIRMED", supersedes=str(latest["event_id"]),
                )
                self.rebuild()
                current = self._snapshot or {}
                required = (current.get("public_state") or {}).get("required_kana")
                return ActivityOutcome(
                    True,
                    f"確認できたよ。最後の言葉を「{replacement}」へ訂正した。"
                    + (f"次は「{required}」から。" if required else ""),
                    ValidationResult.VALID, "event_rebuilt", current.get("state_version"), ActivityInputIntent.CORRECTION.value,
                    current.get("current_actor_id") == "assistant",
                )
        if latest is not None and (self._snapshot or {}).get("current_actor_id") == "assistant":
            # State is already correct; answer the correction and actually retry
            # Poppo's move in the same response generation.
            return ActivityOutcome(False, result=ValidationResult.VALID, reason="state_already_correct",
                                   state_version=self._snapshot["state_version"], input_intent=ActivityInputIntent.CORRECTION.value,
                                   requires_assistant_move=True)
        self.rebuild()
        return ActivityOutcome(True, "直前の入力を状態から確認したけれど、訂正する言葉を一つに決められなかった。訂正したい言葉を一語で教えて。",
                               ValidationResult.UNCLEAR_AUDIO, "correction_requires_word", self._snapshot["state_version"], ActivityInputIntent.CORRECTION.value)

    def _word_used_before_event(self, key: str, event_id: str) -> bool:
        for event in self._data.get("events", []):
            if str(event.get("event_id") or "") == event_id:
                break
            if event.get("event_type") not in {"USER_INPUT_ACCEPTED", "ACTION_COMMITTED", "AI_MOVE_COMMITTED"}:
                continue
            payload = event.get("payload") or {}
            other = str(payload.get("word_key") or _normalise_kana(str(payload.get("word") or "")))
            if other == key:
                return True
        return False

    def _extract_word(self, text: str) -> tuple[str, str]:
        return self._reading.extract_word(text)

    def _correction_candidate(self, text: str) -> tuple[str, str]:
        """Extract only the replacement side of a natural repair utterance."""
        parts = re.split(
            r"(?:じゃなくて|ではなくて|ではなく|正しくは|本当は|"
            r"(?:いや[、,\s]*)?(?:全然)?(?:違う|ちがう)[。!！、,\s]*)",
            str(text or ""),
        )
        for part in reversed(parts):
            surface, reading = self._reading.extract_word(part)
            if reading:
                return surface, reading
        return "", ""

    def _latest_pending_move(self, actor_id: str) -> dict[str, Any] | None:
        snapshot = self._snapshot or {}
        session_id = str(snapshot.get("activity_session_id") or "")
        resolved = {
            str((item.get("payload") or {}).get("pending_event_id") or "")
            for item in self._data.get("events", [])
            if item.get("event_type") in {
                "PENDING_MOVE_RESOLVED", "PENDING_MOVE_CONFIRMED",
            }
        }
        return next(
            (
                item for item in reversed(self._data.get("events", []))
                if item.get("event_type") == "PENDING_MOVE_CREATED"
                and item.get("actor_id") == actor_id
                and item.get("activity_session_id") == session_id
                and str(item.get("event_id") or "") not in resolved
            ),
            None,
        )

    def _resolve_pending_move(
        self, pending: dict[str, Any], actor_id: str, utterance_id: str | None,
        reason: str, *, replacement: str | None = None,
    ) -> None:
        self._append(
            "PENDING_MOVE_RESOLVED", actor_id,
            {
                "pending_event_id": pending.get("event_id"),
                "reason": reason,
                "replacement": replacement,
            },
            utterance_id=utterance_id, provenance="USER_CONFIRMED",
        )

    def _canonical_word(self, raw: str, *, actor_id: str, utterance_id: str | None = None) -> CanonicalWord | None:
        surface, normalized = self._extract_word(raw)
        key = _normalise_kana(normalized).strip("\u3002!?！？\u3001, \t\r\n")
        if not key:
            return None
        return CanonicalWord(
            raw_text=str(raw), spoken_form=surface or key, normalized_kana=key,
            comparison_key=key, first_kana=first_kana(key), last_kana=last_kana(key),
            source_actor_id=actor_id, source_utterance_id=utterance_id,
        )

    @staticmethod
    def _used_keys(state: dict[str, Any]) -> set[str]:
        public = state.get("public_state") or {}
        return {str(item) for item in (public.get("used_word_keys") or public.get("used_words") or [])}

    def _attempt_key(self, state: dict[str, Any], actor_id: str) -> str:
        return ":".join((str(state.get("activity_session_id") or ""), str(state.get("state_version") or 0), actor_id))

    def _attempt_state(self, state: dict[str, Any], actor_id: str) -> dict[str, Any]:
        key = self._attempt_key(state, actor_id)
        attempts = self._data.setdefault("turn_attempts", {})
        value = attempts.setdefault(key, {
            "activity_session_id": state.get("activity_session_id"), "state_version": state.get("state_version"),
            "actor_id": actor_id, "required_kana": (state.get("public_state") or {}).get("required_kana"),
            "attempt_count": 0, "rejected_candidate_keys": [], "rejection_reasons": [],
        })
        return value

    def retry_context(self) -> str | None:
        """Canonical exclusions for a single re-proposal; no model memory required."""
        state = self._snapshot
        if not state or state.get("current_actor_id") != "assistant":
            return None
        attempt = self._attempt_state(state, "assistant")
        rejected = ", ".join(attempt.get("rejected_candidate_keys") or []) or "none"
        used = ", ".join(sorted(self._used_keys(state))) or "none"
        return (
            "[ACTIVITY RETRY — immutable]\n"
            f"required_kana={(state.get('public_state') or {}).get('required_kana') or 'none'}\n"
            f"used_word_keys={used}\nrejected_this_turn={rejected}\n"
            "Do not reuse any rejected or used word. Output exactly one new quoted hiragana word."
        )

    def _submit_word(self, text: str, actor_id: str, utterance_id: str | None, *,
                     intent: ActivityInputIntent = ActivityInputIntent.GAME_MOVE,
                     observed_event_id: str | None = None) -> ActivityOutcome:
        state = self._snapshot
        if not state or state.get("integrity_status") != "VALID":
            self.rebuild()
            return ActivityOutcome(True, "今の手番情報が食い違っていたので、確定イベントから再確認したよ。続ける前にもう一度一語で教えて。",
                                   ValidationResult.STATE_CONFLICT, "integrity_rebuilt", state.get("state_version"), intent.value)
        if state.get("current_actor_id") != actor_id:
            return self._guard_response(state, "wrong_turn", intent)
        canonical = self._canonical_word(text, actor_id=actor_id, utterance_id=utterance_id)
        if canonical is None:
            return ActivityOutcome(True, "聞き取った言葉の読みを確定できなかった。ひらがなで一語だけ教えて。",
                                   ValidationResult.NORMALIZATION_FAILED, "reading_unknown", state["state_version"], intent.value)
        public = state["public_state"]
        required = public.get("required_kana")
        if required and canonical.first_kana != required:
            return ActivityOutcome(True, f"今は「{required}」からだから、その言葉ではつながらないね。", ValidationResult.INVALID_RULE,
                                   "wrong_initial", state["state_version"], intent.value)
        if canonical.comparison_key in self._used_keys(state):
            return ActivityOutcome(True, f"「{canonical.spoken_form}」はもう使った言葉として記録されているよ。別の言葉にしよう。", ValidationResult.INVALID_RULE,
                                   "duplicate", state["state_version"], intent.value)
        pending = self._latest_pending_move(actor_id)
        if pending is not None:
            self._resolve_pending_move(
                pending, actor_id, utterance_id, "accepted",
                replacement=canonical.spoken_form,
            )
        event = self._append("USER_INPUT_ACCEPTED", actor_id, {
            "surface": canonical.spoken_form, "word": canonical.normalized_kana,
            "word_key": canonical.comparison_key, "word_record": asdict(canonical),
            "observed_event_id": observed_event_id,
        }, utterance_id=utterance_id, provenance="STT_FINAL")
        self._record_fact(FactType.CANONICAL_FACT, "submitted_word", canonical.normalized_kana, event, provenance="STT_FINAL")
        self.rebuild()
        if canonical.last_kana == "ん":
            return ActivityOutcome(True, f"「{canonical.spoken_form}」は「ん」で終わるから、今回はここで決着だね。", ValidationResult.VALID,
                                   "terminal_n", self._snapshot["state_version"], intent.value)
        # The LLM is deliberately still responsible for its next word. It receives
        # this checked state and must submit an ActionProposal before it is spoken.
        return ActivityOutcome(False, result=ValidationResult.VALID, state_version=self._snapshot["state_version"],
                               input_intent=intent.value, requires_assistant_move=True)

    def _guard_response(self, state: dict[str, Any], reason: str, intent: ActivityInputIntent) -> ActivityOutcome:
        key = (str(state.get("activity_session_id") or ""), int(state.get("state_version") or 0), reason)
        self._guard_counts[key] = self._guard_counts.get(key, 0) + 1
        if self._guard_counts[key] > self._max_identical_guards:
            self._append("ACTIVITY_REPAIR_REQUIRED", "system", {"reason": reason}, provenance="VALIDATOR")
            self.rebuild()
            if (self._snapshot or {}).get("integrity_status") != "VALID":
                self._append("ACTIVITY_PAUSED", "system", {"reason": "guard_loop"}, provenance="VALIDATOR")
                self.save()
                return ActivityOutcome(True, "同じ手番確認が続いたから、活動を安全に止めたよ。しりとりをやり直すなら言って。",
                                       ValidationResult.STATE_CONFLICT, "guard_loop_paused", state.get("state_version"), intent.value)
            return ActivityOutcome(True, "同じ手番確認が続いたので、確定イベントから状態を再構築したよ。今は私の番なら私が答え、あなたの番なら一語を待つね。",
                                   ValidationResult.STATE_CONFLICT, "guard_loop_rebuilt", self._snapshot.get("state_version"), intent.value)
        return ActivityOutcome(True, "今は私の番だね。私が答えてから続けよう。", ValidationResult.WRONG_TURN,
                               reason, state.get("state_version"), intent.value)

    def validate_proposal(self, proposal: ActionProposal) -> tuple[ValidationResult, str | None]:
        state = self._snapshot
        if not state or state.get("integrity_status") != "VALID":
            return ValidationResult.STATE_CONFLICT, "state_not_valid"
        if proposal.activity_session_id != state.get("activity_session_id") or proposal.based_on_state_version != state.get("state_version"):
            return ValidationResult.STALE_INPUT, "state_version_changed"
        if proposal.action_type != "SUBMIT_WORD" or proposal.actor_id != state.get("current_actor_id"):
            return ValidationResult.WRONG_TURN, "unsupported_or_wrong_actor"
        surface = str(proposal.proposed_value.get("word") or "")
        canonical = self._canonical_word(surface, actor_id=proposal.actor_id)
        if canonical is None:
            return ValidationResult.NORMALIZATION_FAILED, "normalization_failed"
        public = state["public_state"]
        if public.get("required_kana") and canonical.first_kana != public["required_kana"]:
            return ValidationResult.INVALID_RULE, "wrong_initial"
        if canonical.comparison_key in self._used_keys(state):
            return ValidationResult.INVALID_RULE, "word_already_used"
        attempt = self._attempt_state(state, proposal.actor_id)
        if canonical.comparison_key in set(attempt.get("rejected_candidate_keys") or []):
            return ValidationResult.INVALID_RULE, "repeated_rejected_candidate"
        if canonical.last_kana == "ん":
            return ValidationResult.INVALID_RULE, "terminal_n"
        return ValidationResult.VALID, None

    def commit_proposal(self, proposal: ActionProposal) -> ActivityOutcome:
        result, reason = self.validate_proposal(proposal)
        if result is not ValidationResult.VALID:
            self._reject_proposal(proposal, result, reason or "invalid")
            self.save()
            return ActivityOutcome(True, None, result, reason, self._snapshot.get("state_version") if self._snapshot else None)
        canonical = self._canonical_word(str(proposal.proposed_value["word"]), actor_id=proposal.actor_id)
        if canonical is None:  # defensive: validation and commit must be atomic
            self._reject_proposal(proposal, ValidationResult.NORMALIZATION_FAILED, "normalization_failed")
            return ActivityOutcome(True, None, ValidationResult.NORMALIZATION_FAILED, "normalization_failed", self._snapshot.get("state_version") if self._snapshot else None)
        before = self.snapshot()
        events_before = list(self._data.get("events", []))
        facts_before = list(self._data.get("facts", []))
        attempts_before = json.loads(json.dumps(self._data.get("turn_attempts", {})))
        try:
            event = self._append("AI_MOVE_COMMITTED", proposal.actor_id, {
                "proposal_id": proposal.proposal_id, "surface": canonical.spoken_form,
                "word": canonical.normalized_kana, "word_key": canonical.comparison_key,
                "word_record": asdict(canonical), "generation_id": proposal.generation_id,
                "required_before": (before.get("public_state") or {}).get("required_kana"),
                "state_version_before": before.get("state_version"),
            }, provenance="VALIDATOR")
            self._record_fact(FactType.CANONICAL_FACT, "submitted_word", canonical.normalized_kana, event, provenance="VALIDATOR")
            self.rebuild()
            event_payload = next((item.get("payload") for item in reversed(self._data.get("events", [])) if item.get("event_id") == event.event_id), None)
            if isinstance(event_payload, dict):
                event_payload["state_version_after"] = self._snapshot.get("state_version")
        except Exception:
            # No partial state is allowed. Restore the in-memory ledger/cache and
            # turn this candidate into a rejected transaction.
            self._data["snapshot"] = before
            self._data["events"] = events_before
            self._data["facts"] = facts_before
            self._data["turn_attempts"] = attempts_before
            self._reject_proposal(proposal, ValidationResult.SYSTEM_ERROR, "atomic_commit_failed")
            self.save()
            return ActivityOutcome(True, None, ValidationResult.SYSTEM_ERROR, "atomic_commit_failed", before.get("state_version"))
        return ActivityOutcome(True, proposal.spoken_reaction, ValidationResult.VALID, state_version=self._snapshot["state_version"])

    def _reject_proposal(self, proposal: ActionProposal, result: ValidationResult, reason: str) -> None:
        state = self._snapshot or {}
        canonical = self._canonical_word(str(proposal.proposed_value.get("word") or ""), actor_id=proposal.actor_id)
        key = canonical.comparison_key if canonical is not None else "<unparseable>"
        attempt = self._attempt_state(state, proposal.actor_id) if state else None
        if attempt is not None:
            attempt["attempt_count"] = int(attempt.get("attempt_count", 0)) + 1
            if key not in attempt["rejected_candidate_keys"]:
                attempt["rejected_candidate_keys"].append(key)
            attempt["rejection_reasons"].append(reason)
        self._append("AI_MOVE_REJECTED", proposal.actor_id, {
            "proposal_id": proposal.proposal_id, "candidate_key": key,
            "validation_result": result.value, "reason": reason,
            "based_on_state_version": proposal.based_on_state_version,
            "generation_id": proposal.generation_id,
        }, provenance="VALIDATOR")

    def validate_and_commit_assistant_text(self, text: str, *, generation_id: str | None = None) -> tuple[ValidationResult, str | None]:
        """Validate an LLM game move before it is passed to TTS.

        This is deliberately conservative: during the assistant's turn it accepts
        one quoted Japanese word only. Ambiguous prose is blocked instead of becoming a
        fabricated move. A stale generation can therefore never revive an old turn.
        """
        state = self._snapshot
        if not self.active or not state or state.get("current_actor_id") != "assistant":
            return ValidationResult.VALID, None
        quotes = re.findall(r"[\u300c\u300e]([^\u300d\u300f]{1,24})[\u300d\u300f]", str(text or ""))
        candidate = next(
            (
                item.strip("\u3002!?！？、, ")
                for item in quotes
                if self._reading.reading(item.strip("\u3002!?！？、, "))
            ),
            "",
        )
        if not candidate:
            bare = str(text or "").strip("\u3002!?！？、, \n\t")
            if self._reading.reading(bare):
                candidate = bare
        if not candidate:
            return ValidationResult.UNCERTAIN, "assistant_word_missing"
        proposal = ActionProposal(
            proposal_id=uuid4().hex,
            activity_session_id=str(state["activity_session_id"]),
            based_on_state_version=int(state["state_version"]),
            actor_id="assistant", action_type="SUBMIT_WORD",
            proposed_value={"word": candidate}, generation_id=generation_id,
        )
        result, reason = self.validate_proposal(proposal)
        if result is not ValidationResult.VALID:
            self._reject_proposal(proposal, result, reason or "invalid")
            attempt = self._attempt_state(state, "assistant")
            if int(attempt.get("attempt_count", 0)) > self._max_ai_reproposals:
                self._append("ACTIVITY_PAUSED", "system", {
                    "reason": "max_reproposals_exceeded", "candidate_key": attempt.get("rejected_candidate_keys", [None])[-1],
                }, provenance="VALIDATOR")
                self.rebuild()
                return ValidationResult.STATE_CONFLICT, "max_reproposals_exceeded"
            self.save()
            return result, reason
        # If the model explicitly announces a next character, it must be the one
        # derived from the accepted word; otherwise block the whole output.
        expected_after = last_kana(self._reading.reading(candidate))
        announced = re.findall(r"(?:\u6b21|\u3064\u304e)[^\u300c\u300e]{0,12}[\u300c\u300e]([\u3041-\u3096\u30a1-\u30fa])[\u300d\u300f]", str(text or ""))
        if announced and any(_normalise_kana(value) != expected_after for value in announced):
            self._reject_proposal(proposal, ValidationResult.INVALID_RULE, "wrong_announced_next")
            self.save()
            return ValidationResult.INVALID_RULE, "wrong_announced_next"
        self.commit_proposal(proposal)
        return ValidationResult.VALID, None

    def rebuild(self, *, save: bool = True) -> None:
        """Rebuild state from accepted events; correction/meta events never mutate play facts."""
        events = [e for e in self._data.get("events", []) if isinstance(e, dict)]
        start = next((e for e in reversed(events) if e.get("event_type") == "ACTIVITY_STARTED"), None)
        if start is None:
            self._data["snapshot"] = None
            if save:
                self.save()
            return
        rules = dict(start.get("payload", {}).get("rules") or {"no_terminal_n": True, "no_duplicates": True})
        snapshot = {
            "activity_session_id": start["activity_session_id"], "activity_type": "word_chain", "activity_name": "日本語しりとり",
            "protocol_id": start.get("payload", {}).get("protocol_id", self.protocol_id), "status": "WAITING_FOR_FIRST_MOVE",
            "participants": [start.get("actor_id", "user"), "assistant"], "current_actor_id": start.get("actor_id", "user"),
            "turn_number": 1, "rules": rules, "public_state": {
                "previous_word": None, "required_kana": None, "used_words": [],
                "used_word_keys": [], "used_word_records": [],
            },
            "state_version": 0, "integrity_status": "VALID", "last_event_id": start["event_id"], "updated_at": _now(),
        }
        ended = False
        paused = False
        replacements = {
            str(event.get("supersedes_event_id")): dict(event.get("payload") or {})
            for event in events
            if event.get("event_type") == "CORRECTION_APPLIED" and event.get("supersedes_event_id")
        }
        invalidated_event_ids = {
            str((event.get("payload") or {}).get("invalidated_event_id"))
            for event in events if event.get("event_type") == "AI_MOVE_INVALIDATED"
        }
        correction_count = 0
        for event in events[events.index(start) + 1:]:
            if event.get("activity_session_id") != start["activity_session_id"]:
                continue
            typ, payload = event.get("event_type"), event.get("payload") or {}
            if typ == "ACTIVITY_ENDED":
                ended = True
                snapshot["last_event_id"] = event["event_id"]
                continue
            if typ == "ACTIVITY_PAUSED":
                paused = True
                snapshot["last_event_id"] = event["event_id"]
                continue
            if typ == "CORRECTION_APPLIED":
                correction_count += 1
                continue
            if typ == "AI_MOVE_INVALIDATED":
                correction_count += 1
                continue
            if typ not in {"USER_INPUT_ACCEPTED", "ACTION_COMMITTED", "AI_MOVE_COMMITTED"}:
                continue
            if str(event.get("event_id")) in invalidated_event_ids:
                continue
            if event.get("event_id") in replacements:
                payload = replacements[str(event["event_id"])]
            word = str(payload.get("word") or "")
            word_key = str(payload.get("word_key") or _normalise_kana(word))
            if not word:
                snapshot["integrity_status"] = "CONFLICTED"
                continue
            used = snapshot["public_state"]["used_words"]
            used_keys = snapshot["public_state"]["used_word_keys"]
            required = snapshot["public_state"]["required_kana"]
            if (required and first_kana(word) != required) or word_key in used_keys:
                snapshot["integrity_status"] = "CONFLICTED"
                continue
            used.append(word)
            used_keys.append(word_key)
            record = payload.get("word_record")
            if isinstance(record, dict):
                snapshot["public_state"]["used_word_records"].append(record)
            snapshot["public_state"].update(previous_word=word, required_kana=last_kana(word))
            snapshot["current_actor_id"] = "assistant" if event.get("actor_id") != "assistant" else start.get("actor_id", "user")
            snapshot["turn_number"] += 1
            snapshot["state_version"] += 1
            snapshot["last_event_id"] = event["event_id"]
            if last_kana(word) == "ん":
                ended = True
        snapshot["state_version"] += correction_count
        if snapshot["integrity_status"] != "VALID":
            snapshot["status"] = "NEEDS_REPAIR"
            snapshot["current_actor_id"] = None
        elif paused:
            snapshot["status"] = "PAUSED"
            snapshot["current_actor_id"] = None
        elif ended:
            snapshot["status"] = "COMPLETED"
            snapshot["current_actor_id"] = None
            snapshot["public_state"]["required_kana"] = None
        elif not snapshot["public_state"]["used_words"]:
            snapshot["status"] = "WAITING_FOR_FIRST_MOVE"
        else:
            snapshot["status"] = "WAITING_FOR_AI" if snapshot["current_actor_id"] == "assistant" else "WAITING_FOR_HUMAN"
        snapshot["updated_at"] = _now()
        self._data["snapshot"] = snapshot
        if save:
            self.save()

    def grounded_context(self) -> str | None:
        state = self._snapshot
        if not self.active or not state:
            return None
        public = state["public_state"]
        used = "、".join(public.get("used_word_keys") or public.get("used_words", [])[-12:]) or "まだなし"
        rejected = "、".join(self._attempt_state(state, "assistant").get("rejected_candidate_keys") or []) if state.get("current_actor_id") == "assistant" else "なし"
        return (
            "[CANONICAL ACTIVITY STATE — immutable]\n"
            f"activity={state['activity_name']}; status={state['status']}; state_version={state['state_version']}\n"
            f"current_actor={state['current_actor_id']}; required_kana={public.get('required_kana') or 'none'}; previous_word={public.get('previous_word') or 'none'}\n"
            f"used_words={used}; rejected_this_turn={rejected}\n"
            "Rules: do not change facts or rules. A game word must be proposed as exactly one quoted hiragana word. "
            "Do not claim a move, next kana, winner, or correction unless it matches this state."
        )

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._snapshot or {}, ensure_ascii=False))
