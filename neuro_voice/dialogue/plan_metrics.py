"""Did the plan actually reach the reply?

`ConversationPlanner` decides something every turn — 738 tokens of it — and
nobody has ever checked whether the decision shows up in what gets said.  The
complaint that started this ("毎回似た構成", "一問一答") is compatible with two
very different faults, and they need opposite fixes:

* the planner picks the same thing every turn — a planner problem;
* the planner varies but the reply does not follow it — a transmission
  problem, and adding a second planner would change nothing.

So this module measures **Plan Adherence**: for each field of the plan that
can be checked against the finished text by rule, whether the reply honoured
it.  Fields that cannot be checked without understanding meaning are reported
as ``unverifiable`` rather than quietly counted as met — and both readings are
published, so a high score over two checked fields cannot be mistaken for a
high score overall.

No LLM call, no IO, no stored transcript: openings and endings are kept as
short hashes because 第12条 forbids copying conversation text into logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import blake2s
import re
import statistics

from neuro_voice.dialogue.echo import bare, sentences

_QUESTION = re.compile(r"[?？]")
#: The openings the critic already flags as canned.
_CANNED_OPENING = re.compile(
    r"^(?:なるほど|そうだね|そうなんだ|確かに|たしかに|わかる|分かる|それは)"
)
_PAST_REFERENCE = re.compile(r"(?:前に|以前|この前|そういえば|さっき|昔)")
_RECONSIDER = re.compile(r"(?:いや待って|あれ、|っていうか|訂正|やっぱり|いやでも)")
_ASSERTION = re.compile(r"(?:と思う|だと思う|気がする|じゃないかな|べき|はず)")

MET = "met"
MISSED = "missed"
UNVERIFIABLE = "unverifiable"


def _digest(text: str) -> str:
    """A short, stable key for repetition checks that stores no words."""
    value = bare(text)
    if not value:
        return ""
    return blake2s(value.encode("utf-8"), digest_size=2).hexdigest()


def opening_key(reply: str, *, chars: int = 12) -> str:
    parts = sentences(reply)
    return _digest((parts[0] if parts else str(reply or ""))[:chars])


def ending_key(reply: str, *, chars: int = 10) -> str:
    parts = sentences(reply)
    return _digest((parts[-1] if parts else str(reply or ""))[-chars:])


def count_questions(reply: str) -> int:
    return len(_QUESTION.findall(str(reply or "")))


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    verdict: str
    detail: str = ""


@dataclass(slots=True)
class Adherence:
    """One turn's verdict, published two ways on purpose."""

    checks: list[Check] = field(default_factory=list)

    def _count(self, verdict: str) -> int:
        return sum(1 for check in self.checks if check.verdict == verdict)

    @property
    def met(self) -> int:
        return self._count(MET)

    @property
    def missed(self) -> int:
        return self._count(MISSED)

    @property
    def unverifiable(self) -> int:
        return self._count(UNVERIFIABLE)

    @property
    def checked(self) -> int:
        return self.met + self.missed

    @property
    def score(self) -> float:
        """Of what could be checked, how much held.  Reads generously."""
        return round(self.met / self.checked, 3) if self.checked else 0.0

    @property
    def strict_score(self) -> float:
        """Unverifiable counted as unmet.  Reads harshly."""
        total = len(self.checks)
        return round(self.met / total, 3) if total else 0.0

    @property
    def checkable_ratio(self) -> float:
        """How much of the plan is even testable by rule."""
        total = len(self.checks)
        return round(self.checked / total, 3) if total else 0.0

    @property
    def violations(self) -> list[str]:
        return [check.name for check in self.checks if check.verdict == MISSED]

    def snapshot(self) -> dict:
        return {
            "adherence": self.score,
            "strict": self.strict_score,
            "checkable_ratio": self.checkable_ratio,
            "checked": self.checked,
            "unverifiable": self.unverifiable,
            "violations": self.violations,
        }


#: Length bands, in characters, calibrated against real replies.
#:
#: The first attempt used short=(0,70) / long=(110,…) and reported 6 violations
#: out of 8 — but the replies were 61-135 characters, so a "short" reply that
#: ran one character over and a "long" one four characters under were both
#: counted as the model ignoring its plan.  The ruler was wrong, not the model.
#:
#: Spoken Japanese runs roughly 25-35 characters per sentence, and the persona
#: asks for 1-3 sentences, so these bands follow sentence counts rather than a
#: guess.  They overlap on purpose: landing next to the boundary is not
#: disobedience, and a measurement that cries wolf is worse than none.
_LENGTH_BANDS = {
    "short": (0, 95),        # おおむね1〜3文
    "medium": (45, 220),     # おおむね2〜6文
    "long": (100, 10_000),   # おおむね4文以上
}

