"""Dialogue-act recognition used to constrain response strategy, not wording."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueAct:
    name: str
    strategy: str


class DialogueActPlanner:
    def classify(self, text: str) -> DialogueAct:
        normalized = (text or "").lower()
        if any(word in normalized for word in (
            "違う", "修正", "訂正", "それじゃない", "っていう意味", "という意味",
        )):
            return DialogueAct("correction_acceptance", "accept the correction, restate the changed understanding, then answer")
        if "?" in normalized or "？" in normalized or any(word in normalized for word in ("教えて", "どう", "なぜ")):
            return DialogueAct("question", "answer directly before adding optional context")
        if any(word in normalized for word in ("つらい", "疲れ", "困っ", "不安")):
            return DialogueAct("support", "acknowledge the feeling briefly, avoid assumptions, offer a practical next step")
        return DialogueAct("statement", "respond to the content naturally; do not add a canned acknowledgement")
