"""One person, several surfaces.

Observed: the same person is 「チビ」 at the local microphone and 「ジーレン」 in
Discord, so two voiceprint profiles were created.  Because relationships,
dialogue profiles and adaptive learning are all keyed by ``speaker:{id}``, the
assistant genuinely knew them as two strangers with half a history each.
"""
from __future__ import annotations

import numpy as np
import pytest

from neuro_voice.dialogue.intelligence import DialogueIntelligence
from neuro_voice.mind.relationship import RelationshipStore
from neuro_voice.mind.speakers import SpeakerRegistry


class FakeConfig:
    def __init__(self, **overrides):
        self._values = dict(overrides)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def section(self, _name):
        return {}


def unit(values):
    vec = np.asarray(values, dtype=np.float32)
    return vec / (np.linalg.norm(vec) + 1e-9)


CHIBI = unit([1.0, 0.05, 0.0])
JIREN = unit([0.97, 0.24, 0.0])      # same voice through Discord compression


def registry(tmp_path):
    return SpeakerRegistry(tmp_path / "speakers.json", threshold=0.999, new_threshold=0.10)


def two_profiles(tmp_path):
    reg = registry(tmp_path)
    local = reg.identify(CHIBI, min_match=0.999)
    reg.set_name(local["id"], "チビ")
    reg.set_alias(local["id"], "local", "チビ")
    discord = reg.identify(JIREN, min_match=0.999)
    reg.set_name(discord["id"], "ジーレン")
    reg.set_alias(discord["id"], "discord", "ジーレン")
    return reg, local["id"], discord["id"]


# ---------------------------------------------------------------------------
# Merging the voiceprint profile
# ---------------------------------------------------------------------------


