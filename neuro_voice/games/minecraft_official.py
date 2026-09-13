"""Import exact Minecraft Java recipes from the locally installed game data.

Recipes in the client JAR are the data files the game itself consumes.  They
are a substantially safer source for counts, ingredient tags and crafting
patterns than hand-written wiki summaries, and also keep modded/older profiles
from accidentally receiving advice for a different release.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MinecraftInstall:
    version: str
    jar_path: Path
    version_json: Path | None = None
    latest_release: str = ""


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    name: str
    count: int
    ids: tuple[str, ...] = ()
    tag: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "count": self.count, "ids": list(self.ids), "tag": self.tag}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecipeIngredient":
        return cls(
            name=str(value.get("name", "")), count=max(1, int(value.get("count", 1))),
            ids=tuple(str(item) for item in value.get("ids", []) if str(item)),
            tag=str(value.get("tag", "")),
        )


@dataclass(frozen=True, slots=True)
class OfficialRecipe:
    recipe_id: str
    output_id: str
    output_name: str
    output_count: int
    method: str
    ingredients: tuple[RecipeIngredient, ...]
    pattern: tuple[str, ...]
    game_version: str
    source_title: str


_TAG_LABELS = {
    "planks": "任意の板材", "wooden_tool_materials": "任意の板材",
    "wooden_slabs": "任意の木材のハーフブロック", "logs": "任意の原木",
    "logs_that_burn": "燃える任意の原木", "coals": "石炭または木炭",
    "wool": "任意の羊毛", "wool_carpets": "任意のカーペット",
    "stone_crafting_materials": "丸石などの石材", "stone_tool_materials": "丸石などの石材",
    "iron_tool_materials": "鉄インゴット", "gold_tool_materials": "金インゴット",
    "diamond_tool_materials": "ダイヤモンド", "netherite_tool_materials": "ネザライトインゴット",
    "boats": "任意のボート", "chest_boats": "任意のチェスト付きボート",
    "beds": "任意のベッド", "banners": "任意の旗", "arrows": "任意の矢",
    "dyes": "任意の染料", "flowers": "任意の花", "sand": "任意の砂",
    "smelts_to_glass": "砂または赤い砂", "fishes": "任意の魚",
}

_METHODS = {
    "crafting_shaped": "作業台（配置あり）",
    "crafting_shapeless": "作業台（配置は自由）",
    "smelting": "かまどで精錬",
    "blasting": "溶鉱炉で精錬",
    "smoking": "燻製器で調理",
    "campfire_cooking": "焚き火で調理",
    "stonecutting": "石切台",
    "smithing_transform": "鍛冶台で強化",
    "smithing_trim": "鍛冶台で装飾",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def discover_install(*, version: str = "auto", jar_path: str = "", minecraft_home: str = "") -> MinecraftInstall | None:
    """Choose the most recently played release/profile, not merely newest JAR."""
    explicit = Path(os.path.expandvars(jar_path)).expanduser() if jar_path else None
    if explicit and explicit.is_file():
        version_json = explicit.with_suffix(".json")
        return MinecraftInstall(explicit.stem, explicit, version_json if version_json.exists() else None)

    home = Path(os.path.expandvars(minecraft_home)).expanduser() if minecraft_home else Path(
        os.environ.get("APPDATA", str(Path.home()))
    ) / ".minecraft"
    versions = home / "versions"
    manifest = _read_json(versions / "version_manifest_v2.json")
    latest_release = str((manifest.get("latest") or {}).get("release", ""))

    def install_for(version_id: str) -> MinecraftInstall | None:
        if not version_id:
            return None
        folder = versions / version_id
        jar = folder / f"{version_id}.jar"
        meta = folder / f"{version_id}.json"
        if jar.is_file():
            return MinecraftInstall(version_id, jar, meta if meta.exists() else None, latest_release)
        inherited = str(_read_json(meta).get("inheritsFrom", "")) if meta.exists() else ""
        return install_for(inherited) if inherited and inherited != version_id else None

    if version and version.lower() != "auto":
        return install_for(version)

    profiles = _read_json(home / "launcher_profiles.json").get("profiles") or {}
    ordered = sorted(
        (value for value in profiles.values() if isinstance(value, dict)),
        key=lambda item: str(item.get("lastUsed", "")), reverse=True,
    )
    for profile in ordered:
        game_dir = Path(str(profile.get("gameDir", ""))) if profile.get("gameDir") else None
        if game_dir:
            direct = game_dir / f"{game_dir.name}.jar"
            if direct.is_file():
                meta = direct.with_suffix(".json")
                return MinecraftInstall(game_dir.name, direct, meta if meta.exists() else None, latest_release)
        found = install_for(str(profile.get("lastVersionId", "")))
        if found:
            return found

    found = install_for(latest_release)
    if found:
        return found
    try:
        candidates = sorted(versions.glob("*/*.jar"), key=lambda item: item.stat().st_mtime, reverse=True)
    except (OSError, PermissionError):
        candidates = []
    if candidates:
        jar = candidates[0]
        meta = jar.with_suffix(".json")
        return MinecraftInstall(jar.stem, jar, meta if meta.exists() else None, latest_release)
    return None


def _load_languages(install: MinecraftInstall, archive: zipfile.ZipFile) -> dict[str, str]:
    translations: dict[str, str] = {}
    try:
        translations.update(json.loads(archive.read("assets/minecraft/lang/en_us.json")))
    except (KeyError, ValueError, TypeError):
        pass
    meta = _read_json(install.version_json) if install.version_json else {}
    asset_id = str((meta.get("assetIndex") or {}).get("id", ""))
    # <home>/versions/<version>/<version>.jar -> <home>/assets
    home = install.jar_path.parent.parent.parent
    index = _read_json(home / "assets" / "indexes" / f"{asset_id}.json") if asset_id else {}
    language = ((index.get("objects") or {}).get("minecraft/lang/ja_jp.json") or {})
    digest = str(language.get("hash", ""))
    if digest:
        localized = home / "assets" / "objects" / digest[:2] / digest
        try:
            translations.update(json.loads(localized.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    return {str(key): str(value) for key, value in translations.items()}


def load_official_recipes(install: MinecraftInstall) -> tuple[list[OfficialRecipe], dict[str, str]]:
    """Read recipes, item tags and localized names from an official client JAR."""
    with zipfile.ZipFile(install.jar_path) as archive:
        names = set(archive.namelist())
        translations = _load_languages(install, archive)
        tags: dict[str, list[str]] = {}
        prefix = "data/minecraft/tags/item/"
        for name in names:
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            try:
                value = json.loads(archive.read(name))
            except (ValueError, TypeError):
                continue
            tag_id = "minecraft:" + name[len(prefix):-5]
            tags[tag_id] = [
                str(item.get("id", "")) if isinstance(item, dict) else str(item)
                for item in value.get("values", [])
            ]

        def resolve_tag(tag_id: str, seen: set[str] | None = None) -> tuple[str, ...]:
            seen = set() if seen is None else seen
            if tag_id in seen:
                return ()
            seen.add(tag_id)
            result: list[str] = []
            for value in tags.get(tag_id, []):
                if value.startswith("#"):
                    result.extend(resolve_tag(value[1:], seen))
                elif value:
                    result.append(value)
            return tuple(dict.fromkeys(result))

        def item_name(item_id: str) -> str:
            path = item_id.split(":", 1)[-1]
            return translations.get(f"item.minecraft.{path}") or translations.get(
                f"block.minecraft.{path}"
            ) or path.replace("_", " ")

        item_names: dict[str, str] = {}
        for key, value in translations.items():
            match = re.match(r"(?:item|block)\.minecraft\.(.+)$", key)
            if match:
                item_names[f"minecraft:{match.group(1)}"] = value

        def ingredient(raw: Any, count: int = 1) -> RecipeIngredient | None:
            if isinstance(raw, list):
                options = [ingredient(item, 1) for item in raw]
                options = [item for item in options if item is not None]
                if not options:
                    return None
                return RecipeIngredient(
                    " または ".join(dict.fromkeys(item.name for item in options)), count,
                    tuple(dict.fromkeys(x for item in options for x in item.ids)),
                )
            if isinstance(raw, dict):
                raw = raw.get("item") or ("#" + str(raw["tag"]) if raw.get("tag") else raw.get("id"))
            if not isinstance(raw, str) or not raw:
                return None
            if raw.startswith("#"):
                tag_id = raw[1:]
                ids = resolve_tag(tag_id)
                path = tag_id.split(":", 1)[-1]
                label = _TAG_LABELS.get(path)
                if not label:
                    examples = [item_name(item) for item in ids[:3]]
                    label = " / ".join(examples) if len(ids) <= 3 else f"{path.replace('_', ' ')}（例: {'、'.join(examples)}）"
                return RecipeIngredient(label, count, ids, tag_id)
            return RecipeIngredient(item_name(raw), count, (raw,))

        recipes: list[OfficialRecipe] = []
        recipe_prefix = "data/minecraft/recipe/"
        for name in sorted(names):
            if not name.startswith(recipe_prefix) or not name.endswith(".json"):
                continue
            try:
                raw = json.loads(archive.read(name))
            except (ValueError, TypeError):
                continue
            result = raw.get("result")
            if isinstance(result, str):
                output_id, output_count = result, 1
            elif isinstance(result, dict):
                output_id = str(result.get("id") or result.get("item") or "")
                output_count = max(1, int(result.get("count", 1)))
            else:
                continue
            if not output_id:
                continue
            recipe_type = str(raw.get("type", "")).split(":")[-1]
            method = _METHODS.get(recipe_type)
            if not method:
                continue
            ingredients: list[RecipeIngredient] = []
            pattern: tuple[str, ...] = ()
            if recipe_type == "crafting_shaped":
                pattern = tuple(str(line) for line in raw.get("pattern", []))
                key = raw.get("key") or {}
                for symbol, value in key.items():
                    amount = sum(line.count(str(symbol)) for line in pattern)
                    parsed = ingredient(value, max(1, amount))
                    if parsed:
                        ingredients.append(parsed)
            elif recipe_type == "crafting_shapeless":
                parsed_items = [ingredient(value) for value in raw.get("ingredients", [])]
                grouped: dict[tuple[str, tuple[str, ...], str], int] = {}
                for item in (x for x in parsed_items if x is not None):
                    key = (item.name, item.ids, item.tag)
                    grouped[key] = grouped.get(key, 0) + 1
                ingredients = [RecipeIngredient(key[0], count, key[1], key[2]) for key, count in grouped.items()]
            elif recipe_type in {"smelting", "blasting", "smoking", "campfire_cooking", "stonecutting"}:
                parsed = ingredient(raw.get("ingredient"))
                if parsed:
                    ingredients.append(parsed)
            elif recipe_type in {"smithing_transform", "smithing_trim"}:
                for label, field in (("鍛冶型", "template"), ("ベース", "base"), ("追加素材", "addition")):
                    parsed = ingredient(raw.get(field))
                    if parsed:
                        ingredients.append(RecipeIngredient(f"{label}: {parsed.name}", 1, parsed.ids, parsed.tag))
            if not ingredients:
                continue
            recipe_id = "minecraft:" + name[len(recipe_prefix):-5]
            recipes.append(OfficialRecipe(
                recipe_id=recipe_id, output_id=output_id, output_name=item_name(output_id),
                output_count=output_count, method=method, ingredients=tuple(ingredients),
                pattern=pattern, game_version=install.version,
                source_title=f"Minecraft Java {install.version} 公式ゲームデータ",
            ))
    return recipes, item_names


def format_recipe(recipe: OfficialRecipe) -> str:
    materials = "、".join(f"{item.name}×{item.count}" for item in recipe.ingredients)
    result = f"{recipe.output_name}×{recipe.output_count}: 方法={recipe.method}; 材料={materials}"
    if recipe.pattern:
        result += "; 配置=" + " / ".join(line.replace(" ", "・") for line in recipe.pattern)
    return result
