from __future__ import annotations

import json
import sqlite3
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_voice.games.minecraft_knowledge import (
    MinecraftKnowledgeBase,
    is_minecraft_knowledge_query,
)
from neuro_voice.games.minecraft_official import (
    MinecraftInstall, OfficialRecipe, RecipeIngredient, load_official_recipes,
)


class OfficialRecipeImportTests(unittest.TestCase):
    def test_shaped_recipe_counts_tagged_ingredients_from_game_jar(self):
        with TemporaryDirectory() as tmp:
            jar = Path(tmp) / "1.test.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/minecraft/lang/en_us.json", json.dumps({
                    "item.minecraft.iron_ingot": "Iron Ingot",
                    "item.minecraft.shield": "Shield",
                    "block.minecraft.oak_planks": "Oak Planks",
                }))
                archive.writestr("data/minecraft/tags/item/wooden_tool_materials.json", json.dumps({
                    "values": ["minecraft:oak_planks"],
                }))
                archive.writestr("data/minecraft/recipe/shield.json", json.dumps({
                    "type": "minecraft:crafting_shaped",
                    "key": {"W": "#minecraft:wooden_tool_materials", "o": "minecraft:iron_ingot"},
                    "pattern": ["WoW", "WWW", " W "],
                    "result": {"id": "minecraft:shield", "count": 1},
                }))
            recipes, _ = load_official_recipes(MinecraftInstall("1.test", jar))
            self.assertEqual(len(recipes), 1)
            counts = {item.name: item.count for item in recipes[0].ingredients}
            self.assertEqual(counts["任意の板材"], 6)
            self.assertEqual(counts["Iron Ingot"], 1)
            self.assertEqual(recipes[0].pattern, ("WoW", "WWW", " W "))


class OfficialCookingAnswerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.kb = MinecraftKnowledgeBase(root / "knowledge.db", root / "unused.json")
        self.kb._initialized = True
        ingredient = RecipeIngredient(
            "生の豚肉", 1, ("minecraft:porkchop",),
        )
        self.kb._recipes = [
            OfficialRecipe(
                recipe_id=f"minecraft:cooked_porkchop_from_{suffix}",
                output_id="minecraft:cooked_porkchop", output_name="焼き豚",
                output_count=1, method=method, ingredients=(ingredient,),
                pattern=(), game_version="1.21.6",
                source_title="Minecraft Java 1.21.6 公式ゲームデータ",
            )
            for suffix, method in (
                ("smelting", "かまどで精錬"),
                ("smoking", "燻製器で調理"),
                ("campfire", "焚き火で調理"),
            )
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_cooking_question_is_stable_knowledge_not_generic_realtime_state(self):
        self.assertTrue(is_minecraft_knowledge_query("この豚肉を焼くにはどうすればいい？"))
        self.assertFalse(is_minecraft_knowledge_query("次はどうすればいい？"))

    def test_raw_ingredient_finds_official_output_and_all_cooking_methods(self):
        recipes = self.kb.search_recipes(
            "この豚肉を焼くにはどうすればいい？", limit=3,
        )
        self.assertEqual({item.output_name for item in recipes}, {"焼き豚"})
        self.assertEqual(
            {item.method for item in recipes},
            {"かまどで精錬", "燻製器で調理", "焚き火で調理"},
        )

    def test_verified_cooking_answer_is_short_and_actionable(self):
        answer = self.kb.verified_recipe_answer(
            "この豚肉を焼くにはどうすればいい？",
        ) or ""
        self.assertIn("かまどの上段", answer)
        self.assertIn("燃料を下段", answer)
        self.assertIn("焼き豚", answer)
        self.assertIn("燻製器", answer)
        self.assertIn("焚き火", answer)

    def test_generic_meat_question_explains_the_shared_cooking_operation(self):
        answer = self.kb.verified_recipe_answer("肉の焼き方を教えて") or ""
        self.assertIn("生肉", answer)
        self.assertIn("かまどの上段", answer)
        self.assertIn("燃料を下段", answer)


class SpokenRecipeCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "knowledge.db"
        conn = sqlite3.connect(self.db)
        conn.execute("""
            CREATE TABLE minecraft_corrections (
                correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_version TEXT NOT NULL, output_id TEXT NOT NULL,
                item_name TEXT NOT NULL, user_text TEXT NOT NULL,
                previous_assistant_text TEXT NOT NULL, canonical_fact TEXT NOT NULL,
                verified_against TEXT NOT NULL, created_at REAL NOT NULL,
                UNIQUE(game_version, output_id, canonical_fact)
            )
        """)
        conn.commit()
        conn.close()
        self.kb = MinecraftKnowledgeBase(self.db, root / "unused.json")
        self.kb._initialized = True
        self.kb._recipes = [OfficialRecipe(
            recipe_id="minecraft:shield", output_id="minecraft:shield", output_name="盾",
            output_count=1, method="作業台（配置あり）",
            ingredients=(
                RecipeIngredient("任意の板材", 6, ("minecraft:oak_planks",), "minecraft:wooden_tool_materials"),
                RecipeIngredient("鉄インゴット", 1, ("minecraft:iron_ingot",)),
            ),
            pattern=("WoW", "WWW", " W "), game_version="1.21.6",
            source_title="Minecraft Java 1.21.6 公式ゲームデータ",
        )]
        self.kb._item_names = {"minecraft:iron_ingot": "鉄インゴット", "minecraft:stick": "棒"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_correction_is_verified_and_persisted(self):
        result = self.kb.maybe_apply_spoken_correction(
            "違うよ、盾は木材6個と鉄インゴット1個だよ",
            "盾は木材4個と鉄2個で作れるよ。",
        )
        self.assertTrue(result and result.verified)
        self.assertIn("任意の板材×6", result.canonical_fact)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM minecraft_corrections").fetchone()[0], 1)
        finally:
            conn.close()

    def test_wrong_count_is_rejected_without_mutating_database(self):
        result = self.kb.maybe_apply_spoken_correction(
            "違う、盾は木材6個と鉄インゴット2個だよ",
            "盾の話をしていた。",
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("鉄インゴットは2ではなく1", result.reason)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM minecraft_corrections").fetchone()[0], 0)
        finally:
            conn.close()

    def test_recipe_context_uses_only_exact_named_output_when_available(self):
        self.kb._recipes.append(OfficialRecipe(
            recipe_id="minecraft:crafter", output_id="minecraft:crafter", output_name="自動作業台",
            output_count=1, method="作業台（配置あり）",
            ingredients=(RecipeIngredient("鉄インゴット", 5, ("minecraft:iron_ingot",)),),
            pattern=("###",), game_version="1.21.6", source_title="official",
        ))
        context = self.kb.context_for("盾の材料と作り方") or ""
        self.assertIn("盾×1", context)
        self.assertNotIn("自動作業台", context)
        self.assertNotIn("洞窟探索", context)
        natural_context = self.kb.context_for("盾はどうやってつくるの？") or ""
        self.assertIn("任意の板材×6", natural_context)


if __name__ == "__main__":
    unittest.main()
