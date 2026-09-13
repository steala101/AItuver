from __future__ import annotations

from neuro_voice.mind.mind import Mind
from neuro_voice.utils.config import Config


class _Speakers:
    def display_name(self, speaker_id, source):
        return "チビ" if speaker_id == 1 else ""

    def list(self):
        # These registered-but-not-speaking names must never reach Whisper.
        return [
            {"id": 1, "name": "チビ"},
            {"id": 2, "name": "ゲスト4"},
            {"id": 3, "name": "昔の参加者"},
        ]


class _Activities:
    def __init__(self, active=False):
        self.active = active

    def snapshot(self):
        return {
            "status": "ACTIVE" if self.active else "COMPLETED",
            "activity_name": "日本語しりとり",
        }


class _Store:
    @staticmethod
    def recent_transcripts(limit=4):
        return []

    @staticmethod
    def recent(limit=4):
        return []


def _bare_mind(*, activity_active=False):
    mind = object.__new__(Mind)
    mind._cfg = Config({})
    mind._speakers = _Speakers()
    mind._activities = _Activities(activity_active)
    mind._store = _Store()
    mind._current_speaker = {"id": 1, "name": "チビ"}
    mind.autonomy_context = lambda source: {}
    return mind


def test_stt_bias_uses_only_current_speaker_and_ignores_finished_activity():
    context = _bare_mind(activity_active=False).speech_recognition_context("local")
    assert "チビ" in context["proper_nouns"]
    assert "ゲスト4" not in context["proper_nouns"]
    assert "昔の参加者" not in context["proper_nouns"]
    assert "日本語しりとり" not in context["activity_terms"]


def test_active_activity_can_still_help_stt():
    context = _bare_mind(activity_active=True).speech_recognition_context("local")
    assert context["activity_terms"] == ["日本語しりとり"]