#: Only the shapes whose defining feature survives into the text.
_SHAPE_CHECKS = {
    "concise_ping": lambda r, q: (
        MET if len(bare(r)) <= 70 else MISSED, "短い一言で返す"
    ),
    "question_hypothesis": lambda r, q: (
        MET if q >= 1 else MISSED, "答えやすい追加質問で終える"
    ),
    "direct_take": lambda r, q: (
        MISSED if _CANNED_OPENING.match(r.strip()) else MET, "定型の相槌で始めない"
    ),
    "memory_bridge": lambda r, q: (
        MET if _PAST_REFERENCE.search(r) else MISSED, "過去の話へつなぐ"
    ),
    "self_reconsider": lambda r, q: (
        MET if _RECONSIDER.search(r) else MISSED, "一度考え直す"
    ),
    "gentle_support": lambda r, q: (
        MISSED if r.rstrip().endswith(("?", "？")) else MET, "質問で締めない"
    ),
    # -- 認知カーネル(bridge.py)が渡す shape。計測できないと unverifiable に
    #    落ち、反映されたかどうかを事後に読めなくなる。
    "clarify_reference": lambda r, q: (
        MET if q >= 1 else MISSED, "確認の質問を一つする"
    ),
    "acknowledge_then_answer": lambda r, q: (
        MISSED if _CANNED_OPENING.match(r.strip()) else MET,
        "定型の相槌ではなく気持ちの受け止めから入る",
    ),
    "urgent_warning": lambda r, q: (
        MET if len(sentences(r)) <= 2 and len(bare(r)) <= 90 else MISSED,
        "警告は短く。詳細は聞かれてから",
    ),
    "resume": lambda r, q: (
        MISSED if r.rstrip().endswith(("?", "？")) else MET,
        "続きを言い切る。質問で戻さない",
    ),
    # 終了シグナルへの短い了承。**通常回答へ膨らんでいないか**を測る。
    "brief_ack": lambda r, q: (
        MET if len(sentences(r)) <= 1 and q == 0 and len(bare(r)) <= 30 else MISSED,
        "1文以内・質問なしで終える",
    ),
}

_STYLE_CHECKS = {
    "concise": lambda r, q: (
        MET if len(bare(r)) <= 90 else MISSED, "一息で返せる短文"
    ),
    "curious": lambda r, q: (
        MET if q >= 1 else MISSED, "具体的な一点へ興味を向ける"
    ),
    "opinionated": lambda r, q: (
        MET if _ASSERTION.search(r) else MISSED, "理由のある自分の見解"
    ),
}


def _get(plan, name, default=None):
    """Read a plan field.

    The plan is a `ConversationPlan` where it is built and a plain dict by the
    time it reaches the commit site (`dict(generation["last_plan"])`), so both
    have to work.  Assuming one shape here would have made every measurement
    silently empty.
    """
    if isinstance(plan, dict):
        value = plan.get(name, default)
    else:
        value = getattr(plan, name, default)
    return default if value is None and default is not None else value


def evaluate(plan, reply: str) -> Adherence:
    """Compare one finished reply against the plan that produced it."""
    text = str(reply or "").strip()
    checks: list[Check] = []
    if not text:
        return Adherence(checks=[Check("reply", UNVERIFIABLE, "発話なし")])
    questions = count_questions(text)

    ask = _get(plan, "ask_follow_up")
    if ask is not None:
        wanted = bool(ask)
        got = questions >= 1
        checks.append(Check(
            "ask_follow_up", MET if wanted == got else MISSED,
            f"計画={'質問する' if wanted else '質問しない'} / 実際={questions}個",
        ))

    length = str(_get(plan, "target_length", "") or "")
    band = _LENGTH_BANDS.get(length)
    if band:
        size = len(bare(text))
        checks.append(Check(
            "target_length", MET if band[0] <= size <= band[1] else MISSED,
            f"計画={length} / 実際={size}文字",
        ))
    elif length:
        checks.append(Check("target_length", UNVERIFIABLE, f"未知の帯={length}"))

    moves = _get(plan, "minimum_moves")
    if isinstance(moves, int) and moves > 0:
        count = len(sentences(text))
        checks.append(Check(
            "minimum_moves", MET if count >= moves else MISSED,
            f"計画>={moves} / 実際={count}文（文数での近似）",
        ))

    shape = str(_get(plan, "response_shape", "") or "")
    if shape:
        rule = _SHAPE_CHECKS.get(shape)
        if rule is None:
            checks.append(Check("response_shape", UNVERIFIABLE, f"{shape}は規則で判定できない"))
        else:
            verdict, detail = rule(text, questions)
            checks.append(Check("response_shape", verdict, f"{shape}: {detail}"))

    style = str(_get(plan, "primary_style", "") or "")
    if style:
        rule = _STYLE_CHECKS.get(style)
        if rule is None:
            checks.append(Check("primary_style", UNVERIFIABLE, f"{style}は規則で判定できない"))
        else:
            verdict, detail = rule(text, questions)
            checks.append(Check("primary_style", verdict, f"{style}: {detail}"))

    features = _get(plan, "selected_features") or ()
    if features:
        checks.append(Check(
            "selected_features", UNVERIFIABLE, f"{len(features)}件は規則で判定できない",
        ))
    return Adherence(checks=checks)


