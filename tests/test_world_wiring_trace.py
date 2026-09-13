"""Phase 6B: 通ったかどうかを後から読めるか。

記録が無いと、**「動いていない」と「動いたが何も起きなかった」を
区別できない。** 前は件数だけを見て「0件だから入力が無かった」と
読み違えていた。
"""
from __future__ import annotations

import json

from neuro_voice.cognition.goals import (
    GoalRecord, GoalStatus, decide_permission, next_actions,
)
from neuro_voice.cognition.observation import WorldObservationAdapter
from neuro_voice.cognition.runtime import InitiativeRuntime
from neuro_voice.cognition.trace import CognitiveTrace
from neuro_voice.diagnostics.wiring import ProbeStatus, run_all

from tests.test_world_wiring import WIRED, Cfg, FakeGameEvent, FakeVision


def runtime(**overrides) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**{**WIRED, **overrides}))


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def test_the_trace_has_the_new_fields():
    trace = CognitiveTrace()
    for name in ("observation_source", "source_event_id", "observation_adapter",
                 "observe_entity_called", "observe_fact_called",
                 "world_state_delta_id", "world_state_persisted",
                 "world_state_persistence_error", "goal_id", "obligation_id",
                 "goal_obligation_sync", "next_action_id", "permission_category",
                 "permission_result", "permission_reason", "vertical_slice_stage"):
        assert hasattr(trace, name), name


def test_untried_and_failed_persistence_look_different():
    """**未保存を成功扱いしないだけでなく、未実施とも区別する。**"""
    trace = CognitiveTrace()
    assert trace.world_state_persisted is None, "既定は「試していない」"
    trace.world_state_persisted = False
    trace.world_state_persistence_error = "OperationalError: disk full"
    snapshot = trace.snapshot()["wiring"]["persistence"]
    assert snapshot["persisted"] is False
    assert "disk full" in snapshot["error"]


def test_the_trace_copies_the_adapter_result():
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                         event_id="e1", now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.observation_source == "speaker"
    assert trace.source_event_id == "e1"
    assert trace.observation_adapter == "WorldObservationAdapter"
    assert trace.observe_entity_called and trace.observe_fact_called
    assert trace.world_state_delta_id


def test_a_dropped_observation_is_visible_as_a_drop():
    """呼んだが落ちた場合、**「呼んでいない」に見えないこと。**"""
    live = runtime()
    live.observe_vision(FakeVision(confidence=.2), now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.observation_source == "vision"
    assert trace.observe_fact_called is False
    # 何も来なかったのではなく、確信が足りずに作られなかった。
    assert live.observation_adapter().counters["vision"] == 1


def test_the_drop_reason_is_recorded():
    live = runtime()
    for index in range(4):
        live.observe_vision(FakeVision(observation_id=f"o{index}"),
                            now=1000.0 + index * .1)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    assert trace.observation_dropped.get("unchanged") == 1


def test_the_stage_records_where_it_stopped():
    trace = CognitiveTrace()
    trace.stage("observation")
    trace.stage("world_state")
    assert trace.snapshot()["wiring"]["stage"] == "world_state"


def test_the_permission_fields_reach_the_snapshot():
    decision = decide_permission("delete_file", action_id="a1")
    trace = CognitiveTrace()
    trace.next_action_id = decision.action_id
    trace.permission_category = str(decision.action_category)
    trace.permission_result = str(decision.result)
    trace.permission_reason = decision.reason
    permission = trace.snapshot()["wiring"]["permission"]
    assert permission["category"] == "destructive"
    assert permission["result"] == "require_confirmation"
    assert permission["reason"] == "target_unknown"


def test_the_trace_stays_serialisable():
    live = runtime()
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    trace.world_state_persisted = True
    text = json.dumps(trace.snapshot(), ensure_ascii=False)
    assert "wiring" in text


def test_the_trace_still_holds_no_content():
    """**画面の中身も音声も内部思考も入れない**（第12条）。"""
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True,
                         event_id="e1", now=1000.0)
    trace = CognitiveTrace()
    trace.note_observation(live.observation_adapter().last_result)
    text = json.dumps(trace.snapshot()["wiring"], ensure_ascii=False)
    assert "チビ" not in text, "話者名まで記録に残している"


# ---------------------------------------------------------------------------
# レイテンシ
# ---------------------------------------------------------------------------


def test_every_required_stage_is_measured():
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9,
                      status=GoalStatus.ACTIVE, priority=1.0)
    live.propose_goal(goal, source="user_stated")
    live.link_obligation(goal.goal_id, "ob1")
    live.sync_obligation_state("ob1", "blocked", event_id="e1")
    live.next_actions()
    latency = live.latency_ms()
    for key in ("observation_adapter_ms", "observe_entity_ms", "observe_fact_ms",
                "entity_resolution_ms", "world_state_delta_ms",
                "goal_matching_ms", "next_action_generation_ms",
                "goal_obligation_sync_ms", "permission_decision_ms",
                "vertical_slice_total_ms"):
        assert key in latency, key


def test_persistence_and_hydration_are_measured(tmp_path):
    from neuro_voice.mind.store import MemoryStore

    store = MemoryStore(tmp_path / "mind.db")
    live = runtime()
    live.attach_store(store)
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    live.flush_world()
    live.hydrate(now=2000.0)
    latency = live.latency_ms()
    assert "world_state_persist_ms" in latency
    assert "world_state_hydrate_ms" in latency
    store.close()


def test_the_total_is_the_sum_of_the_stages():
    live = runtime()
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    latency = live.latency_ms()
    parts = sum(latency.get(key, .0) for key in
                (*InitiativeRuntime.SLICE_LATENCY_KEYS, "observation_adapter_ms"))
    assert abs(latency["vertical_slice_total_ms"] - parts) < .01


