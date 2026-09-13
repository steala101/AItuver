from __future__ import annotations

import unittest

from neuro_voice.games.companion import GameCompanionDirector, companion_prompt
from neuro_voice.vision.models import VisualObservation


class GameCompanionDirectorTests(unittest.TestCase):
    def _observe(self, **kwargs) -> VisualObservation:
        values = {
            "summary": "森を探索中",
            "game": "minecraft",
            "confidence": .9,
            "commentary_worthy": True,
        }
        values.update(kwargs)
        return VisualObservation(**values)

    def test_menu_and_loading_screens_stay_silent(self):
        director = GameCompanionDirector(minimum_gap_sec=0)
        observation = self._observe(
            summary="Minecraft game menu is open",
            scene_changes=["pause menu opened"],
        )
        self.assertIsNone(director.observe(observation, now=10))

    def test_clear_combat_change_becomes_urgent_event(self):
        director = GameCompanionDirector(minimum_gap_sec=0)
        event = director.observe(self._observe(
            summary="A zombie is attacking the player",
            actions=["fighting"], scene_changes=["zombie approached"],
            player_state=["low health"], notable_events=["combat started"],
        ), now=10)
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "danger")
        self.assertGreaterEqual(event.priority, .9)

    def test_same_semantic_event_is_not_repeated(self):
        director = GameCompanionDirector(minimum_gap_sec=0, duplicate_ttl_sec=30)
        observation = self._observe(
            summary="A diamond was found", scene_changes=["diamond discovered"],
            notable_events=["diamond acquired"],
        )
        self.assertIsNotNone(director.observe(observation, now=10))
        self.assertIsNone(director.observe(observation, now=11))

    def test_static_gameplay_scene_is_not_an_event(self):
        director = GameCompanionDirector(minimum_gap_sec=0)
        observation = self._observe(
            summary="The player is standing in a forest",
            actions=[], scene_changes=[], notable_events=[], commentary_worthy=False,
        )
        self.assertIsNone(director.observe(observation, now=10))

    def test_importance_policy_can_suppress_minor_actions(self):
        director = GameCompanionDirector(
            minimum_gap_sec=0, minimum_importance=.7, reaction_probability=1,
        )
        observation = self._observe(
            summary="木を一本掘った", actions=["採掘"],
            scene_changes=["木を一本壊した"], importance=.2,
        )
        self.assertIsNone(director.observe(observation, now=10))
        self.assertEqual(director.last_suppression_reason, "below_importance_threshold")

    def test_prompt_requests_varied_co_player_reaction(self):
        director = GameCompanionDirector(minimum_gap_sec=0)
        event = director.observe(self._observe(
            summary="A village was found", scene_changes=["village discovered"],
        ), now=10)
        prompt = companion_prompt(event, previous_comment="お、いいね！")
        self.assertIn("ゲーム相棒モード", prompt)
        self.assertIn("同じ出だし", prompt)
        self.assertIn("実況者の説明口調", prompt)

    def test_prompt_carries_player_action_and_optional_advice(self):
        director = GameCompanionDirector(minimum_gap_sec=0)
        event = director.observe(self._observe(
            summary="夜の森でゾンビに追われている",
            actions=["高い場所へ逃げている"],
            scene_changes=["ゾンビが接近した"],
            player_state=["health_estimate: 低い"],
            notable_events=["戦闘が始まった"],
            suggested_help="足元にブロックを積んで距離を取る",
        ), now=10)
        self.assertIsNotNone(event)
        prompt = companion_prompt(event)
        self.assertIn("health_estimate: 低い", prompt)
        self.assertIn("足元にブロックを積んで距離を取る", prompt)
        self.assertIn("画面の説明だけで終わらず", prompt)
        self.assertIn("毎回読み上げず", prompt)


if __name__ == "__main__":
    unittest.main()
