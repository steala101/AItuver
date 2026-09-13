"""Turn written hesitation into heard hesitation.

A speech synthesiser reads 「うーん……そうだなあ」 at the same pace as everything
else, so a written pause is not a heard one.  This module reads the final text
— ordinary Japanese, no internal markers — and works out where real silence
belongs and which part should slow down.

Two constraints shaped it:

* **No tags.**  Whatever the model writes goes to the screen as well as the
  speaker, so the carrier has to be punctuation a person would actually type
  (第1条 1.4: internal labels never reach surface or TTS).  ``……`` is that
  carrier; it needs no stripping because it is already Japanese.
* **Nothing invented.**  Silence is inserted only where the text asks for it.
  This module never adds a pause to text that reads fluently — that decision
  belongs upstream, where it is known whether the assistant is actually
  unsure (see ``dialogue/hesitation.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
import re

#: 「……」「…」「‥」 and long runs of dots.  Two ellipsis characters read as a
#: longer beat than one, which is how people write it, so the length matters.
_ELLIPSIS = re.compile(r"[…‥]{1,4}|\.{3,6}")
#: Hesitation openers.  Recognised, never inserted: this list decides how
#: something already written is *delivered*, not whether it appears.
#
# Two shapes, because a bare 「あ」 or 「そうだね」 is not hesitation:
# 「ありがとう」 opens with あ, and 「そうだね、それは面白い」 is agreement.  Short
# fillers therefore have to stand alone (followed by punctuation or nothing),
# while the unambiguous phrases do not need the boundary.
_FILLER = re.compile(
    r"^(?:"
    r"(?:う[ーぅ]?ん|え[ーぇ]?[とっ]と?|あ[ーぁ]|そうだ(?:な[ぁあ]|ね[ぇえ])|ん[ーん])"
    r"(?=[、,。．.…‥]|$)"
    r"|なんて言えば|なんというか|どう言えば"
    r")"
)
#: A trailing 「けど」「かな」 leaves the thought open; a short beat after it is
#: what makes it sound considered rather than clipped.
_OPEN_ENDING = re.compile(r"(?:けど|けれど|かな|かなあ|かも|気がする|んだよね)[。．.]?$")


@dataclass(frozen=True, slots=True)
class SpokenPiece:
    """One synthesis job plus the silence that follows it."""

    text: str
    pause_after_ms: int = 0
    #: Multiplies the configured speaking rate.  Below 1.0 is slower.
    speed_scale: float = 1.0

    @property
    def speaks(self) -> bool:
        return bool(self.text.strip())


def _pause_for(ellipsis: str, *, unit_ms: int, max_ms: int) -> int:
    """Longer written pauses read as longer silences, up to a ceiling."""
    dots = len(ellipsis) if ellipsis[0] in "…‥" else max(1, len(ellipsis) // 3)
    return int(min(max_ms, unit_ms * max(1, dots)))


def plan_delivery(
    text: str,
    *,
    unit_pause_ms: int = 320,
    max_pause_ms: int = 900,
    trailing_pause_ms: int = 220,
    filler_speed: float = 0.88,
    max_pauses: int = 3,
) -> list[SpokenPiece]:
    """Split one sentence into synthesis jobs separated by real silence.

    Returns a single piece when the sentence reads straight through, so the
    ordinary path costs nothing.
    """
    source = str(text or "").strip()
    if not source:
        return []

    pieces: list[SpokenPiece] = []
    cursor = 0
    pauses_used = 0
    for match in _ELLIPSIS.finditer(source):
        if pauses_used >= max(0, int(max_pauses)):
            break
        chunk = source[cursor:match.start()].strip()
        cursor = match.end()
        if not chunk:
            # A sentence opening with 「……」 is a beat before speaking at all;
            # keep it by attaching the silence to the piece that follows.
            continue
        pieces.append(SpokenPiece(
            text=chunk,
            pause_after_ms=_pause_for(
                match.group(0), unit_ms=unit_pause_ms, max_ms=max_pause_ms,
            ),
            speed_scale=_speed_for(chunk, filler_speed),
        ))
        pauses_used += 1

    tail = source[cursor:].strip()
    if tail:
        pieces.append(SpokenPiece(
            text=tail,
            pause_after_ms=trailing_pause_ms if _OPEN_ENDING.search(tail) else 0,
            speed_scale=_speed_for(tail, filler_speed),
        ))
    elif pieces:
        # The sentence ended on the ellipsis; that trailing silence is the
        # point of it, so leave the last pause in place.
        pass
    return pieces or [SpokenPiece(text=source)]


def _speed_for(chunk: str, filler_speed: float) -> float:
    """Slow only the part that carries the hesitation, not the whole reply."""
    if _FILLER.match(chunk.lstrip("　 ")):
        return max(0.5, min(1.0, float(filler_speed)))
    return 1.0


def has_hesitation(text: str) -> bool:
    """Whether this text asks to be delivered with a pause or a filler."""
    source = str(text or "").strip()
    return bool(_ELLIPSIS.search(source) or _FILLER.match(source))


def count_gestures(text: str) -> int:
    """How many hesitation gestures one reply carries, for the damping input."""
    source = str(text or "").strip()
    if not source:
        return 0
    return len(_ELLIPSIS.findall(source)) + (1 if _FILLER.match(source) else 0)
