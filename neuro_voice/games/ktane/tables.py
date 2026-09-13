"""表が大きすぎて記憶で書けないモジュールのデータを、外から読む。

キーパッド・心理戦・迷路の3つは、解法そのものは単純だが**表が大きい**。
心理戦は28語ぶんの順序付きリスト、迷路は9面ぶんの壁の配置がある。
これを記憶から書き起こすと、一箇所間違えただけで静かに誤った指示を出す。
爆弾では誤りが爆発になるので、**推測で埋めない**。

そこで解法はコードに置き、表は
`game_profiles/keep_talking_and_nobody_explodes/manual/tables/*.json` から読む。
利用者が公式マニュアル（bombmanual.com、無料公開）を見ながら埋める。
埋まっていなければ「まだ取り込んでいない」と言う。黙って推測しない。

書式は `docs/KTANE_TABLES.md` に書いてある。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class KtaneTables:
    """取り込み済みの表。空のままでも動く（答えないだけ）。"""

    #: 6列 × 各列の記号名。記号は利用者が決めた呼び名で書く。
    keypad_columns: list[list[str]] = field(default_factory=list)
    #: 画面の語 → 読むべきボタンの位置（1〜6、左上から右へ、上段→中段→下段）。
    whos_on_first_positions: dict[str, int] = field(default_factory=dict)
    #: ボタンの語 → 押す候補の順序付きリスト。最初に盤面にある語を押す。
    whos_on_first_order: dict[str, list[str]] = field(default_factory=dict)
    #: 迷路。識別用の丸2つの座標と、通れる辺の一覧。
    mazes: list[dict] = field(default_factory=list)
    #: 読み込みで落ちたファイルの理由。利用者へそのまま見せる。
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def has_keypad(self) -> bool:
        return len(self.keypad_columns) >= 2

    @property
    def has_whos_on_first(self) -> bool:
        return bool(self.whos_on_first_positions and self.whos_on_first_order)

    @property
    def has_mazes(self) -> bool:
        return bool(self.mazes)

    def missing(self) -> list[str]:
        names = []
        if not self.has_keypad:
            names.append("キーパッド")
        if not self.has_whos_on_first:
            names.append("心理戦")
        if not self.has_mazes:
            names.append("迷路")
        return names


def _read(path: Path, name: str, tables: KtaneTables):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        # 壊れたJSONで会話ごと落とさない（第17条）。理由だけ残す。
        tables.errors[name] = str(error)
        return None


def load_tables(folder: str | Path | None) -> KtaneTables:
    """プロファイルのフォルダから表を読む。無い項目は空のまま。"""
    tables = KtaneTables()
    if not folder:
        return tables
    root = Path(folder) / "manual" / "tables"

    keypad = _read(root / "keypad.json", "keypad", tables)
    if isinstance(keypad, dict):
        columns = keypad.get("columns")
        if isinstance(columns, list):
            tables.keypad_columns = [
                [str(symbol) for symbol in column]
                for column in columns
                if isinstance(column, list) and column
            ]

    who = _read(root / "whos_on_first.json", "whos_on_first", tables)
    if isinstance(who, dict):
        positions = who.get("display_to_position")
        if isinstance(positions, dict):
            tables.whos_on_first_positions = {
                _normalise(key): int(value)
                for key, value in positions.items()
                if str(value).isdigit() and 1 <= int(value) <= 6
            }
        order = who.get("label_to_order")
        if isinstance(order, dict):
            tables.whos_on_first_order = {
                _normalise(key): [_normalise(word) for word in value]
                for key, value in order.items()
                if isinstance(value, list) and value
            }

    maze = _read(root / "maze.json", "maze", tables)
    if isinstance(maze, dict):
        entries = maze.get("mazes")
        if isinstance(entries, list):
            tables.mazes = [item for item in entries if isinstance(item, dict) and item.get("passages")]
    return tables


def _normalise(word: str) -> str:
    """表記ゆれを吸収する。空白・記号を落として大文字へ。

    「THEY'RE」「THEYRE」「they re」を別物として持つと、片方しか当たらない。
    """
    return "".join(char for char in str(word or "").upper() if char.isalnum())
