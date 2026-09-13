"""Durable, conservative per-user facts and interaction preferences."""
from __future__ import annotations

import re
import time
from typing import Any


_STOP = "。！？!?、,，\n"


class UserModel:
    """Keeps direct user statements separate from conversation history.

    Extraction is deliberately conservative: it only promotes explicit,
    stable-looking statements.  Everything else remains ordinary dialogue.
    """

    @staticmethod
    def ensure(profile: dict[str, Any]) -> dict[str, Any]:
        model = profile.setdefault("model", {})
        facts = model.setdefault("facts", {})
        facts.setdefault("name", "")
        for key in ("interests", "current_projects", "preferences"):
            facts.setdefault(key, [])
        policy = model.setdefault("interaction_policy", {})
        policy.setdefault("response_length", "medium")
        policy.setdefault("technical_depth", "medium")
        policy.setdefault("ask_confirmation_after_steps", False)
        policy.setdefault("avoid_repeating_known_setup", True)
        policy.setdefault("preferred_output", "conversation")
        policy.setdefault("current_goal", "")
        return model

    @staticmethod
    def _add(items: list[str], value: str, *, limit: int = 12) -> None:
        value = value.strip(" \t「」『』")[:80]
        if len(value) < 2 or value in items:
            return
        items.append(value)
        del items[:-limit]

    @classmethod
    def observe(cls, profile: dict[str, Any], text: str, *, now: float | None = None) -> dict[str, Any]:
        model = cls.ensure(profile)
        facts = model["facts"]
        policy = model["interaction_policy"]
        text = str(text or "").strip()
        if not text:
            return model

        name = re.search(r"(?:私|僕|俺)(?:の)?名前(?:は|って)\s*([^" + _STOP + r"]{2,24})", text)
        if name:
            facts["name"] = name.group(1).strip()
        for match in re.finditer(r"(?:(?:私|僕|俺)(?:は|が)\s*)?([^" + _STOP + r"]{2,40}?)(?:が|は)(?:好き|好きです|好きだ)", text):
            cls._add(facts["interests"], match.group(1))
        for match in re.finditer(r"([^" + _STOP + r"]{2,48})(?:を)?(?:作って(?:いる|います)?|開発して(?:いる|います)?|実装して(?:いる|います)?)", text):
            cls._add(facts["current_projects"], match.group(1))
        for match in re.finditer(r"([^" + _STOP + r"]{2,40})(?:がいい|を好む|のほうが好き)", text):
            cls._add(facts["preferences"], match.group(1))

        # Persist only an explicit *general* preference.  A single
        # 「今回は詳しく教えて」 is a turn instruction, not a lifelong user
        # trait; the ConversationPlanner handles that turn directly.
        general_length_preference = re.search(
            r"(?:今後|これから|いつも|普段|基本的に|返答(?:は|を)|回答(?:は|を)|"
            r"説明(?:は|を)|話すとき(?:は)?)",
            text,
        )
        if general_length_preference and re.search(r"(?:短め|簡潔|手短|要点だけ)", text):
            policy["response_length"] = "short"
        elif general_length_preference and re.search(r"(?:詳しく|丁寧に|深掘り|長め)", text):
            policy["response_length"] = "long"
        if re.search(r"(?:コード|設計|実装|API|ログ|スタックトレース|技術)", text, re.I):
            policy["technical_depth"] = "high"
            policy["preferred_output"] = "implementation"
        if re.search(r"(?:確認してから|一つずつ確認|承認して)", text):
            policy["ask_confirmation_after_steps"] = True
        goal = re.search(r"([^" + _STOP + r"]{3,80})(?:を)?(?:実装したい|改善したい|作りたい)", text)
        if goal:
            policy["current_goal"] = goal.group(1).strip()
        model["updated_at"] = time.time() if now is None else float(now)
        return model

    @classmethod
    def prompt_context(cls, profile: dict[str, Any]) -> str:
        model = cls.ensure(profile)
        facts = model["facts"]
        policy = model["interaction_policy"]
        fact_parts = []
        if facts.get("name"):
            fact_parts.append(f"name={facts['name']}")
        for key in ("interests", "current_projects", "preferences"):
            if facts.get(key):
                fact_parts.append(f"{key}={', '.join(facts[key][-4:])}")
        facts_text = "; ".join(fact_parts) or "no confirmed durable facts"
        return (
            f"Confirmed User Facts: {facts_text}.\n"
            "Interaction Policy: "
            f"length={policy['response_length']}; depth={policy['technical_depth']}; "
            f"confirm_steps={bool(policy['ask_confirmation_after_steps'])}; "
            f"output={policy['preferred_output']}; goal={policy['current_goal'] or 'unknown'}.\n"
            "未確認の推測をユーザー事実にしない。"
        )
