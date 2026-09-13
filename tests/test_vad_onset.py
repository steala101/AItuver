"""Where an utterance starts.

Observed: 「うん」「えーと」 at the beginning of a sentence never reached the
transcript.  They are quieter than the VAD start threshold, so the recogniser
only became confident at the following content word — and everything before
that moment was being discarded as silence.
"""
from __future__ import annotations

import pytest

from neuro_voice.vad.segmentation import leading_silence_frames, trim_range


# One frame ≈ 32 ms at the pipeline's default frame size.
SILENCE = 0.02
FILLER = 0.35        # 「えーと」: audible, but below the 0.5 start threshold
SPEECH = 0.92


def test_a_quiet_filler_is_not_treated_as_silence():
    """The core regression: 「えーと」 sits below the start threshold."""
    probabilities = [SILENCE] * 6 + [FILLER] * 8 + [SPEECH] * 3
    assert leading_silence_frames(probabilities, onset_threshold=0.20) == 6


def test_the_old_assumption_would_have_dropped_the_filler():
    """Everything before the trigger was assumed silent; that is the bug."""
    probabilities = [SILENCE] * 6 + [FILLER] * 8 + [SPEECH] * 3
    old = len(probabilities) - 3          # pre_roll length minus voiced frames
    new = leading_silence_frames(probabilities, onset_threshold=0.20)
    assert old == 14 and new == 6
    assert new < old


def test_real_silence_is_still_trimmed():
    probabilities = [SILENCE] * 12 + [SPEECH] * 4
    assert leading_silence_frames(probabilities, onset_threshold=0.20) == 12


def test_keep_frames_leaves_room_for_a_consonant_onset():
    probabilities = [SILENCE] * 10 + [SPEECH] * 3
    assert leading_silence_frames(probabilities, onset_threshold=0.20, keep_frames=5) == 5
    # Never runs off the front of the buffer.
    assert leading_silence_frames(probabilities, onset_threshold=0.20, keep_frames=99) == 0


def test_speech_from_the_very_first_frame_trims_nothing():
    assert leading_silence_frames([SPEECH] * 5, onset_threshold=0.20) == 0


def test_an_empty_or_broken_buffer_is_safe():
    assert leading_silence_frames([]) == 0
    assert leading_silence_frames(None) == 0
    assert leading_silence_frames([None, "x", SPEECH], onset_threshold=0.20) == 2


def test_the_threshold_is_what_decides():
    probabilities = [SILENCE] * 4 + [FILLER] * 6 + [SPEECH] * 2
    # A threshold above the filler level puts us back to the old behaviour.
    assert leading_silence_frames(probabilities, onset_threshold=0.50) == 10
    assert leading_silence_frames(probabilities, onset_threshold=0.20) == 4


# ---------------------------------------------------------------------------
# Slicing the buffer
# ---------------------------------------------------------------------------


def test_trim_range_keeps_the_middle():
    start, end = trim_range(40, leading_silence=6, trailing_silence=10, keep_trailing=3)
    assert (start, end) == (6, 33)


def test_trim_range_never_produces_an_empty_slice():
    start, end = trim_range(5, leading_silence=99, trailing_silence=99)
    assert start < end <= 5


def test_trim_range_handles_no_silence():
    assert trim_range(20, leading_silence=0, trailing_silence=0) == (0, 20)


def test_trim_range_on_an_empty_buffer():
    assert trim_range(0, leading_silence=3, trailing_silence=3) == (0, 0)


def test_trailing_keep_cannot_extend_past_the_buffer():
    start, end = trim_range(10, leading_silence=0, trailing_silence=2, keep_trailing=99)
    assert end == 10


@pytest.mark.parametrize("keep_trailing", [0, 1, 5, 50])
def test_the_slice_is_always_valid(keep_trailing):
    start, end = trim_range(
        30, leading_silence=4, trailing_silence=7, keep_trailing=keep_trailing,
    )
    assert 0 <= start < end <= 30