def test_the_slice_is_not_slow():
    """**安全側の判定が遅いと、迂回したくなる。**"""
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    live.observe_game_event(FakeGameEvent(), now=1000.0)
    live.propose_goal(GoalRecord(description="x", owner_ids=("a",), confidence=.9,
                                 status=GoalStatus.ACTIVE, priority=1.0),
                      source="user_stated")
    live.next_actions()
    assert live.latency_ms()["vertical_slice_total_ms"] < 50.0


def test_permission_decisions_are_cheap():
    goal = GoalRecord(description="x", owner_ids=("a",), confidence=.9,
                      status=GoalStatus.ACTIVE, priority=1.0)
    actions = next_actions([goal])
    assert actions
    assert all(item.decision.decided_ms < 5.0 for item in actions)


def test_latency_works_before_anything_ran():
    live = runtime()
    assert live.latency_ms()["vertical_slice_total_ms"] == 0.0
    assert live.status()["observation"]["latency_ms"] == {}


# ---------------------------------------------------------------------------
# 配線診断
# ---------------------------------------------------------------------------


PROBE_KEYS = ("world.producers", "world.observation", "world.intake",
              "world.persistence", "goals.obligation_bridge",
              "goals.permission_decision")


def config_from_yaml():
    import yaml
    from pathlib import Path

    data = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "config.yaml"
         ).read_text(encoding="utf-8"))

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
    keys = {item.probe.key for item in run_all(config_from_yaml()).results}
    for key in PROBE_KEYS:
        assert key in keys, key


def test_nothing_needs_attention():
    """**要注意 0 件を保つ。** 赤が常時1件あると、全部を見なくなる。"""
    report = run_all(config_from_yaml())
    assert report.snapshot()["attention"] == [], report.snapshot()["attention"]


def test_the_new_probes_are_off_not_broken():
    """既定オフのフラグは `off`。**`broken` と混ぜない。**"""
    results = {item.probe.key: str(item.status)
               for item in run_all(config_from_yaml()).results}
    for key in ("world.producers", "world.persistence", "goals.obligation_bridge"):
        assert results[key] == str(ProbeStatus.OFF), (key, results[key])


def test_the_observation_probe_reads_the_real_adapter(monkeypatch):
    """**プローブが本物を呼んでいること。** 再輸出を見ていると差し替わらない。"""
    import neuro_voice.cognition.observation as module

    calls = []
    original = module.WorldObservationAdapter.observe_speaker

    def spy(self, **kwargs):
        calls.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(module.WorldObservationAdapter, "observe_speaker", spy)
    run_all(config_from_yaml(), keys=("world.observation",))
    assert calls, "プローブがアダプタを呼んでいない"


def test_a_cut_producer_turns_the_probe_red(monkeypatch, tmp_path):
    """配線を切ったら赤くなること。**赤くならない点検は点検ではない。**"""
    import neuro_voice.pipeline as pipeline_module

    fake = tmp_path / "pipeline.py"
    fake.write_text("# 呼ぶ側が無い\n", encoding="utf-8")
    monkeypatch.setattr(pipeline_module, "__file__", str(fake))
    report = run_all(config_from_yaml(), keys=("world.producers",))
    result = report.results[0]
    assert str(result.status) == str(ProbeStatus.DISCONNECTED)
    assert "話者" in result.detail


def test_a_flat_permission_policy_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.goals as goals_module

    monkeypatch.setitem(
        goals_module.CATEGORY_POLICY,
        goals_module.ActionCategory.EXTERNAL_WRITE,
        goals_module.PermissionResult.ALLOW_AUTOMATIC)
    report = run_all(config_from_yaml(), keys=("goals.permission_decision",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_missing_session_marker_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.observation as module

    monkeypatch.setattr(module, "SESSION_SCOPED_PREDICATES", frozenset())
    report = run_all(config_from_yaml(), keys=("world.persistence",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_a_one_way_bridge_turns_the_probe_red(monkeypatch):
    import neuro_voice.cognition.goals as goals_module

    monkeypatch.setattr(goals_module, "OBLIGATION_TO_GOAL", {"pending": "active"})
    report = run_all(config_from_yaml(), keys=("goals.obligation_bridge",))
    assert str(report.results[0].status) == str(ProbeStatus.BROKEN)


def test_the_probes_do_not_touch_real_state():
    """点検で実際の世界状態を書き換えないこと。"""
    live = runtime()
    live.observe_speaker(speaker_id=3, name="チビ", confirmed=True, now=1000.0)
    before = len(live.world.entities), len(live.world.facts)
    run_all(config_from_yaml())
    assert (len(live.world.entities), len(live.world.facts)) == before


def test_the_probe_sink_records_both_calls():
    from neuro_voice.diagnostics.wiring import _RecordingRuntime

    sink = _RecordingRuntime()
    WorldObservationAdapter(sink).observe_speaker(
        speaker_id=1, name="点検", confirmed=True, event_id="p", now=1000.0)
    assert sink.entities and sink.facts


def test_the_new_flags_are_watched():
    flags = run_all(config_from_yaml()).flags
    for key in ("world_state.observation_wiring_enabled",
                "world_state.persistence_enabled",
                "goals.obligation_bridge_enabled",
                "permissions.enforcement_enabled"):
        assert key in flags, key


def test_a_dependent_flag_shows_what_blocks_it():
    """**上位が off なら下位を上げても効かない**、が読めること。"""
    class Partial:
        def get(self, key, default=None):
            return True if key == "world_state.speaker_observation_enabled" else default

    entry = run_all(Partial()).flags["world_state.speaker_observation_enabled"]
    assert entry["ineffective"] is True
    assert "world_state.enabled" in entry["blocked_by"]
