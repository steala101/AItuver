"""Deciding where an utterance actually starts.

Speech does not begin at the moment the VAD becomes confident.  People open
their mouth with a quiet 「うん」「えーと」「あのー」, and only the following
content word crosses the start threshold.  If everything before that moment is
treated as silence and trimmed, those words never reach the recognizer and the
transcript starts mid-sentence.

The buffered pre-roll already contains them.  What is needed is to trim only
the part that is genuinely silent, using a threshold low enough to notice a
mumbled filler.
"""
from __future__ import annotations

from typing import Sequence


def leading_silence_frames(
    probabilities: Sequence[float],
    *,
    onset_threshold: float = 0.20,
    keep_frames: int = 0,
) -> int:
    """How many frames at the very start carry no speech at all.

    Scans forward and stops at the first frame with any voice activity, so a
    quiet filler sitting in the middle of the pre-roll is never discarded.
    ``keep_frames`` leaves a little room before that first sound, because a
    consonant onset often begins just below any threshold.
    """
    values = list(probabilities or ())
    if not values:
        return 0
    silent = 0
    for value in values:
        try:
            probability = float(value)
        except (TypeError, ValueError):
            probability = 0.0
        if probability >= onset_threshold:
            break
        silent += 1
    return max(0, silent - max(0, int(keep_frames)))


def trim_range(
    total_frames: int,
    *,
    leading_silence: int,
    trailing_silence: int,
    keep_trailing: int = 0,
) -> tuple[int, int]:
    """The slice of buffered frames to hand to the recognizer."""
    total = max(0, int(total_frames))
    start = max(0, min(int(leading_silence), max(0, total - 1)))
    tail = max(0, int(trailing_silence) - max(0, int(keep_trailing)))
    end = max(start + 1, total - tail) if total else 0
    return start, min(end, total)
