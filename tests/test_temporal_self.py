import tempfile
import unittest
from pathlib import Path

from neuro_voice.mind.temporal_self import TemporalSelf
from neuro_voice.tts.style import SpeechStyle


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class TemporalSelfTests(unittest.TestCase):
    def test_session_data_is_persisted_without_transcript_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temporal.json"
            model = TemporalSelf(path, _Config(), persona_key="poppo")
            model.observe_activity()
            model.close()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("total_uptime_seconds", saved)
            self.assertNotIn("secret user utterance", saved)
            reopened = TemporalSelf(path, _Config(), persona_key="poppo")
            self.assertGreaterEqual(reopened.snapshot()["session_count"], 2)
            reopened.close()

    def test_affect_and_voice_change_gradually(self):
        cfg = _Config({"voice_affect.max_delta_per_utterance": .05})
        with tempfile.TemporaryDirectory() as directory:
            model = TemporalSelf(Path(directory) / "temporal.json", cfg, persona_key="poppo")
            base = SpeechStyle(speed=1.0, pitch=0.0, intonation=1.0, volume=1.0)
            first = model.smooth_style(base, source="local")
            model.observe_affect("joy", intensity=1.0)
            second = model.smooth_style(base, source="local")
            self.assertLessEqual(abs(second.intonation - first.intonation), .051)
            self.assertLessEqual(abs(second.speed - first.speed), .051)
            model.close()

    def test_return_context_does_not_claim_cadence_from_few_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            model = TemporalSelf(Path(directory) / "temporal.json", _Config(), persona_key="poppo")
            context = model.return_context()
            self.assertEqual("low", context["cadence_confidence"])
            self.assertIsNone(context["gap_ratio"])
            model.close()
