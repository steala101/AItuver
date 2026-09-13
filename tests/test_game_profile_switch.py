"""ゲームプロファイルを、config.yaml を手で書き換えずに会話で切り替える。

これまでは `handle_final_input` の冒頭で、選択中がKTANEでなければ即座に
戻っていた。つまり Minecraft のまま「爆弾解除を始めよう」と言っても
**何も起きなかった**。音声で話しかける相手に、モードを変えるためだけに
エディタを開かせる作りになっていた。

切り替えは状態の変更なので、どの発話がそれに当たるかはコードが決める
（第5条）。ただし線引きは狭くする: 「切り替えて」と読める言い方と、
プロファイルの別名が**両方そろった時だけ**動く。片方だけなら普通の会話
として通す。半分がその確認のテスト。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from neuro_voice.games.session import GameProfileSessionManager

KTANE = "keep_talking_and_nobody_explodes"


class FakeConfig:
    """`persist` が呼ばれたことまで見えるようにした最小の設定。"""

    def __init__(self, values: dict):
        self._values = dict(values)
        self.persisted: list[tuple[str, object]] = []

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def persist(self, key, value):
        self._values[key] = value
        self.persisted.append((key, value))
        return True


def _write_profiles(root: Path) -> None:
    (root / "minecraft").mkdir(parents=True)
    (root / "minecraft" / "profile.json").write_text(json.dumps({
        "id": "minecraft", "display_name": "Minecraft",
        "aliases": ["マイクラ", "minecraft"],
        "assistant_role": "ゲーム仲間", "interaction_mode": "visual_companion",
        "vision_policy": "observe_gameplay",
        "conversation_instruction": "一緒に遊ぶ",
    }, ensure_ascii=False), encoding="utf-8")
    (root / KTANE).mkdir(parents=True)
    (root / KTANE / "profile.json").write_text(json.dumps({
        "id": KTANE, "display_name": "Keep Talking and Nobody Explodes",
        "display_name_ja": "完全爆弾解除マニュアル",
        "aliases": ["keep talking and nobody explodes", "ktane", "爆弾解除", "爆弾処理班"],
        "assistant_role": "マニュアル担当", "interaction_mode": "manual_expert",
        "vision_policy": "manual_expert_no_bomb_view",
        "conversation_instruction": "画面は見ない",
        "manual": {"version": "1-ja", "verification_code": "122"},
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "game_profiles"
    _write_profiles(root)
    cfg = FakeConfig({"game_profiles.root": str(root), "video.game_profile": "minecraft"})
    return GameProfileSessionManager(cfg, tmp_path / "game_session.json")


def say(manager, text):
    return manager.handle_final_input(text, actor_id="a", source="local")


# ---------------------------------------------------------------------------
# 切り替わること
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "爆弾解除モードにして",
    "ktaneに切り替えて",
    "完全爆弾解除マニュアルに変えて",
    "爆弾処理班モードに切り替えて",
])
def test_a_switch_request_changes_the_selected_profile(manager, text):
    outcome = say(manager, text)
    assert outcome.handled
    assert manager.selected.id == KTANE
    assert manager._cfg.persisted == [("video.game_profile", KTANE)]


def test_switching_back_works_too(manager):
    say(manager, "爆弾解除モードにして")
    outcome = say(manager, "マイクラに戻して")
    assert outcome.handled
    assert manager.selected.id == "minecraft"


def test_the_longest_alias_wins(manager):
    """「keep talking」で途中一致して終わらないこと。"""
    say(manager, "keep talking and nobody explodesに切り替えて")
    assert manager.selected.id == KTANE


def test_switching_to_the_profile_already_selected_says_so(manager):
    outcome = say(manager, "マイクラにして")
    assert outcome.handled
    assert outcome.reason == "game_profile_unchanged"
    assert manager._cfg.persisted == [], "変わっていないのに書き戻さない"


# ---------------------------------------------------------------------------
# 開始発話ひとつで、切り替えて始める
# ---------------------------------------------------------------------------


def test_starting_from_another_profile_switches_first(manager):
    """マイクラのまま「爆弾解除を始めよう」で通るようにするのが本題。"""
    outcome = say(manager, "爆弾解除を始めよう")
    assert outcome.handled
    assert manager.selected.id == KTANE
    assert manager.active
    assert outcome.reason == "game_profile_switched_and_started"


def test_starting_when_already_on_the_profile_does_not_claim_to_switch(manager):
    say(manager, "爆弾解除モードにして")
    manager._cfg.persisted.clear()
    outcome = say(manager, "爆弾解除を始めよう")
    assert outcome.reason == "game_profile_session_started"
    assert manager._cfg.persisted == []


def test_switching_away_ends_a_running_session(manager):
    """別のゲームへ移ったのに爆弾解除が生き残ると、持ち主のいない状態になる。"""
    say(manager, "爆弾解除を始めよう")
    assert manager.active
    say(manager, "マイクラに戻して")
    assert not manager.active


# ---------------------------------------------------------------------------
# 誤爆しないこと（線引きの本体）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "静かにして",
    "もう少し声を大きくして",
    "音量を変えて",
    "その話は後にして",
    "マイクラの話なんだけどさ",          # 名前はあるが切り替えの意図がない
    "昨日ktaneの動画を見たよ",
    "爆弾解除って難しそうだよね",
])
def test_ordinary_speech_does_not_switch_anything(manager, text):
    outcome = say(manager, text)
    assert not outcome.handled, f"普通の会話を切り替えとして扱った: {text}"
    assert manager.selected.id == "minecraft"
    assert manager._cfg.persisted == []


def test_an_unknown_game_is_not_invented(manager):
    outcome = say(manager, "スプラトゥーンモードに切り替えて")
    assert outcome.handled
    assert outcome.reason == "game_profile_unknown"
    assert "Minecraft" in outcome.reply
    assert "完全爆弾解除マニュアル" in outcome.reply
    assert manager.selected.id == "minecraft", "存在しないプロファイルへ移らない"


def test_a_switch_verb_without_a_game_word_is_left_alone(manager):
    """「静かにして」で使えるゲームの一覧を読み上げ始めない。"""
    assert not say(manager, "静かにして").handled


# ---------------------------------------------------------------------------
# 今どのモードか
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "今どのゲームモード？",
    "今のモードは？",
    "いま何のゲームモードだっけ",
])
def test_the_current_mode_can_be_asked(manager, text):
    outcome = say(manager, text)
    assert outcome.handled
    assert outcome.reason == "game_profile_status"
    assert "Minecraft" in outcome.reply


def test_the_status_reports_whether_a_session_is_running(manager):
    assert "まだ始めてない" in say(manager, "今のモードは？").reply
    say(manager, "爆弾解除を始めよう")
    assert "進行中" in say(manager, "今のモードは？").reply


# ---------------------------------------------------------------------------
# 切り替えは設定として残る
# ---------------------------------------------------------------------------


def test_the_choice_is_written_back_to_the_config_file(manager):
    """再起動しても同じモードで立ち上がるように。"""
    say(manager, "爆弾解除モードにして")
    assert ("video.game_profile", KTANE) in manager._cfg.persisted


def test_the_switch_takes_effect_without_a_restart(manager):
    """`selected` は毎回configを読み直すので、その場で効く。"""
    say(manager, "爆弾解除モードにして")
    assert "完全爆弾解除マニュアル" in manager.grounded_context()
    say(manager, "爆弾解除を始めよう")
    assert "完全爆弾解除マニュアル" in manager.grounded_context()


# ---------------------------------------------------------------------------
# 切り替えただけの状態でも、答えられることは答える
# ---------------------------------------------------------------------------


def test_the_manual_version_is_available_before_the_session_starts(manager):
    """「爆弾解除モードにして」の直後に版と検証コードを聞かれても答えられること。

    以前はセッション開始前の `grounded_context` が None を返していたので、
    ポッポの手元には何の情報も無かった。しかもKTANE中はウェブ検索も止まる。
    「始めよう」と言うまで版すら答えられないのは、利用者から見えない前提だった。
    """
    say(manager, "爆弾解除モードにして")
    context = manager.grounded_context()
    assert context is not None
    assert "1-ja" in context
    assert "122" in context


def test_the_idle_context_says_the_session_has_not_started(manager):
    say(manager, "爆弾解除モードにして")
    assert "未開始" in manager.grounded_context()


def test_the_manual_facts_come_from_the_profile_not_from_code(manager, tmp_path):
    """`profile.json` を直したら発話も変わること。

    版と検証コードは `profile.json` / `manual/sources.json` / `session.py` の
    3箇所に書かれていた。JSONを直してもコードが古い値を言い続ける状態だった。
    """
    path = tmp_path / "game_profiles" / KTANE / "profile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["manual"] = {"version": "2-ja", "verification_code": "999"}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    manager.registry.reload()
    say(manager, "爆弾解除を始めよう")
    context = manager.grounded_context()
    assert "2-ja" in context and "999" in context
    assert "1-ja" not in context and "122" not in context


def test_a_profile_without_a_manual_says_nothing_about_one(manager, tmp_path):
    """マニュアルを持たないプロファイルで、中身の無い参照文を作らない。"""
    assert manager.registry.get("minecraft").manual_reference == ""
    path = tmp_path / "game_profiles" / KTANE / "profile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["manual"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    manager.registry.reload()
    say(manager, "爆弾解除モードにして")
    assert "参照マニュアル" not in manager.grounded_context()