def test_merge_leaves_one_person(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    assert len(reg.list()) == 2
    merged = reg.merge(discord_id, local_id)
    assert merged is not None
    assert len(reg.list()) == 1
    assert reg.list()[0]["id"] == local_id


def test_both_names_survive_as_surface_aliases(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    reg.merge(discord_id, local_id)
    assert reg.display_name(local_id, "local") == "チビ"
    assert reg.display_name(local_id, "discord") == "ジーレン"


def test_an_unknown_surface_falls_back_to_the_canonical_name(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    reg.merge(discord_id, local_id)
    assert reg.display_name(local_id, "youtube") == "チビ"
    assert reg.display_name(local_id, "") == "チビ"


def test_history_is_kept_rather_than_halved(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    for _ in range(5):
        reg.on_turn(local_id)
    profiles = {int(p["id"]): p for p in reg.list()}
    local_turns = profiles[local_id]["turns"]
    discord_turns = profiles[discord_id]["turns"]
    reg.merge(discord_id, local_id)
    assert reg.list()[0]["turns"] == local_turns + discord_turns


def test_the_merged_voiceprint_matches_both_recordings(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    reg.merge(discord_id, local_id)
    assert reg.identify(CHIBI)["id"] == local_id
    assert reg.identify(JIREN)["id"] == local_id


def test_merging_a_missing_profile_is_refused(tmp_path):
    reg, local_id, _discord_id = two_profiles(tmp_path)
    assert reg.merge(999, local_id) is None
    assert reg.merge(local_id, local_id) is None


def test_a_surface_alias_is_recorded_from_the_merge(tmp_path):
    reg = registry(tmp_path)
    first = reg.identify(CHIBI, min_match=0.999)
    reg.set_name(first["id"], "チビ")
    second = reg.identify(JIREN, min_match=0.999)
    reg.set_name(second["id"], "ジーレン")
    reg.merge(second["id"], first["id"], source_surface="discord", target_surface="local")
    assert reg.display_name(first["id"], "discord") == "ジーレン"
    assert reg.display_name(first["id"], "local") == "チビ"


# ---------------------------------------------------------------------------
# Suggesting a merge
# ---------------------------------------------------------------------------


def test_a_close_pair_is_suggested(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    candidates = reg.merge_candidates(minimum=0.82)
    assert candidates
    assert {candidates[0]["source_id"], candidates[0]["target_id"]} == {local_id, discord_id}


def test_clearly_different_voices_are_not_suggested(tmp_path):
    reg = registry(tmp_path)
    a = reg.identify(unit([1.0, 0.0, 0.0]), min_match=0.999)
    reg.set_name(a["id"], "チビ")
    b = reg.identify(unit([0.0, 0.0, 1.0]), min_match=0.999)
    reg.set_name(b["id"], "別人")
    assert reg.merge_candidates(minimum=0.82) == []


def turns_of(reg, speaker_id, count):
    """Set the recorded history directly.

    Driving this through ``identify()`` also drags in thresholds, hysteresis
    and centroid updates, which made the result depend on the platform's clock
    resolution.  What is under test here is only the tie-break.
    """
    with reg._lock:
        for profile in reg._profiles:
            if int(profile["id"]) == int(speaker_id):
                profile["turns"] = int(count)
                return
    raise AssertionError(f"no such speaker: {speaker_id}")


def test_the_longer_history_is_offered_as_the_target(tmp_path):
    reg, local_id, discord_id = two_profiles(tmp_path)
    turns_of(reg, discord_id, 10)
    turns_of(reg, local_id, 1)
    candidate = reg.merge_candidates(minimum=0.82)[0]
    assert candidate["target_id"] == discord_id
    assert candidate["source_id"] == local_id


def test_the_offer_does_not_depend_on_registration_order(tmp_path):
    """A tie used to be decided by whichever profile came first in the list."""
    reg, local_id, discord_id = two_profiles(tmp_path)
    turns_of(reg, discord_id, 4)
    turns_of(reg, local_id, 4)
    first = reg.merge_candidates(minimum=0.82)[0]
    with reg._lock:
        reg._profiles.reverse()
    second = reg.merge_candidates(minimum=0.82)[0]
    assert first["target_id"] == second["target_id"]
    assert first["source_id"] == second["source_id"]


# ---------------------------------------------------------------------------
# The keyed stores that actually hold the history
# ---------------------------------------------------------------------------


def test_relationship_merge_keeps_the_stronger_bond(tmp_path):
    store = RelationshipStore(tmp_path / "rel.json", FakeConfig())
    # get() intentionally returns a copy, so write to the live state.
    store.get("speaker:2"), store.get("speaker:1")
    weak = store._states["speaker:2"]
    weak.trust, weak.emotional_closeness, weak.interaction_count = 0.55, 0.30, 4
    strong = store._states["speaker:1"]
    strong.trust, strong.emotional_closeness, strong.interaction_count = 0.80, 0.62, 30

    assert store.merge("speaker:2", "speaker:1") is True
    merged = store.get("speaker:1")
    assert merged.trust == pytest.approx(0.80)
    assert merged.emotional_closeness == pytest.approx(0.62)
    assert merged.interaction_count == 34
    assert "speaker:2" not in store.all_snapshots()


def test_relationship_merge_does_not_import_strain_from_both(tmp_path):
    """Being one person must not mean carrying two profiles' worth of friction."""
    store = RelationshipStore(tmp_path / "rel.json", FakeConfig())
    store.get("speaker:2"), store.get("speaker:1")
    rough = store._states["speaker:2"]
    rough.irritation, rough.caution, rough.tension = 0.70, 0.60, 0.50
    calm = store._states["speaker:1"]
    calm.irritation, calm.caution, calm.tension = 0.05, 0.10, 0.00

    store.merge("speaker:2", "speaker:1")
    merged = store.get("speaker:1")
    assert merged.irritation == pytest.approx(0.05)
    assert merged.caution == pytest.approx(0.10)
    assert merged.tension == pytest.approx(0.00)


def test_relationship_merge_moves_an_orphan_state(tmp_path):
    store = RelationshipStore(tmp_path / "rel.json", FakeConfig())
    store.get("speaker:2")
    store._states["speaker:2"].trust = 0.77
    assert store.merge("speaker:2", "speaker:9") is True
    assert store.get("speaker:9").trust == pytest.approx(0.77)
    assert "speaker:2" not in store.all_snapshots()


def test_relationship_merge_refuses_a_missing_source(tmp_path):
    store = RelationshipStore(tmp_path / "rel.json", FakeConfig())
    assert store.merge("speaker:404", "speaker:1") is False
    assert store.merge("speaker:1", "speaker:1") is False


def test_dialogue_profile_merge_sums_the_evidence(tmp_path):
    dialogue = DialogueIntelligence(FakeConfig(), tmp_path / "dialogue_x.json")
    dialogue.observe_turn("speaker:1", "マイクラの話をしよう")
    dialogue.observe_turn("speaker:1", "レッドストーンが好き")
    dialogue.observe_turn("speaker:2", "エクセルの話もするよ")

    before_local = dialogue.snapshot("speaker:1")["user"]["turns"]
    before_discord = dialogue.snapshot("speaker:2")["user"]["turns"]
    assert dialogue.merge_users("speaker:2", "speaker:1") is True
    assert dialogue.snapshot("speaker:1")["user"]["turns"] == before_local + before_discord


def test_dialogue_profile_merge_is_idempotent_on_a_missing_source(tmp_path):
    dialogue = DialogueIntelligence(FakeConfig(), tmp_path / "dialogue_x.json")
    dialogue.observe_turn("speaker:1", "こんばんは")
    assert dialogue.merge_users("speaker:404", "speaker:1") is False
    assert dialogue.merge_users("speaker:1", "speaker:1") is False


def test_adaptive_learning_follows_the_merged_speaker(tmp_path):
    dialogue = DialogueIntelligence(FakeConfig(), tmp_path / "dialogue_x.json")
    store = dialogue._adaptive
    store.upsert("topic_lifecycle", "excel", {"topic": "excel"}, user_id="speaker:2")
    assert store.latest("topic_lifecycle", user_id="speaker:2")
    assert store.reassign_user("speaker:2", "speaker:1") >= 1
    assert store.latest("topic_lifecycle", user_id="speaker:2") == []
    assert store.latest("topic_lifecycle", user_id="speaker:1")
