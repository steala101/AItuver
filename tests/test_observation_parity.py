"""Phase 6C: 入力元ごとの接続差を埋める。

Phase 6B で「実イベントへ繋ぐ」と言ったが、繋いだのは3経路だけだった。
Discord の入退室は入らず、画面は1種類しか見ず、KTANE は世界状態を
持っていなかった。**入力元ごとに差があると、片方で動く機能が
もう片方では黙って死ぬ。**

いちばん守りたいのは3つ:

* **Bot 自身を人間の参加者として数えない**
* **退出で Entity を消さない**（消すと再入室のたびに別人になる）
* **再起動しただけで「居る」と言わない**（実際の一覧で確かめ直す）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.goals import GoalStatus, GoalType
from neuro_voice.cognition.observation import (
    ACTIVE_SCREEN, DISCORD_PRESENCE_TTL, KTANE_BOMB, KTANE_EVENTS,
    SCENE_TYPES, SESSION_SCOPED_PREDICATES, from_discord_presence,
    from_ktane_event, from_vision, normalise_scene,
)
from neuro_voice.cognition.runtime import InitiativeRuntime
from neuro_voice.cognition.world import EntityType, ItemStatus

from tests.test_world_wiring import WIRED, Cfg, FakeVision, store_at


def runtime(**overrides) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**{**WIRED, **overrides}))


class Member:
    def __init__(self, user_id, name, bot=False):
        self.id = user_id
        self.display_name = name
        self.bot = bot


BOT = Member(99, "ポッポ", bot=True)


# ===========================================================================
# 1. Discord の入退室
# ===========================================================================


def test_a_join_creates_a_participant():
    """入室 → PARTICIPANT Entity → 会話に居る。"""
    entities, facts = from_discord_presence(
        user_id=7, display_name="ジーレン", present=True, channel="general",
        event_id="e1", now=1000.0)
    assert entities[0].entity_type is EntityType.PARTICIPANT
    assert entities[0].external_id == "discord:7"
    present = next(f for f in facts if f.predicate == "present_in_conversation")
    assert present.value is True
    assert present.source_event_id == "e1"
    assert any(f.predicate == "voice_channel" and f.value == "general" for f in facts)


def test_a_leave_does_not_delete_the_entity():
    """**退出で Entity を消さない。** 消すと再入室のたびに別人になる。"""
    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, channel="general", now=1000.0)
    before = live.world.find("ジーレン")
    assert before is not None
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=False, now=1010.0)
    after = live.world.find("ジーレン")
    assert after is not None and after.entity_id == before.entity_id
    assert live.world.fact("discord:7", "present_in_conversation").value is False


def test_the_channel_is_cleared_on_leave():
    """居ないのに「どのチャンネルに居る」が残らないこと。"""
    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, channel="general", now=1000.0)
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=False, now=1010.0)
    channel = live.world.fact("discord:7", "voice_channel")
    assert channel.value == "" or not channel.usable(now=1020.0)


def test_a_rejoin_reuses_the_same_entity():
    """join → leave → join で**同じ人**として扱う。"""
    live = runtime()
    for present, moment in ((True, 1000.0), (False, 1010.0), (True, 1020.0)):
        live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                      present=present, channel="general",
                                      now=moment)
    participants = [item for item in live.world.entities.values()
                    if str(item.entity_type) == str(EntityType.PARTICIPANT)]
    assert len(participants) == 1
    assert live.world.fact("discord:7", "present_in_conversation").value is True


def test_a_display_name_change_does_not_split_the_person():
    """**表示名だけで人物を分けない。** ID が同じなら同じ人。"""
    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    live.observe_discord_presence(user_id=7, display_name="Zilen",
                                  present=True, now=1010.0)
    assert live.world.fact("discord:7", "present_in_conversation") is not None
    assert len(live.observation_adapter().discord_present) == 1


def test_the_bot_is_never_a_participant():
    """**自分を人間として数えない。**"""
    assert from_discord_presence(user_id=99, display_name="ポッポ",
                                 present=True, is_bot=True) == ([], [])
    live = runtime()
    assert live.observe_discord_presence(user_id=99, display_name="ポッポ",
                                         present=True, is_bot=True) == 0
    assert not live.world.entities


def test_discord_identity_is_kept_apart_from_voice_identity():
    """**接続上の人物と、声紋で推定した人物は別物。**

    どちらかをもう片方の根拠にすると、取り違えが恒久化する。
    """
    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    live.observe_speaker(speaker_id=7, name="チビ", confirmed=True, now=1001.0)
    assert live.world.fact("discord:7", "present_in_conversation") is not None
    assert live.world.fact("speaker:7", "present_in_conversation") is not None
    assert live.world.find("ジーレン") is not live.world.find("チビ")


def test_a_discord_participant_is_less_certain_than_a_voice_match():
    """Discord ID は接続として確実だが、**誰の声かは言っていない**。"""
    discord_entity = from_discord_presence(
        user_id=7, display_name="ジーレン", present=True)[0][0]
    from neuro_voice.cognition.observation import from_speaker

    voice_entity = from_speaker(speaker_id=7, name="チビ", confirmed=True)[0][0]
    assert discord_entity.confidence < voice_entity.confidence


# ---------------------------------------------------------------------------
# 重複・再接続・再起動
# ---------------------------------------------------------------------------


def test_the_same_join_event_applies_once():
    live = runtime()
    for _ in range(5):
        live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                      present=True, event_id="same", now=1000.0)
    assert live.observation_adapter().intake.dropped.get("duplicate_event_id", 0) >= 4


def test_reconcile_uses_the_actual_member_list():
    """**いま実際に居る人**で合わせ直す。イベントの取りこぼしを埋める。"""
    live = runtime()
    result = live.reconcile_discord(
        [Member(7, "ジーレン"), Member(8, "チビ"), BOT], channel="general", now=1000.0)
    assert sorted(result["joined"]) == [7, 8]
    assert result["present"] == 2, "Botを数に入れている"
    assert live.world.fact("discord:7", "present_in_conversation").value is True


def test_reconcile_marks_the_missing_as_gone():
    live = runtime()
    live.reconcile_discord([Member(7, "ジーレン"), Member(8, "チビ")], now=1000.0)
    result = live.reconcile_discord([Member(7, "ジーレン")], now=1010.0)
    assert result["left"] == [8]
    assert live.world.fact("discord:8", "present_in_conversation").value is False
    assert live.world.find("チビ") is not None, "退出で消している"


def test_reconcile_is_idempotent():
    """同じ一覧を何度渡しても、出入りは1回ずつ。"""
    live = runtime()
    live.reconcile_discord([Member(7, "ジーレン")], now=1000.0)
    again = live.reconcile_discord([Member(7, "ジーレン")], now=1010.0)
    assert again["joined"] == [] and again["left"] == []


def test_reconcile_accepts_plain_dicts():
    """Discord.py のオブジェクトでも、既存の `voice_members_snapshot` でも。"""
    live = runtime()
    result = live.reconcile_discord([{"id": 7, "name": "ジーレン"}], now=1000.0)
    assert result["joined"] == [7]


def test_presence_is_not_current_after_a_restart(tmp_path):
    """**再起動しただけで「居る」と言わない。**"""
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_discord_presence(user_id=7, display_name="ジーレン",
                                   present=True, channel="general", now=1000.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    fact = second.world.facts.get("discord:7:present_in_conversation")
    assert fact is not None, "Entityごと消えている"
    assert str(fact.status) == str(ItemStatus.STALE)
    assert not fact.usable(now=2000.0)
    # 人そのものは残る。**関係を積み上げ直させない。**
    assert second.world.find("ジーレン") is not None
    reopened.close()


def test_presence_returns_after_reconnecting(tmp_path):
    """復元後、**実際の参加者一覧を見て初めて**「居る」に戻る。"""
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_discord_presence(user_id=7, display_name="ジーレン",
                                   present=True, now=1000.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    second.reconcile_discord([Member(7, "ジーレン")], now=2001.0)
    assert second.world.fact("discord:7", "present_in_conversation").usable(now=2001.0)
    reopened.close()


def test_the_presence_predicates_are_session_scoped():
    for predicate in ("present_in_conversation", "voice_channel"):
        assert predicate in SESSION_SCOPED_PREDICATES, predicate


def test_presence_does_not_live_forever():
    assert 0 < DISCORD_PRESENCE_TTL <= 3600.0


# ---------------------------------------------------------------------------
# Discord bot 側の配線
# ---------------------------------------------------------------------------


def bot_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "discord_bridge"
            / "bot.py").read_text(encoding="utf-8")


def test_the_bot_calls_the_presence_path():
    text = bot_source()
    assert "self.note_voice_presence()" in text
    assert "def note_voice_presence" in text
    assert "reconcile_discord(" in text


def test_the_presence_path_hangs_off_the_member_refresh():
    """**接続点は1つ。** 入退室・再接続・キックが全部ここを通る。"""
    text = bot_source()
    start = text.index("def _refresh_voice_members")
    assert "self.note_voice_presence()" in text[start:start + 900]


def test_a_presence_change_does_not_speak_directly():
    """**入退室のたびに必ず声が出る実装にしない。**

    注意層まで入れて、話すかどうかは Action Selector と Speech Gate へ。
    """
    text = bot_source()
    start = text.index("def note_voice_presence")
    body = text[start:start + 1600]
    assert "note_attention_event" in body
    for banned in ("self._speak(", "await self._speak", "_greet_"):
        assert banned not in body, banned


def test_participant_change_is_a_real_event_type():
    from neuro_voice.cognition.attention import AttentionEventType

    assert str(AttentionEventType.PARTICIPANT_CHANGE) == "participant_change"


def test_a_presence_change_alone_produces_no_speech_opportunity():
    """出入りしただけで発話機会を作らない。"""
    from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
    from neuro_voice.cognition.initiative import opportunities_from_events

    event = AttentionEvent(
        event_type=str(AttentionEventType.PARTICIPANT_CHANGE),
        source="discord", summary="VCの人数が2人になった", salience=.35)
    assert opportunities_from_events([event], now=1000.0) == []


# ===========================================================================
# 2. 映像の画面種別
# ===========================================================================


def test_only_the_real_scene_types_exist():
    """**架空の分類を足さない。** 正本は `vision/service.py` のプロンプト。"""
    prompt = (Path(__file__).resolve().parents[1] / "neuro_voice" / "vision"
              / "service.py").read_text(encoding="utf-8")
    for scene in SCENE_TYPES:
        if scene == "gameplay":
            assert "gameplay" in prompt
            continue
        assert scene in prompt, f"プロンプトが出さない分類: {scene}"


@pytest.mark.parametrize("raw,expected", [
    ("minecraft_gameplay", "gameplay"),
    ("gameplay", "gameplay"),
    ("menu", "menu"),
    ("MENU", "menu"),
    ("pause menu", "menu"),
    ("loading", "loading"),
    ("error", "error"),
    ("unknown", "unknown"),
    ("", "unknown"),
    ("宇宙船のコックピット", "unknown"),
])
def test_scene_types_are_normalised(raw, expected):
    assert normalise_scene(raw) == expected


def test_an_unrecognised_scene_becomes_unknown_not_a_guess():
    """**近そうな分類へ勝手に寄せない。** メニューを戦闘中と言わない。"""
    assert normalise_scene("shop_screen") == "unknown"
    assert normalise_scene("cutscene") == "unknown"


def test_the_scene_lands_on_the_screen_not_the_session():
    _entities, facts = from_vision(FakeVision(), now=1000.0)
    assert facts[0].subject_id == ACTIVE_SCREEN
    assert facts[0].predicate == "scene"


def test_the_same_scene_does_not_pile_up_facts():
    """**毎フレーム新しい Fact を作らない。**"""
    live = runtime()
    for index in range(50):
        live.observe_vision(FakeVision(observation_id=f"o{index}"),
                            now=1000.0 + index * 5.0)
    assert len(live.world.facts) == 1
    assert live.world.fact(ACTIVE_SCREEN, "scene").value == "gameplay"


def test_a_scene_change_updates_the_same_fact():
    """遷移は UPDATE。**元イベントIDも持ち替える。**"""
    from neuro_voice.cognition.world import DeltaOperation

    live = runtime()
    live.observe_vision(FakeVision(scene_type="menu", observation_id="o1"),
                        now=1000.0)
    live.observe_vision(FakeVision(scene_type="minecraft_gameplay",
                                   observation_id="o2"), now=1010.0)
    fact = live.world.fact(ACTIVE_SCREEN, "scene")
    assert fact.value == "gameplay"
    assert fact.source_event_ids == ("o2",)
    assert len(live.world.facts) == 1
    assert str(live.world.recent_changes[-1].operation) == str(DeltaOperation.UPDATE)


@pytest.mark.parametrize("before,after,transition", [
    ("menu", "minecraft_gameplay", "menu->gameplay"),
    ("minecraft_gameplay", "menu", "gameplay->menu"),
    ("minecraft_gameplay", "error", "gameplay->error"),
    ("minecraft_gameplay", "宇宙船", "gameplay->unknown"),
])
def test_transitions_are_reported(before, after, transition):
    """**種類の数ではなく、変化を捉えられることに意味がある。**"""
    live = runtime()
    live.observe_vision(FakeVision(scene_type=before, observation_id="a"),
                        now=1000.0)
    live.observe_vision(FakeVision(scene_type=after, observation_id="b"),
                        now=1010.0)
    assert live.observation_adapter().last_result.detail["transition"] == transition


def test_staying_on_the_same_scene_is_not_a_transition():
    live = runtime()
    live.observe_vision(FakeVision(observation_id="a"), now=1000.0)
    live.observe_vision(FakeVision(observation_id="b"), now=1010.0)
    assert live.observation_adapter().last_result.detail["transition"] == ""
    assert live.observation_adapter().counters["scene_transitions"] == 1


def test_a_faint_reading_does_not_overwrite_a_confident_scene():
    """**低確信の認識で、いまの画面を即座に塗り替えない。**"""
    live = runtime()
    live.observe_vision(FakeVision(scene_type="minecraft_gameplay",
                                   confidence=.95, observation_id="a"),
                        now=1000.0)
    live.observe_vision(FakeVision(scene_type="menu", confidence=.2,
                                   observation_id="b"), now=1010.0)
    assert live.world.fact(ACTIVE_SCREEN, "scene").value == "gameplay"


def test_a_faint_reading_never_names_a_specific_screen():
    _entities, facts = from_vision(
        FakeVision(scene_type="menu", confidence=.2), now=1000.0)
    assert facts[0].value == "unknown"


def test_the_parity_flag_keeps_the_old_behaviour():
    """フラグを下ろせば Phase 6B と同じ——ゲーム中の画面だけ。"""
    live = runtime(**{"world_state.vision_scene_parity_enabled": False})
    assert live.observe_vision(FakeVision(scene_type="menu"), now=1000.0) == 0
    assert live.observe_vision(FakeVision(scene_type="minecraft_gameplay"),
                               now=1010.0) >= 1


# ===========================================================================
# 3. KTANE
# ===========================================================================


def test_only_the_real_ktane_events_are_accepted():
    """**架空のイベント型を足さない。**"""
    assert from_ktane_event(event="module_exploded_in_style") == ([], [])
    assert set(KTANE_EVENTS) == {
        "bomb_started", "module_detected", "strike_recorded", "bomb_ended"}


def test_starting_a_bomb_creates_the_bomb():
    entities, facts = from_ktane_event(
        event="bomb_started", session_id="s1", event_id="e1", now=1000.0)
    assert entities[0].external_id == KTANE_BOMB
    assert entities[0].entity_type is EntityType.TASK_TARGET
    values = {item.predicate: item.value for item in facts}
    assert values["current_game"] == "ktane"
    assert values["bomb_active"] is True
    assert all(item.source_event_id == "e1" for item in facts)


def test_a_module_becomes_the_current_module():
    live = runtime()
    live.observe_ktane_event(event="module_detected", module="wires",
                             session_id="s1", now=1000.0)
    assert live.world.fact(KTANE_BOMB, "current_module").value == "wires"


def test_an_empty_module_is_not_recorded():
    assert from_ktane_event(event="module_detected", module="") == ([], [])


def test_strikes_are_recorded():
    """**ミスの回数は解法そのものを変える**（サイモン等）。"""
    live = runtime()
    live.observe_ktane_event(event="strike_recorded", strikes=2,
                             session_id="s1", now=1000.0)
    assert live.world.fact(KTANE_BOMB, "strike_count").value == 2


def test_the_same_module_event_applies_once():
    """**同じイベントを複数回受けても、進捗へは一度だけ。**"""
    live = runtime()
    for _ in range(4):
        live.observe_ktane_event(event="module_detected", module="wires",
                                 session_id="s1", event_id="same", now=1000.0)
    assert live.observation_adapter().intake.dropped.get("duplicate_event_id", 0) >= 3
    assert len(live.world.facts) <= 2


def test_ending_a_bomb_records_how_it_ended():
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1", now=1000.0)
    live.observe_ktane_event(event="bomb_ended", outcome="exploded",
                             session_id="s1", now=1100.0)
    assert live.world.fact(KTANE_BOMB, "bomb_active").value is False
    assert live.world.fact(KTANE_BOMB, "session_result").value == "exploded"


def test_an_unstated_ending_stays_unknown():
    """**終わった = 解除できた、ではない。** 言われていないなら分からない。"""
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1", now=1000.0)
    live.observe_ktane_event(event="bomb_ended", session_id="s1", now=1100.0)
    assert live.world.fact(KTANE_BOMB, "session_result").value == "unknown"


def test_the_bomb_state_is_session_scoped():
    for predicate in ("bomb_active", "current_module", "strike_count", "current_game"):
        assert predicate in SESSION_SCOPED_PREDICATES, predicate


def test_a_restart_does_not_resume_the_old_bomb(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_ktane_event(event="bomb_started", session_id="s1", now=1000.0)
    first.observe_ktane_event(event="module_detected", module="wires",
                              session_id="s1", now=1001.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    for key in ("ktane_bomb:bomb_active", "ktane_bomb:current_module"):
        assert not second.world.facts[key].usable(now=2000.0), key
    reopened.close()


# ---------------------------------------------------------------------------
# KTANE の目標
# ---------------------------------------------------------------------------


def test_starting_a_bomb_opens_a_goal():
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1",
                             confidence=.95, now=1000.0)
    goal = next(iter(live.goals.values()))
    assert goal.goal_type is GoalType.GAME_OBJECTIVE
    assert str(goal.status) == str(GoalStatus.ACTIVE)
    assert "解除" in goal.description


def test_a_faint_start_does_not_open_a_goal():
    """**低確信の認識だけで目標を ACTIVE にしない。**"""
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1",
                             confidence=.4, now=1000.0)
    assert not live.goals


def test_only_one_goal_per_bomb():
    live = runtime()
    for _ in range(3):
        live.observe_ktane_event(event="bomb_started", session_id="s1",
                                 confidence=.95, now=1000.0)
    assert len(live.goals) == 1


@pytest.mark.parametrize("outcome,succeeded", [
    ("defused", True), ("exploded", False), ("unknown", True),
])
def test_the_goal_closes_with_an_outcome(outcome, succeeded):
    """**終わったこと（状態）と、うまくいったこと（結果）は別。**"""
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1",
                             confidence=.95, now=1000.0)
    live.observe_ktane_event(event="bomb_ended", outcome=outcome,
                             session_id="s1", now=1100.0)
    goal = next(iter(live.goals.values()))
    assert str(goal.status) == str(GoalStatus.COMPLETED)
    assert goal.outcome == outcome
    assert goal.succeeded is succeeded


def test_an_unfinished_goal_has_no_outcome():
    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1",
                             confidence=.95, now=1000.0)
    assert next(iter(live.goals.values())).succeeded is None


def test_the_goal_never_speaks_on_its_own():
    """目標は行動候補への**補正**まで。発話はしない。"""
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state

    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1",
                             confidence=.95, now=1000.0)
    bias = live.goal_bias()
    assert bias, "補正すら効いていない"
    decision = CognitiveKernel(goal_bias=bias).decide(
        CognitiveEvent(event_type=EventType.SILENCE_TIMEOUT), build_state(), now=1000.0)
    assert decision.selected_action is not None


# ---------------------------------------------------------------------------
# KTANE: セッション管理からの実経路
# ---------------------------------------------------------------------------


class Config:
    def __init__(self, **values):
        self._values = {"game_profiles.root": "", **values}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def persist(self, key, value):
        self._values[key] = value


def session_manager(tmp_path):
    from neuro_voice.games.session import GameProfileSessionManager

    return GameProfileSessionManager(Config(), tmp_path / "game.json")


def collected(manager):
    events: list[dict] = []
    manager.add_observer(lambda **payload: events.append(payload))
    return events


def test_the_session_manager_announces_the_start(tmp_path):
    """**実経路E**: 「始めよう」→ 出来事。会話由来で、画面は見ない。"""
    manager = session_manager(tmp_path)
    events = collected(manager)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    kinds = [item["event"] for item in events]
    assert "bomb_started" in kinds
    assert events[0]["session_id"]
    assert events[0]["confidence"] >= .9


def test_the_session_manager_announces_the_end(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    manager.handle_final_input("爆弾解除を終了しよう", actor_id="chibi", source="local")
    ended = [item for item in events if item["event"] == "bomb_ended"]
    assert ended and ended[0]["outcome"] == "unknown"


@pytest.mark.parametrize("said,outcome", [
    ("解除できた！", "defused"),
    ("爆発した", "exploded"),
    ("次はどのモジュール？", None),
])
def test_the_outcome_comes_from_what_was_said(tmp_path, said, outcome):
    """**画面は見ていない。** 結果はユーザーが言うまで分からない。"""
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    manager.handle_final_input(said, actor_id="chibi", source="local")
    ended = [item for item in events if item["event"] == "bomb_ended"]
    if outcome is None:
        assert not ended
    else:
        assert ended and ended[0]["outcome"] == outcome


def test_the_outcome_is_announced_once(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    for _ in range(3):
        manager.handle_final_input("爆発した", actor_id="chibi", source="local")
    assert len([i for i in events if i["event"] == "bomb_ended"]) == 1


def test_the_module_is_announced_when_it_changes(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    manager.handle_final_input("次は配線のモジュール", actor_id="chibi", source="local")
    modules = [item for item in events if item["event"] == "module_detected"]
    assert modules and modules[0]["module"]


def test_the_same_module_is_not_announced_twice(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    for _ in range(3):
        manager.handle_final_input("配線のモジュール", actor_id="chibi", source="local")
    assert len([i for i in events if i["event"] == "module_detected"]) == 1


def test_strikes_reach_the_observer(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    events = collected(manager)
    manager.handle_final_input("ミスが2回ある", actor_id="chibi", source="local")
    strikes = [item for item in events if item["event"] == "strike_recorded"]
    assert strikes and strikes[-1]["strikes"] == 2


def test_a_new_bomb_forgets_the_old_one(tmp_path):
    manager = session_manager(tmp_path)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    manager.handle_final_input("配線のモジュール", actor_id="chibi", source="local")
    events = collected(manager)
    manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi", source="local")
    manager.handle_final_input("配線のモジュール", actor_id="chibi", source="local")
    assert [i["event"] for i in events][:1] == ["bomb_started"]
    assert any(i["event"] == "module_detected" for i in events), "前の爆弾の記憶が残っている"


def test_a_broken_observer_does_not_break_the_game(tmp_path):
    """観測が転んでもゲームは続く（第17条）。"""
    manager = session_manager(tmp_path)

    def explode(**_):
        raise RuntimeError("boom")

    manager.add_observer(explode)
    outcome = manager.handle_final_input("爆弾解除を始めよう", actor_id="chibi",
                                         source="local")
    assert outcome.handled


def test_the_observer_is_registered_by_both_sides():
    """**第19条**: Local と Discord の両方から繋ぐ。"""
    root = Path(__file__).resolve().parents[1] / "neuro_voice"
    for path in (root / "pipeline.py", root / "discord_bridge" / "bot.py"):
        text = path.read_text(encoding="utf-8")
        assert "attach_game_observer(self._note_game_session_event)" in text, path.name
        assert "observe_ktane_event(" in text, path.name


def test_switching_persona_keeps_the_observer():
    """**人格を変えた瞬間に配線が静かに切れない。**"""
    text = (Path(__file__).resolve().parents[1] / "neuro_voice" / "mind"
            / "mind.py").read_text(encoding="utf-8")
    assert "_reattach_game_observers" in text
    assert text.count("_reattach_game_observers") >= 2


# ---------------------------------------------------------------------------
# WARN と通常経路の分離
# ---------------------------------------------------------------------------


def fast_path(action):
    from neuro_voice.cognition.rollout import SpeechRequest, SpeechSource, check_speech

    return check_speech(
        SpeechRequest(source_type=SpeechSource.GAME_WARNING_FAST_PATH,
                      source_action=action, confidence=.9),
        cognition_enabled=True)


def test_the_fast_path_only_carries_warnings():
    """**高速経路から通常の回答を喋らない。**"""
    from neuro_voice.cognition.types import ActionType

    assert fast_path(ActionType.WARN).allowed

    for action in (ActionType.ANSWER, ActionType.COMMENT, ActionType.ASK_CLARIFICATION):
        blocked = fast_path(action)
        assert not blocked.allowed, action
        assert blocked.reason == "fast_path_action_not_allowlisted"


def test_ktane_does_not_widen_the_fast_path():
    """KTANE を理由に高速経路の許可を増やしていないこと。"""
    from neuro_voice.cognition.rollout import FAST_PATH_ALLOWLIST, SpeechSource
    from neuro_voice.cognition.types import ActionType

    assert FAST_PATH_ALLOWLIST[SpeechSource.GAME_WARNING_FAST_PATH] == frozenset(
        {ActionType.WARN})


def test_ktane_advice_is_advice_only():
    """攻略助言は助言。**ゲーム操作は今回入れない。**"""
    from neuro_voice.cognition.goals import ActionCategory, decide_permission

    assert decide_permission("game_advice").action_category is ActionCategory.ADVICE_ONLY
    assert decide_permission("game_advice").automatic
    # 操作は取り返しがつかない側のまま。
    assert decide_permission("game_input").requires_confirmation
    assert decide_permission("game_input").action_category is ActionCategory.DESTRUCTIVE


def test_world_updates_are_internal():
    from neuro_voice.cognition.goals import ActionCategory, decide_permission

    assert decide_permission(
        "update_internal_state").action_category is ActionCategory.INTERNAL


# ===========================================================================
# 機能フラグ
# ===========================================================================


def test_the_new_flags_default_to_off():
    live = InitiativeRuntime(Cfg())
    for flag in ("discord_presence_enabled", "vision_scene_parity_enabled",
                 "ktane_observation_enabled"):
        assert not getattr(live, flag), flag


@pytest.mark.parametrize("flag,call", [
    ("world_state.discord_presence_enabled",
     lambda live: live.observe_discord_presence(user_id=7, display_name="x",
                                                present=True)),
    ("world_state.ktane_observation_enabled",
     lambda live: live.observe_ktane_event(event="bomb_started", session_id="s")),
])
def test_each_source_turns_off_independently(flag, call):
    live = runtime(**{flag: False})
    assert call(live) == 0
    assert not live.world.entities


def test_the_config_declares_every_new_flag():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))
    world = data["world_state"]
    for flag in ("discord_presence_enabled", "vision_scene_parity_enabled",
                 "ktane_observation_enabled"):
        assert flag in world, flag
        assert world[flag] is False, f"{flag} が既定で有効になっている"


def test_reconcile_is_quiet_while_disabled():
    live = runtime(**{"world_state.discord_presence_enabled": False})
    assert live.reconcile_discord([Member(7, "x")])["reason"] == "disabled"


# ===========================================================================
# Cognitive Trace
# ===========================================================================


def test_the_trace_has_the_new_fields():
    from neuro_voice.cognition.trace import CognitiveTrace

    trace = CognitiveTrace()
    for name in ("observation_source", "source_event_id", "observation_adapter",
                 "discord_participant_id", "participant_presence_delta",
                 "vision_scene_type", "vision_scene_transition",
                 "ktane_event_type", "ktane_entity_ids", "ktane_fact_ids",
                 "ktane_goal_id", "speech_source_type", "permission_result",
                 "speech_gate_result", "suppression_reason"):
        assert hasattr(trace, name), name


def test_the_trace_records_who_joined():
    from neuro_voice.cognition.trace import CognitiveTrace

    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.observation_source == "discord"
    assert trace.discord_participant_id == "discord:7"
    assert trace.participant_presence_delta == "joined"


def test_the_trace_records_a_departure():
    from neuro_voice.cognition.trace import CognitiveTrace

    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=False, now=1010.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.participant_presence_delta == "left"


def test_the_trace_records_the_scene_change():
    from neuro_voice.cognition.trace import CognitiveTrace

    live = runtime()
    live.observe_vision(FakeVision(scene_type="menu", observation_id="a"),
                        now=1000.0)
    live.observe_vision(FakeVision(observation_id="b"), now=1010.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.vision_scene_type == "gameplay"
    assert trace.vision_scene_transition == "menu->gameplay"


def test_the_trace_records_the_ktane_event():
    from neuro_voice.cognition.trace import CognitiveTrace

    live = runtime()
    live.observe_ktane_event(event="bomb_started", session_id="s1", now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.ktane_event_type == "bomb_started"


def test_the_trace_keeps_no_names():
    """**話者名も表示名も残さない**（第12条）。"""
    import json

    from neuro_voice.cognition.trace import CognitiveTrace

    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert "ジーレン" not in json.dumps(trace.snapshot(), ensure_ascii=False)


# ===========================================================================
# 配線診断
# ===========================================================================


NEW_PROBES = ("world.discord_presence", "world.presence_speech",
              "world.scene_types", "world.scene_transition",
              "world.ktane", "world.ktane_goal", "world.ktane_fast_path")


def yaml_config():
    import yaml

    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "config"
                           / "config.yaml").read_text(encoding="utf-8"))

    class YamlCfg:
        def get(self, key, default=None):
            current = data
            for part in str(key).split("."):
                if not isinstance(current, dict):
                    return default
                current = current.get(part)
                if current is None:
                    return default
            return current

    return YamlCfg()


def test_the_new_probes_are_registered():
    from neuro_voice.diagnostics.wiring import run_all

    keys = {item.probe.key for item in run_all(yaml_config()).results}
    for key in NEW_PROBES:
        assert key in keys, key


def test_nothing_needs_attention():
    """**要注意 0 件を保つ。**"""
    from neuro_voice.diagnostics.wiring import run_all

    report = run_all(yaml_config())
    assert report.snapshot()["attention"] == [], report.snapshot()["attention"]


def test_a_bot_leak_turns_the_probe_red(monkeypatch):
    """**赤くならない点検は点検ではない。**"""
    import neuro_voice.cognition.observation as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    original = module.from_discord_presence
    monkeypatch.setattr(
        module, "from_discord_presence",
        lambda **kwargs: original(**{**kwargs, "is_bot": False}))
    report = run_all(yaml_config(), keys=("world.discord_presence",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_an_invented_scene_type_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.observation as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setattr(module, "SCENE_TYPES", (*module.SCENE_TYPES, "cutscene"))
    report = run_all(yaml_config(), keys=("world.scene_types",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)
    assert "cutscene" in report.results[0].detail


def test_a_widened_fast_path_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.rollout as module
    from neuro_voice.cognition.types import ActionType
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    monkeypatch.setitem(
        module.FAST_PATH_ALLOWLIST, module.SpeechSource.GAME_WARNING_FAST_PATH,
        frozenset({ActionType.WARN, ActionType.ANSWER}))
    report = run_all(yaml_config(), keys=("world.ktane_fast_path",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_silent_game_session_turns_the_probe_red(monkeypatch, tmp_path):
    import neuro_voice.games.session as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    quiet = tmp_path / "session.py"
    quiet.write_text("# 出来事を配る側が無い\n", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(quiet))
    report = run_all(yaml_config(), keys=("world.ktane",))
    assert str(report.results[0].status) == str(ProbeStatus.DISCONNECTED)


def test_a_speaking_presence_path_turns_the_probe_red(monkeypatch, tmp_path):
    import neuro_voice.discord_bridge.bot as module
    from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

    chatty = tmp_path / "bot.py"
    chatty.write_text(
        "def note_voice_presence(self):\n"
        "    self.note_attention_event('participant_change')\n"
        "    self._speak(self._loop, 'こんにちは')\n", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(chatty))
    report = run_all(yaml_config(), keys=("world.presence_speech",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_the_new_flags_are_watched():
    from neuro_voice.diagnostics.wiring import run_all

    flags = run_all(yaml_config()).flags
    for key in ("world_state.discord_presence_enabled",
                "world_state.vision_scene_parity_enabled",
                "world_state.ktane_observation_enabled"):
        assert key in flags, key


def test_the_probes_do_not_touch_real_state():
    from neuro_voice.diagnostics.wiring import run_all

    live = runtime()
    live.observe_discord_presence(user_id=7, display_name="ジーレン",
                                  present=True, now=1000.0)
    before = len(live.world.entities), len(live.world.facts)
    run_all(yaml_config())
    assert (len(live.world.entities), len(live.world.facts)) == before


# ===========================================================================
# 正本の印（今回見つけた実バグ）
# ===========================================================================


def test_an_owner_can_flip_its_own_fact():
    """**持ち主が言い直したら、確信の大小に関わらず通る**（第5条）。

    ここが効いていないと、VCから出た人がずっと居ることになる。
    実際そうなっていた。
    """
    from neuro_voice.cognition.world import (
        DeltaOperation, GroundedWorldState, WorldStateGate,
    )

    state, gate = GroundedWorldState(), WorldStateGate()
    gate.observe_fact(state, "discord:7", "present_in_conversation", True,
                      confidence=.9, authoritative=True, now=1000.0)
    delta = gate.observe_fact(state, "discord:7", "present_in_conversation", False,
                              confidence=.9, authoritative=True, now=1010.0)
    assert state.fact("discord:7", "present_in_conversation").value is False
    assert str(delta.operation) == str(DeltaOperation.UPDATE)
    assert delta.rejected_reason == ""


def test_a_guess_still_cannot_flip_a_confident_fact():
    """**推測は今までどおり僅差では覆せない。** 印を付けた所だけが変わる。"""
    from neuro_voice.cognition.world import GroundedWorldState, WorldStateGate

    state, gate = GroundedWorldState(), WorldStateGate()
    gate.observe_fact(state, "player", "hp", "low", confidence=.9, now=1000.0)
    delta = gate.observe_fact(state, "player", "hp", "high", confidence=.9,
                              now=1010.0)
    assert state.fact("player", "hp").value == "low"
    assert delta.rejected_reason == "below_contradiction_margin"


def test_the_restored_fact_is_still_usable_after_an_owner_update():
    """正本の更新でも、確信が下限を割れば `UNCERTAIN` のままにする。"""
    from neuro_voice.cognition.world import (
        GroundedWorldState, ItemStatus, WorldStateGate,
    )

    state, gate = GroundedWorldState(), WorldStateGate()
    gate.observe_fact(state, "discord:7", "present_in_conversation", True,
                      confidence=.9, authoritative=True, now=1000.0)
    gate.observe_fact(state, "discord:7", "present_in_conversation", False,
                      confidence=.2, authoritative=True, now=1010.0)
    assert str(state.fact("discord:7", "present_in_conversation").status) == str(
        ItemStatus.UNCERTAIN)
