"""Compact, observable working memory for continuous conversation.

This is intentionally not a chain-of-thought store.  It keeps only the
conversation state a person could reasonably carry between turns: what is
being discussed, why the turn matters, and a few explicitly grounded open
threads worth revisiting later.
"""
from __future__ import annotations

import re
import time
from typing import Any


DEFAULT_PERSONA_TRAITS: dict[str, float] = {
    "curiosity": .70,
    "humor": .55,
    "playfulness": .55,
    "cheekiness": .35,
    "spontaneity": .50,
    "emotional_expression": .60,
    "suggestion": .58,
    "contrarian": .48,
    "empathy": .62,
    "topic_shift": .48,
}

PERSONA_TRAIT_LABELS: tuple[tuple[str, str], ...] = (
    ("curiosity", "好奇心"),
    ("humor", "ユーモア"),
    ("playfulness", "遊び心"),
    ("cheekiness", "生意気さ"),
    ("spontaneity", "無邪気な反応"),
    ("emotional_expression", "感情表現"),
    ("suggestion", "提案力"),
    ("contrarian", "反論性"),
    ("empathy", "共感性"),
    ("topic_shift", "話題転換"),
)

_ONGOING = re.compile(r"(?:途中|続き|その後|まだ|今度|あとで|作(?:って|り)|実装|改善|開発|進め|試す)")
_RESOLVED = re.compile(r"(?:できた|終わった|解決|直った|完成|やめた|諦め)")
_QUESTION = re.compile(r"[?？]|(?:どう|なぜ|なんで|教えて|知りたい)")
_EXPLICIT_OPEN = re.compile(
    r"(?:まだ|途中|継続中|今度|あとで).{0,28}(?:実装|改善|開発|作|進め|試|確認|直|調べ)"
    r"|(?:実装|改善|開発|作|進め|試|確認|直|調べ).{0,28}(?:途中|続き|まだ|今度|あとで|継続中)"
)
_VAGUE_THEME = re.compile(
    r"^(?:それ|あれ|これ|その話|あの話|この話|さっきの話|前の話|"
    r"さっき言ってた.{0,16}|前に言ってた.{0,16}|また覚えてるって話)$"
)


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def normalize_persona_traits(raw: dict[str, Any] | None) -> dict[str, float]:
    """Return a complete, bounded trait map from editable persona settings."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: round(_clamp(raw.get(key, default)), 3)
            for key, default in DEFAULT_PERSONA_TRAITS.items()}


class WorkingMemory:
    """Per-user short-lived thought context stored inside the dialogue profile."""

    @staticmethod
    def ensure(profile: dict[str, Any]) -> dict[str, Any]:
        state = profile.setdefault("working_memory", {})
        state.setdefault("active_theme", "")
        state.setdefault("conversation_goal", "今の発話を正確に受け止める")
        state.setdefault("user_focus", "")
        state.setdefault("continuing_thought", "")
        state.setdefault("open_questions", [])
        state.setdefault("recent_themes", [])
        state.setdefault("last_revisit_turn", -99)
        state.setdefault("last_updated", 0.0)
        return state

    @staticmethod
    def _prune(state: dict[str, Any], now: float) -> None:
        valid = []
        for item in state.get("open_questions", []):
            if not isinstance(item, dict) or item.get("resolved"):
                continue
            expiry = float(item.get("expiry", now + 1) or now + 1)
            if expiry >= now:
                valid.append(item)
        state["open_questions"] = valid[-5:]

    @staticmethod
    def observe(
        profile: dict[str, Any], text: str, topics: list[str], *, now: float | None = None,
        turn_index: int = 0,
    ) -> dict[str, Any]:
        """Refresh the current focus from explicit user signals only."""
        now = time.time() if now is None else float(now)
        state = WorkingMemory.ensure(profile)
        WorkingMemory._prune(state, now)
        clean = " ".join(str(text or "").split())[:240]
        theme = (topics[-1] if topics else clean[:64]).strip()
        if theme:
            state["active_theme"] = theme
            recent = list(state.get("recent_themes", [])) + [theme]
            state["recent_themes"] = list(dict.fromkeys(recent))[-6:]

        if _QUESTION.search(clean):
            goal = "質問に答えたうえで、必要なら一段だけ考えを広げる"
        elif _ONGOING.search(clean):
            goal = "進行中の話を途切れさせず、次に確認すべき点を残す"
        else:
            goal = "今の発話を受け止め、自分の見方か次の一歩を一つ添える"
        state["conversation_goal"] = goal
        state["user_focus"] = (f"ユーザーは「{theme}」を掘り下げている" if theme else "")

        # Only projects or clearly unfinished matters become open questions.
        # This prevents the assistant from inventing intrusive curiosity about
        # a user's motives or private life.
        concrete_theme = bool(
            theme
            and len(theme) >= 3
            and not _VAGUE_THEME.fullmatch(theme)
            and theme not in {"話", "こと", "内容", "続き", "その後"}
        )
        if concrete_theme and _EXPLICIT_OPEN.search(clean) and not _RESOLVED.search(clean):
            question = f"「{theme}」のその後、今どこまで進んだ？"
            existing = next((item for item in state["open_questions"]
                             if item.get("topic") == theme), None)
            if existing is None:
                state["open_questions"].append({
                    "topic": theme, "question": question, "source": "unfinished_user_topic",
                    "source_excerpt": clean[:140],
                    "created_turn": int(turn_index), "last_mentioned_turn": None,
                    "expiry": now + 30 * 86400, "resolved": False,
                })
            else:
                existing["expiry"] = now + 30 * 86400
                existing["source_excerpt"] = clean[:140]
        if _RESOLVED.search(clean):
            for item in state["open_questions"]:
                if item.get("topic") and item["topic"] in clean:
                    item["resolved"] = True
        state["continuing_thought"] = (
            f"{state['active_theme']}について、{state['conversation_goal']}"
            if state.get("active_theme") else state["conversation_goal"]
        )
        state["last_updated"] = now
        return state

    @staticmethod
    def revisit_candidate(
        profile: dict[str, Any], *, intent: str, momentum: float, turn_index: int,
        topic_shift: float, curiosity: float,
    ) -> dict[str, Any] | None:
        """Choose at most one grounded open thread for a natural return."""
        state = WorkingMemory.ensure(profile)
        if intent in {"question", "correction", "support", "monologue"} or momentum < .52:
            return None
        if int(turn_index) - int(state.get("last_revisit_turn", -99)) < 4:
            return None
        if curiosity < .42 or topic_shift < .34:
            return None
        for item in reversed(state.get("open_questions", [])):
            if int(turn_index) - int(item.get("created_turn", turn_index)) < 4:
                continue
            if not item.get("resolved") and item.get("question"):
                return dict(item)
        return None

    @staticmethod
    def record_response(profile: dict[str, Any], plan: dict[str, Any], reply: str) -> None:
        state = WorkingMemory.ensure(profile)
        continuity = str(plan.get("continuity_question", "") or "")
        if continuity and reply.strip():
            for item in state.get("open_questions", []):
                if item.get("question") == continuity:
                    item["last_mentioned_turn"] = int(plan.get("turn_id", 0))
                    state["last_revisit_turn"] = int(plan.get("turn_id", 0))
                    break
        if reply.strip():
            state["continuing_thought"] = str(plan.get("continuity_focus") or state.get("continuing_thought", ""))[:180]

    @staticmethod
    def snapshot(profile: dict[str, Any]) -> dict[str, Any]:
        state = WorkingMemory.ensure(profile)
        return {
            "active_theme": str(state.get("active_theme", "")),
            "conversation_goal": str(state.get("conversation_goal", "")),
            "user_focus": str(state.get("user_focus", "")),
            "continuing_thought": str(state.get("continuing_thought", "")),
            "open_questions": [
                {
                    "topic": str(item.get("topic", "")),
                    "question": str(item.get("question", "")),
                    "source": str(item.get("source", "")),
                    "source_excerpt": str(item.get("source_excerpt", "")),
                    "created_turn": int(item.get("created_turn", 0) or 0),
                    "last_mentioned_turn": item.get("last_mentioned_turn"),
                }
                for item in state.get("open_questions", []) if not item.get("resolved")
            ][-3:],
        }
