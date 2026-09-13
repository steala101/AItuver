"""Phase 7D ⑤: **書き込み側**にペルソナを付ける。

Phase 7C は読み側だけを直した。書き込みが空のままだったので、
**新しく作られる記憶は全部「所有者不明」**で溜まっていた。空のまま
溜めると、あとで分離を有効にした瞬間に全部隠れる。

実機ログでも `bind_persona()` は定義があるだけで**一度も呼ばれて
いなかった**。ここは「呼ばれているか」まで見る。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from neuro_voice.cognition.persona_scope import PersonaScope
from neuro_voice.mind.store import MemoryStore


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "mind_test.db")


def _row(store: MemoryStore, memory_id: int) -> sqlite3.Row:
    return store._conn.execute(
        "SELECT persona_id, scope FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# 持ち主が付く
# ---------------------------------------------------------------------------


def test_a_bound_persona_owns_new_facts(store: MemoryStore):
    store.bind_persona("poppo")
    memory_id = store.add("紅茶が好き", scope=str(PersonaScope.PERSONA_PRIVATE))
    assert memory_id
    row = _row(store, memory_id)
    assert row["persona_id"] == "poppo"
    assert row["scope"] == str(PersonaScope.PERSONA_PRIVATE)


def test_a_bound_persona_owns_new_episodes(store: MemoryStore):
    store.bind_persona("poppo")
    memory_id = store.add_episode(
        "合言葉を教わった", scope=str(PersonaScope.PERSONA_PRIVATE))
    assert _row(store, memory_id)["persona_id"] == "poppo"


def test_an_explicit_owner_wins_over_the_bound_one(store: MemoryStore):
    """呼び出し側が指定したら、そちらを使う。"""
    store.bind_persona("poppo")
    memory_id = store.add_episode("共有の事実", persona_id="luna",
                                  scope=str(PersonaScope.PERSONA_PRIVATE))
    assert _row(store, memory_id)["persona_id"] == "luna"


def test_shared_scopes_do_not_need_an_owner(store: MemoryStore):
    """世界状態やツール定義に持ち主は要らない。**拒否しない。**"""
    for scope in (PersonaScope.GLOBAL_SYSTEM, PersonaScope.WORLD_SHARED,
                  PersonaScope.EXPLICIT_SHARED):
        assert store.add_episode(f"共有 {scope}", scope=str(scope))


def test_reflections_carry_the_owner(store: MemoryStore):
    """**仮説にも持ち主を付ける。**

    仮説は複数の記憶から作られるので、元の記憶が分かれていても
    仮説が共有だと、そこから逆に漏れる。
    """
    store.bind_persona("poppo")
    store.save_reflection({"reflection_id": "r1", "statement": "短く答えるとよい"})
    row = store._conn.execute(
        "SELECT persona_id, scope FROM reflections WHERE reflection_id = 'r1'"
    ).fetchone()
    assert row["persona_id"] == "poppo"
    assert row["scope"] == str(PersonaScope.PERSONA_PRIVATE)


# ---------------------------------------------------------------------------
# 持ち主が空のとき
# ---------------------------------------------------------------------------


def test_an_unowned_private_memory_is_kept_by_default(store: MemoryStore):
    """**既定では保存する。** 警告だけ。

    `persona_scope_enabled` が false の間は、持ち主が空でも検索条件に
    入らないので読める。そこで拒否すると**読める記憶を落とす**。
    """
    memory_id = store.add_episode("持ち主不明",
                                  scope=str(PersonaScope.PERSONA_PRIVATE))
    assert memory_id
    assert _row(store, memory_id)["persona_id"] == ""


def test_strict_mode_refuses_an_unowned_private_memory(store: MemoryStore, caplog):
    """分離が有効なら**保存しない。** 共有へ昇格させない。"""
    store.bind_persona("", strict=True)
    assert store.add_episode("持ち主不明",
                             scope=str(PersonaScope.PERSONA_PRIVATE)) == 0
    assert store.add("持ち主不明", scope=str(PersonaScope.PERSONA_PRIVATE)) == 0
    assert store.save_reflection({"reflection_id": "r1", "statement": "x"}) == 0
    assert store._conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0


def test_strict_mode_still_allows_shared_scopes(store: MemoryStore):
    store.bind_persona("", strict=True)
    assert store.add_episode("世界の事実",
                             scope=str(PersonaScope.WORLD_SHARED))


def test_an_unscoped_write_is_not_refused(store: MemoryStore):
    """スコープを書いていない古い呼び出しは**そのまま通す。**

    ここで拒否すると、まだ直していない経路の記憶が黙って消える。
    """
    store.bind_persona("", strict=True)
    assert store.add_episode("スコープ無し")
    assert store.add("スコープ無し")


# ---------------------------------------------------------------------------
# 実際に呼ばれているか
# ---------------------------------------------------------------------------


def test_mind_binds_the_persona_wherever_it_builds_a_store():
    """**定義があるだけでは効かない。**

    実機ログで `bind_persona()` が一度も呼ばれておらず、読み側の
    絞り込みも書き込み側の持ち主も空のままだった。Store を作り直す
    所（初期化とペルソナ切替）の両方で結ぶこと。
    """
    text = (Path(__file__).resolve().parents[1]
            / "neuro_voice/mind/mind.py").read_text(encoding="utf-8")
    builds = text.count("self._episodes = EpisodicMemoryService(")
    binds = text.count("self._bind_persona_scope()")
    assert builds >= 2, "Store を作る所が減っている。テストを見直すこと"
    assert binds == builds, f"Store を {builds} 箇所で作り、結ぶのは {binds} 箇所"


def test_the_write_side_binds_regardless_of_the_flag():
    """書き込みの持ち主は**フラグに関係なく**付ける。

    空のまま溜めると、あとで分離を有効にした瞬間に全部隠れる。
    フラグで変えてよいのは「拒否するか」だけ。
    """
    text = (Path(__file__).resolve().parents[1]
            / "neuro_voice/mind/mind.py").read_text(encoding="utf-8")
    start = text.index("    def _bind_persona_scope(")
    body = text[start:text.index("    def persona_switch(", start)]
    assert "self._store.bind_persona(key, strict=scoped)" in body
    # 持ち主そのものを条件付きにしていないこと。
    assert "bind_persona(key if scoped else" not in body


def test_the_episode_writer_stamps_the_owner():
    text = (Path(__file__).resolve().parents[1]
            / "neuro_voice/mind/episodes.py").read_text(encoding="utf-8")
    start = text.index("    def _write(")
    body = text[start:start + 900]
    assert "persona_id=self.persona_id" in body
    assert "PERSONA_PRIVATE" in body


# ---------------------------------------------------------------------------
# 古いDBを開けるか
# ---------------------------------------------------------------------------


def test_an_old_database_gains_the_reflection_columns(tmp_path: Path):
    """`CREATE TABLE IF NOT EXISTS` は**既にある表に列を足さない。**

    足りないまま開くと、仮説の保存が `OperationalError` で落ちる。
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE reflections (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " reflection_id TEXT NOT NULL UNIQUE, target_type TEXT NOT NULL,"
        " statement TEXT NOT NULL, source_memory_ids TEXT DEFAULT '',"
        " support_count INTEGER NOT NULL DEFAULT 0,"
        " contradiction_count INTEGER NOT NULL DEFAULT 0,"
        " confidence REAL NOT NULL DEFAULT 0.3,"
        " action_deltas TEXT DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',"
        " created_at REAL NOT NULL, last_verified_at REAL NOT NULL);")
    conn.commit()
    conn.close()

    store = MemoryStore(path)
    store.bind_persona("poppo")
    assert store.save_reflection({"reflection_id": "r1", "statement": "x"})
    columns = {row[1] for row in store._conn.execute(
        "PRAGMA table_info(reflections)")}
    assert {"persona_id", "scope"} <= columns
