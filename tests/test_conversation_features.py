from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_voice.dialogue.adaptive_store import AdaptiveConversationStore
from neuro_voice.dialogue.feature_engines import ConversationFeatureSuite, HumorEngine
from neuro_voice.dialogue.feature_models import ValueProfile
from neuro_voice.dialogue.intelligence import DialogueIntelligence
from neuro_voice.mind.relationship import RelationshipStore
from neuro_voice.utils.config import Config


def _config(**overrides) -> Config:
    data = {
        "dialogue": {
            "enabled": True,
            "conversation_generation": {
                "enabled": True, "candidate_count": 4,
                "style_repeat_penalty": .72, "emotion_inertia": .78,
            },
            "curiosity": {"min_turn_interval": 1, "threshold": .3},
        },
        "conversation_engine": {"variant": "enhanced"},
        "conversation_features": {
            "mode": "normal", "developer_ui": True,
            "modes": {
                "normal": {"feature_activation_multiplier": 1.0},
                "conservative": {"feature_activation_multiplier": .5},
                "expressive": {"feature_activation_multiplier": 1.4},
                "debug_showcase": {"feature_activation_multiplier": 2.0},
            },
            "debug": {"force_feature": None},
        },
        "features": {
            "humor_engine": True, "casual_conversation_engine": True,
            "story_engine": True, "imagination_engine": True,
            "playful_fantasy_engine": True, "world_knowledge_engine": True,
            "self_growth": True, "relationship_evolution": True,
            "value_system": True, "experience_memory": True,
        },
        "mind": {"relationship": {"enabled": True}},
    }
    cfg = Config(data)
    for key, value in overrides.items():
        cfg.set(key.replace("__", "."), value)
    return cfg


class ConversationFeatureSelectionTests(unittest.TestCase):
    def test_childlike_traits_choose_contextual_cheeky_humor_pattern(self):
        profile = {
            "conversation_generation": {"recent_feature_patterns": []},
            "_feature_context": {
                "relationship": {"comfort": .8},
                "persona_traits": {"playfulness": .9, "cheekiness": .8},
            },
        }
        proposal = HumorEngine().propose(
            text="この装置、やっと動いた！", profile=profile, intent="good_news",
            momentum=.8, values=ValueProfile(),
        )
        self.assertIsNotNone(proposal)
        self.assertIn(proposal.pattern_key, {
            "playful_challenge", "light_tsukkomi", "mock_grandiosity",
        })

    def test_lively_conversation_selects_bounded_features(self):
        suite = ConversationFeatureSuite(_config())
        profile = {"conversation_generation": {"recent_features": [], "recent_feature_patterns": []},
                   "_feature_context": {"relationship": {"comfort": .8, "trust": .7}}}
        result = suite.select(
            text="それ、めちゃくちゃ面白い！", profile=profile, intent="statement",
            momentum=.82, values=ValueProfile(),
        )
        names = {proposal.feature_type for proposal in result.selected}
        self.assertIn("humor_engine", names)
        self.assertLessEqual(len(result.selected), 3)
        self.assertTrue(result.evaluations)

    def test_serious_or_low_mood_suppresses_humor_and_fantasy(self):
        cfg = _config(conversation_features__debug__force_feature="humor_engine")
        result = ConversationFeatureSuite(cfg).select(
            text="病院のことで不安でつらい", profile={}, intent="support",
            momentum=.9, values=ValueProfile(),
        )
        names = {proposal.feature_type for proposal in result.selected}
        self.assertNotIn("humor_engine", names)
        self.assertNotIn("playful_fantasy_engine", names)

    def test_imagination_and_story_have_explicit_modes(self):
        suite = ConversationFeatureSuite(_config())
        imagined = suite.select(
            text="立場を逆にしたらどうなるか想像して", profile={}, intent="question",
            momentum=.7, values=ValueProfile(),
        )
        proposal = next(item for item in imagined.proposed if item.feature_type == "imagination_engine")
        self.assertEqual(proposal.pattern_key, "role_reversal")
        story = suite.select(
            text="前回の物語の続きを話して", profile={}, intent="story",
            momentum=.7, values=ValueProfile(),
        )
        proposal = next(item for item in story.proposed if item.feature_type == "story_engine")
        self.assertEqual(proposal.pattern_key, "story_continuation")

    def test_feature_toggle_and_baseline_variant(self):
        cfg = _config(features__humor_engine=False)
        result = ConversationFeatureSuite(cfg).select(
            text="面白い話だね！", profile={}, intent="statement",
            momentum=.9, values=ValueProfile(),
        )
        self.assertNotIn("humor_engine", {item.feature_type for item in result.proposed})
        with TemporaryDirectory() as tmp:
            baseline = _config(conversation_engine__variant="baseline")
            dialogue = DialogueIntelligence(baseline, Path(tmp) / "dialogue.json")
            dialogue.observe_turn("u", "面白い話だね！", now=100)
            dialogue.prompt_context("u", "面白い話だね！")
            plan = dialogue.current_plan("u")
            self.assertEqual(plan["engine_variant"], "baseline")
            self.assertFalse(plan["selected_features"])
            self.assertTrue(plan["feature_evaluations"])


