"""Phase 6B: 実イベントへの配線・永続化・Obligation橋・権限。

前回は部品だけで「動く」と言っていた。**呼ぶ側が無ければ動いていない。**
ここは呼ぶ側があること、再起動を越えて残ること、権限が実際に効くことを
確かめる。

いちばん守りたいのは**「再起動しただけでゲーム途中の状態を現在の事実として
断定しない」**。復元は嘘をつきやすい。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.goals import (
    ActionCategory, GoalRecord, GoalStatus, PermissionResult,
    categorise, decide_permission, next_actions,
)
from neuro_voice.cognition.observation import (
    ACTIVE_SCREEN, SESSION_SCOPED_PREDICATES, ObservationIntake, from_game_event,
    from_speaker, from_vision,
)
from neuro_voice.cognition.runtime import InitiativeRuntime
from neuro_voice.cognition.world import EntityType, ItemStatus


class Cfg:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


WIRED = {
    "world_state.enabled": True,
    "world_state.entity_tracking_enabled": True,
    "world_state.fact_tracking_enabled": True,
    "world_state.observation_wiring_enabled": True,
    "world_state.speaker_observation_enabled": True,
    "world_state.vision_observation_enabled": True,
    "world_state.game_observation_enabled": True,
    # Phase 6C。入力元ごとの差を埋める分も上げておく。
    "world_state.discord_presence_enabled": True,
    "world_state.vision_scene_parity_enabled": True,
    "world_state.ktane_observation_enabled": True,
    "world_state.persistence_enabled": True,
    "goals.enabled": True,
    "goals.persistence_enabled": True,
    "goals.obligation_bridge_enabled": True,
    "goals.next_action_enabled": True,
}


def runtime(**overrides) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**{**WIRED, **overrides}))


def store_at(tmp_path):
    from neuro_voice.mind.store import MemoryStore

    return MemoryStore(tmp_path / "mind.db")


class FakeVision:
    def __init__(self, scene_type="minecraft_gameplay", confidence=.85,
                 observation_id="obs1"):
        self.scene_type = scene_type
        self.confidence = confidence
        self.observation_id = observation_id


class FakeGameEvent:
    def __init__(self, kind="danger", priority=.9, signature="sig1", summary="HPが低い"):
        self.kind = kind
        self.priority = priority
        self.signature = signature
        self.summary = summary


def pipeline_source() -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice" / "pipeline.py"
            ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 実イベントproducerが呼んでいるか
# ---------------------------------------------------------------------------


def test_the_three_producers_call_the_adapter():
    """**部品があるだけでは動いていない。** 呼ぶ側を確かめる。"""
    text = pipeline_source()
    for needle in ("self.note_speaker_observation(",
                   "self.initiative().observe_vision(observation)",
                   "self.initiative().observe_game_event(event)"):
        assert needle in text, needle


def test_the_pipeline_hydrates_and_flushes():
    text = pipeline_source()
    assert "self._initiative.hydrate()" in text
    assert "def flush_world_state" in text
    assert "self.flush_world_state()" in text


class BarePipeline:
    """`VoicePipeline` の**本物のメソッド**を、音も画面も無しで呼ぶための殻。"""

    def __init__(self, cfg, store=None):
        from neuro_voice.pipeline import VoicePipeline

        self._real = VoicePipeline.__new__(VoicePipeline)
        self._real._cfg = cfg
        self._real._initiative = None
        self._real._mind = type("M", (), {"_store": store})() if store else None
        self._real._emit = self.record
        self.events: list[tuple[str, dict]] = []

    def record(self, name, **payload):
        self.events.append((str(name), payload))

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_pipeline_methods_really_run(tmp_path):
    """ソースに文字列があるだけでなく、**呼んで動く**こと。"""
    store = store_at(tmp_path)
    live = BarePipeline(Cfg(**WIRED), store=store)
    runtime = live.initiative()
    assert runtime.observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                                   event_id="e1", now=1000.0) >= 1
    result = live.flush_world_state()
    assert result["persisted"] is True
    assert result["goals"]["persisted"] is True
    store.close()

    # 別プロセス相当。前回の状態が戻り、一時状態は古い扱いになる。
    reopened = store_at(tmp_path)
    restarted = BarePipeline(Cfg(**WIRED), store=reopened)
    assert restarted.initiative().world.find("チビ") is not None
    assert any(name == "world_state_hydrated" for name, _ in restarted.events)
    reopened.close()


def test_the_pipeline_reports_a_failed_write(tmp_path):
    """**未保存を成功扱いしない。** 画面にも出す。"""
    live = BarePipeline(Cfg(**WIRED), store=BrokenStore())
    live.initiative().observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                                      now=1000.0)
    assert live.flush_world_state()["persisted"] is False
    assert any(name == "world_state_persist_failed" for name, _ in live.events)


def test_a_disabled_store_is_not_reported_as_a_failure(tmp_path):
    """DBを繋いでいないだけで警告を出さない。**赤を安売りしない。**"""
    live = BarePipeline(Cfg(**WIRED))
    assert live.flush_world_state()["reason"] == "no_store"
    assert not live.events


# -- 実経路A: 話者 ----------------------------------------------------------


def test_the_real_speaker_path_runs():
    """`_respond` が渡すのと同じ形の profile で、本物のメソッドを通す。"""
    live = BarePipeline(Cfg(**WIRED))
    assert live.note_speaker_observation(
        {"id": 3, "name": "チビ", "auto_name": False}, event_id="u1") >= 1
    world = live.initiative().world
    assert world.find("チビ") is not None
    assert world.fact("session", "speaking").source_event_ids == ("u1",)


def test_a_provisional_name_does_not_become_a_participant():
    """`auto_name` は「声紋では確定できなかった」印。**参加者にしない。**"""
    live = BarePipeline(Cfg(**WIRED))
    live.note_speaker_observation({"id": 3, "name": "話者2", "auto_name": True})
    world = live.initiative().world
    assert world.fact("session", "speaking") is None
    assert all(str(item.entity_type) == "unknown" for item in world.entities.values())


def test_a_missing_profile_is_harmless():
    live = BarePipeline(Cfg(**WIRED))
    assert live.note_speaker_observation(None) == 0
    assert live.note_speaker_observation({}) == 0


def test_a_broken_world_state_does_not_break_the_turn():
    """世界状態が転んでも会話は続く（第17条）。"""
    live = BarePipeline(Cfg(**WIRED))

    class Exploding:
        def observe_speaker(self, **kwargs):
            raise RuntimeError("boom")

    live._real._initiative = Exploding()
    assert live.note_speaker_observation({"id": 3, "name": "チビ",
                                          "auto_name": False}) == 0


# -- 実経路B/C: 映像とゲーム ------------------------------------------------


class FakeDirector:
    def __init__(self, event):
        self._event = event
        self.last_suppression_reason = ""
        self.seen = []

    def observe(self, observation):
        self.seen.append(observation)
        return self._event


class GameObservation:
    scene_type = "minecraft_gameplay"
    confidence = .85
    observation_id = "obs-1"


def game_pipeline(event):
    cfg = Cfg(**{**WIRED,
                 "video.game_commentary_enabled": True,
                 "vision.proactive_reaction_enabled": True,
                 "video.game_profile": "minecraft",
                 "initiative.enabled": True})
    live = BarePipeline(cfg)
    live._real._game_companion_director = FakeDirector(event)
    live._real.note_directive_environment_event = lambda *a, **k: False
    live._real._autonomy_decide = lambda *a, **k: type(
        "D", (), {"should_speak": False,
                  "action_type": type("T", (), {"value": "none"})()})()
    live._real._pending_game_event = None
    return live


def test_the_real_vision_and_game_paths_run():
    """`_on_game_observation` を本物のまま通す。**文字列一致ではなく実行。**"""
    import asyncio

    event = FakeGameEvent(kind="danger", priority=.9, signature="sig-real")
    live = game_pipeline(event)
    asyncio.run(live._on_game_observation(GameObservation()))
    world = live.initiative().world
    assert world.fact(ACTIVE_SCREEN, "scene").value == "gameplay"
    assert world.fact("game", "situation").value == "danger"
    assert world.fact("game", "situation").source_event_ids == ("sig-real",)


def test_the_vision_path_runs_even_when_no_game_event_follows():
    """**場面の取り込みが実況の有無に巻き込まれないこと。**"""
    import asyncio

    live = game_pipeline(None)
    asyncio.run(live._on_game_observation(GameObservation()))
    world = live.initiative().world
    assert world.fact(ACTIVE_SCREEN, "scene") is not None
    assert world.fact("game", "situation") is None


# ---------------------------------------------------------------------------
# 話者経路
# ---------------------------------------------------------------------------


def test_a_confirmed_speaker_becomes_a_participant():
    entities, facts = from_speaker(speaker_id=3, name="チビ", confirmed=True,
                                   event_id="e1", now=1000.0)
    assert entities[0].entity_type is EntityType.PARTICIPANT
    assert entities[0].external_id == "speaker:3"
    predicates = {item.predicate for item in facts}
    assert predicates == {"present_in_conversation", "speaking"}
    assert all(item.source_event_id == "e1" for item in facts)


def test_the_current_speaker_fact_is_short_lived():
    _entities, facts = from_speaker(speaker_id=3, name="チビ", confirmed=True,
                                    now=1000.0)
    speaking = next(item for item in facts if item.predicate == "speaking")
    presence = next(item for item in facts if item.predicate == "present_in_conversation")
    assert speaking.ttl < presence.ttl


def test_an_unknown_speaker_is_not_merged_into_a_person():
    """**不明話者を既知人物へ統合しない。**"""
    entities, facts = from_speaker(speaker_id=-1, name="", confirmed=False,
                                   now=1000.0)
    assert entities[0].entity_type is EntityType.UNKNOWN
    assert facts == [], "不明話者で誰かの事実を更新しない"


def test_an_unconfirmed_named_voice_is_still_unknown():
    entities, _facts = from_speaker(speaker_id=3, name="チビ", confirmed=False,
                                    now=1000.0)
    assert entities[0].entity_type is EntityType.UNKNOWN


def test_the_speaker_path_reaches_the_world_state():
    live = runtime()
    assert live.observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                                event_id="e1", now=1000.0) >= 1
    assert live.world.find("チビ") is not None
    fact = live.world.fact("session", "speaking")
    assert fact is not None and fact.source_event_ids == ("e1",)


# ---------------------------------------------------------------------------
# 映像経路
# ---------------------------------------------------------------------------


def test_a_confident_scene_becomes_a_fact():
    _entities, facts = from_vision(FakeVision(), now=1000.0)
    assert facts and facts[0].predicate == "scene"
    assert facts[0].source_event_id == "obs1"


def test_a_low_confidence_scene_is_not_asserted():
    """**低い認識で具体的な画面を名乗らない。** 分からないとは言う。"""
    _entities, facts = from_vision(FakeVision(confidence=.3), now=1000.0)
    assert facts and facts[0].value == "unknown"


def test_a_missing_scene_produces_nothing():
    assert from_vision(FakeVision(scene_type=""), now=1000.0) == ([], [])


def test_the_vision_path_reaches_the_world_state():
    live = runtime()
    assert live.observe_vision(FakeVision(), now=1000.0) >= 1
    assert live.world.fact(ACTIVE_SCREEN, "scene") is not None


# ---------------------------------------------------------------------------
# ゲーム経路
# ---------------------------------------------------------------------------


def test_a_real_game_event_becomes_a_fact():
    """既存の分類器が付けた `kind` をそのまま使う。**架空の型は足さない。**"""
    _entities, facts = from_game_event(FakeGameEvent(), now=1000.0)
    assert facts and facts[0].value == "danger"
    assert facts[0].source_event_id == "sig1"


def test_a_low_priority_game_event_is_skipped():
    assert from_game_event(FakeGameEvent(priority=.2), now=1000.0) == ([], [])


def test_the_game_path_reaches_the_world_state():
    live = runtime()
    assert live.observe_game_event(FakeGameEvent(), now=1000.0) >= 1
    assert live.world.fact("game", "situation").value == "danger"


def test_a_game_fact_can_drive_next_actions():
    live = runtime()
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    live.propose_goal(GoalRecord(
        description="ダイヤを掘る", owner_ids=("chibi",), confidence=.9,
        priority=1.0), source="user_stated")
    assert live.next_actions()


# ---------------------------------------------------------------------------
# 重複と高頻度
# ---------------------------------------------------------------------------


def test_the_same_event_id_is_applied_once():
    """**同じevent IDを2回受信してもDeltaは一度だけ。**"""
    intake = ObservationIntake()
    _e, facts = from_speaker(speaker_id=3, name="チビ", confirmed=True,
                             event_id="same", now=1000.0)
    assert intake.accept(facts[0], now=1000.0)
    assert not intake.accept(facts[0], now=1050.0)
    assert intake.dropped["duplicate_event_id"] == 1


def test_unchanged_observations_are_dropped():
    intake = ObservationIntake()
    accepted = sum(
        intake.accept(from_vision(FakeVision(observation_id=f"o{i}"), now=1000.0)[1][0],
                      now=1000.0 + i * .1)
        for i in range(10))
    assert accepted == 1
    assert intake.dropped["unchanged"] == 9


def test_a_burst_is_rate_limited():
    """**高頻度観測でFact件数が無制限に増えない。**"""
    intake = ObservationIntake(dedup_window=0.0, rate_limit=5)
    accepted = sum(
        intake.accept(from_vision(FakeVision(scene_type=f"s{i}",
                                             observation_id=f"o{i}"), now=1000.0)[1][0],
                      now=1000.0)
        for i in range(30))
    assert accepted <= 5
    assert intake.dropped["rate_limited"] >= 20


def test_low_confidence_observations_are_dropped():
    intake = ObservationIntake()
    _e, facts = from_speaker(speaker_id=-1, name="", confirmed=False, now=1000.0)
    entities, _f = from_speaker(speaker_id=-1, name="", confirmed=False, now=1000.0)
    assert not intake.accept(entities[0], now=1000.0)
    assert intake.dropped["below_confidence"] == 1


def test_many_observations_do_not_explode_the_fact_table():
    live = runtime()
    for index in range(200):
        live.observe_vision(FakeVision(scene_type=f"scene{index}",
                                       observation_id=f"o{index}"),
                            now=1000.0 + index * .01)
    # 同じ subject:predicate なので1件へ収束する。
    assert len(live.world.facts) <= 3


# ---------------------------------------------------------------------------
# 永続化と復元
# ---------------------------------------------------------------------------


def test_the_world_and_goals_survive_a_restart(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    first.observe_fact("project", "name", "AItuber", confidence=.9, ttl=99999, now=1000.0)
    first.propose_goal(GoalRecord(
        description="設定を確認する", owner_ids=("chibi",), confidence=.9),
        source="user_stated")
    assert first.flush_world()["persisted"]
    assert first.flush_goals()["persisted"]
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    result = second.hydrate(now=2000.0)
    assert result["hydrated"]
    assert second.world.find("チビ") is not None
    assert second.goals
    reopened.close()


def test_a_transient_fact_is_not_current_after_a_restart(tmp_path):
    """**再起動しただけでゲーム途中の状態を現在の事実にしない。**"""
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_game_event(FakeGameEvent(), now=1000.0)
    first.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    for key in ("game:situation", "session:speaking"):
        fact = second.world.facts.get(key)
        assert fact is not None, key
        assert str(fact.status) == str(ItemStatus.STALE), key
        assert not fact.usable(now=2000.0), key
    reopened.close()


def test_a_long_lived_fact_survives_as_current(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_fact("project", "name", "AItuber", confidence=.9, ttl=99999, now=1000.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    fact = second.world.facts.get("project:name")
    assert fact is not None and fact.usable(now=2000.0)
    reopened.close()


def test_an_active_goal_returns_as_paused(tmp_path):
    """**状態を確かめるまで動き出さない。**"""
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    goal = GoalRecord(description="ダイヤを掘る", owner_ids=("chibi",), confidence=.9)
    first.propose_goal(goal, source="user_stated")
    assert str(goal.status) == str(GoalStatus.ACTIVE)
    first.flush_goals()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    restored = next(iter(second.goals.values()))
    assert str(restored.status) == str(GoalStatus.PAUSED)
    reopened.close()


def test_a_completed_goal_stays_completed(tmp_path):
    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    goal = GoalRecord(description="ダイヤを掘る", owner_ids=("chibi",),
                      confidence=.9, status=GoalStatus.COMPLETED)
    first.goals[goal.goal_id] = goal
    first.flush_goals()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    assert str(next(iter(second.goals.values())).status) == str(GoalStatus.COMPLETED)
    reopened.close()


def test_a_restored_entity_is_inferred_not_visible(tmp_path):
    """復元は「在ると推測」まで。**見えているとは限らない。**"""
    from neuro_voice.cognition.world import Presence

    store = store_at(tmp_path)
    first = runtime()
    first.attach_store(store)
    first.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    first.flush_world()
    store.close()

    reopened = store_at(tmp_path)
    second = runtime()
    second.attach_store(reopened)
    second.hydrate(now=2000.0)
    entity = second.world.find("チビ")
    assert str(entity.presence) == str(Presence.INFERRED)
    reopened.close()


def test_the_session_scoped_predicates_are_declared():
    for predicate in ("speaking", "scene", "situation", "hp"):
        assert predicate in SESSION_SCOPED_PREDICATES


# ---------------------------------------------------------------------------
# 保存の失敗
# ---------------------------------------------------------------------------


class BrokenStore:
    def save_world_entities(self, rows):
        raise RuntimeError("disk full")

    def save_world_facts(self, rows):
        raise RuntimeError("disk full")

    def save_goals(self, rows):
        raise RuntimeError("disk full")


def test_a_failed_write_does_not_stop_the_conversation():
    """**未保存を成功扱いしない。** でも会話は止めない（第17条）。"""
    live = runtime()
    live.attach_store(BrokenStore())
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    result = live.flush_world()
    assert result["persisted"] is False
    assert "disk full" in result["reason"]
    assert live.counters["world_persist_failed"] == 1
    # 状態は残っているので、会話側は続けられる。
    assert live.world.find("チビ") is not None


def test_a_failed_goal_write_is_reported_too():
    live = runtime()
    live.attach_store(BrokenStore())
    live.propose_goal(GoalRecord(description="x", owner_ids=("a",), confidence=.9),
                      source="user_stated")
    assert live.flush_goals()["persisted"] is False
    assert live.last_persistence_error


# ---------------------------------------------------------------------------
# Obligation との橋
# ---------------------------------------------------------------------------


def test_a_goal_links_to_an_obligation(tmp_path):
    store = store_at(tmp_path)
    live = runtime()
    live.attach_store(store)
    goal = GoalRecord(description="設定を確認する", owner_ids=("chibi",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    assert live.link_obligation(goal.goal_id, "ob1", event_id="e1")
    assert "ob1" in goal.obligation_ids
    assert store.goal_obligations()
    store.close()


@pytest.mark.parametrize("start,obligation_status,expected", [
    (GoalStatus.PAUSED, "pending", GoalStatus.ACTIVE),
    (GoalStatus.ACTIVE, "blocked", GoalStatus.BLOCKED),
    (GoalStatus.ACTIVE, "fulfilled", GoalStatus.COMPLETED),
    (GoalStatus.ACTIVE, "resolved", GoalStatus.COMPLETED),
    (GoalStatus.ACTIVE, "cancelled", GoalStatus.ABANDONED),
])
def test_the_obligation_state_reaches_the_goal(start, obligation_status, expected):
    live = runtime()
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    live.link_obligation(goal.goal_id, "ob1")
    goal.status = start
    changed = live.sync_obligation_state("ob1", obligation_status, event_id="e1")
    assert changed == [goal.goal_id]
    assert str(goal.status) == str(expected)


def test_an_unchanged_obligation_state_is_not_a_change():
    """**同じ状態を配っても更新扱いにしない。** ループの種になる。"""
    live = runtime()
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    live.link_obligation(goal.goal_id, "ob1")
    assert live.sync_obligation_state("ob1", "pending", event_id="e1") == []


def test_the_goal_state_reaches_the_obligation():
    live = runtime()
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    live.link_obligation(goal.goal_id, "ob1")
    goal.status = GoalStatus.COMPLETED
    assert live.obligation_updates_for(goal.goal_id) == [("ob1", "fulfilled")]


def test_the_same_event_does_not_bounce_back_and_forth():
    """**無限更新ループを作らない。**"""
    live = runtime()
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    live.link_obligation(goal.goal_id, "ob1")
    assert live.sync_obligation_state("ob1", "fulfilled", event_id="e1")
    assert live.sync_obligation_state("ob1", "fulfilled", event_id="e1") == []


def test_the_bridge_needs_its_flag():
    live = runtime(**{"goals.obligation_bridge_enabled": False})
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9)
    live.propose_goal(goal, source="user_stated")
    assert not live.link_obligation(goal.goal_id, "ob1")


# ---------------------------------------------------------------------------
# 権限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action,category,result", [
    ("update_internal_state", ActionCategory.INTERNAL, PermissionResult.ALLOW_AUTOMATIC),
    ("game_advice", ActionCategory.ADVICE_ONLY, PermissionResult.ALLOW_AUTOMATIC),
    ("propose_utterance", ActionCategory.SPEECH, PermissionResult.ALLOW_AUTOMATIC),
    ("web_search", ActionCategory.EXTERNAL_READ, PermissionResult.ALLOW_AUTOMATIC),
    ("persist_setting", ActionCategory.EXTERNAL_WRITE, PermissionResult.REQUIRE_CONFIRMATION),
])
def test_each_category_gets_its_policy(action, category, result):
    decision = decide_permission(action)
    assert decision.action_category is category
    assert str(decision.result) == str(result)


def test_deleting_needs_confirmation_and_a_target():
    """**「何を消すのか」を確かめずに消さない。**"""
    without = decide_permission("delete_file")
    assert str(without.result) == str(PermissionResult.REQUIRE_CONFIRMATION)
    assert without.requires_target_check
    assert without.reason == "target_unknown"
    with_target = decide_permission("delete_file", target_ids=("a.txt",))
    assert with_target.requires_confirmation


def test_sending_to_a_person_needs_the_recipient():
    decision = decide_permission("send_message")
    assert decision.action_category is ActionCategory.PERSON_DIRECTED
    assert decision.requires_confirmation and decision.requires_target_check


def test_an_unknown_action_requires_confirmation():
    """**分からなければ自動実行しない。**"""
    decision = decide_permission("do_something_new")
    assert str(decision.result) == str(PermissionResult.REQUIRE_CONFIRMATION)
    assert decision.reason == "unknown_action"
    assert categorise("do_something_new") is None


def test_next_actions_are_not_all_automatic():
    """**固定の `AUTOMATIC` を廃止した。**"""
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9,
                      status=GoalStatus.ACTIVE, priority=1.0)
    actions = next_actions([goal])
    assert actions
    for action in actions:
        assert action.decision is not None, "判定が付いていない"
        assert str(action.category) != ""


def test_an_action_without_a_decision_needs_approval():
    from neuro_voice.cognition.goals import NextAction, Permission

    assert NextAction(kind="x").permission is Permission.NEEDS_APPROVAL


def test_the_permission_is_decided_in_code_not_by_the_planner():
    """判定が Planner の文章に依存していないこと。"""
    from neuro_voice.cognition import goals

    source = Path(goals.__file__).read_text(encoding="utf-8")
    assert "def decide_permission" in source
    assert "prompt" not in source.lower().split("def decide_permission")[1][:600]


# ---------------------------------------------------------------------------
# 垂直スライス（部品を通しで）
# ---------------------------------------------------------------------------


def test_the_vertical_slice_runs_end_to_end(tmp_path):
    """目標 → 実イベント → 世界状態 → 次の行動 → 権限 → 保存 → 再起動。"""
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state

    store = store_at(tmp_path)
    live = runtime()
    live.attach_store(store)

    # 1. ユーザーが目標を明示する。
    goal = GoalRecord(description="ダイヤを掘る", owner_ids=("chibi",),
                      confidence=.9, priority=1.0,
                      completion_conditions=("ダイヤ",))
    verdict = live.propose_goal(goal, source="user_stated")
    assert str(verdict.status) == str(GoalStatus.ACTIVE)
    live.link_obligation(goal.goal_id, "ob1", event_id="turn1")

    # 2. 実イベント（既存の分類器が出す形）が来る。
    assert live.observe_game_event(FakeGameEvent(kind="progress", priority=.8,
                                                 signature="sig-progress"),
                                   now=1000.0) >= 1
    assert live.world.fact("game", "situation").value == "progress"

    # 3. 次の小さな行動と、その許可。
    actions = live.next_actions()
    assert actions and actions[0].decision is not None

    # 4. Action Selector を通る（目標は補正であって命令ではない）。
    decision = CognitiveKernel(goal_bias=live.goal_bias()).decide(
        CognitiveEvent(event_type=EventType.VISUAL_CHANGE), build_state(), now=1000.0)
    assert decision.selected_action is not None

    # 5. 保存して再起動。
    assert live.flush_world()["persisted"]
    assert live.flush_goals()["persisted"]
    store.close()

    reopened = store_at(tmp_path)
    restarted = runtime()
    restarted.attach_store(reopened)
    restarted.hydrate(now=2000.0)
    assert len(restarted.goals) == 1, "重複した目標を作らない"
    restored = next(iter(restarted.goals.values()))
    assert str(restored.status) == str(GoalStatus.PAUSED)
    assert "ob1" in restored.obligation_ids
    # ゲーム途中の状態は現在の事実として戻らない。
    assert not restarted.world.facts["game:situation"].usable(now=2000.0)
    reopened.close()


def test_the_slice_is_measured():
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    live.propose_goal(GoalRecord(description="x", owner_ids=("a",), confidence=.9),
                      source="user_stated")
    live.next_actions()
    status = live.status()
    for key in ("entity_resolution_ms", "world_state_delta_ms",
                "goal_matching_ms", "next_action_generation_ms"):
        assert key in status["latency_ms"], key
    assert "observation_adapter_ms" in status["observation"]["latency_ms"]


# ---------------------------------------------------------------------------
# 機能フラグ
# ---------------------------------------------------------------------------


def test_the_new_flags_default_to_off():
    live = InitiativeRuntime(Cfg())
    for flag in ("observation_wiring_enabled", "speaker_observation_enabled",
                 "vision_observation_enabled", "game_observation_enabled",
                 "world_persistence_enabled", "goal_persistence_enabled",
                 "obligation_bridge_enabled", "permission_enforcement_enabled"):
        assert not getattr(live, flag), flag


def test_nothing_is_observed_while_the_wiring_is_off():
    live = runtime(**{"world_state.observation_wiring_enabled": False})
    assert live.observe_speaker(speaker_id=3, name="チビ", confirmed=True) == 0
    assert live.observe_vision(FakeVision()) == 0
    assert live.observe_game_event(FakeGameEvent()) == 0


def test_each_source_turns_on_independently():
    live = runtime(**{"world_state.vision_observation_enabled": False})
    assert live.observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                                now=1000.0) >= 1
    assert live.observe_vision(FakeVision(), now=1000.0) == 0


def test_persistence_needs_its_own_flag():
    live = runtime(**{"world_state.persistence_enabled": False})
    live.attach_store(object())
    assert live.flush_world()["persisted"] is False
