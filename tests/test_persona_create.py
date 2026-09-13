"""ペルソナの新規追加。

守りたいのは3つ:

* **キーはファイル名になる。** `mind_<key>.db` などがここから作られる
  ので、`..` や `/` を通さない。
* **config.yaml のコメントを消さない。** あれは設定の説明そのもの。
* **作っただけでは切り替わらない。** 記憶も関係値も空の人格へいきなり
  入れ替わると、取り消しが効かない。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from neuro_voice.ui.webview_app import (
    _PERSONA_KEY_RE, _persist_yaml_new_preset,
)

SAMPLE = """\
# 先頭のコメント
persona:
  active: neuro              # 使用するペルソナ
  presets:
    neuro:
      name: ポッポ
      first_person: 私
      system_prompt: |
        あなたは「ポッポ」。
        短く話す。
# presets の外のコメント
tts:
  backend: voicevox
"""


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# キーの検査
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "..", "../other", "a/b", "A", "1st", "", " ", "x" * 33, "a-b", "ポッポ",
])
def test_dangerous_keys_are_rejected(key):
    """**キーはファイル名になる。** 別ペルソナのファイルを指せないこと。"""
    assert not _PERSONA_KEY_RE.match(key), key


@pytest.mark.parametrize("key", ["momiji", "a", "p2", "my_persona", "x" * 32])
def test_ordinary_keys_are_accepted(key):
    assert _PERSONA_KEY_RE.match(key), key


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def test_a_new_preset_is_appended(config: Path):
    assert _persist_yaml_new_preset(
        config, "momiji", {"name": "もみじ", "first_person": "わたし",
                           "voicevox_speaker": 8}, "あなたは「もみじ」。")
    text = config.read_text(encoding="utf-8")
    assert "    momiji:\n" in text
    assert '      name: "もみじ"' in text
    assert "      voicevox_speaker: 8\n" in text
    assert "      system_prompt: |\n        あなたは「もみじ」。\n" in text


def test_the_existing_preset_survives(config: Path):
    _persist_yaml_new_preset(config, "momiji", {"name": "もみじ"}, "こんにちは。")
    text = config.read_text(encoding="utf-8")
    assert "    neuro:\n" in text
    assert "        あなたは「ポッポ」。\n" in text
    assert "        短く話す。\n" in text


def test_comments_are_kept(config: Path):
    """**yaml.dump で書き直さない。** コメントは設定の説明そのもの。"""
    _persist_yaml_new_preset(config, "momiji", {"name": "もみじ"}, "こんにちは。")
    text = config.read_text(encoding="utf-8")
    assert "# 先頭のコメント" in text
    assert "# presets の外のコメント" in text
    assert "active: neuro              # 使用するペルソナ" in text


def test_the_new_preset_stays_inside_presets(config: Path):
    """**`presets` の外へ落ちない。** 落ちると読み込み時に消える。"""
    _persist_yaml_new_preset(config, "momiji", {"name": "もみじ"}, "こんにちは。")
    lines = config.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line == "    momiji:")
    tail = "\n".join(lines[index:])
    assert "# presets の外のコメント" not in tail.split("    momiji:")[0]
    assert lines.index("  presets:") < index
    assert index < lines.index("tts:")


def test_it_parses_back_as_yaml(config: Path):
    yaml = pytest.importorskip("yaml")
    _persist_yaml_new_preset(
        config, "momiji", {"name": "もみじ", "first_person": ""},
        "あなたは「もみじ」。\n短く話す。")
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    presets = data["persona"]["presets"]
    assert set(presets) == {"neuro", "momiji"}
    assert presets["momiji"]["name"] == "もみじ"
    # **空欄はそのまま空。** 書かれていない項目が人格を作らない。
    assert presets["momiji"]["first_person"] == ""
    assert presets["momiji"]["system_prompt"].splitlines() == [
        "あなたは「もみじ」。", "短く話す。"]
    # 既存側は無傷。
    assert presets["neuro"]["name"] == "ポッポ"
    assert data["persona"]["active"] == "neuro"
    assert data["tts"]["backend"] == "voicevox"


def test_a_colon_in_the_name_does_not_break_the_file(config: Path):
    """`名前: 変な: 値` を素で書くと YAML が壊れる。**引用する。**"""
    yaml = pytest.importorskip("yaml")
    _persist_yaml_new_preset(config, "odd", {"name": 'も: み"じ'}, "テスト。")
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["persona"]["presets"]["odd"]["name"] == 'も: み"じ'


# ---------------------------------------------------------------------------
# 作った後に「適用」できるか（往復）
# ---------------------------------------------------------------------------


def test_every_field_apply_writes_can_be_written_back(config: Path):
    """**作っただけで終わりにしない。** 次に「適用」を押した時に、
    `apply_persona()` が書き換える全部の鍵が実在すること。

    最初に出した版はここが抜けていて、`conversation_traits` の行が
    無いために `_persist_yaml_mapping()` が False を返し、実機で
    **追加した直後のペルソナだけ**「config.yaml への保存は一部失敗」
    と出た。型のテストだけでは見つからない——往復させないと分からない。
    """
    from neuro_voice.memory.persona import PERSONA_FIELDS
    from neuro_voice.ui.webview_app import (
        _persist_yaml_block, _persist_yaml_mapping, _persist_yaml_value,
    )

    traits = {"curiosity": 0.5, "humor": 0.5}
    assert _persist_yaml_new_preset(
        config, "momiji", {"name": "もみじ", "first_person": ""}, "", traits)

    base = "persona.presets.momiji."
    for field, _, _ in PERSONA_FIELDS:
        assert _persist_yaml_value(config, base + field, "テスト"), field
    assert _persist_yaml_block(config, base + "system_prompt", "書き換え。")
    assert _persist_yaml_mapping(
        config, base + "conversation_traits", {"curiosity": 0.9})

    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    preset = data["persona"]["presets"]["momiji"]
    assert preset["conversation_traits"] == {"curiosity": 0.9}
    assert preset["system_prompt"].strip() == "書き換え。"
    assert data["persona"]["presets"]["neuro"]["name"] == "ポッポ"


def test_empty_traits_still_leave_a_writable_key(config: Path):
    from neuro_voice.ui.webview_app import _persist_yaml_mapping

    assert _persist_yaml_new_preset(config, "momiji", {"name": "もみじ"}, "", None)
    assert _persist_yaml_mapping(
        config, "persona.presets.momiji.conversation_traits", {"humor": 0.4})


# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------


def test_a_preset_is_removed(config: Path):
    from neuro_voice.ui.webview_app import _remove_yaml_preset

    yaml = pytest.importorskip("yaml")
    _persist_yaml_new_preset(config, "momiji", {"name": "もみじ"}, "こんにちは。")
    assert _remove_yaml_preset(config, "momiji")
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert set(data["persona"]["presets"]) == {"neuro"}
    # 残す方は無傷。
    assert data["persona"]["presets"]["neuro"]["name"] == "ポッポ"
    assert data["persona"]["presets"]["neuro"]["system_prompt"].strip() == "あなたは「ポッポ」。\n短く話す。"
    assert data["tts"]["backend"] == "voicevox"


def test_deleting_keeps_comments_and_neighbours(config: Path):
    from neuro_voice.ui.webview_app import _remove_yaml_preset

    _persist_yaml_new_preset(config, "aaa", {"name": "A"}, "Aだよ。")
    _persist_yaml_new_preset(config, "bbb", {"name": "B"}, "Bだよ。")
    assert _remove_yaml_preset(config, "aaa")
    text = config.read_text(encoding="utf-8")
    assert "# 先頭のコメント" in text
    assert "# presets の外のコメント" in text
    assert "    bbb:\n" in text
    assert "    aaa:\n" not in text
    assert "Aだよ。" not in text
    assert "Bだよ。" in text


def test_deleting_an_unknown_preset_is_reported(config: Path):
    from neuro_voice.ui.webview_app import _remove_yaml_preset

    assert not _remove_yaml_preset(config, "nope")
    assert "    neuro:\n" in config.read_text(encoding="utf-8")


def test_delete_never_touches_the_data_files():
    """**記憶ファイルは消さない。** 消すと戻せない。

    設定を1つ消したつもりで、そのペルソナと話した記録が全部
    無くなるのがいちばんまずい。
    """
    source = Path(__file__).resolve().parents[1] / "neuro_voice/ui/webview_app.py"
    text = source.read_text(encoding="utf-8")
    body = text[text.index("    def delete_persona("):
                text.index("    def apply_persona(")]
    for forbidden in ("unlink", "rmtree", "shutil", "os.remove"):
        assert forbidden not in body, forbidden
    # 残っているファイルは伝える。消せないのと知らせないのは違う。
    assert "_persona_data_files(" in body
    assert "remaining_files" in body


def test_delete_refuses_the_active_and_the_last_one():
    source = Path(__file__).resolve().parents[1] / "neuro_voice/ui/webview_app.py"
    text = source.read_text(encoding="utf-8")
    body = text[text.index("    def delete_persona("):
                text.index("    def apply_persona(")]
    assert "使用中のペルソナは削除できません" in body
    assert "最後のペルソナは削除できません" in body
    # 判定は削除の**前**にあること。
    assert body.index("最後のペルソナは削除できません") < body.index("_remove_yaml_preset(")


def test_the_data_file_list_covers_every_per_persona_file():
    """`mind.py` が作るペルソナ別ファイルを、一覧が取りこぼさないこと。"""
    from neuro_voice.ui.webview_app import PERSONA_DATA_PATTERNS

    mind = (Path(__file__).resolve().parents[1]
            / "neuro_voice/mind/mind.py").read_text(encoding="utf-8")
    known = {pattern.format(key="K") for pattern in PERSONA_DATA_PATTERNS}
    for match in re.finditer(r'f"([a-z_]+)_\{[^}]*key[^}]*\}(\.[a-z]+)"', mind):
        assert f"{match.group(1)}_K{match.group(2)}" in known, match.group(0)


def test_the_ui_has_the_delete_control():
    html = (Path(__file__).resolve().parents[1]
            / "neuro_voice/ui/assets/index.html").read_text(encoding="utf-8")
    assert 'id="personaDelete"' in html
    start = html.index('$("personaDelete").addEventListener')
    body = html[start:start + 900]
    assert "delete_persona" in body
    # **確認を挟む。** 押し間違いで設定が消えないように。
    assert "confirm(" in body


def test_a_missing_file_is_reported(tmp_path: Path):
    assert not _persist_yaml_new_preset(
        tmp_path / "nope.yaml", "momiji", {"name": "もみじ"}, "こんにちは。")


def test_a_config_without_presets_is_reported(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("persona:\n  name: ポッポ\n", encoding="utf-8")
    assert not _persist_yaml_new_preset(path, "momiji", {"name": "もみじ"}, "x")


# ---------------------------------------------------------------------------
# 追加しただけでは切り替わらない
# ---------------------------------------------------------------------------


def test_creating_does_not_switch_the_active_persona():
    """**`create_persona` は `persona.active` を触らない。**

    作った直後に切り替えると、記憶も関係値も空の人格へいきなり
    入れ替わる。名前と性格を書いてから「適用」で切り替える方が、
    取り消しが効く。
    """
    source = Path(__file__).resolve().parents[1] / "neuro_voice/ui/webview_app.py"
    text = source.read_text(encoding="utf-8")
    start = text.index("    def create_persona(")
    body = text[start:text.index("    def apply_persona(", start)]
    assert 'cfg.set("persona.active"' not in body
    assert "switch_persona" not in body
    assert "persona.active" not in body


def test_the_ui_does_not_switch_on_add():
    html = (Path(__file__).resolve().parents[1]
            / "neuro_voice/ui/assets/index.html").read_text(encoding="utf-8")
    start = html.index('$("personaAdd").addEventListener')
    body = html[start:start + 900]
    assert "create_persona" in body
    assert "apply_persona" not in body
    assert 'renderPersonaPreset(r.key)' in body


def test_the_ui_has_the_add_controls():
    html = (Path(__file__).resolve().parents[1]
            / "neuro_voice/ui/assets/index.html").read_text(encoding="utf-8")
    for element in ("personaNewKey", "personaNewName", "personaAdd"):
        assert re.search(rf'id="{element}"', html), element