@dataclass(slots=True)
class TurnRecord:
    """What one turn contributes to the diversity picture.  No text."""

    turn: int = 0
    planner_enabled: bool = True
    shape: str = ""
    style: str = ""
    #: What the plan asked for.  Without these, a violation cannot be read
    #: afterwards — the first run reported six length violations and there was
    #: no way to tell whether the plan had asked for short or long.
    planned_length: str = ""
    planned_question: bool | None = None
    questions: int = 0
    reply_chars: int = 0
    opening: str = ""
    ending: str = ""
    memory_used: int = 0
    adherence: Adherence | None = None

    def snapshot(self) -> dict:
        value = {
            "turn": self.turn, "planner": self.planner_enabled,
            "shape": self.shape, "style": self.style,
            "planned_length": self.planned_length,
            "planned_question": self.planned_question,
            "questions": self.questions, "reply_chars": self.reply_chars,
            "opening_hash": self.opening, "ending_hash": self.ending,
            "memory_used": self.memory_used,
        }
        if self.adherence is not None:
            value.update(self.adherence.snapshot())
        return value


def record_for(plan, reply: str, *, turn: int = 0, planner_enabled: bool = True,
               memory_used: int = 0) -> TurnRecord:
    text = str(reply or "")
    asked = _get(plan, "ask_follow_up")
    return TurnRecord(
        turn=int(turn),
        planner_enabled=bool(planner_enabled),
        shape=str(_get(plan, "response_shape", "") or ""),
        style=str(_get(plan, "primary_style", "") or ""),
        planned_length=str(_get(plan, "target_length", "") or ""),
        planned_question=None if asked is None else bool(asked),
        questions=count_questions(text),
        reply_chars=len(bare(text)),
        opening=opening_key(text),
        ending=ending_key(text),
        memory_used=int(memory_used),
        adherence=evaluate(plan, text),
    )


class DiversityWindow:
    """Rolling view of the last N turns: what repeats, and how much.

    This is the measurement half of the anti-repetition work.  Deciding what
    to *do* about a repeat belongs elsewhere, and should be a penalty rather
    than a ban — forbidding the most natural phrasing produces an assistant
    that paraphrases itself into awkwardness.
    """

    def __init__(self, size: int = 8) -> None:
        self.size = max(2, int(size))
        self._turns: list[TurnRecord] = []

    def record(self, turn: TurnRecord) -> None:
        self._turns.append(turn)
        del self._turns[:-self.size]

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)

    def _streak(self, attribute: str) -> int:
        """How many turns in a row ended with the same value."""
        values = [getattr(item, attribute) for item in self._turns if getattr(item, attribute)]
        if not values:
            return 0
        streak, last = 1, values[-1]
        for value in reversed(values[:-1]):
            if value != last:
                break
            streak += 1
        return streak

    def _repeat_ratio(self, attribute: str) -> float:
        values = [getattr(item, attribute) for item in self._turns if getattr(item, attribute)]
        if len(values) < 2:
            return 0.0
        return round(1 - len(set(values)) / len(values), 3)

    def consecutive_questions(self) -> int:
        streak = 0
        for item in reversed(self._turns):
            if item.questions <= 0:
                break
            streak += 1
        return streak

    def metrics(self) -> dict:
        lengths = [item.reply_chars for item in self._turns] or [0]
        scored = [item.adherence for item in self._turns if item.adherence is not None]
        checked = [a for a in scored if a.checked]
        return {
            "window": len(self._turns),
            "consecutive_questions": self.consecutive_questions(),
            "question_ratio": round(
                sum(1 for item in self._turns if item.questions) / max(1, len(self._turns)), 3,
            ),
            "opening_repeat_ratio": self._repeat_ratio("opening"),
            "ending_repeat_ratio": self._repeat_ratio("ending"),
            "shape_streak": self._streak("shape"),
            "style_streak": self._streak("style"),
            "shape_distribution": self._distribution("shape"),
            "style_distribution": self._distribution("style"),
            "planned_length_distribution": self._distribution("planned_length"),
            "length_mean": round(statistics.fmean(lengths), 1),
            "length_stdev": round(statistics.pstdev(lengths), 1),
            "memory_use_ratio": round(
                sum(1 for item in self._turns if item.memory_used) / max(1, len(self._turns)), 3,
            ),
            "adherence_mean": round(
                statistics.fmean([a.score for a in checked]), 3,
            ) if checked else None,
            "strict_mean": round(
                statistics.fmean([a.strict_score for a in scored]), 3,
            ) if scored else None,
            "checkable_ratio_mean": round(
                statistics.fmean([a.checkable_ratio for a in scored]), 3,
            ) if scored else None,
        }

    def _distribution(self, attribute: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self._turns:
            value = getattr(item, attribute)
            if value:
                counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
