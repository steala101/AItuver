"""Saying the same thing twice, detected by comparing text.

Two separate places produce it, for the same underlying reason — the model has
nothing new and the most salient text in front of it is what it just said:

* a radio segment that opens by restating how the previous one ended;
* a reply that repeats the previous reply almost word for word, which happens
  when the person's turn adds nothing ("マジでそう。") and there is no new
  ground to move to.

Both are mechanical properties of the text, so code decides them rather than
the prompt (第2条).  Asking the model more firmly not to repeat itself does
not work reliably, and every extra sentence of instruction costs latency.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

_SENTENCE_END = re.compile(r"(?<=[。．！!？?])\s*")
_NOISE = re.compile(r"[\s　、。，．,\.!！?？…・「」『』（）()\"'~〜ー]+")


@dataclass(frozen=True, slots=True)
class EchoSource:
    """One piece of text against which a new reply is compared.

    ``assistant`` means a self-repeat and ``user`` means a near-verbatim
    parrot.  They are different conversational failures and intentionally use
    different thresholds.
    """

    text: str
    kind: str = "assistant"


@dataclass(frozen=True, slots=True)
class EchoMatch:
    kind: str
    score: float


@dataclass(frozen=True, slots=True)
class HistoricalEchoResolution:
    """Privacy-safe choice after a historical similarity check.

    Historical text similarity is not an idempotency key.  The delivery ledger
    owns same-turn/SpeechRequest duplicate rejection; this resolver only keeps
    an explicit reply from becoming silent before its first delivery.
    """

    text: str
    resolution: str


def resolve_historical_echo(
    primary: str, regenerated: str, *, regenerated_matches_history: bool = False,
    regenerated_is_safe_fallback: bool = False,
) -> HistoricalEchoResolution:
    """Choose one valid, still-undelivered response without using text as an ID."""
    primary = str(primary or "").strip()
    regenerated = str(regenerated or "").strip()
    if regenerated and regenerated_is_safe_fallback:
        return HistoricalEchoResolution(regenerated, "SAFE_FALLBACK")
    if regenerated and not regenerated_matches_history:
        return HistoricalEchoResolution(regenerated, "REGENERATED")
    if primary:
        return HistoricalEchoResolution(
            primary,
            "ALLOW_HISTORICAL_SIMILARITY" if regenerated_matches_history else "PRIMARY",
        )
    if regenerated:
        return HistoricalEchoResolution(regenerated, "ALLOW_HISTORICAL_SIMILARITY")
    return HistoricalEchoResolution("", "")


def bare(text: str) -> str:
    """Strip punctuation and spacing so wording is compared, not typography."""
    return _NOISE.sub("", str(text or ""))


def sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_END.split(str(text or "").strip()) if part.strip()]


def containment(candidate: str, source: str, *, minimum_chars: int = 8) -> float:
    """How much of ``candidate`` already appears, in order, inside ``source``.

    A symmetric similarity is the wrong measure: the text being compared
    against is often far longer, which drags the ratio down even for a
    verbatim echo.  Observed case scored 0.61 as a similarity and 1.0 as
    containment.

    Short strings return 0: 「うん。」 repeated is phrasing, not repetition.
    """
    left, right = bare(candidate), bare(source)
    if len(left) < max(1, int(minimum_chars)) or not right:
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(left)


def is_echo(
    candidate: str, previous, *, threshold: float = 0.85,
    minimum_chars: int = 8,
) -> bool:
    """Whether this text says what was already said.

    ``previous`` may be one string or several.  There are two ways to say
    nothing new — repeat yourself, or repeat the person you are talking to —
    and both are the same measurement against a different source.
    """
    sources = [previous] if isinstance(previous, str) else list(previous or ())
    return any(
        containment(candidate, str(source or ""), minimum_chars=minimum_chars)
        >= float(threshold)
        for source in sources
    )


class ReplyEchoGuard:
    """Hold the opening of a reply until it is known not to be a repeat.

    Streaming straight to the screen and the speaker means a verbatim repeat
    is already out before anyone can tell.  Buffering the *whole* reply to
    check it would add its full generation time to every turn, which is the
    opposite of what is wanted.  One sentence is enough to decide, and costs
    only display latency: the speech path waits for a complete sentence in
    any case.

    ``feed`` returns ``(verdict, sentences, text)`` where verdict is:

    * ``"holding"`` — still buffering, caller does nothing
    * ``"repeat"``  — this reply restates the previous one; do not say it
    * ``"clear"``   — release ``text`` and ``sentences``, then stop guarding
    """

    def __init__(
        self, previous, segmenter, *, threshold: float = 0.85,
        probe_min_chars: int = 12, max_probe_sentences: int = 2,
        user_threshold: float = 0.96, user_min_chars: int = 16,
    ) -> None:
        #: One or several sources.  The previous reply catches self-repetition;
        #: the person's own words catch parroting, which the prompt used to
        #: forbid in a sentence that the model followed only sometimes.
        raw_sources = [previous] if isinstance(previous, (str, EchoSource)) else list(previous or ())
        self._sources: list[EchoSource] = [
            item if isinstance(item, EchoSource) else EchoSource(str(item or ""))
            for item in raw_sources
        ]
        self._segmenter = segmenter
        self._threshold = float(threshold)
        self._probe_min_chars = max(8, int(probe_min_chars))
        self._max_probe_sentences = max(1, int(max_probe_sentences))
        self._user_threshold = max(float(threshold), float(user_threshold))
        self._user_min_chars = max(self._probe_min_chars, int(user_min_chars))
        self._buffer = ""
        self._pending: list[str] = []
        self._last_match: EchoMatch | None = None

    @property
    def active(self) -> bool:
        return any(bare(item.text) for item in self._sources)

    @property
    def match(self) -> EchoMatch | None:
        """Why the last ``repeat`` verdict was produced (safe for telemetry)."""
        return self._last_match

    def _match(self, candidate: str, *, minimum_chars: int) -> EchoMatch | None:
        best: EchoMatch | None = None
        for source in self._sources:
            kind = "user" if source.kind == "user" else "assistant"
            required_chars = (
                max(minimum_chars, self._user_min_chars)
                if kind == "user" else minimum_chars
            )
            score = containment(
                candidate, source.text, minimum_chars=required_chars,
            )
            threshold = self._user_threshold if kind == "user" else self._threshold
            if score >= threshold and (best is None or score > best.score):
                best = EchoMatch(kind=kind, score=score)
        return best

    def _is_repeat(self, candidate: str, *, minimum_chars: int) -> bool:
        match = self._match(candidate, minimum_chars=minimum_chars)
        if match is None:
            return False
        self._last_match = match
        return True

    def repeats(self, text: str) -> bool:
        """Check a completed retry without mutating its segmenter state."""
        parts = sentences(text)[: self._max_probe_sentences]
        candidates = ["".join(parts), *parts] if parts else [str(text or "")]
        return any(
            self._is_repeat(candidate, minimum_chars=self._probe_min_chars)
            for candidate in candidates
        )

    def feed(self, token: str) -> tuple[str, list[str], str]:
        self._buffer += str(token or "")
        self._pending.extend(self._segmenter.feed(token))
        if not self._pending:
            return "holding", [], ""
        verdict = self._probe()
        if verdict == "repeat":
            return "repeat", [], ""
        if verdict == "holding":
            return "holding", [], ""
        return "clear", list(self._pending), self._buffer

    def _probe(self) -> str:
        """Inspect enough substance to see past a short changed interjection.

        A model often changes only ``あはは、確かに！`` to
        ``あはは、やっぱり！`` and then repeats the long sentence that
        follows.  Releasing the stream after that tiny first sentence makes
        the existing echo guard blind to the actual duplicate.  Hold at most
        one extra sentence only when the opening itself is too short to be a
        meaningful comparison.
        """
        combined = "".join(self._pending)
        if self._is_repeat(combined, minimum_chars=self._probe_min_chars):
            return "repeat"
        if any(
            len(bare(sentence)) >= self._probe_min_chars
            and self._is_repeat(sentence, minimum_chars=self._probe_min_chars)
            for sentence in self._pending
        ):
            return "repeat"
        first_is_tiny = len(bare(self._pending[0])) < self._probe_min_chars
        if first_is_tiny and len(self._pending) < self._max_probe_sentences:
            return "holding"
        return "clear"

    def flush(self) -> tuple[str, list[str], str]:
        """Decide on whatever arrived, for a reply with no sentence end."""
        if not self._buffer.strip():
            return "clear", list(self._pending), self._buffer
        if self._pending and self._probe() == "repeat":
            return "repeat", [], ""
        candidate = "".join(self._pending) if self._pending else self._buffer
        if self._is_repeat(candidate, minimum_chars=self._probe_min_chars):
            return "repeat", [], ""
        return "clear", list(self._pending), self._buffer


def trim_echoed_opening(
    text: str, previous: str, *, threshold: float = 0.85, max_dropped: int = 2,
) -> str:
    """Drop opening sentences that merely restate what was just said.

    Returns ``""`` when the whole thing is an echo — the caller should treat
    that as having nothing to say rather than say nothing.
    """
    parts = sentences(text)
    if not parts or not bare(previous):
        return str(text or "").strip()
    dropped = 0
    while parts and dropped < max(0, int(max_dropped)):
        if containment(parts[0], previous) < threshold:
            break
        parts.pop(0)
        dropped += 1
    if not dropped:
        return str(text or "").strip()
    return "".join(parts).strip()
