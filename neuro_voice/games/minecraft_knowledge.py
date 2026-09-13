"""Local, source-attributed Minecraft guidance retrieval.

The bundled pack is deliberately concise and authored as original summaries.
It is stored in SQLite on first use so querying never needs the network or an
LLM.  Additional user-authored/licensed JSON packs can be merged later without
changing the retrieval path.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from neuro_voice.games.minecraft_official import (
    MinecraftInstall, OfficialRecipe, RecipeIngredient, discover_install,
    format_recipe, load_official_recipes,
)

logger = logging.getLogger(__name__)

_COOKING_QUERY_TERMS = (
    "焼き方", "焼くには", "焼きたい", "どう焼", "調理", "料理",
    "どうやって焼", "焼ける", "焼けば", "かまど", "燻製器", "くん製器",
    "焚き火", "精錬",
)
_COOKING_METHODS = frozenset({
    "かまどで精錬", "燻製器で調理", "焚き火で調理",
})


def is_minecraft_knowledge_query(text: str) -> bool:
    """Return whether a Minecraft turn asks for stable game knowledge.

    These questions should be answered from the active Java client's official
    data before a costly current-frame inference is considered.  Explicit
    requests to inspect the screen are handled separately by the vision owner.
    """
    compact = re.sub(r"\s+", "", text or "").casefold()
    if not compact:
        return False
    return any(term in compact for term in (
        "作り方", "つくり方", "どう作", "どうやって作", "どうやってつく",
        "作るには", "つくるには", "作りたい", "つくりたい", "材料", "素材",
        "レシピ", "クラフト", "何個必要", "何が必要", "必要なもの", "必要な物",
        "配置", "入手方法", "どこで手に入",
        *_COOKING_QUERY_TERMS,
    ))


@dataclass(frozen=True, slots=True)
class MinecraftKnowledgeEntry:
    entry_id: str
    title: str
    body: str
    tags: tuple[str, ...]
    source_title: str
    source_url: str


@dataclass(frozen=True, slots=True)
class MinecraftCorrectionResult:
    status: str
    item_name: str = ""
    canonical_fact: str = ""
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def system_context(self) -> str:
        if self.verified:
            return (
                "Minecraft知識訂正の検証結果: ユーザーの訂正は、現在プレイ中バージョンの"
                f"公式ゲームデータと一致した。DBへ検証済み訂正として保存済み。\n{self.canonical_fact}\n"
                "訂正へのお礼を短く伝え、以前の誤回答を言い訳せず正しい内容を答えること。"
            )
        return (
            "Minecraft知識訂正の検証結果: まだDBは変更していない。"
            f"理由={self.reason}。公式データで確認できた内容: {self.canonical_fact or 'なし'}。"
            "ユーザーへ盲目的に同意せず、確認結果を穏やかに伝えること。"
        )


class MinecraftKnowledgeBase:
    """Small SQLite RAG store for gameplay guidance, independent of the web."""

    def __init__(
        self, database_path: str | Path, seed_path: str | Path, *,
        game_version: str = "auto", jar_path: str = "", minecraft_home: str = "",
        auto_sync: bool = True, verify_corrections: bool = True,
    ):
        self._database_path = Path(database_path)
        self._seed_path = Path(seed_path)
        self._game_version = game_version
        self._jar_path = jar_path
        self._minecraft_home = minecraft_home
        self._auto_sync = bool(auto_sync)
        self._verify_corrections = bool(verify_corrections)
        self._initialized = False
        self._memory_entries: list[MinecraftKnowledgeEntry] = []
        self._recipes: list[OfficialRecipe] = []
        self._item_names: dict[str, str] = {}
        self._install: MinecraftInstall | None = None
        self._verified_corrections: dict[str, str] = {}

    @classmethod
    def from_config(cls, cfg) -> "MinecraftKnowledgeBase":
        root = Path(__file__).resolve().parents[2]
        database = Path(str(cfg.get("game_assistant.minecraft.database_path", "data/minecraft_knowledge.db")))
        if not database.is_absolute():
            database = root / database
        seed = Path(str(cfg.get("game_assistant.minecraft.seed_path", "data/minecraft_knowledge.json")))
        if not seed.is_absolute():
            seed = root / seed
        return cls(
            database, seed,
            game_version=str(cfg.get("game_assistant.minecraft.version", "auto")),
            jar_path=str(cfg.get("game_assistant.minecraft.jar_path", "") or ""),
            minecraft_home=str(cfg.get("game_assistant.minecraft.minecraft_home", "") or ""),
            auto_sync=bool(cfg.get("game_assistant.minecraft.auto_sync_official_data", True)),
            verify_corrections=bool(cfg.get("game_assistant.minecraft.verify_spoken_corrections", True)),
        )

    def _load_seed(self) -> list[MinecraftKnowledgeEntry]:
        raw = json.loads(self._seed_path.read_text(encoding="utf-8"))
        entries = raw.get("entries", raw) if isinstance(raw, dict) else raw
        result: list[MinecraftKnowledgeEntry] = []
        for item in entries:
            if not isinstance(item, dict) or not item.get("id") or not item.get("body"):
                continue
            result.append(MinecraftKnowledgeEntry(
                entry_id=str(item["id"]),
                title=str(item.get("title", item["id"])),
                body=str(item["body"]),
                tags=tuple(str(tag).lower() for tag in item.get("tags", []) if str(tag).strip()),
                source_title=str(item.get("source_title", "Bundled original summary")),
                source_url=str(item.get("source_url", "")),
            ))
        return result

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._memory_entries = self._load_seed()
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._database_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS minecraft_knowledge (
                        entry_id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                        tags TEXT NOT NULL, source_title TEXT NOT NULL, source_url TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS minecraft_recipes (
                        recipe_key TEXT PRIMARY KEY, game_version TEXT NOT NULL,
                        recipe_id TEXT NOT NULL, output_id TEXT NOT NULL,
                        output_name TEXT NOT NULL, output_count INTEGER NOT NULL,
                        method TEXT NOT NULL, ingredients_json TEXT NOT NULL,
                        pattern_json TEXT NOT NULL, source_title TEXT NOT NULL,
                        imported_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS minecraft_corrections (
                        correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        game_version TEXT NOT NULL, output_id TEXT NOT NULL,
                        item_name TEXT NOT NULL, user_text TEXT NOT NULL,
                        previous_assistant_text TEXT NOT NULL, canonical_fact TEXT NOT NULL,
                        verified_against TEXT NOT NULL, created_at REAL NOT NULL,
                        UNIQUE(game_version, output_id, canonical_fact)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS minecraft_metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    )
                """)
                # Seed summaries are replaceable supporting advice.  Upsert on
                # every initialization so correcting the bundled pack actually
                # reaches an existing DB instead of being ignored forever.
                conn.executemany(
                    """INSERT INTO minecraft_knowledge VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(entry_id) DO UPDATE SET title=excluded.title, body=excluded.body,
                       tags=excluded.tags, source_title=excluded.source_title, source_url=excluded.source_url""",
                    [(entry.entry_id, entry.title, entry.body, json.dumps(entry.tags, ensure_ascii=False),
                      entry.source_title, entry.source_url) for entry in self._memory_entries],
                )
                if self._auto_sync:
                    self._sync_official_recipes(conn)
                if not self._recipes:
                    self._load_recipes_from_database(conn)
                for output_id, fact in conn.execute(
                    "SELECT output_id, canonical_fact FROM minecraft_corrections ORDER BY created_at"
                ):
                    self._verified_corrections[str(output_id)] = str(fact)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # The seed still provides useful retrieval in read-only installs.
            logger.exception("Minecraft knowledge database initialization failed; using seed in memory")
        self._initialized = True

    @property
    def active_version(self) -> str:
        self._ensure_initialized()
        if self._install is not None:
            return self._install.version
        return self._recipes[0].game_version if self._recipes else "unknown"

    @property
    def recipe_count(self) -> int:
        self._ensure_initialized()
        return len(self._recipes)

    def refresh_official_data(self) -> tuple[str, int]:
        """Force a fresh import from the currently selected launcher profile."""
        self._initialized = False
        self._recipes = []
        self._item_names = {}
        self._install = None
        self._ensure_initialized()
        return self.active_version, self.recipe_count

    def _sync_official_recipes(self, conn: sqlite3.Connection) -> None:
        try:
            install = discover_install(
                version=self._game_version, jar_path=self._jar_path,
                minecraft_home=self._minecraft_home,
            )
        except (OSError, PermissionError):
            logger.debug("Minecraft install discovery is unavailable; using persisted recipes", exc_info=True)
            return
        if install is None:
            logger.warning("Minecraft Java installation not found; retaining the last imported recipes")
            return
        recipes, item_names = load_official_recipes(install)
        if not recipes:
            logger.warning("No explicit recipes found in Minecraft Java %s", install.version)
            return
        self._install, self._recipes, self._item_names = install, recipes, item_names
        conn.execute("DELETE FROM minecraft_recipes WHERE game_version = ?", (install.version,))
        imported_at = time.time()
        conn.executemany(
            """INSERT INTO minecraft_recipes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                f"{recipe.game_version}:{recipe.recipe_id}", recipe.game_version,
                recipe.recipe_id, recipe.output_id, recipe.output_name, recipe.output_count,
                recipe.method,
                json.dumps([item.as_dict() for item in recipe.ingredients], ensure_ascii=False),
                json.dumps(recipe.pattern, ensure_ascii=False), recipe.source_title, imported_at,
            ) for recipe in recipes],
        )
        metadata = {
            "active_version": install.version,
            "latest_release_seen": install.latest_release,
            "recipe_source": str(install.jar_path),
            "recipe_count": str(len(recipes)),
            "last_sync_at": str(imported_at),
        }
        conn.executemany(
            """INSERT INTO minecraft_metadata(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            metadata.items(),
        )
        logger.info(
            "Minecraft official recipes synchronized: version=%s recipes=%d latest_release=%s",
            install.version, len(recipes), install.latest_release or "unknown",
        )

    def _load_recipes_from_database(self, conn: sqlite3.Connection) -> None:
        version_row = conn.execute(
            "SELECT value FROM minecraft_metadata WHERE key='active_version'"
        ).fetchone()
        version = str(version_row[0]) if version_row else ""
        rows = conn.execute(
            """SELECT recipe_id, output_id, output_name, output_count, method,
                      ingredients_json, pattern_json, game_version, source_title
               FROM minecraft_recipes WHERE (? = '' OR game_version = ?)""",
            (version, version),
        ).fetchall()
        self._recipes = [OfficialRecipe(
            recipe_id=str(row[0]), output_id=str(row[1]), output_name=str(row[2]),
            output_count=int(row[3]), method=str(row[4]),
            ingredients=tuple(RecipeIngredient.from_dict(item) for item in json.loads(row[5])),
            pattern=tuple(json.loads(row[6])), game_version=str(row[7]), source_title=str(row[8]),
        ) for row in rows]

    @staticmethod
    def _terms(value: str) -> set[str]:
        text = re.sub(r"\s+", "", (value or "").lower())
        words = set(re.findall(r"[a-z0-9_]+|[ぁ-んァ-ン一-龯ー]+", text))
        grams = {text[index:index + 2] for index in range(max(0, len(text) - 1))}
        common = {"よう", "する", "いる", "ない", "こと", "これ", "それ", "ため", "から", "まで", "ので", "です", "ます"}
        return (words | grams) - common

    def search(self, query: str, *, limit: int = 3) -> list[MinecraftKnowledgeEntry]:
        self._ensure_initialized()
        query_terms = self._terms(query)
        if not query_terms:
            return []
        scored: list[tuple[int, MinecraftKnowledgeEntry]] = []
        for entry in self._memory_entries:
            title_terms = self._terms(entry.title)
            body_terms = self._terms(entry.body)
            tag_terms = self._terms(" ".join(entry.tags))
            title_hits = len(query_terms & title_terms)
            tag_hits = len(query_terms & tag_terms)
            body_hits = len(query_terms & body_terms)
            # Japanese bigrams are useful for game terms but a lone ordinary
            # phrase fragment must not turn everyday chat into a game lookup.
            score = 5 * title_hits + 3 * tag_hits + body_hits
            if title_hits or tag_hits or body_hits >= 2:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1].entry_id))
        return [entry for _, entry in scored[:max(1, limit)]]

    @staticmethod
    def _is_recipe_query(text: str) -> bool:
        return is_minecraft_knowledge_query(text)

    @staticmethod
    def _is_cooking_query(text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "").casefold()
        return any(term in compact for term in _COOKING_QUERY_TERMS)

    def search_recipes(self, query: str, *, limit: int = 3) -> list[OfficialRecipe]:
        self._ensure_initialized()
        compact = re.sub(r"\s+", "", query or "").casefold()
        query_terms = self._terms(query)
        scored: list[tuple[int, bool, bool, OfficialRecipe]] = []
        for recipe in self._recipes:
            output_compact = re.sub(r"\s+", "", recipe.output_name).casefold()
            id_path = recipe.output_id.split(":", 1)[-1].replace("_", " ")
            output_exact = bool(output_compact and output_compact in compact)
            ingredient_names = [
                re.sub(r"^(?:生の|任意の|燃える任意の)", "", item.name).casefold()
                for item in recipe.ingredients
            ]
            ingredient_exact = any(
                len(name) >= 2 and name in compact for name in ingredient_names
            )
            title_hits = len(query_terms & self._terms(recipe.output_name))
            id_hits = len(query_terms & self._terms(id_path))
            ingredient_hits = len(query_terms & self._terms(" ".join(item.name for item in recipe.ingredients)))
            method_hit = (
                ("かまど" in compact and recipe.method == "かまどで精錬")
                or (("燻製" in compact or "くん製" in compact) and recipe.method == "燻製器で調理")
                or ("焚き火" in compact and recipe.method == "焚き火で調理")
            )
            score = (
                (1000 if output_exact else 0)
                + (500 if ingredient_exact else 0)
                + (100 if method_hit else 0)
                + title_hits * 12
                + id_hits * 8
                + ingredient_hits
            )
            if output_exact or ingredient_exact or title_hits or id_hits:
                scored.append((score, output_exact, ingredient_exact, recipe))
        if any(output_exact for _, output_exact, _, _ in scored):
            scored = [item for item in scored if item[1]]
        elif any(ingredient_exact for _, _, ingredient_exact, _ in scored):
            scored = [item for item in scored if item[2]]
        scored.sort(key=lambda pair: (-pair[0], pair[3].recipe_id))
        # Multiple paths can produce the same output. Keep distinct methods,
        # but place the ordinary crafting recipe first.
        return [recipe for _, _, _, recipe in scored[:max(1, limit)]]

    def verified_recipe_answer(self, user_text: str) -> str | None:
        """Build a short spoken cooking answer from official client recipes.

        Only deterministic cooking facts are surfaced directly. Other recipe
        questions continue through the normal conversation planner with the
        official rows injected as evidence.
        """
        if not self._is_cooking_query(user_text):
            return None
        compact = re.sub(r"\s+", "", user_text or "")
        if "肉" in compact:
            named_meats = {
                re.sub(r"^(?:生の|任意の|燃える任意の)", "", ingredient.name)
                for recipe in self._recipes
                if recipe.method in _COOKING_METHODS
                for ingredient in recipe.ingredients
                if "肉" in ingredient.name
            }
            if named_meats and not any(
                len(name) >= 2 and name in compact for name in named_meats
            ):
                return (
                    "牛肉や豚肉などの生肉は、かまどの上段、石炭や木炭などの燃料を"
                    "下段に入れれば焼けるよ。燻製器なら速く、焚き火なら燃料なしでも焼ける。"
                )
        recipes = [
            item for item in self.search_recipes(user_text, limit=12)
            if item.method in _COOKING_METHODS
        ]
        if not recipes:
            # "肉の焼き方" omits the animal, so no single official output can
            # be selected. The operation is nevertheless common to the
            # official beef/pork/chicken/mutton recipes.
            has_official_meat_recipe = any(
                item.method in _COOKING_METHODS
                and any("肉" in ingredient.name for ingredient in item.ingredients)
                for item in self._recipes
            )
            if "肉" in compact and has_official_meat_recipe:
                return (
                    "牛肉や豚肉などの生肉は、かまどの上段、石炭や木炭などの燃料を"
                    "下段に入れれば焼けるよ。燻製器なら速く、焚き火なら燃料なしでも焼ける。"
                )
            return None
        # Keep alternative cooking methods for the same official output only.
        target_output = recipes[0].output_id
        recipes = [item for item in recipes if item.output_id == target_output]
        primary = next(
            (item for item in recipes if item.method == "かまどで精錬"),
            recipes[0],
        )
        ingredient = primary.ingredients[0].name if primary.ingredients else "材料"
        result = primary.output_name
        methods = {item.method for item in recipes}
        if "焚き火" in compact and "焚き火で調理" in methods:
            return f"{ingredient}を焚き火に置いて待てば、{result}になるよ。燃料は要らない。"
        if ("燻製" in compact or "くん製" in compact) and "燻製器で調理" in methods:
            return (
                f"{ingredient}を燻製器の上段、石炭や木炭などの燃料を下段に入れれば、"
                f"{result}になるよ。かまどより速く焼ける。"
            )
        answer = (
            f"{ingredient}をかまどの上段、石炭や木炭などの燃料を下段に入れれば、"
            f"{result}になるよ。"
        )
        alternatives: list[str] = []
        if "燻製器で調理" in methods:
            alternatives.append("燻製器ならもっと速い")
        if "焚き火で調理" in methods:
            alternatives.append("焚き火でも燃料なしで焼ける")
        if alternatives:
            answer += " " + "し、".join(alternatives) + "よ。"
        return answer

    @staticmethod
    def _number(value: str) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
        digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if value in digits:
            return digits[value]
        if len(value) == 2 and value[0] == "十" and value[1] in digits:
            return 10 + digits[value[1]]
        if len(value) == 2 and value[1] == "十" and value[0] in digits:
            return digits[value[0]] * 10
        return None

    @staticmethod
    def _ingredient_aliases(ingredient: RecipeIngredient) -> tuple[str, ...]:
        name = re.sub(r"^(?:任意の|燃える任意の)", "", ingredient.name)
        aliases = {ingredient.name, name}
        if "板材" in ingredient.name:
            aliases.update({"板材", "木材", "木の板"})
        if "原木" in ingredient.name:
            aliases.update({"原木", "丸太"})
        if "石炭または木炭" in ingredient.name:
            aliases.update({"石炭", "木炭"})
        if "石材" in ingredient.name:
            aliases.update({"丸石", "石材"})
        return tuple(sorted((item for item in aliases if len(item) >= 2), key=len, reverse=True))

    @classmethod
    def _mentioned_count(cls, text: str, aliases: tuple[str, ...]) -> tuple[bool, int | None]:
        compact = re.sub(r"\s+", "", text or "")
        number = r"([0-9]+|[一二三四五六七八九十]+)"
        for alias in aliases:
            escaped = re.escape(alias)
            after = re.search(rf"{escaped}(?:が|を|は|で)?(?:×|x)?{number}(?:個|つ|本|枚)?", compact, re.I)
            before = re.search(rf"{number}(?:個|つ|本|枚)?(?:の)?{escaped}", compact, re.I)
            match = after or before
            if match:
                return True, cls._number(match.group(1))
            if alias in compact:
                return True, None
        return False, None

    def maybe_apply_spoken_correction(
        self, user_text: str, previous_assistant_text: str,
    ) -> MinecraftCorrectionResult | None:
        """Verify a natural correction against the active Java recipe data."""
        if not self._verify_corrections:
            return None
        compact = re.sub(r"\s+", "", user_text or "")
        if not any(term in compact for term in (
            "違う", "間違", "じゃなく", "ではなく", "正しくは", "訂正",
            "本当は", "必要だよ", "必要なの", "レシピは", "材料は",
        )):
            return None
        self._ensure_initialized()
        combined = f"{user_text} {previous_assistant_text}"
        mentioned = [
            recipe for recipe in self._recipes
            if recipe.output_name and recipe.output_name in combined
        ]
        if not mentioned:
            mentioned = self.search_recipes(combined, limit=1)
        if not mentioned:
            return MinecraftCorrectionResult("insufficient", reason="訂正対象のアイテムを特定できない")
        recipe = sorted(mentioned, key=lambda item: len(item.output_name), reverse=True)[0]
        canonical = format_recipe(recipe)
        allowed_ids = {item_id for ingredient in recipe.ingredients for item_id in ingredient.ids}
        any_ingredient = False
        explicit_count = False
        mismatches: list[str] = []
        for ingredient in recipe.ingredients:
            found, claimed = self._mentioned_count(user_text, self._ingredient_aliases(ingredient))
            if not found:
                continue
            any_ingredient = True
            if claimed is not None:
                explicit_count = True
                if claimed != ingredient.count:
                    mismatches.append(f"{ingredient.name}は{claimed}ではなく{ingredient.count}")

        # Reject a clearly quantified extra item such as "棒を2本" when the
        # active recipe does not accept it.  Only exact localized item names
        # are considered so ordinary Japanese nouns do not become false hits.
        for item_id, localized in self._item_names.items():
            if item_id in allowed_ids or item_id == recipe.output_id or len(localized) < 2:
                continue
            found, claimed = self._mentioned_count(user_text, (localized,))
            if found and claimed is not None:
                mismatches.append(f"{localized}は公式材料に含まれない")
                break
        if mismatches:
            return MinecraftCorrectionResult(
                "rejected", recipe.output_name, canonical, "、".join(mismatches),
            )
        if not any_ingredient or not explicit_count:
            return MinecraftCorrectionResult(
                "insufficient", recipe.output_name, canonical,
                "材料名と個数を公式データへ一意に照合できない",
            )

        try:
            conn = sqlite3.connect(str(self._database_path))
            try:
                conn.execute(
                    """INSERT INTO minecraft_corrections(
                           game_version, output_id, item_name, user_text,
                           previous_assistant_text, canonical_fact, verified_against, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(game_version, output_id, canonical_fact) DO UPDATE SET
                           user_text=excluded.user_text,
                           previous_assistant_text=excluded.previous_assistant_text,
                           verified_against=excluded.verified_against,
                           created_at=excluded.created_at""",
                    (recipe.game_version, recipe.output_id, recipe.output_name, user_text,
                     previous_assistant_text, canonical, recipe.source_title, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.exception("Verified Minecraft correction could not be saved")
            return MinecraftCorrectionResult(
                "rejected", recipe.output_name, canonical, f"DB保存に失敗: {exc}",
            )
        self._verified_corrections[recipe.output_id] = canonical
        logger.info("Verified Minecraft correction saved: %s / %s", recipe.output_name, canonical)
        return MinecraftCorrectionResult("verified", recipe.output_name, canonical, recipe.source_title)

    @staticmethod
    def _observation_text(observation: Any | None) -> str:
        if observation is None:
            return ""
        values: Iterable[Any] = (
            getattr(observation, "summary", ""),
            *getattr(observation, "objects", []),
            *getattr(observation, "actions", []),
            *getattr(observation, "visible_text", []),
            *getattr(observation, "player_state", []),
            *getattr(observation, "notable_events", []),
        )
        return " ".join(str(item) for item in values if item)

    def context_for(self, user_text: str, observation: Any | None = None, *, limit: int = 3) -> str | None:
        query = f"{user_text} {self._observation_text(observation)}"
        recipe_query = self._is_recipe_query(user_text)
        recipes = self.search_recipes(user_text, limit=min(3, limit)) if recipe_query else []
        # Once an exact official recipe is available, do not dilute it with a
        # loosely matched hand-authored gameplay summary.  This was the main
        # source of plausible-sounding but irrelevant recipe guidance.
        entries = [] if recipes else self.search(query, limit=max(1, limit))
        if not entries:
            # A generic "what next?" benefits from the core path, but do not
            # inject it into unrelated conversation merely because the game is open.
            compact = re.sub(r"\s+", "", user_text or "")
            if any(term in compact for term in ("次", "何をす", "進め", "攻略", "初心者", "どうす")):
                entries = [entry for entry in self._memory_entries if entry.entry_id == "progression.overview"][:1]
        if not entries and not recipes:
            return None
        lines = [
            "Minecraft local knowledge (verified). Exact recipe data below comes from the active "
            "Java client JAR and overrides memory, general web knowledge, and model guesses."
        ]
        for recipe in recipes:
            lines.append(f"- [公式レシピ / Java {recipe.game_version}] {format_recipe(recipe)} [{recipe.source_title}]")
            correction = self._verified_corrections.get(recipe.output_id)
            if correction:
                lines.append(f"  [会話中に公式照合済みの訂正] {correction}")
        for entry in entries:
            source = f" [{entry.source_title}]" if entry.source_title else ""
            lines.append(f"- {entry.title}: {entry.body}{source}")
        if recipe_query and not recipes:
            lines.append(
                "- この質問に一致する明示的な公式レシピは現在のJARから見つからない。"
                "特殊クラフト・MOD追加物・別Editionの可能性があるため、材料や個数を推測しない。"
            )
        return "\n".join(lines)

    def quick_answer(self, user_text: str, observation: Any | None = None) -> str | None:
        """Return an exact local answer for a few safe, high-frequency starter questions."""
        self._ensure_initialized()
        compact = re.sub(r"\s+", "", user_text or "")
        entry_id = None
        if any(term in compact for term in ("始めたばかり", "最初", "何をしたら", "何すれば", "まずどう", "初心者")):
            entry_id = "early.first_day"
        elif any(term in compact for term in (
            "体力", "死にそう", "危ない", "逃げ", "襲われ", "囲まれ", "追われ",
            "ゾンビ", "クリーパー", "スケルトン", "敵が", "敵に",
        )):
            entry_id = "survival.low_health"
        elif any(term in compact for term in ("ネザー行", "ネザーに", "ポータル")):
            entry_id = "nether.preparation"
        if entry_id is None:
            return None
        entry = next((item for item in self._memory_entries if item.entry_id == entry_id), None)
        return entry.body if entry is not None else None
