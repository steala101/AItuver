from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from neuro_voice.games.profiles import GameProfileRegistry, default_profile_root
from neuro_voice.games.session import GameProfileSessionManager


class FakeConfig:
    def __init__(self, values: dict[str, object]):
        self.values = values
        self.persisted: list[tuple[str, object]] = []

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        self.values[key] = value

    def persist(self, key: str, value) -> bool:
        self.values[key] = value
        self.persisted.append((key, value))
        return True


class GameProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_repository_profiles_are_discoverable(self) -> None:
        registry = GameProfileRegistry(default_profile_root())
        self.assertEqual(
            [profile.id for profile in registry.all()],
            ["keep_talking_and_nobody_explodes", "minecraft"],
        )
        ktane = registry.get("ktane")
        self.assertIsNotNone(ktane)
        self.assertEqual(ktane.interaction_mode, "manual_expert")
        self.assertFalse(ktane.allows_vision)
        self.assertEqual(registry.get("マイクラ").id, "minecraft")

    def test_folder_must_match_profile_id(self) -> None:
        folder = self.tmp_path / "wrong"
        folder.mkdir()
        (folder / "profile.json").write_text(
            json.dumps({"id": "right", "display_name": "Right"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "folder"):
            GameProfileRegistry(self.tmp_path)

    def test_ktane_session_has_same_semantics_for_local_and_discord(self) -> None:
        cfg = FakeConfig({
            "game_profiles.root": str(default_profile_root()),
            "video.game_profile": "keep_talking_and_nobody_explodes",
        })
        local = GameProfileSessionManager(cfg, self.tmp_path / "local.json")
        discord = GameProfileSessionManager(cfg, self.tmp_path / "discord.json")
        local_started = local.handle_final_input(
            "爆弾解除を始めよう", actor_id="local:mic", source="local",
        )
        discord_started = discord.handle_final_input(
            "爆弾解除を始めよう", actor_id="123", source="discord",
        )
        self.assertTrue(local_started.handled and discord_started.handled)
        self.assertEqual(local_started.reply, discord_started.reply)
        self.assertTrue(local.active and discord.active)
        self.assertIn("爆弾の画面は見ない", local_started.reply)
        self.assertIn("プレイ中は一般Web検索を行わない", local.grounded_context())

    def test_ktane_session_stop_is_persisted(self) -> None:
        cfg = FakeConfig({
            "game_profiles.root": str(default_profile_root()),
            "video.game_profile": "keep_talking_and_nobody_explodes",
        })
        path = self.tmp_path / "state.json"
        manager = GameProfileSessionManager(cfg, path)
        manager.handle_final_input("KTANEをやろう", actor_id="a", source="local")
        stopped = manager.handle_final_input(
            "爆弾解除は終わりにしよう", actor_id="a", source="local",
        )
        restored = GameProfileSessionManager(cfg, path)
        self.assertTrue(stopped.handled)
        self.assertFalse(restored.active)

    def test_starting_the_bomb_session_from_minecraft_switches_the_profile(self) -> None:
        """以前はここで何も起きなかった。

        「マイクラが選択中なら爆弾解除の開始語を無視する」という振る舞いは、
        モードを変えるためだけに config.yaml を手で編集させることを意味して
        いた。音声で話しかける相手の作りとして不自然なので、開始語ひとつで
        切り替えまで行うようにした（詳細は `tests/test_game_profile_switch.py`）。
        """
        cfg = FakeConfig({
            "game_profiles.root": str(default_profile_root()),
            "video.game_profile": "minecraft",
        })
        manager = GameProfileSessionManager(cfg, self.tmp_path / "state.json")
        result = manager.handle_final_input(
            "爆弾解除を始めよう", actor_id="a", source="local",
        )
        self.assertTrue(result.handled)
        self.assertTrue(manager.active)
        self.assertEqual(manager.selected.id, "keep_talking_and_nobody_explodes")
        self.assertFalse(manager.selected.allows_vision)
        self.assertIn(
            ("video.game_profile", "keep_talking_and_nobody_explodes"), cfg.persisted,
        )

    def test_minecraft_talk_alone_does_not_start_a_manual_session(self) -> None:
        """切り替えの線引き: 名前が出ただけでは何も起きない。"""
        cfg = FakeConfig({
            "game_profiles.root": str(default_profile_root()),
            "video.game_profile": "minecraft",
        })
        manager = GameProfileSessionManager(cfg, self.tmp_path / "state.json")
        result = manager.handle_final_input(
            "爆弾解除って難しそうだよね", actor_id="a", source="local",
        )
        self.assertFalse(result.handled)
        self.assertFalse(manager.active)
        self.assertEqual(manager.selected.id, "minecraft")
        self.assertEqual(cfg.persisted, [])


if __name__ == "__main__":
    unittest.main()
