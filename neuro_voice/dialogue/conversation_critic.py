"""Fast post-turn critique that influences the next conversation plan."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class ConversationCritique:
    diversity_score: float
    issues: tuple[str, ...]
    opening: str
    ending: str
    structure: str
    consecutive_question: bool
    too_similar: bool
    passed: bool = True
    scores: dict[str, float | None] = field(default_factory=dict)
    measurement_status: dict[str, str] = field(default_factory=dict)
    detected_problems: tuple[str, ...] = ()
    required_fixes: tuple[str, ...] = ()
    optional_improvements: tuple[str, ...] = ()
    regenerate: bool = False

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class ConversationCritic:
    _CANNED = re.compile(r"^(?:なるほど|そうだね|そうなんだ|確かに|わかる|それは)[、,。 ]*")
    _EMPATHY = re.compile(r"^(?:なるほど|そうだね|わかる|それは|つらかった|大変だった)")
    _EXPLAIN = re.compile(r"(?:というのは|なぜなら|だから|つまり|例えば)")
    _OWN_VIEW = re.compile(r"(?:私は|僕は|私なら|僕なら|と思う|と感じる|と見て|気になる|面白いのは|ただ、|一方で)")
    _UNCERTAINTY = re.compile(r"(?:たぶん|可能性|仮説|推測|断定でき|確信|かもしれ)")

    @staticmethod
    def _edge(text: str) -> tuple[str, str]:
        clean = re.sub(r"\s+", "", text)
        opening = re.split(r"[、。！？!?]", clean, maxsplit=1)[0][:18]
        chunks = [x for x in re.split(r"[。！？!?]", clean) if x]
        ending = chunks[-1][-18:] if chunks else ""
        return opening, ending

    def evaluate(self, reply: str, recent_replies: list[str]) -> ConversationCritique:
        text = (reply or "").strip()
        opening, ending = self._edge(text)
        question = text.endswith(("?", "？"))
        previous = recent_replies[-1] if recent_replies else ""
        previous_question = previous.strip().endswith(("?", "？"))
        max_similarity = max(
            (SequenceMatcher(None, text[:240], item[:240]).ratio() for item in recent_replies[-4:] if item),
            default=0.0,
        )
        previous_opening, previous_ending = self._edge(previous) if previous else ("", "")
        empathy = bool(self._EMPATHY.search(text))
        explain = bool(self._EXPLAIN.search(text))
        if empathy and explain and question:
            structure = "empathy_explain_question"
        elif question:
            structure = "ends_question"
        elif len(text) < 35:
            structure = "brief"
        elif explain:
            structure = "explanation"
        else:
            structure = "freeform"
        issues: list[str] = []
        if self._CANNED.search(text):
            issues.append("canned_opening")
        if opening and opening == previous_opening:
            issues.append("repeated_opening")
        if ending and ending == previous_ending:
            issues.append("repeated_ending")
        if question and previous_question:
            issues.append("consecutive_question")
        if max_similarity >= .72:
            issues.append("too_similar")
        if structure == "empathy_explain_question":
            issues.append("template_structure")
        question_count = text.count("？") + text.count("?")
        if question_count >= 2:
            issues.append("question_overuse")
        if len(text) > 520:
            issues.append("too_verbose")
        if len(text) >= 55 and not self._OWN_VIEW.search(text) and not explain:
            issues.append("weak_ai_perspective")
        if re.search(r"(?:絶対|間違いなく|確実に)", text) and not self._UNCERTAINTY.search(text):
            issues.append("overconfident_wording")
        score = max(0.0, 1.0 - len(set(issues)) * .15 - max(0.0, max_similarity - .55) * .7)
        unique = set(issues)
        scores: dict[str, float | None] = {
            "structure_diversity": round(0.35 if "template_structure" in unique else 1.0, 3),
            "opening_variety": round(0.35 if {"canned_opening", "repeated_opening"} & unique else 1.0, 3),
            "ending_variety": round(0.4 if "repeated_ending" in unique else 1.0, 3),
            "question_balance": round(0.25 if {"consecutive_question", "question_overuse"} & unique else 1.0, 3),
            "novelty": round(max(0.0, 1.0 - max_similarity), 3),
            "ai_perspective": round(0.4 if "weak_ai_perspective" in unique else 1.0, 3),
            "verbosity_fit": round(0.3 if "too_verbose" in unique else 1.0, 3),
            "factual_caution": round(0.4 if "overconfident_wording" in unique else 1.0, 3),
            "goal_focus": None,
            "personality_continuity": None,
            "memory_naturalness": None,
            "humor_fit": None,
            "emotional_fit": None,
            "topic_scope": None,
        }
        measurement_status = {
            key: ("HEURISTIC" if value is not None else "NOT_MEASURED")
            for key, value in scores.items()
        }
        required: list[str] = []
        optional: list[str] = []
        if "consecutive_question" in unique or "question_overuse" in unique:
            required.append("次は質問で丸投げせず、先に見解か具体案を返す")
        if "template_structure" in unique:
            required.append("共感→説明→質問の固定構造を崩す")
        if "overconfident_wording" in unique:
            required.append("推測と事実を分離する")
        if "too_similar" in unique:
            optional.append("異なる観点・文長・リズムを選ぶ")
        if "weak_ai_perspective" in unique:
            optional.append("AI側の具体的な見方を一つ加える")
        passed = not any(item in unique for item in {
            "template_structure", "consecutive_question", "question_overuse",
            "too_similar", "overconfident_wording",
        })
        return ConversationCritique(
            round(score, 3), tuple(dict.fromkeys(issues)), opening, ending, structure,
            question and previous_question, max_similarity >= .72, passed, scores,
            measurement_status,
            tuple(dict.fromkeys(issues)), tuple(required), tuple(optional), False,
        )

    @staticmethod
    def feedback_prompt(critique: dict[str, Any]) -> str:
        issues = set(critique.get("issues", []))
        guidance = []
        if "canned_opening" in issues or "repeated_opening" in issues:
            guidance.append("前回と同じ相槌から始めず、本題・感情・意外な具体点のどれかから入る")
        if "consecutive_question" in issues:
            guidance.append("今回は質問で締めず、見解や余韻で返す")
        if "template_structure" in issues:
            guidance.append("共感→説明→質問の並びを崩す")
        if "too_similar" in issues:
            guidance.append("前回と違う観点・長さ・文の運びを選ぶ")
        if "question_overuse" in issues:
            guidance.append("質問は一つまでにし、AI側の考えを先に話す")
        if "weak_ai_perspective" in issues:
            guidance.append("ユーザーの言い換えで終わらず、具体的な自分の見方を一つ含める")
        if "too_verbose" in issues:
            guidance.append("音声会話として要点を絞る")
        if "overconfident_wording" in issues:
            guidance.append("事実と推測を分け、確度を自然に示す")
        if not guidance:
            return ""
        return "【Conversation Criticから次ターンへのフィードバック】" + "。".join(guidance) + "。"
