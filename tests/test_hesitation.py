"""Hesitating only where there is something to hesitate about.

The request was explicit: not a mechanical feature, but the gestures a person
makes at the places they would genuinely stop and think.  That rules out
sprinkling 「えっと」 at random, and it also rules out the opposite failure —
sounding equally certain about a name half-heard through a bad microphone and
about the time of day.

第1条 forbids misrepresenting knowledge or ability to appear more human, so a
performed hesitation is not merely tacky here, it is prohibited.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.hesitation import (
    Assessment, UncertaintyKind, assess, permission_block,
)


# ---------------------------------------------------------------------------
# Certainty must stay silent
# ---------------------------------------------------------------------------


def test_a_clear_turn_gets_no_allowance():
    result = assess()
    assert result.uncertain is False
    assert result.allowance == 0
    assert result.reason == "certain"


def test_certainty_is_stated_rather_than_left_blank():
    """An empty block would let the previous turn's permission linger."""
    block = permission_block(assess())
    assert "迷っている点がない" in block
    assert "入れない" in block


def test_a_clean_transcript_is_not_doubt():
    assert assess(transcript_confidence=0.97).uncertain is False


def test_a_found_memory_is_not_doubt():
    assert assess(memory_requested=True, memory_hits=4).uncertain is False


def test_a_question_with_grounds_is_not_doubt():
    assert assess(factual_question=True, has_grounds=True).uncertain is False


def test_a_settled_opinion_is_not_doubt():
    assert assess(opinion_requested=True, settled_view=True).uncertain is False


# ---------------------------------------------------------------------------
# Real doubt, from signals the system already computes
# ---------------------------------------------------------------------------


def test_a_doubtful_transcript_counts():
    result = assess(transcript_confidence=0.45)
    assert result.uncertain
    assert result.strongest.kind is UncertaintyKind.MISHEARD


def test_a_named_doubtful_word_is_carried_through():
    result = assess(transcript_confidence=0.9, doubtful_words=["こうしょう"])
    assert result.strongest.subject == "こうしょう"
    assert "こうしょう" in permission_block(result)


def test_an_unresolved_reference_counts():
    result = assess(reference_unresolved=True, reference_subject="さっきの話")
    assert result.strongest.kind is UncertaintyKind.AMBIGUOUS_REFERENCE
    assert "さっきの話" in permission_block(result)


def test_a_memory_that_will_not_come_counts():
    result = assess(memory_requested=True, memory_hits=0)
    assert result.strongest.kind is UncertaintyKind.THIN_MEMORY


def test_a_single_faint_memory_is_weaker_than_none():
    one = assess(memory_requested=True, memory_hits=1).strongest
    none = assess(memory_requested=True, memory_hits=0).strongest
    assert one.strength < none.strength


def test_a_question_without_grounds_counts():
    result = assess(factual_question=True, has_grounds=False)
    assert result.strongest.kind is UncertaintyKind.UNGROUNDED_FACT


def test_an_unsettled_opinion_counts():
    result = assess(opinion_requested=True, settled_view=False)
    assert result.strongest.kind is UncertaintyKind.UNSETTLED_VIEW


# ---------------------------------------------------------------------------
# How much may show
# ---------------------------------------------------------------------------


def test_one_gesture_is_the_norm():
    assert assess(reference_unresolved=True).allowance == 1


def test_two_only_when_the_turn_is_doubtful_twice_over():
    result = assess(
        transcript_confidence=0.3, reference_unresolved=True,
    )
    assert result.allowance == 2


def test_hesitating_every_turn_is_damped():
    """A person who stalls on every sentence is not thoughtful."""
    result = assess(reference_unresolved=True, recent_gestures=3)
    assert result.allowance == 0
    assert result.reason.startswith("damped")


def test_one_recent_gesture_does_not_damp():
    assert assess(reference_unresolved=True, recent_gestures=1).allowance == 1


def test_the_allowance_can_be_capped():
    result = assess(
        transcript_confidence=0.3, reference_unresolved=True, max_allowance=1,
    )
    assert result.allowance == 1


def test_noise_below_the_floor_is_not_doubt():
    result = assess(memory_requested=True, memory_hits=1, floor=0.6)
    assert result.uncertain is False


# ---------------------------------------------------------------------------
# What the model is told
# ---------------------------------------------------------------------------


def test_the_permission_names_the_state_not_a_performance():
    block = permission_block(assess(memory_requested=True, memory_hits=0))
    assert "はっきりしない" in block
    assert "最大1回" in block
    # A stage direction would produce a tic; the block must not contain one.
    assert "えっと" not in block
    assert "うーん" not in block


def test_the_permission_forbids_blanket_vagueness():
    block = permission_block(assess(reference_unresolved=True))
    assert "迷っていない部分ははっきり話す" in block
    assert "機械的に付けない" in block or "機械的に" in block


def test_a_misheard_turn_may_simply_ask():
    block = permission_block(assess(transcript_confidence=0.3))
    assert "確かめてよい" in block


def test_the_assessment_is_observable():
    snapshot = assess(reference_unresolved=True).snapshot()
    assert snapshot["allowance"] == 1
    assert snapshot["signals"][0]["kind"] == "AMBIGUOUS_REFERENCE"


def test_an_empty_assessment_has_no_strongest_signal():
    assert Assessment().strongest is None
