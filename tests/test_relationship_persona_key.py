"""Phase 7D ⑥: 関係値を `persona_id + person_id` で持つ。

ファイル名（`relationships_<key>.json`）では既に分かれていたが、
**中身に持ち主が書かれていなかった。** 名前だけの分離は、ファイルを
コピー・改名・復元した瞬間に崩れる——そして崩れても気づけない。

守りたいのは3つ:

* **他ペルソナのファイルを読まない**
* **他ペルソナのファイルを空で上書きしない**（読めないより悪い）
* 持ち主の無い旧ファイルは、**その1つのペルソナへ引き取る**
  （全ペルソナへ配らない）。冪等で、取り消せる。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from neuro_voice.mind.relationship import RelationshipStore


class FakeConfig:
    def __init__(self, values: dict | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name, fallback=None):
        return self._values.get(name, fallback)


def _write(path: Path, persona_id, entries: dict) -> None:
    data: dict = {"schema_version": 2, "relationships": entries}
    if persona_id is not None:
        data["persona_id"] = persona_id
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


ENTRY = {"chibi": {"speaker_id": "chibi", "trust": 0.8, "interaction_count": 9}}


@pytest.fixture()
def cfg() -> FakeConfig:
    return FakeConfig()


# ---------------------------------------------------------------------------
# 持ち主が書かれる
# ---------------------------------------------------------------------------


def test_the_owner_is_written(tmp_path: Path, cfg):
    store = RelationshipStore(tmp_path / "r.json", cfg, persona_id="poppo")
    store.observe("chibi")
    store.save()
    raw = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert raw["persona_id"] == "poppo"
    assert raw["schema_version"] == 2


def test_the_same_owner_loads_normally(tmp_path: Path, cfg):
    path = tmp_path / "r.json"
    _write(path, "poppo", ENTRY)
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert store.get("chibi").interaction_count == 9
    assert not store.readonly


def test_no_persona_id_keeps_the_old_behaviour(tmp_path: Path, cfg):
    """`persona_id` を渡さない呼び出しは**素通し**。既存経路を壊さない。"""
    path = tmp_path / "r.json"
    _write(path, "poppo", ENTRY)
    store = RelationshipStore(path, cfg)
    assert store.get("chibi").interaction_count == 9
    assert not store.readonly


# ---------------------------------------------------------------------------
# 他ペルソナのファイル
# ---------------------------------------------------------------------------


def test_a_foreign_file_is_not_read(tmp_path: Path, cfg):
    path = tmp_path / "r.json"
    _write(path, "luna", ENTRY)
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert store.readonly
    assert store.ownership()["foreign_owner"] == "luna"
    # 既定値の新規状態になっていること（相手の値を持っていない）。
    assert store.get("chibi").interaction_count == 0


def test_a_foreign_file_is_not_overwritten(tmp_path: Path, cfg):
    """**空で上書きしない。** 読めないより、消える方がずっと悪い。"""
    path = tmp_path / "r.json"
    _write(path, "luna", ENTRY)
    store = RelationshipStore(path, cfg, persona_id="poppo")
    store.observe("chibi")
    store.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["persona_id"] == "luna"
    assert raw["relationships"]["chibi"]["interaction_count"] == 9


def test_the_compat_flag_allows_reading_a_foreign_file(tmp_path: Path):
    """互換 fallback は**フラグ付き**。既定では効かない。"""
    path = tmp_path / "r.json"
    _write(path, "luna", ENTRY)
    cfg = FakeConfig({"mind.relationship.allow_foreign_persona_file": True})
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert not store.readonly
    assert store.get("chibi").interaction_count == 9


# ---------------------------------------------------------------------------
# 旧ファイルの引き取り
# ---------------------------------------------------------------------------


def test_an_unowned_file_is_adopted_by_this_persona(tmp_path: Path, cfg):
    """**引き取りであって配布ではない。** ファイル名で既に分かれている。"""
    path = tmp_path / "r.json"
    _write(path, None, ENTRY)
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert store.ownership()["adopted_legacy"]
    assert store.get("chibi").interaction_count == 9
    store.save()
    assert json.loads(path.read_text(encoding="utf-8"))["persona_id"] == "poppo"


def test_adoption_is_idempotent(tmp_path: Path, cfg):
    """一度ヘッダが付けば、二度目は引き取りにならない。"""
    path = tmp_path / "r.json"
    _write(path, None, ENTRY)
    RelationshipStore(path, cfg, persona_id="poppo").save()
    again = RelationshipStore(path, cfg, persona_id="poppo")
    assert not again.ownership()["adopted_legacy"]
    assert again.get("chibi").interaction_count == 9


def test_a_second_persona_cannot_adopt_the_same_file(tmp_path: Path, cfg):
    """**全ペルソナへ自動コピーしない。** 引き取れるのは1人だけ。"""
    path = tmp_path / "r.json"
    _write(path, None, ENTRY)
    RelationshipStore(path, cfg, persona_id="poppo").save()
    other = RelationshipStore(path, cfg, persona_id="luna")
    assert other.readonly
    assert other.get("chibi").interaction_count == 0


def test_an_empty_unowned_file_is_not_an_adoption(tmp_path: Path, cfg):
    """中身が無いなら引き取るものも無い。記録を増やさない。"""
    path = tmp_path / "r.json"
    _write(path, None, {})
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert not store.ownership()["adopted_legacy"]


def test_the_adoption_can_be_rolled_back(tmp_path: Path, cfg):
    """**値は動かしていない**ので、ヘッダを消すだけで元に戻る。"""
    path = tmp_path / "r.json"
    _write(path, None, ENTRY)
    store = RelationshipStore(path, cfg, persona_id="poppo")
    assert store.rollback_adoption()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["persona_id"] == ""
    assert raw["relationships"]["chibi"]["interaction_count"] == 9
    # 二度目は何もしない。
    assert not store.rollback_adoption()


def test_rollback_does_nothing_without_an_adoption(tmp_path: Path, cfg):
    path = tmp_path / "r.json"
    _write(path, "poppo", ENTRY)
    assert not RelationshipStore(path, cfg, persona_id="poppo").rollback_adoption()


# ---------------------------------------------------------------------------
# 実際に渡されているか
# ---------------------------------------------------------------------------


def test_mind_passes_the_persona_to_every_relationship_store():
    """**定義があるだけでは効かない。**

    `bind_persona()` が一度も呼ばれていなかった件（⑤）と同じ形を
    繰り返さないために、渡している箇所の数を数えて比べる。
    """
    text = (Path(__file__).resolve().parents[1]
            / "neuro_voice/mind/mind.py").read_text(encoding="utf-8")
    built = text.count("RelationshipStore(")
    passed = text.count("persona_id=self._persona_key") + text.count("persona_id=key")
    assert built >= 4, "RelationshipStore を作る箇所が減っている。テストを見直すこと"
    assert passed == built, f"{built} 箇所で作り、持ち主を渡すのは {passed} 箇所"
