"""Conservative learning from the next observable user reaction."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


_POSITIVE = re.compile(r"(?:いいね|面白い|笑った|助かった|分かりやすい|その感じ|好き)")
_LAUGHTER = re.compile(r"(?:笑|ｗ{2,}|w{2,}|はは|ふふ|ウケる)")
_NEGATIVE = re.compile(r"(?:違う|間違|噛み合わ|分かってない|微妙|つまらない|やめて)")
_REPEAT = re.compile(r"(?:同じこと|また同じ|繰り返|さっきと同じ)")
_ACK = re.compile(
    r"^(?:うん+|はい+|そう|そうだね|なるほど|了解|ありがとう|へえ+|ああ)"
    r"[。！!？?、,\s]*$"
)
_QUESTION = re.compile(r"[?？]|(?:どう|なぜ|なんで|何|どこ|いつ|誰)")


@dataclass(frozen=True, slots=True)
class ReactionSignal:
    kind: str
    score: float
    confidence: float
    explicit: bool
    target_move: str
    surface: str
    group: bool

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class ReactionLearner:
    """Maps reactions to tiny bounded move-weight updates.

    Silence and interruption alone are intentionally not treated as dislike:
    either may simply mean turn-taking or a backchannel.
    """

    @staticmethod
    def classify(
        text: str, *, previous_move: str, surface: str, group: bool,
    ) -> ReactionSignal | None:
        value = str(text or "").strip()
        if not value or not previous_move:
            return None
        if _REPEAT.search(value):
            return ReactionSignal(
                "repeat_complaint", .05, .96, True, previous_move, surface, group,
            )
        positive = bool(_POSITIVE.search(value))
        negative = bool(_NEGATIVE.search(value))
        if negative and not positive:
            return ReactionSignal(
                "explicit_negative", .12, .92, True, previous_move, surface, group,
            )
        if positive and not negative:
            return ReactionSignal(
                "explicit_positive", .90, .90, True, previous_move, surface, group,
            )
        if _LAUGHTER.search(value):
            return ReactionSignal(
                "amusement", .82, .78, False, previous_move, surface, group,
            )
        if _ACK.fullmatch(value):
            return ReactionSignal(
                "accepted_closure", .62, .66, False, previous_move, surface, group,
            )
        if _QUESTION.search(value) or len(value) >= 18:
            return ReactionSignal(
                "conversation_continued", .66, .58, False, previous_move, surface, group,
            )
        return None

    @staticmethod
    def updated_weight(old: float, signal: ReactionSignal) -> float:
        alpha = .08 if signal.explicit else .025
        target = .70 + .60 * signal.score
        value = float(old) * (1.0 - alpha) + target * alpha
        return round(max(.70, min(1.30, value)), 4)
