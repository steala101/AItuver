import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_voice.mind.relationship import RelationshipStore


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _event(speaker, kind, intensity=.8, confidence=.9, summary="event"):
    return {"speaker_id": speaker, "event_type": kind, "intensity": intensity,
            "confidence": confidence, "summary": summary}


class RelationshipStateTests(unittest.TestCase):
    def test_one_rude_turn_does_not_flip_a_relationship(self):
        with TemporaryDirectory() as tmp:
            store = RelationshipStore(Path(tmp) / "relationships.json", _Config())
            before = store.snapshot("speaker:a")
            store.apply_events([_event("speaker:a", "disrespect")])
            after = store.snapshot("speaker:a")
            self.assertGreater(after["caution"], before["caution"])
            self.assertLess(after["respect"], before["respect"])
            self.assertGreater(after["trust"], .45)
            self.assertNotEqual(after["style"]["label"], "boundary_needed")

    def test_repeated_disrespect_changes_only_the_same_speaker(self):
        with TemporaryDirectory() as tmp:
            store = RelationshipStore(Path(tmp) / "relationships.json", _Config())
            for i in range(4):
                store.apply_events([_event("speaker:b", "mockery", summary=f"mockery-{i}")])
            a, b = store.snapshot("speaker:a"), store.snapshot("speaker:b")
            self.assertEqual(a["unresolved_hurt"], 0.0)
            self.assertGreater(b["unresolved_hurt"], 0.0)
            self.assertLess(b["comfort"], a["comfort"])

    def test_apology_repairs_hurt_without_instantly_restoring_trust(self):
        with TemporaryDirectory() as tmp:
            store = RelationshipStore(Path(tmp) / "relationships.json", _Config())
            store.apply_events([_event("speaker:a", "boundary_violation")])
            hurt, trust = store.snapshot("speaker:a")["unresolved_hurt"], store.snapshot("speaker:a")["trust"]
            store.apply_events([_event("speaker:a", "apology"), _event("speaker:a", "repair_attempt")])
            repaired = store.snapshot("speaker:a")
            self.assertLess(repaired["unresolved_hurt"], hurt)
            self.assertLessEqual(repaired["trust"], .51)
            self.assertGreaterEqual(repaired["repair_willingness"], .8)

    def test_persistence_and_safe_decay(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "relationships.json"
            store = RelationshipStore(path, _Config())
            store.apply_events([_event("speaker:a", "hostility")])
            state = store._states["speaker:a"]
            state.last_updated_at = time.time() - 8 * 3600
            initial = state.irritation
            store.save()
            reloaded = RelationshipStore(path, _Config())
            after = reloaded.snapshot("speaker:a")
            self.assertLess(after["irritation"], initial)
            self.assertTrue(path.exists())

    def test_relationship_prompt_never_orders_agreement(self):
        with TemporaryDirectory() as tmp:
            store = RelationshipStore(Path(tmp) / "relationships.json", _Config())
            store.apply_events([_event("speaker:a", "kindness")])
            prompt = store.prompt("speaker:a")
            self.assertIn("結論は変えない", prompt)
            self.assertIn("同意せず", prompt)

    def test_corrupt_file_falls_back_to_a_safe_default(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "relationships.json"
            path.write_text("{broken", encoding="utf-8")
            store = RelationshipStore(path, _Config())
            state = store.snapshot("speaker:a")
            self.assertEqual(state["trust"], .5)
            self.assertEqual(state["caution"], .1)
