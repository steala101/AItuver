"""Speaker identity must not flip mid-conversation.

Observed failure: one person's consecutive turns were labelled ジーレン, チビ,
ジーレン, チビ.  Voiceprint cosine scores wobble by a few hundredths between
utterances, so whichever stored profile happened to win each turn took the
label — and the transcript read like two people talking.
"""
from __future__ import annotations

import numpy as np
import pytest

from neuro_voice.mind.speakers import SpeakerRegistry


def unit(values):
    vec = np.asarray(values, dtype=np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9)


ALICE = unit([1.0, 0.0, 0.0])
BOB = unit([0.85, 0.527, 0.0])            # deliberately similar to ALICE
WOBBLE = unit([0.80, 0.3036, 0.5175])     # ALICE's voice, scoring closer to BOB
FAR = unit([0.0, 0.0, 1.0])


def registry(tmp_path, **kwargs):
    return SpeakerRegistry(tmp_path / "speakers.json", **kwargs)


def enrol(reg, vec, name):
    # Force a distinct profile; the point of these tests is two stored people.
    profile = reg.identify(vec, min_match=0.999)
    reg.set_name(profile["id"], name)
    return profile


def test_a_near_tie_keeps_the_current_speaker(tmp_path):
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10, sticky_margin=0.08)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    # ALICE speaks; a slightly noisy sample of the same voice follows.
    first = reg.identify(ALICE)
    assert first["name"] == "チビ"
    second = reg.identify(WOBBLE)
    assert second["name"] == "チビ", "近接スコアで話者が入れ替わってはいけない"


def test_a_clearly_different_voice_still_switches(tmp_path):
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10, sticky_margin=0.08)
    enrol(reg, ALICE, "チビ")
    enrol(reg, FAR, "ジーレン")
    assert reg.identify(ALICE)["name"] == "チビ"
    assert reg.identify(FAR)["name"] == "ジーレン", "明確に別人なら切り替わるべき"


def test_stickiness_expires_so_a_new_session_is_free(tmp_path):
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10,
                   sticky_window_s=0.0, sticky_margin=0.5)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    reg.identify(ALICE)
    # With the window disabled the raw best match wins again.
    assert reg.identify(BOB)["name"] == "ジーレン"


def test_hysteresis_can_be_switched_off(tmp_path):
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10, sticky_margin=0.0)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    reg.identify(ALICE)
    assert reg.identify(BOB)["name"] == "ジーレン"


def test_excluded_names_are_never_sticky(tmp_path):
    """The loopback path excludes the owner; stickiness must respect that."""
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10, sticky_margin=0.08)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    reg.identify(ALICE)
    result = reg.identify(WOBBLE, exclude_names={"チビ"})
    assert result["name"] != "チビ"


def test_a_genuinely_new_voice_is_still_registered(tmp_path):
    reg = registry(tmp_path, threshold=0.90, new_threshold=0.80, sticky_margin=0.08)
    enrol(reg, ALICE, "チビ")
    reg.identify(ALICE)
    result = reg.identify(FAR)
    assert result["is_new"] is True


# ---------------------------------------------------------------------------
# Hysteresis must not overrule certainty
# ---------------------------------------------------------------------------


def test_a_certain_match_beats_the_previous_speaker(tmp_path):
    """The margin is for wobble, not for overriding a confident match.

    ジーレン's own recording scored 1.00 against their profile while チビ sat
    at 0.98.  With a plain 0.08 margin the previous speaker still won, so the
    person who had just been registered kept being called by someone else's
    name.
    """
    reg = registry(tmp_path, threshold=0.99, new_threshold=0.10, sticky_margin=0.30)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    assert reg.identify(ALICE)["name"] == "チビ"
    assert reg.identify(BOB)["name"] == "ジーレン"


def test_wobble_below_the_certain_threshold_still_sticks(tmp_path):
    """The original protection has to survive the fix above."""
    reg = registry(tmp_path, threshold=0.999, new_threshold=0.10, sticky_margin=0.08)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    reg.identify(ALICE)
    assert reg.identify(WOBBLE)["name"] == "チビ"


def test_who_spoke_last_survives_a_coarse_clock(tmp_path, monkeypatch):
    """Windows' ``time.time()`` only ticks about every 15ms.

    Two utterances inside one tick share a timestamp, and "the person who was
    just speaking" then fell back to the order profiles happened to sit in.
    """
    import neuro_voice.mind.speakers as speakers

    monkeypatch.setattr(speakers.time, "time", lambda: 1000.0)
    reg = registry(tmp_path, threshold=0.999, new_threshold=0.10, sticky_margin=0.30)
    enrol(reg, ALICE, "チビ")
    enrol(reg, BOB, "ジーレン")
    # BOB registered last, so BOB is the continuing speaker even though both
    # profiles carry an identical ``last_seen``.
    assert reg.identify(WOBBLE)["name"] == "ジーレン"
