"""When the assistant is genuinely unsure, and may show it.

Pausing, trailing off and thinking aloud are easy to fake and hollow when
faked: a speaker who says 「うーん……」 while perfectly certain is performing,
not hesitating.  第1条 forbids exactly that — knowledge, memory and ability
must not be misrepresented to look more human.

So this module does not decide *how* to hesitate.  It answers one question
honestly: **is there something about this turn that the assistant does not
actually know?**  The signals are ones the system already computes for other
reasons — a doubtful transcript, an unresolved 「それ」, a memory it cannot
find, a question it has no grounds to answer.  Only when one of those is
present does the surface layer receive permission to let it show, and the
model — which is the only part that knows *which clause* is the uncertain
one — decides where.

Pure state: no LLM call, no IO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class UncertaintyKind(StrEnum):
    #: The words themselves are in doubt (ASR).
    MISHEARD = "MISHEARD"
    #: 「それ」「さっきの」 could not be tied to anything concrete.
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    #: Asked about something remembered, and the recall is thin.
    THIN_MEMORY = "THIN_MEMORY"
    #: Asked a factual question with no grounds to answer it.
    UNGROUNDED_FACT = "UNGROUNDED_FACT"
    #: Asked for a judgement where the assistant genuinely has no settled view.
    UNSETTLED_VIEW = "UNSETTLED_VIEW"


#: How each kind reads out loud, for the permission text.  Deliberately
#: descriptions of the *state*, not instructions for a performance.
_DESCRIPTION = {
    UncertaintyKind.MISHEARD: "相手の言葉を正しく聞き取れた自信がない",
    UncertaintyKind.AMBIGUOUS_REFERENCE: "指しているものを特定できていない",
    UncertaintyKind.THIN_MEMORY: "思い出そうとしているが、はっきりしない",
    UncertaintyKind.UNGROUNDED_FACT: "答えの根拠を持っていない",
    UncertaintyKind.UNSETTLED_VIEW: "自分の中でも意見が定まっていない",
}


@dataclass(frozen=True, slots=True)
class UncertaintySignal:
    kind: UncertaintyKind
    #: 0..1.  Below ``Assessment.floor`` it is noise, not doubt.
    strength: float = 0.0
    #: What specifically is uncertain, in the person's own words where
    #: possible.  Empty is fine; it just makes the permission vaguer.
    subject: str = ""

    @property
    def description(self) -> str:
        return _DESCRIPTION.get(self.kind, "")


@dataclass(slots=True)
class Assessment:
    """What is honestly unclear this turn, and how much may show."""

    signals: list[UncertaintySignal] = field(default_factory=list)
    #: How many hesitation gestures the turn may carry.  Zero means none.
    allowance: int = 0
    #: Why the allowance is what it is, for the log.
    reason: str = "certain"

    @property
    def uncertain(self) -> bool:
        return bool(self.signals) and self.allowance > 0

    @property
    def strongest(self) -> UncertaintySignal | None:
        return max(self.signals, key=lambda s: s.strength, default=None)

    def snapshot(self) -> dict:
        return {
            "allowance": self.allowance,
            "reason": self.reason,
            "signals": [
                {"kind": str(s.kind), "strength": round(s.strength, 3), "subject": s.subject}
                for s in self.signals
            ],
        }


#: Below this a signal is measurement noise rather than doubt.
FLOOR = 0.35


def _signal(kind: UncertaintyKind, strength: float, subject: str = "") -> UncertaintySignal:
    return UncertaintySignal(
        kind=kind,
        strength=max(0.0, min(1.0, float(strength))),
        subject=" ".join(str(subject or "").split())[:40],
    )


def assess(
    *,
    transcript_confidence: float = 1.0,
    doubtful_words: object = (),
    reference_unresolved: bool = False,
    reference_subject: str = "",
    memory_requested: bool = False,
    memory_hits: int = 0,
    factual_question: bool = False,
    has_grounds: bool = True,
    opinion_requested: bool = False,
    settled_view: bool = True,
    recent_gestures: int = 0,
    floor: float = FLOOR,
    max_allowance: int = 2,
) -> Assessment:
    """Collect what is genuinely unclear about this turn.

    Every argument is something the pipeline already computes for another purpose:
    word confidences from the STT repair pass, the kernel's reference
    resolution, how many memories came back, whether a search was warranted.
    Nothing here is invented to justify a performance.

    ``recent_gestures`` damps the allowance: a person who hesitates on every
    single sentence is not thoughtful, they are broken.
    """
    signals: list[UncertaintySignal] = []

    confidence = max(0.0, min(1.0, float(transcript_confidence)))
    words = [str(w).strip() for w in (doubtful_words or ()) if str(w).strip()]
    if confidence < 0.75 or words:
        signals.append(_signal(
            UncertaintyKind.MISHEARD,
            max(1.0 - confidence, 0.5 if words else 0.0),
            words[0] if words else "",
        ))

    if reference_unresolved:
        signals.append(_signal(
            UncertaintyKind.AMBIGUOUS_REFERENCE, 0.8, reference_subject,
        ))

    if memory_requested and int(memory_hits) <= 0:
        signals.append(_signal(UncertaintyKind.THIN_MEMORY, 0.7))
    elif memory_requested and int(memory_hits) == 1:
        signals.append(_signal(UncertaintyKind.THIN_MEMORY, 0.45))

    if factual_question and not has_grounds:
        signals.append(_signal(UncertaintyKind.UNGROUNDED_FACT, 0.75))

    if opinion_requested and not settled_view:
        signals.append(_signal(UncertaintyKind.UNSETTLED_VIEW, 0.6))

    real = [s for s in signals if s.strength >= float(floor)]
    if not real:
        return Assessment(signals=[], allowance=0, reason="certain")

    # One gesture is the norm.  Two only when the turn is doubtful in more
    # than one way at once, which is rare and genuinely worth hearing.
    allowance = 1
    if len(real) >= 2 and max(s.strength for s in real) >= 0.7:
        allowance = 2
    allowance = min(allowance, max(0, int(max_allowance)))

    damped = max(0, allowance - max(0, int(recent_gestures) - 1))
    if damped < allowance:
        return Assessment(
            signals=real, allowance=damped,
            reason=f"damped_recent_gestures={recent_gestures}",
        )
    return Assessment(signals=real, allowance=allowance, reason="uncertain")


def permission_block(assessment: Assessment) -> str:
    """The turn-scoped permission handed to the surface layer.

    Phrased as a description of the assistant's own state rather than a stage
    direction.  A rule like 「たまに『えっと』と言え」 produces a tic; naming what
    is actually unclear lets the model put the hesitation where the difficulty
    is, which is the only place it belongs.
    """
    if not assessment.uncertain:
        # Saying nothing would let the previous turn's permission linger in
        # the model's head, so the certain case is stated explicitly.
        return (
            "【今回の確信度】今回は迷っている点がない。"
            "言い淀み、ためらいの間、考え込む素振りを入れない。"
            "分かっていることを分かっている調子で話す。"
        )
    strongest = assessment.strongest
    lines = [
        "【今回の確信度】次の点について、実際にはっきりしていない:",
    ]
    for signal in assessment.signals:
        subject = f"（{signal.subject}）" if signal.subject else ""
        lines.append(f"- {signal.description}{subject}")
    lines.append(
        f"この迷いが実際にある場所に限り、言い淀み・少しの間・考えながら話す言い方を"
        f"最大{assessment.allowance}回まで使ってよい。"
    )
    lines.append(
        "文の頭へ機械的に付けない。迷っていない部分ははっきり話す。"
        "間を置くなら「……」で書く。演技として全体を曖昧にしない。"
    )
    if strongest is not None and strongest.kind is UncertaintyKind.MISHEARD:
        lines.append(
            "聞き取りが怪しい時は、曖昧なまま話を進めるより、"
            "自然な形で一度だけ確かめてよい。"
        )
    return "\n".join(lines)