class AdaptiveConversationTests(unittest.TestCase):
    def test_store_schema_persists_and_prunes_debug_history(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "adaptive.db"
            store = AdaptiveConversationStore(path, session_id="s1")
            store.append("ai_experience_episodes", {"outcome": "ok"}, user_id="u")
            for index in range(25):
                store.record_trace(str(index), "u", {"selected_features": []})
            self.assertEqual(store.latest("ai_experience_episodes", user_id="u")[0]["payload"]["outcome"], "ok")
            store.prune(max_debug=20)
            self.assertEqual(len(store.recent_traces("u", limit=50)), 20)
            conn = sqlite3.connect(path)
            try:
                names = {row[0] for row in conn.execute("SELECT name FROM adaptive_schema")}
            finally:
                conn.close()
            self.assertIn("ai_generalized_lessons", names)
            self.assertIn("conversation_feature_feedback", names)

    def test_learning_is_gradual_and_requires_repeated_explicit_feedback(self):
        with TemporaryDirectory() as tmp:
            cfg = _config(conversation_features__debug__force_feature="humor_engine")
            dialogue = DialogueIntelligence(cfg, Path(tmp) / "dialogue.json")
            dialogue.observe_turn("u", "面白い話をして", now=100)
            dialogue.prompt_context("u", "面白い話をして")
            dialogue.record_response("u", "ふふ、それはロボット級の勢いだね！")
            initial_humor = dialogue.snapshot("u")["values"]["humor"]
            for index in range(3):
                reaction = "その冗談、もっとその感じで。面白いし自然になった"
                dialogue.observe_turn("u", reaction, now=110 + index * 10)
                if index < 2:
                    dialogue.prompt_context("u", reaction)
                    dialogue.record_response("u", "ふふ、またロボット級になりそうだね！")
            weights = dialogue._adaptive.feature_weights("u")
            lessons = dialogue._adaptive.latest("ai_generalized_lessons", user_id="u")
            final_humor = dialogue.snapshot("u")["values"]["humor"]
            self.assertGreater(weights.get("humor_engine", 1.0), 1.0)
            self.assertTrue(lessons)
            self.assertGreater(final_humor, initial_humor)
            self.assertLessEqual(final_humor - initial_humor, .02)

    def test_developer_snapshot_exposes_decisions_not_reasoning(self):
        with TemporaryDirectory() as tmp:
            dialogue = DialogueIntelligence(_config(), Path(tmp) / "dialogue.json")
            dialogue.observe_turn("u", "もしAIと一緒に宇宙ゲームを作ったら面白そう", now=100)
            dialogue.prompt_context("u", "もしAIと一緒に宇宙ゲームを作ったら面白そう")
            dialogue.record_response("u", "もしその世界なら、宇宙船が会話で育つ設定が面白いと思う！")
            debug = dialogue.developer_snapshot("u")
            self.assertIn("developer_trace", debug)
            self.assertIn("candidate_evaluations", debug["developer_trace"])
            self.assertIn("critic", debug["developer_trace"])
            self.assertIn("quality_metrics", debug)
            self.assertIn("ab_comparison", debug)
            self.assertNotIn("chain_of_thought", str(debug))
            sessions = dialogue._adaptive.latest("topic_lifecycle", user_id="u", limit=10)
            self.assertTrue(any(row["payload"].get("factual_status") == "fiction" for row in sessions))


class RelationshipEvolutionTests(unittest.TestCase):
    def test_new_relationship_axes_are_person_scoped_and_persistent(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "relationships.json"
            store = RelationshipStore(path, _config())
            before = store.snapshot("alice")
            store.observe("alice")
            store.apply_events([{
                "speaker_id": "alice", "event_type": "humor_connection",
                "intensity": .8, "confidence": .9, "summary": "一緒に笑った",
            }])
            after = store.snapshot("alice")
            bob = store.snapshot("bob")
            self.assertGreater(after["familiarity"], before["familiarity"])
            self.assertGreater(after["playfulness"], before["playfulness"])
            self.assertEqual(bob["playfulness"], before["playfulness"])
            reloaded = RelationshipStore(path, _config())
            self.assertAlmostEqual(reloaded.snapshot("alice")["playfulness"], after["playfulness"])


if __name__ == "__main__":
    unittest.main()
