"""What counts as a "topic", and when there isn't one yet.

Observed: every session opened with the same remark, traceable to a stored
topic that was actually a whole sentence —
「今あのマイクオンにしたまま声口元から話してたわ」.  Japanese is not
space-delimited, so a character class covering kanji, hiragana and katakana
matches an entire utterance as one word.
"""
from __future__ import annotations

import json

import pytest

from neuro_voice.dialogue.intelligence import DialogueIntelligence


class FakeConfig:
    def __init__(self, **overrides):
        self._values = dict(overrides)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def section(self, _name):
        return {}


def intelligence(tmp_path):
    return DialogueIntelligence(FakeConfig(), tmp_path / "dialogue_x.json")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_a_sentence_is_not_one_topic(tmp_path):
    topics = intelligence(tmp_path)._topics(
        "今あのマイクオンにしたまま声口元から話してたわ"
    )
    assert "今あのマイクオンにしたまま声口元から話してたわ" not in topics
    assert all(len(item) <= 12 for item in topics)
    assert topics


def test_useful_words_survive(tmp_path):
    topics = intelligence(tmp_path)._topics("マイクラのレッドストーン回路が面白い")
    assert "マイクラ" in topics
    assert "レッドストーン" in topics


def test_latin_terms_are_kept(tmp_path):
    assert "vlookup" in intelligence(tmp_path)._topics("VLOOKUPの使い方を調べてた")


def test_filler_words_are_not_topics(tmp_path):
    topics = intelligence(tmp_path)._topics("結構、本当に全部そうだと思う")
    for junk in ("結構", "本当", "全部"):
        assert junk not in topics


def test_bare_hiragana_is_not_a_topic(tmp_path):
    """Hiragana on its own is mostly grammar, not subject matter."""
    assert intelligence(tmp_path)._topics("そうなんだけどね、まあそういうことで") == []


def test_empty_input_is_safe(tmp_path):
    assert intelligence(tmp_path)._topics("") == []
    assert intelligence(tmp_path)._topics(None) == []


# ---------------------------------------------------------------------------
# Repairing what was already written
# ---------------------------------------------------------------------------


def test_sentences_already_stored_are_dropped_on_load(tmp_path):
    """The bad seed outlives the conversation unless it is cleaned up."""
    path = tmp_path / "dialogue_x.json"
    path.write_text(json.dumps({
        "version": 5,
        "users": {
            "local:user": {
                "turns": 3, "chars": 10, "questions": 0, "pause_like": 0,
                "topics": [
                    "今あのマイクオンにしたまま声口元から話してたわ",
                    "マイクラ",
                    "特に何もないんだけど",
                ],
                "emotions": [], "last_seen": 0.0,
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    loaded = DialogueIntelligence(FakeConfig(), path)
    topics = loaded.snapshot("local:user")["user"]["topics"]
    assert "マイクラ" in topics
    assert "今あのマイクオンにしたまま声口元から話してたわ" not in topics
    assert "特に何もないんだけど" not in topics


def test_cleanup_keeps_the_profile_otherwise_intact(tmp_path):
    path = tmp_path / "dialogue_x.json"
    path.write_text(json.dumps({
        "version": 5,
        "users": {"local:user": {
            "turns": 7, "chars": 100, "questions": 2, "pause_like": 0,
            "topics": ["長い文章がここに入っていたとします本当に長い"],
            "emotions": [], "last_seen": 12.0,
        }},
    }, ensure_ascii=False), encoding="utf-8")

    loaded = DialogueIntelligence(FakeConfig(), path)
    user = loaded.snapshot("local:user")["user"]
    assert user["turns"] == 7
    assert user["topics"] == []


def test_a_missing_file_still_starts(tmp_path):
    loaded = DialogueIntelligence(FakeConfig(), tmp_path / "absent.json")
    assert loaded.snapshot("local:user")["user"]["topics"] == []


@pytest.mark.parametrize("stored", [None, [], ["", "  "], "not-a-list"])
def test_cleanup_tolerates_junk(tmp_path, stored):
    assert DialogueIntelligence._clean_stored_topics(stored) == []
