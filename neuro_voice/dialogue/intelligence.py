"""Small, deterministic control layer for natural real-time dialogue.

This module deliberately does not ask an LLM to expose chain-of-thought.  It
keeps compact observations and turns them into response constraints instead.
The state is scoped by persona by the caller-provided JSON path.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from neuro_voice.dialogue.curiosity import CuriosityEngine
from neuro_voice.dialogue.user_model import UserModel
from neuro_voice.dialogue.response_director import ResponseDirector
from neuro_voice.dialogue.conversation_planner import ConversationPlanner
from neuro_voice.dialogue.conversation_critic import ConversationCritic
from neuro_voice.dialogue.surface_realizer import SurfaceRealizer
from neuro_voice.dialogue.conversation_contract import (
    ConversationContract, enforce_reply, enforce_sentence,
    needs_grounding_buffer,
)
from neuro_voice.dialogue.adaptive_store import AdaptiveConversationStore
from neuro_voice.dialogue.expression_plan import build_expression_plan
from neuro_voice.dialogue.reaction_learning import ReactionLearner
from neuro_voice.dialogue.semantic_graph import SemanticAssociationGraph
from neuro_voice.dialogue.working_memory import (
    WorkingMemory, normalize_persona_traits,
)

logger = logging.getLogger(__name__)


_QUESTION = re.compile(r"[？?]|(?:なぜ|なんで|どうして|どこ|いつ|誰|どれ|どんな|どうやって)")
_PERSONAL = re.compile(r"(?:好き|苦手|趣味|仕事|学校|住んで|名前|誕生|家族|体調|痛|病院|眠|疲れ|プロジェクト|作って|開発)")
#: Japanese is not space-delimited, so one character class covering kanji,
#: hiragana and katakana matches an entire sentence as a single "word".  That
#: is how 「今あのマイクオンにしたまま声口元から話してたわ」 ended up stored as a
#: topic and became the seed for the same self-initiated remark every session.
#: Splitting by script keeps word-like pieces; bare hiragana is mostly grammar.
_TOPIC_WORD = re.compile(
    r"[一-龥々]{2,6}"                 # kanji compound
    r"|[ァ-ヴー]{2,10}"               # katakana word
    r"|[A-Za-z][A-Za-z0-9_-]{1,15}"   # latin word
)
#: A stored topic longer than this is a sentence, not a topic.
_TOPIC_MAX_CHARS = 12
_POSITIVE_FEEDBACK = re.compile(r"(?:面白い|それいい|いいね|好き|助かった|役立った|分かりやすい|自然になった|その感じ)")
_NEGATIVE_FEEDBACK = re.compile(r"(?:つまらない|微妙|違う|間違|機械的|単調|同じこと|質問が多い|長すぎ|短くして|噛み合わ|分かってない)")
_EXPLICIT_FEEDBACK = re.compile(r"(?:もっと|その話し方|返し|応答|質問|説明|冗談|会話|機械的|単調|同じこと)")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _feature_reflected(feature: str, reply: str) -> bool:
    """Cheap observable proxy for whether planned guidance reached the reply.

    This intentionally avoids another LLM call.  It is used for diagnostics and
    conservative learning only, never as proof that a model followed a hidden
    instruction perfectly.
    """
    text = str(reply or "").strip()
    if len(text) < 4:
        return False
    patterns = {
        "humor_engine": r"(?:笑|ふふ|はは|ツッコミ|みたいな|並み|級|！|!)",
        "casual_conversation_engine": r"(?:そういえば|ちょっと|なんか|気になる|思う|感じ)",
        "imagination_engine": r"(?:もし|想像|仮説|かもしれ|可能性|別の見方)",
        "story_engine": r"(?:例えば|たとえば|場面|物語|ある日|シーン)",
        "playful_fantasy_engine": r"(?:もし|世界|設定|能力|架空|異世界|妄想)",
        "world_knowledge_engine": r"(?:つまり|具体的|要点|仕組み|手順|原因|場合)",
        "value_system": r"(?:私は|私なら|と思う|賛成|反対|違う|大事|重視)",
        "relationship_evolution": r"(?:前に|この前|さっき|一緒|また|覚えて)",
    }
    pattern = patterns.get(feature)
    return bool(re.search(pattern, text)) if pattern else True


class DialogueIntelligence:
    """Persistent User Model + response-planning signals for one persona."""

    def __init__(self, cfg, path: str | Path, *, persona_key: str = ""):
        self._cfg = cfg
        self._path = Path(path)
        self._persona_key = str(persona_key or "").strip()
        self._enabled = bool(cfg.get("dialogue.enabled", True))
        self._min_emotion_confidence = float(
            cfg.get("dialogue.emotional_memory.min_confidence", 0.75)
        )
        self._min_memory_importance = int(
            cfg.get("dialogue.memory.min_importance_to_store", 45)
        )
        self._curiosity_every = max(1, int(cfg.get("dialogue.curiosity.min_turn_interval", 3)))
        self._curiosity = CuriosityEngine(
            self._curiosity_every,
            threshold=float(cfg.get("dialogue.curiosity.threshold", 0.48)),
        )
        self._response_director = ResponseDirector()
        self._conversation_planner = ConversationPlanner(cfg)
        self._conversation_critic = ConversationCritic()
        self._surface_realizer = SurfaceRealizer()
        self._contract_buffers: dict[tuple[str, str, str], str] = {}
        self._recent_reply_avoidance_enabled = bool(cfg.get(
            "dialogue.conversation_generation.recent_reply_avoidance.enabled", False,
        ))
        self._recent_reply_count = max(1, min(8, int(cfg.get(
            "dialogue.conversation_generation.recent_reply_avoidance.count", 4,
        ))))
        self._recent_reply_max_chars = max(12, min(80, int(cfg.get(
            "dialogue.conversation_generation.recent_reply_avoidance.max_chars", 48,
        ))))
        self._recent_reply_max_total_chars = max(100, min(500, int(cfg.get(
            "dialogue.conversation_generation.recent_reply_avoidance.max_total_chars", 280,
        ))))
        adaptive_path = self._path.with_name(self._path.stem.replace("dialogue_", "adaptive_") + ".db")
        self._adaptive = AdaptiveConversationStore(adaptive_path)
        self._reaction_learner = ReactionLearner()
        self._semantic_graph = SemanticAssociationGraph(
            self._adaptive,
            enabled=bool(cfg.get("dialogue.semantic_graph.enabled", True)),
            max_nodes=int(cfg.get("dialogue.semantic_graph.max_nodes_per_user", 120)),
        )
        self._last_learning: dict[str, list[dict[str, Any]]] = {}
        self._last_trace: dict[str, dict[str, Any]] = {}
        # Rolling view of what repeats, and whether the plan is landing.
        from neuro_voice.dialogue.plan_metrics import DiversityWindow

        self._diversity = DiversityWindow(
            size=int(cfg.get("conversation.metrics.window", 8)),
        )
        #: Latest measurement, for the pipeline to log and the UI to show.
        self.last_plan_metrics: dict[str, Any] = {}
        self._state = self._default_state()
        self._load()

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "version": 5,
            "users": {},
            "momentum": 0.35,
            "last_turn_at": 0.0,
            "turn_index": 0,
            "last_curiosity_turn": -99,
            "tool_uses": {},
            "persona_conversation_traits": {"baseline": {}, "drift": {}},
        }

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                base = self._default_state()
                for key in base:
                    if key in raw:
                        base[key] = raw[key]
                self._state = base
                # Repair topics recorded before extraction split by script.
                for profile in (self._state.get("users") or {}).values():
                    if isinstance(profile, dict):
                        profile["topics"] = self._clean_stored_topics(
                            profile.get("topics"),
                        )
        except Exception:
            # Dialogue adaptation is optional; a corrupt adaptive file must
            # never prevent the assistant from starting.
            self._state = self._default_state()

    def save(self) -> None:
        if not self._enabled:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def close(self) -> None:
        self.save()
        try:
            self._adaptive.prune()
            self._adaptive.close()
        except Exception:
            pass

    def configure_features(self) -> None:
        self._conversation_planner.configure()

    def _persona_traits(self) -> dict[str, float]:
        """Resolve editable persona modules plus small experience-derived drift."""
        configured = self._cfg.get(
            f"persona.presets.{self._persona_key}.conversation_traits", None,
        ) if self._persona_key else None
        if not isinstance(configured, dict):
            configured = self._cfg.get("dialogue.personality_modules.defaults", {})
        baseline = normalize_persona_traits(configured)
        stored = self._state.setdefault("persona_conversation_traits", {"baseline": {}, "drift": {}})
        stored["baseline"] = baseline
        drift = stored.setdefault("drift", {})
        return {
            key: round(_clamp(value + float(drift.get(key, 0.0) or 0.0), .05, .95), 3)
            for key, value in baseline.items()
        }

    def _adjust_persona_traits_from_feedback(self, text: str, updates: list[dict[str, Any]]) -> None:
        """Let repeated explicit conversational feedback gently shape expression."""
        lowered = str(text or "")
        positive = bool(_POSITIVE_FEEDBACK.search(lowered))
        negative = bool(_NEGATIVE_FEEDBACK.search(lowered))
        if positive == negative:
            return
        targets = {
            "humor": r"(?:笑|冗談|ツッコミ|面白)",
            "playfulness": r"(?:遊び心|無邪気|子供っぽ|茶目っ気|ふざけ)",
            "cheekiness": r"(?:生意気|小憎らし|からか|煽り|挑発)",
            "spontaneity": r"(?:反応|リアクション|勢い|ノリ|素直)",
            "curiosity": r"(?:質問|聞いて|深掘り)",
            "emotional_expression": r"(?:感情|テンション|淡々)",
            "suggestion": r"(?:提案|案|どうすれば|次の一歩)",
            "contrarian": r"(?:反対|違う|肯定|意見)",
            "empathy": r"(?:共感|優し|寄り添)",
            "topic_shift": r"(?:話題|脱線|広げ)",
        }
        matched = [key for key, pattern in targets.items() if re.search(pattern, lowered)]
        if not matched:
            # Feedback about an adopted feature is still an experience, but
            # it should alter only its nearest expression axis.
            feature_map = {
                "humor_engine": "humor", "value_system": "contrarian",
                "casual_conversation_engine": "topic_shift",
                "relationship_evolution": "empathy",
            }
            matched = [feature_map[item.get("feature", "")] for item in updates
                       if feature_map.get(item.get("feature", ""))]
        if not matched:
            return
        state = self._state.setdefault("persona_conversation_traits", {"baseline": {}, "drift": {}})
        drift = state.setdefault("drift", {})
        delta = .006 if positive else -.006
        for key in dict.fromkeys(matched):
            drift[key] = round(_clamp(float(drift.get(key, 0.0)) + delta, -.18, .18), 4)

    def _user(self, user_key: str) -> dict[str, Any]:
        key = str(user_key or "local_user")[:80]
        users = self._state["users"]
        if key not in users:
            users[key] = {
                "turns": 0, "chars": 0, "questions": 0, "pause_like": 0,
                "topics": [], "emotions": [], "last_seen": 0.0,
            }
        UserModel.ensure(users[key])
        return users[key]

    def merge_users(self, source_key: str, target_key: str) -> bool:
        """Fold one user profile into another when they turn out to be one person.

        Counts add up and observed topics/emotions are concatenated, because
        both halves are genuine evidence about the same person.
        """
        source_key = str(source_key or "")[:80]
        target_key = str(target_key or "")[:80]
        if not source_key or not target_key or source_key == target_key:
            return False
        users = self._state.get("users") or {}
        source = users.get(source_key)
        if source is None:
            return False
        target = users.get(target_key)
        if target is None:
            users[target_key] = source
            users.pop(source_key, None)
            self.save()
            return True
        for key in ("turns", "chars", "questions", "pause_like"):
            target[key] = int(target.get(key, 0)) + int(source.get(key, 0))
        for key in ("topics", "emotions"):
            combined = list(target.get(key) or []) + list(source.get(key) or [])
            target[key] = list(dict.fromkeys(combined))[-12:]
        target["last_seen"] = max(
            float(target.get("last_seen", 0.0) or 0.0),
            float(source.get("last_seen", 0.0) or 0.0),
        )
        facts = dict(target.get("facts") or {})
        for name, value in (source.get("facts") or {}).items():
            if isinstance(value, list):
                merged = list(facts.get(name) or []) + list(value)
                facts[name] = list(dict.fromkeys(merged))[-10:]
            elif not facts.get(name):
                facts[name] = value
        if facts:
            target["facts"] = facts
        UserModel.ensure(target)
        users.pop(source_key, None)
        self._adaptive_reassign(source_key, target_key)
        self.save()
        logger.info("会話プロファイルを統合: %s → %s", source_key, target_key)
        return True

    def _adaptive_reassign(self, source_key: str, target_key: str) -> None:
        store = getattr(self, "_adaptive", None)
        reassign = getattr(store, "reassign_user", None)
        if callable(reassign):
            try:
                reassign(source_key, target_key)
            except Exception:
                logger.exception("適応記録の話者付け替えに失敗")

    @staticmethod
    def _topics(text: str) -> list[str]:
        ignored = {
            "これ", "それ", "こと", "ため", "いる", "ある", "です", "ます", "した", "して",
            "今日", "自分", "感じ", "本当", "普通", "全部", "一緒", "多分", "結構",
        }
        words = [
            word.lower() for word in _TOPIC_WORD.findall(str(text or ""))
            if word.lower() not in ignored and len(word) <= _TOPIC_MAX_CHARS
        ]
        return list(dict.fromkeys(words))[-4:]

    @staticmethod
    def _clean_stored_topics(values: Any) -> list[str]:
        """Drop entries written before topics were extracted properly.

        Whole utterances were being stored as topics, and they outlive the
        conversation that produced them: every session started by remarking on
        the same stale sentence.  Length alone does not separate the two
        (「特に何もないんだけど」 is short), so only values the current extractor
        could itself produce are kept.
        """
        if not isinstance(values, (list, tuple)):
            return []
        cleaned: list[str] = []
        for raw in values:
            value = " ".join(str(raw or "").split())
            if not value or len(value) > _TOPIC_MAX_CHARS:
                continue
            if not _TOPIC_WORD.fullmatch(value):
                continue
            if value not in cleaned:
                cleaned.append(value)
        return cleaned[-20:]

    def observe_turn(
        self,
        user_key: str,
        text: str,
        *,
        emotion: str | None = None,
        emotion_confidence: float = 0.0,
        now: float | None = None,
        source: str = "local",
        group: bool = False,
        allow_adaptive_store: bool = True,
    ) -> dict[str, Any]:
        """Update User Model, emotional timeline, and conversation momentum."""
        if not self._enabled:
            return {}
        now = time.time() if now is None else float(now)
        profile = self._user(user_key)
        move_updates = self._learn_move_from_reaction(
            user_key, profile, text, source=source, group=group,
            allow_store=allow_adaptive_store,
        )
        feature_updates = self._learn_from_reaction(
            user_key, profile, text, emotion=emotion, emotion_confidence=emotion_confidence,
            group=group, allow_store=allow_adaptive_store,
        )
        self._last_learning[user_key] = [*move_updates, *feature_updates]
        profile["turns"] = int(profile.get("turns", 0)) + 1
        profile["chars"] = int(profile.get("chars", 0)) + len(text)
        profile["questions"] = int(profile.get("questions", 0)) + int(bool(_QUESTION.search(text)))
        profile["pause_like"] = int(profile.get("pause_like", 0)) + int("…" in text or "えー" in text or "あの" in text)
        profile["last_seen"] = now
        UserModel.observe(profile, text, now=now)
        extracted_topics = self._topics(text)
        topics = list(profile.get("topics", [])) + extracted_topics
        profile["topics"] = list(dict.fromkeys(topics))[-20:]
        WorkingMemory.observe(
            profile, text, extracted_topics, now=now,
            turn_index=int(self._state.get("turn_index", 0)) + 1,
        )
        self._semantic_graph.observe(
            user_key, extracted_topics, group=group, source=source,
            allow_store=allow_adaptive_store,
        )
        self._semantic_graph.observe_correction(
            user_key, text, group=group, source=source,
            allow_store=allow_adaptive_store,
        )
        for topic in extracted_topics[-2:]:
            self._adaptive.upsert(
                "topic_lifecycle", topic,
                {"topic": topic, "state": "active", "last_text": text[:160], "turn": self._state.get("turn_index", 0)},
                user_id=user_key, source="user_utterance", confidence=.85, importance=.45,
            )
        self._update_curiosities(user_key, profile, text, now)
        if emotion and emotion != "neutral" and emotion_confidence >= self._min_emotion_confidence:
            events = list(profile.get("emotions", []))
            events.append({"at": now, "label": str(emotion)[:24], "confidence": round(emotion_confidence, 3)})
            profile["emotions"] = events[-120:]

        previous = float(self._state.get("last_turn_at", 0.0) or 0.0)
        gap = now - previous if previous else 12.0
        engagement = 0.30 + min(len(text), 120) / 260
        if _QUESTION.search(text):
            engagement += 0.12
        if gap < 35:
            engagement += 0.16
        elif gap > 180:
            engagement -= 0.18
        old = float(self._state.get("momentum", 0.35))
        self._state["momentum"] = round(_clamp(old * 0.58 + engagement * 0.42, 0.05, 0.98), 3)
        self._state["last_turn_at"] = now
        self._state["turn_index"] = int(self._state.get("turn_index", 0)) + 1
        self._adjust_persona_traits_from_feedback(text, self._last_learning.get(user_key, []))
        self.save()
        return self.snapshot(user_key)

    def record_response(self, user_key: str, reply: str = "") -> None:
        if self._enabled:
            profile = self._user(user_key)
            profile["last_seen"] = time.time()
            if not reply.strip():
                self.save()
                return
            self._curiosity.record_actual_question(
                profile, reply, int(self._state.get("turn_index", 0)),
            )
            generation = self._conversation_planner.ensure_state(profile)
            critique = self._conversation_critic.evaluate(
                reply, list(generation.get("recent_replies", [])),
            )
            plan = dict(generation.get("last_plan", {}))
            turn_id = f"{self._adaptive.session_id}:{self._state.get('turn_index', 0)}"
            selected = list(plan.get("selected_features", []))
            for item in selected:
                feature = str(item.get("feature_type", ""))
                if not feature:
                    continue
                reflected = _feature_reflected(feature, reply)
                self._adaptive.append(
                    "conversation_feature_usage",
                    {"turn_id": turn_id, "feature": feature, "reason": item.get("reason", ""),
                     "summary": item.get("content_summary", ""), "reflected_in_prompt": True,
                     "reflected_in_reply": reflected},
                    user_id=user_key, record_key=f"{turn_id}:{feature}", importance=.35,
                )
                if feature == "humor_engine" and item.get("pattern_key"):
                    self._adaptive.upsert(
                        "humor_pattern_history", str(item["pattern_key"]),
                        {"pattern": item["pattern_key"], "last_used": time.time(),
                         "topic": plan.get("selected_topic", ""), "reaction": "pending"},
                        user_id=user_key, source="planner", confidence=.9, importance=.4,
                    )
                if feature in {"story_engine", "playful_fantasy_engine"}:
                    self._adaptive.upsert(
                        "topic_lifecycle", f"creative_session:{user_key}",
                        {"type": "creative_session", "mode": item.get("pattern_key") or feature,
                         "current_scene": reply[:240], "open_hooks": [],
                         "topic": plan.get("selected_topic", ""),
                         "last_updated": time.time(), "factual_status": "fiction"},
                        user_id=user_key, source="fictional_conversation", confidence=.9,
                        importance=.48, expiry=time.time() + 14 * 86400,
                    )
            for evaluation in plan.get("feature_evaluations", []):
                self._adaptive.append(
                    "candidate_evaluations", {"turn_id": turn_id, **dict(evaluation)},
                    user_id=user_key, record_key=f"{turn_id}:{evaluation.get('candidate_id', uuid.uuid4().hex[:6])}",
                    importance=.25,
                )
            for index, candidate in enumerate(plan.get("move_candidates", [])):
                self._adaptive.append(
                    "candidate_evaluations",
                    {
                        "turn_id": turn_id,
                        "candidate_type": "conversation_move",
                        **dict(candidate),
                    },
                    user_id=user_key,
                    record_key=f"{turn_id}:move:{index}",
                    importance=.22,
                    expiry=time.time() + 14 * 86400,
                )
            critic_data = critique.snapshot()
            self._adaptive.append(
                "critic_results", {"turn_id": turn_id, **critic_data}, user_id=user_key,
                record_key=turn_id, importance=.4,
            )
            trace = self._build_trace(user_key, profile, reply, plan, critic_data, turn_id)
            self._adaptive.record_trace(turn_id, user_key, trace)
            self._last_trace[user_key] = {"turn_id": turn_id, **trace}
            self._conversation_planner.commit(profile, reply, critique.snapshot())
            # The only place the plan and the finished reply are both in hand.
            # Measurement only: no LLM call, no text stored (第12条).
            self._record_plan_metrics(plan, reply, generation)
            WorkingMemory.record_response(profile, plan, reply)
            self.save()

    def _record_plan_metrics(self, plan, reply: str, generation: dict) -> None:
        """Did the plan reach the reply?  Nobody has ever checked.

        The complaint "毎回似た構成" fits two opposite faults — a planner that
        repeats itself, and a planner whose choices never survive into the
        text.  Adding a second planner would only help the first.  This
        records enough to tell them apart.
        """
        if not bool(self._cfg.get("conversation.metrics.enabled", True)):
            return
        try:
            from neuro_voice.dialogue.plan_metrics import record_for

            observed = generation.get("last_observation_context") or {}
            # `last_plan` survives in the profile after the planner is switched
            # off, so the control arm of the A/B recorded eight turns of
            # `proposal_path / reflective / long` that no planner had chosen —
            # stale values from the last enabled turn, complete with
            # `target_length` violations against a plan that did not exist.
            # The comparison is worthless if the control side invents a plan.
            if not self._conversation_planner.enabled:
                plan = {}
            record = record_for(
                plan, reply,
                turn=int(self._state.get("turn_index", 0)),
                planner_enabled=self._conversation_planner.enabled,
                memory_used=len(observed.get("memory_snippets") or ()),
            )
            self._diversity.record(record)
            self.last_plan_metrics = {
                **record.snapshot(), "window": self._diversity.metrics(),
            }
        except Exception:
            logger.debug("計画反映率の記録に失敗", exc_info=True)

    def emotional_trend(self, user_key: str) -> str | None:
        events = self._user(user_key).get("emotions", [])[-8:]
        labels = [str(e.get("label", "")) for e in events]
        if len(labels) >= 2 and sum(x in {"sad", "angry", "fear", "tired"} for x in labels) >= 2:
            return "recently_low"
        if len(labels) >= 2 and sum(x in {"joy", "fun", "surprised"} for x in labels) >= 2:
            return "recently_positive"
        return None

    def memory_importance(self, text: str, kind: str = "fact") -> int:
        """Return 0-100; low-value episodes stay out of long-term memory."""
        score = 18
        if kind == "preference":
            score += 35
        if _PERSONAL.search(text):
            score += 38
        if re.search(r"(?:覚えて|大事|ずっと|今後|目標|将来)", text):
            score += 22
        if re.search(r"(?:今日|さっき).{0,12}(?:食べ|行っ|見|した)", text):
            score -= 12
        return int(_clamp(score, 0, 100))

    @staticmethod
    def importance_to_legacy(score: int) -> int:
        if score >= 85:
            return 5
        if score >= 65:
            return 4
        if score >= 45:
            return 3
        if score >= 25:
            return 2
        return 1

    @property
    def min_memory_importance(self) -> int:
        return self._min_memory_importance

    def should_use_tool(self, tool: str, text: str, *, now: float | None = None) -> tuple[bool, str]:
        """Tool Scheduler: prevent duplicate calls, never suppress an explicit request."""
        now = time.time() if now is None else float(now)
        explicit = bool(re.search(r"(?:調べ|検索|探し|最新|ニュース|web|ウェブ)", text, re.I))
        key = f"{tool}:{text.strip().lower()[:120]}"
        last = float(self._state.get("tool_uses", {}).get(key, 0.0) or 0.0)
        if now - last < 12.0:
            return False, "duplicate_cooldown"
        # The caller's intent classifier has already established relevance.
        # Keep implicit factual requests eligible while blocking duplicates.
        return True, "explicit_request" if explicit else "relevance_checked"

    def mark_tool_use(self, tool: str, text: str, *, now: float | None = None) -> None:
        key = f"{tool}:{text.strip().lower()[:120]}"
        self._state.setdefault("tool_uses", {})[key] = time.time() if now is None else float(now)
        self._state["tool_uses"] = dict(list(self._state["tool_uses"].items())[-100:])
        self.save()

    def prompt_context(
        self, user_key: str, text: str, relationship: dict[str, Any] | None = None,
        memory_snippets: list[str] | None = None,
        allow_recent_reply_context: bool = True,
        response_id: str = "", source: str = "",
        recall_requested: bool = False, recall_grounded: bool = False,
        group: bool = False,
        internal_state: dict[str, Any] | None = None,
    ) -> str:
        """Compact internal planner context, intentionally not a reasoning transcript.

        `internal_state` は持続している内面から来た**粗いラベルだけ**
        （Phase 4 の `expression_constraints`）。口調・返答量・断定の強さへ
        効かせる。**生の数値も履歴も渡さない。**
        """
        if not self._enabled:
            return ""
        profile = self._user(user_key)
        turns = int(profile.get("turns", 0))
        average = int(profile.get("chars", 0)) / max(turns, 1)
        question_rate = int(profile.get("questions", 0)) / max(turns, 1)
        momentum = float(self._state.get("momentum", 0.35))
        working_memory = WorkingMemory.snapshot(profile)
        persona_traits = self._persona_traits()
        trend = self.emotional_trend(user_key)
        curiosity_decision = self._curiosity.decide(
            text=text,
            topics=self._topics(text),
            profile=profile,
            momentum=momentum,
            turn_index=int(self._state.get("turn_index", 0)),
        )
        direction = self._response_director.decide(
            text,
            allow_follow_up=curiosity_decision.should_ask,
            momentum=momentum,
        )
        relation = (relationship or {}).get("label", "はじめまして")
        pace = "短めの往復を好む傾向" if average < 32 else "説明も受け取れる傾向"
        mood = "最近は負荷が高そう。断定せず、やわらかく配慮する。" if trend == "recently_low" else ""
        curiosity = (
            "ASK_FOLLOW_UP=yes. 回答後、情報を得る意味がある時だけ話題に沿う質問を一つ。"
            if curiosity_decision.should_ask else "ASK_FOLLOW_UP=no. 質問のための質問はしない。"
        )
        planner_context = ""
        suppress_unresolved = False
        if self._conversation_planner.enabled:
            plan_started = time.perf_counter()
            active_curiosities = [row["payload"] for row in self._adaptive.latest(
                "ai_active_curiosities", user_id=user_key, limit=4,
            ) if not row["payload"].get("resolved")]
            profile["_feature_context"] = {
                "relationship": relationship or {},
                "memory_snippets": list(memory_snippets or []),
                "active_curiosities": active_curiosities,
                "persona_traits": persona_traits,
            }
            associations = self._semantic_graph.associations(
                user_key, self._topics(text), group=group, limit=2,
            )
            association_labels = [
                f"{item.source}→{item.target} ({item.relation})"
                for item in associations
            ]
            try:
                plan = self._conversation_planner.plan(
                    user_key, text, profile, momentum=momentum,
                    memory_snippets=memory_snippets or [],
                    allow_follow_up=curiosity_decision.should_ask,
                    turn_index=int(self._state.get("turn_index", 0)),
                    learned_weights=self._adaptive.feature_weights(user_key),
                    value_profile=self._adaptive.value_profile(user_key),
                    working_memory=working_memory,
                    persona_traits=persona_traits,
                    response_id=response_id,
                    source=source,
                    recall_requested=recall_requested,
                    recall_grounded=recall_grounded,
                    semantic_associations=association_labels,
                    group=group,
                    internal_state=internal_state,
                )
            finally:
                profile.pop("_feature_context", None)
            plan_ms = (time.perf_counter() - plan_started) * 1000
            generation = self._conversation_planner.ensure_state(profile)
            generation["last_plan_ms"] = round(plan_ms, 3)
            generation["last_observation_context"] = {
                "relationship": dict(relationship or {}),
                "memory_snippets": [str(item)[:180] for item in (memory_snippets or [])[:5]],
                "active_curiosities": active_curiosities[:4],
                "semantic_associations": [
                    item.snapshot() for item in associations
                ],
            }
            critic_feedback = self._conversation_critic.feedback_prompt(
                dict(generation.get("last_critique", {})),
            )
            recent_replies = (
                list(generation.get("recent_replies", []))
                if self._recent_reply_avoidance_enabled and allow_recent_reply_context else []
            )
            planner_context = (
                self._conversation_planner.prompt(plan) + "\n"
                + self._surface_realizer.prompt(
                    plan,
                    critic_feedback,
                    recent_replies=recent_replies,
                    recent_reply_count=self._recent_reply_count,
                    recent_reply_max_chars=self._recent_reply_max_chars,
                    recent_reply_max_total_chars=self._recent_reply_max_total_chars,
                )
            )
            guidance = self._surface_realizer.recent_reply_context(
                recent_replies,
                count=self._recent_reply_count,
                max_chars=self._recent_reply_max_chars,
                max_total_chars=self._recent_reply_max_total_chars,
            )
            generation["last_recent_reply_guidance"] = {
                "enabled": (
                    self._recent_reply_avoidance_enabled
                    and bool(allow_recent_reply_context)
                ),
                "examples": min(len(recent_replies), self._recent_reply_count),
                "chars": len(guidance),
            }
            suppress_unresolved = bool(plan.continuation_requested)
            if suppress_unresolved:
                curiosity = "ASK_FOLLOW_UP=no. 継続トーク中は質問でユーザーへ返さない。"
        return (
            "【対話制御・内部用】\n"
            f"User Model: {turns}ターン、{pace}、質問頻度={question_rate:.0%}。"
            f"関係={relation}; 勢い={momentum:.2f}; "
            f"感情傾向={mood or '目立つ変化なし'}。\n"
            f"Working Memory: 今の焦点={working_memory.get('active_theme') or '現在の話'}。"
            f"目的={working_memory.get('conversation_goal') or '自然に応答する'}。\n"
            + ("Unresolved Questions: 次の保留がある。現在の問いを優先し、会話の流れが自然な時だけ一つ再訪してよい。"
               + " / ".join(item.get("question", "") for item in working_memory.get("open_questions", [])[:3]) + "\n"
               if working_memory.get("open_questions") and not suppress_unresolved else "")
            + "Confidence Manager: 事実・記憶・推測の確度を混同しない。\n"
            f"Curiosity Engine: {curiosity}\n"
            + direction.prompt() + "\n"
            + UserModel.prompt_context(profile) + "\n"
            + planner_context + "\n"
            + "Internal Thought Layer: 上記は発話でなく内部方針。"
        )

    def _contract(
        self, user_key: str, *, source: str, response_id: str,
    ) -> ConversationContract | None:
        plan = self.current_plan(user_key)
        if not plan:
            return None
        contract = ConversationContract.from_plan(plan)
        if not response_id or contract.response_id != str(response_id):
            return None
        if contract.source and contract.source != str(source or ""):
            return None
        return contract

    def enforce_output_sentence(
        self, user_key: str, text: str, *, source: str, response_id: str,
    ) -> str:
        """Validate one sentence immediately before TTS on either transport."""
        contract = self._contract(user_key, source=source, response_id=response_id)
        if contract is None:
            return text
        key = (str(user_key), str(source), str(response_id))
        combined = self._contract_buffers.pop(key, "") + str(text or "")
        if needs_grounding_buffer(combined):
            self._contract_buffers[key] = combined
            return ""
        result = enforce_sentence(combined, contract)
        if result.reasons:
            logger.warning(
                "Conversation contract adjusted spoken sentence: %s",
                ",".join(result.reasons),
            )
        return result.text

    def flush_output_sentence(
        self, user_key: str, *, source: str, response_id: str,
    ) -> str:
        """Flush a held final fragment when generation ends without punctuation."""
        key = (str(user_key), str(source), str(response_id))
        buffered = self._contract_buffers.pop(key, "")
        if not buffered:
            return ""
        contract = self._contract(user_key, source=source, response_id=response_id)
        if contract is None:
            return buffered
        result = enforce_sentence(buffered, contract)
        if result.reasons:
            logger.warning(
                "Conversation contract adjusted final spoken fragment: %s",
                ",".join(result.reasons),
            )
        return result.text

    def enforce_output_reply(
        self, user_key: str, text: str, *, source: str, response_id: str,
    ) -> str:
        """Return the canonical reply used by UI, history and memory."""
        contract = self._contract(user_key, source=source, response_id=response_id)
        if contract is None:
            return text
        self._contract_buffers.pop((str(user_key), str(source), str(response_id)), None)
        result = enforce_reply(text, contract)
        if result.reasons:
            logger.warning(
                "Conversation contract adjusted final reply: %s",
                ",".join(result.reasons),
            )
        return result.text

    def snapshot(self, user_key: str) -> dict[str, Any]:
        profile = self._user(user_key)
        generation = self._conversation_planner.ensure_state(profile)
        return {
            "momentum": float(self._state.get("momentum", 0.35)),
            "user": {
                "turns": int(profile.get("turns", 0)),
                "question_rate": round(int(profile.get("questions", 0)) / max(int(profile.get("turns", 0)), 1), 3),
                "topics": list(profile.get("topics", []))[-5:],
                "emotional_trend": self.emotional_trend(user_key),
                "facts": dict(UserModel.ensure(profile)["facts"]),
                "interaction_policy": dict(UserModel.ensure(profile)["interaction_policy"]),
            },
            "conversation_generation": {
                "last_plan": dict(generation.get("last_plan", {})),
                "last_critique": dict(generation.get("last_critique", {})),
                "recent_styles": list(generation.get("recent_styles", []))[-5:],
                "recent_shapes": list(generation.get("recent_shapes", []))[-5:],
                "ai_emotion": dict(generation.get("ai_emotion", {})),
                "recent_features": list(generation.get("recent_features", []))[-8:],
                "feature_evaluations": list(generation.get("last_feature_evaluations", [])),
                "planning_ms": float(generation.get("last_plan_ms", 0.0)),
                "recent_reply_guidance": dict(
                    generation.get("last_recent_reply_guidance", {}),
                ),
            },
            "working_memory": WorkingMemory.snapshot(profile),
            "persona_modules": self._persona_traits(),
            "values": self._adaptive.value_profile(user_key).snapshot(),
            "active_curiosities": [row["payload"] for row in self._adaptive.latest(
                "ai_active_curiosities", user_id=user_key, limit=5,
            )],
            "learning_updates": list(self._last_learning.get(user_key, [])),
            "quality_metrics": self.quality_metrics(user_key),
            "developer_trace": dict(self._last_trace.get(user_key, {})),
            "semantic_associations": list(
                generation.get("last_observation_context", {}).get(
                    "semantic_associations", [],
                )
            ),
        }

    def voice_direction(self, user_key: str) -> dict[str, Any]:
        """Return the current planner emotion/delivery for StyleManager."""
        profile = self._user(user_key)
        generation = self._conversation_planner.ensure_state(profile)
        plan = dict(generation.get("last_plan", {}))
        planned_emotion = str(plan.get("ai_emotion", "calm"))
        emotion = {
            "happy": "joy", "excited": "fun", "surprised": "surprised",
            "concerned": "sad", "interested": "neutral", "gentle": "neutral",
            "calm": "neutral",
        }.get(planned_emotion, "neutral")
        expression = build_expression_plan(
            plan, source=str(plan.get("source") or ""),
        )
        delivery = expression.voice.delivery
        return {
            "emotion": emotion, "delivery": delivery,
            "ai_emotion": planned_emotion,
            "expression_plan": expression.snapshot(),
        }

    def expression_plan(self, user_key: str, *, source: str = "") -> dict[str, Any]:
        """Return a future-avatar-safe expression event; it performs no action."""
        plan = self.current_plan(user_key)
        return build_expression_plan(plan, source=source).snapshot()

    def current_plan(self, user_key: str) -> dict[str, Any]:
        """Sanitized observable plan summary; contains no reasoning transcript."""
        profile = self._user(user_key)
        generation = self._conversation_planner.ensure_state(profile)
        return dict(generation.get("last_plan", {}))

    def developer_snapshot(self, user_key: str) -> dict[str, Any]:
        """Observable state only; never returns hidden model reasoning."""
        snap = self.snapshot(user_key)
        plan = snap["conversation_generation"]["last_plan"]
        snap["ab_comparison"] = {
            "configured_variant": str(self._cfg.get("conversation_engine.variant", "enhanced")),
            "baseline": {
                "optional_features": [], "candidate_count": len(plan.get("candidates", [])),
                "expected_character": "直接回答中心・従来Planner",
            },
            "enhanced": {
                "optional_features": [item.get("feature_type") for item in plan.get("selected_features", [])],
                "candidate_count": len(plan.get("candidates", [])) + len(plan.get("feature_evaluations", [])),
                "expected_character": "状況根拠付きの任意要素を0〜3件統合",
            },
            "experimental": {
                "optional_features": [item.get("feature_type") for item in plan.get("selected_features", [])],
                "candidate_count": len(plan.get("candidates", [])) + len(plan.get("feature_evaluations", [])),
                "expected_character": "同じ安全条件で境界候補を8%だけ積極評価",
            },
        }
        snap["recent_traces"] = self._adaptive.recent_traces(user_key, limit=12)
        return snap

    def quality_metrics(self, user_key: str) -> dict[str, Any]:
        profile = self._user(user_key)
        generation = self._conversation_planner.ensure_state(profile)
        replies = list(generation.get("recent_replies", []))[-10:]
        if not replies:
            return {"turns": 0}
        openings = [self._conversation_critic._edge(item)[0] for item in replies]
        endings = [self._conversation_critic._edge(item)[1] for item in replies]
        question_flags = [item.rstrip().endswith(("?", "？")) for item in replies]
        similarities = [
            SequenceMatcher(None, replies[i - 1][:240], replies[i][:240]).ratio()
            for i in range(1, len(replies))
        ]
        traces = self._adaptive.recent_traces(user_key, limit=40)
        selected = [feature for trace in traces for feature in trace.get("selected_features", [])]
        realized = [feature for trace in traces for feature in trace.get("realized_features", [])]
        proposed = [feature for trace in traces for feature in trace.get("proposed_features", [])]
        feedback = self._adaptive.latest("conversation_feature_feedback", user_id=user_key, limit=100)
        positive = sum(float(row["payload"].get("score", .5)) > .62 for row in feedback)
        return {
            "turns": len(replies),
            "same_opening_rate": round(1 - len(set(openings)) / max(1, len(openings)), 3),
            "same_ending_rate": round(1 - len(set(endings)) / max(1, len(endings)), 3),
            "question_ending_rate": round(sum(question_flags) / len(question_flags), 3),
            "consecutive_question_rate": round(sum(a and b for a, b in zip(question_flags, question_flags[1:])) / max(1, len(question_flags) - 1), 3),
            "previous_reply_similarity": round(sum(similarities) / max(1, len(similarities)), 3),
            "ai_average_chars": round(sum(map(len, replies)) / len(replies), 1),
            "user_average_chars": round(int(profile.get("chars", 0)) / max(1, int(profile.get("turns", 1))), 1),
            "feature_adoption_rate": round(len(selected) / max(1, len(proposed)), 3),
            "feature_realization_rate": round(len(realized) / max(1, len(selected)), 3),
            "feature_rejection_rate": round(max(0, len(proposed) - len(selected)) / max(1, len(proposed)), 3),
            "positive_feature_feedback_rate": round(positive / max(1, len(feedback)), 3),
            "memory_usage_rate": round(sum("memory" in feature for feature in selected) / max(1, len(traces)), 3),
            "humor_activation_rate": round(sum(feature == "humor_engine" for feature in selected) / max(1, len(traces)), 3),
            "planning_ms": float(generation.get("last_plan_ms", 0.0)),
        }

    def _build_trace(
        self, user_key: str, profile: dict[str, Any], reply: str, plan: dict[str, Any],
        critique: dict[str, Any], turn_id: str,
    ) -> dict[str, Any]:
        selected = [item.get("feature_type", "") for item in plan.get("selected_features", [])]
        realized = [feature for feature in selected if _feature_reflected(str(feature), reply)]
        evaluations = list(plan.get("feature_evaluations", []))
        proposed = [item.get("candidate_id", "") for item in evaluations]
        rejected = [
            {"feature": item.get("candidate_id", ""), "reasons": item.get("rejection_reasons", [])}
            for item in evaluations if item.get("candidate_id") not in selected
        ]
        observed = dict(profile.get("conversation_generation", {}).get("last_observation_context", {}))
        return {
            "intent": plan.get("intent", ""),
            "user_emotion": (profile.get("emotions", [{}])[-1] if profile.get("emotions") else {}),
            "ai_emotion": plan.get("ai_emotion", ""),
            "temperature": plan.get("temperature", ""),
            "relationship": dict(observed.get("relationship", {})),
            "retrieved_memories": list(observed.get("memory_snippets", [])),
            "active_curiosities": [row["payload"] for row in self._adaptive.latest("ai_active_curiosities", user_id=user_key, limit=3)],
            "proposed_features": proposed,
            "selected_features": selected,
            "realized_features": realized,
            "rejected_features": rejected,
            "candidate_evaluations": evaluations,
            "move_candidates": list(plan.get("move_candidates", [])),
            "selected_move": plan.get("conversation_move", ""),
            "selection_reasons": list(plan.get("selection_reasons", [])),
            "semantic_associations": list(
                observed.get("semantic_associations", []),
            ),
            "critic": critique,
            "learning_updates": list(self._last_learning.get(user_key, [])),
            "reply_preview": reply[:320],
            "planning_ms": float(profile.get("conversation_generation", {}).get("last_plan_ms", 0.0)),
            "variant": plan.get("engine_variant", "enhanced"),
        }

    def _learn_from_reaction(
        self, user_key: str, profile: dict[str, Any], text: str, *,
        emotion: str | None, emotion_confidence: float,
        group: bool = False, allow_store: bool = True,
    ) -> list[dict[str, Any]]:
        if not bool(self._cfg.get("features.self_growth", True)) or not allow_store:
            return []
        generation = self._conversation_planner.ensure_state(profile)
        plan = dict(generation.get("last_plan", {}))
        selected = [
            item for item in plan.get("selected_features", [])
            if _feature_reflected(str(item.get("feature_type", "")),
                                  str(generation.get("recent_replies", [""])[-1] if generation.get("recent_replies") else ""))
        ]
        if not selected:
            return []
        positive = bool(_POSITIVE_FEEDBACK.search(text))
        negative = bool(_NEGATIVE_FEEDBACK.search(text))
        explicit = bool(_EXPLICIT_FEEDBACK.search(text)) and (positive or negative)
        if positive == negative:
            if len(text.strip()) >= 18:
                score, signal = .58, "conversation_continued"
            else:
                return []
        else:
            score, signal = ((.88, "explicit_positive") if positive else (.12, "explicit_negative"))
        alpha = .18 if explicit else .045
        current = self._adaptive.feature_weights(user_key)
        updates = []
        for item in selected:
            feature = str(item.get("feature_type", ""))
            if not feature:
                continue
            old = float(current.get(feature, 1.0))
            target = .5 + score
            new = old * (1 - alpha) + target * alpha
            update = {"feature": feature, "old_weight": round(old, 4),
                      "weight": round(new, 4), "signal": signal, "alpha": alpha}
            self._adaptive.upsert(
                "conversation_learning_updates", feature, update, user_id=user_key,
                source=signal, confidence=.9 if explicit else .58, importance=.65 if explicit else .35,
            )
            self._adaptive.append(
                "conversation_feature_feedback",
                {
                    "feature": feature, "score": score, "signal": signal,
                    "reaction": "" if group else text[:160],
                    "group_aggregate": bool(group),
                },
                user_id=user_key, importance=.45 if explicit else .25,
            )
            updates.append(update)
            if explicit:
                preference = "positive" if score > .5 else "negative"
                self._adaptive.upsert(
                    "ai_preferences", f"feature:{feature}",
                    {"feature": feature, "preference": preference, "score": score,
                     "source": "explicit_user_reaction", "updated_at": time.time()},
                    user_id=user_key, source=signal, confidence=.9, importance=.62,
                )
                history = [row["payload"] for row in self._adaptive.latest(
                    "conversation_feature_feedback", user_id=user_key, limit=12,
                ) if row["payload"].get("feature") == feature and
                    str(row["payload"].get("signal", "")).startswith("explicit_")]
                same = [row for row in history if (float(row.get("score", .5)) > .5) == (score > .5)]
                if len(same) >= 3:
                    lesson = {
                        "feature": feature, "direction": preference,
                        "evidence_count": len(same),
                        "lesson": f"{feature}への明示的反応は{preference}傾向",
                        "confidence": round(min(.95, .58 + .06 * len(same)), 3),
                        "last_evidence_at": time.time(),
                    }
                    self._adaptive.upsert(
                        "ai_generalized_lessons", f"feature:{feature}:{preference}", lesson,
                        user_id=user_key, source="repeated_explicit_feedback",
                        confidence=lesson["confidence"], importance=.78,
                    )
                    value_axis = {
                        "humor_engine": "humor", "imagination_engine": "creativity",
                        "story_engine": "creativity", "playful_fantasy_engine": "creativity",
                        "world_knowledge_engine": "practicality", "value_system": "honesty",
                        "casual_conversation_engine": "curiosity",
                    }.get(feature)
                    if value_axis:
                        self._adaptive.adjust_value(
                            user_key, value_axis, .004 if score > .5 else -.003,
                            evidence_count=len(same),
                        )
        if explicit and not group and bool(self._cfg.get("features.experience_memory", True)):
            factual = "verified_conversation"
            episode = {
                "episode_id": uuid.uuid4().hex, "timestamp": time.time(),
                "participants": [user_key, "assistant"], "topic": plan.get("selected_topic", ""),
                "event_summary": "会話方針への明示的な反応", "ai_action": ",".join(
                    item.get("feature_type", "") for item in selected),
                "user_reaction": text[:240], "outcome": signal,
                "emotional_impact": {str(emotion or "neutral"): float(emotion_confidence)},
                "lesson_candidate": updates[0]["feature"] if updates else None,
                "importance": .72, "confidence": .92, "source_message_ids": [],
                "factual_status": factual,
            }
            self._adaptive.append("ai_experience_episodes", episode, user_id=user_key,
                                  importance=.72, confidence=.92)
        return updates

    def _learn_move_from_reaction(
        self, user_key: str, profile: dict[str, Any], text: str, *,
        source: str, group: bool, allow_store: bool,
    ) -> list[dict[str, Any]]:
        """Update the previous selected move from the next observable reaction."""
        if not bool(self._cfg.get("features.self_growth", True)) or not allow_store:
            return []
        generation = self._conversation_planner.ensure_state(profile)
        plan = dict(generation.get("last_plan", {}))
        move = str(plan.get("conversation_move") or "")
        signal = self._reaction_learner.classify(
            text, previous_move=move, surface=source, group=group,
        )
        if signal is None:
            return []
        key = f"move:{move}"
        old = float(self._adaptive.feature_weights(user_key).get(key, 1.0))
        new = self._reaction_learner.updated_weight(old, signal)
        update = {
            "feature": key,
            "old_weight": round(old, 4),
            "weight": new,
            "signal": signal.kind,
            "confidence": signal.confidence,
            "surface": source,
            "group": bool(group),
        }
        self._adaptive.upsert(
            "conversation_learning_updates", key, update,
            user_id=user_key, source=signal.kind,
            confidence=signal.confidence,
            importance=.58 if signal.explicit else .32,
        )
        # Structured signal only: never persist the utterance here.
        self._adaptive.append(
            "conversation_reaction_signals", signal.snapshot(),
            user_id=user_key, source=source,
            confidence=signal.confidence,
            importance=.45 if signal.explicit else .24,
        )
        return [update]

    def _update_curiosities(self, user_key: str, profile: dict[str, Any], text: str, now: float) -> None:
        if not bool(self._cfg.get("features.self_growth", True)):
            return
        topics = self._topics(text)
        if not topics:
            return
        if re.search(r"(?:途中|続き|その後|まだ|今度|あとで|作って|実装して|改善して)", text):
            topic = topics[-1]
            self._adaptive.upsert(
                "ai_active_curiosities", topic,
                {"topic": topic, "source": "unfinished_or_project", "intensity": .68,
                 "created_at": now, "last_mentioned_at": None,
                 "question_candidates": [f"{topic}のその後を自然な時に確認する"],
                 "expiry": now + 30 * 86400, "resolved": False},
                user_id=user_key, source="user_statement", confidence=.72,
                importance=.62, expiry=now + 30 * 86400,
            )
