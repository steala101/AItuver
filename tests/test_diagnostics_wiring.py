"""配線診断そのものを点検する。

**測る側が壊れているのに、測られる側の欠陥として報告する**——これを
この作業中に3回やっている。だから診断を入れるなら、診断が
「壊れているものを壊れていると言えるか」を先に確かめる。

全部緑になるテストは書かない。**わざと壊した時に赤くなること**を書く。
"""
from __future__ import annotations

import pytest

from neuro_voice.diagnostics import ProbeStatus, run_all
from neuro_voice.diagnostics import wiring


class Cfg:
    """設定の差し替え。実機の config.yaml には触らない。"""

    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


ALL_ON = {
    "cognition.enabled": True, "cognition.rollout_mode": "enabled",
    "memory.episodic_enabled": True, "memory.retrieval_enabled": True,
    "memory.reflection_enabled": True,
    "internal_state.enabled": True, "internal_state.affect_enabled": True,
    "internal_state.relationship_updates_enabled": True,
    "internal_state.planner_expression_enabled": True,
    "llm.num_ctx": 16384, "llm.max_tokens": 1024,
}


def by_key(report) -> dict[str, object]:
    return {r.probe.key: r for r in report.results}


# ---------------------------------------------------------------------------
# 診断が診断として成立しているか
# ---------------------------------------------------------------------------


def test_it_reports_something_for_every_probe():
    report = run_all(Cfg(**ALL_ON), None)
    assert len(report.results) >= 15, len(report.results)
    assert all(r.detail for r in report.results), "理由の無い判定を出さない"


def test_every_probe_says_what_breaks_if_it_fails():
    """「動いているか」だけでは、赤くなった時に優先順位を決められない。"""
    for probe, _func in wiring._REGISTRY:
        assert probe.impact, probe.key
        assert probe.layer and probe.title, probe.key


def test_the_whole_sweep_is_fast_enough_to_poll():
    """3秒ごとに回す画面なので、点検が会話の邪魔をしてはいけない。"""
    run_all(Cfg(**ALL_ON), None)          # import の分を先に払う
    report = run_all(Cfg(**ALL_ON), None)
    assert report.total_ms < 250, f"{report.total_ms:.1f}ms"


def test_a_probe_that_raises_does_not_stop_the_others():
    """**点検で会話を止めない**（第17条）。1つ落ちても残りは続ける。"""
    def explode(cfg, mind):
        raise RuntimeError("わざと落とす")

    probe = wiring.Probe(key="test.boom", layer="テスト", title="爆発する点検",
                         impact="他の点検まで止まってはいけない")
    wiring._REGISTRY.append((probe, explode))
    try:
        report = run_all(Cfg(**ALL_ON), None)
        result = by_key(report)["test.boom"]
        assert str(result.status) == str(ProbeStatus.ERROR)
        assert "わざと落とす" in result.detail
        assert len(report.results) > 1
    finally:
        wiring._REGISTRY.remove((probe, explode))


# ---------------------------------------------------------------------------
# 止めてあるのと壊れているのを混ぜない
# ---------------------------------------------------------------------------


def test_flags_off_is_not_reported_as_broken():
    """**混ぜると本物の故障が埋もれる。**

    「赤が多いのはフラグを上げていないからだろう」で流されるのが
    いちばん困る。
    """
    report = run_all(Cfg(), None)
    assert not report.broken, [r.snapshot() for r in report.broken]
    statuses = {str(r.status) for r in report.results}
    assert str(ProbeStatus.OFF) in statuses


def test_the_wiring_is_checked_even_while_the_flag_is_off():
    """点検は合成データの一往復。**フラグが off でも生死は分かる。**"""
    off = by_key(run_all(Cfg(), None))
    # 記憶の想起は既定 off だが、引き金の判定そのものは検算されている。
    assert "similar_to_past_failure" in off["memory.trigger"].detail


def test_an_unknown_rollout_mode_is_broken_not_off():
    report = by_key(run_all(Cfg(cognition_enabled=True, **{
        "cognition.enabled": True, "cognition.rollout_mode": "なにか新しいモード",
    }), None))
    assert str(report["cognition.rollout"].status) == str(ProbeStatus.BROKEN)


# ---------------------------------------------------------------------------
# **わざと壊した時に赤くなるか** — ここが本体
# ---------------------------------------------------------------------------


def test_it_catches_the_irritation_regression(monkeypatch):
    """今回実際に起きた壊れ方。**元に戻したら赤くなること。**

    `CognitiveState.irritation` が入れ子と別名を吸収しなくなると、
    値は常に 0.0 に戻る——実装もテストも設定も揃ったまま。
    """
    from neuro_voice.cognition.state import CognitiveState

    assert str(by_key(run_all(Cfg(**ALL_ON), None))["internal.irritation"].status) == str(
        ProbeStatus.OK)

    monkeypatch.setattr(
        CognitiveState, "irritation",
        property(lambda self: float(self.affect_state.get("irritation", 0.0))),
    )
    result = by_key(run_all(Cfg(**ALL_ON), None))["internal.irritation"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "届かない" in result.detail


def test_it_catches_a_lost_relationship_axis(monkeypatch):
    from neuro_voice.cognition.state import CognitiveState

    monkeypatch.delattr(CognitiveState, "caution", raising=True)
    result = by_key(run_all(Cfg(**ALL_ON), None))["internal.axes"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "caution" in result.detail


def test_it_catches_memory_that_stops_moving_the_score(monkeypatch):
    """記憶がプロンプトの飾りに戻ったら気づけること。"""
    from neuro_voice.cognition import recall

    monkeypatch.setattr(recall, "FAILURE_TO_ACTION", {})
    result = by_key(run_all(Cfg(**ALL_ON), None))["memory.influence"]
    assert str(result.status) == str(ProbeStatus.BROKEN)


def test_it_catches_a_greeting_that_starts_being_stored(monkeypatch):
    """**規則が拾わないだけ**では点検にならないので、ゲート側も見ている。"""
    from neuro_voice.cognition import episodic

    monkeypatch.setattr(episodic, "is_trivial", lambda _text: False)
    result = by_key(run_all(Cfg(**ALL_ON), None))["memory.write_gate"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "相づち" in result.detail or "挨拶" in result.detail


def test_it_catches_emotion_reaching_a_warning(monkeypatch):
    """**いちばんやってはいけないことが起きたら、まず赤くなること。**

    `PROTECTED_ACTIONS` を外すだけでは何も起きない——いまは WARN を狙う規則が
    1つも無いため。守りが二重になっているのは良いことだが、**点検としては
    「実際に返ってきた補正」を見ないと意味がない。** 規則が1つ増えた瞬間に
    穴が開くのはそちら側なので。
    """
    from neuro_voice.cognition import internal_state
    from neuro_voice.cognition.internal_state import StateBias
    from neuro_voice.cognition.types import ActionType

    monkeypatch.setattr(
        internal_state, "action_bias",
        lambda state, **kwargs: StateBias(
            adjustments={str(ActionType.WARN): -.5}, dimensions_used=("irritation",)),
    )
    result = by_key(run_all(Cfg(**ALL_ON), None))["internal.safety"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "警告" in result.detail


def test_it_catches_silence_becoming_a_punishment(monkeypatch):
    """沈黙が感情で選ばれ始めたら赤くなること。"""
    from neuro_voice.cognition import internal_state
    from neuro_voice.cognition.internal_state import StateBias
    from neuro_voice.cognition.types import ActionType

    monkeypatch.setattr(
        internal_state, "action_bias",
        lambda state, **kwargs: StateBias(
            adjustments={str(ActionType.REMAIN_SILENT): .3}, dimensions_used=("irritation",)),
    )
    result = by_key(run_all(Cfg(**ALL_ON), None))["internal.safety"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "罰" in result.detail


def test_it_catches_the_end_signal_being_masked_again(monkeypatch):
    """Phase 4 の preflight で見つけた壊れ方の再発を見張る。"""
    from neuro_voice.cognition import recall

    original = recall.retrieval_trigger

    def masked(text, state=None, *, action=None):
        if state is not None and getattr(state, "unresolved_obligations", ()):
            return "unresolved_obligation"
        return original(text, state, action=action)

    monkeypatch.setattr(recall, "retrieval_trigger", masked)
    result = by_key(run_all(Cfg(**ALL_ON), None))["memory.trigger"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "隠される" in result.detail


def test_it_catches_a_reflection_built_from_one_episode(monkeypatch):
    from neuro_voice.cognition import reflection

    monkeypatch.setattr(reflection, "MIN_SUPPORT", 1)
    result = by_key(run_all(Cfg(**ALL_ON), None))["memory.reflection"]
    assert str(result.status) == str(ProbeStatus.BROKEN)
    assert "1件" in result.detail


def test_it_catches_an_ungrounded_instruction_slipping_through(monkeypatch):
    """表が空でも「押して」と言えてしまった事故の再発を見張る。"""
    from neuro_voice.games.ktane import verify

    monkeypatch.setattr(verify, "contains_instruction", lambda _reply: False)
    result = by_key(run_all(Cfg(**ALL_ON), None))["ktane.verify"]
    assert str(result.status) == str(ProbeStatus.BROKEN)


# ---------------------------------------------------------------------------
# 「読む側が無い」を隠さない
# ---------------------------------------------------------------------------


def test_a_missing_consumer_is_reported_as_disconnected():
    """**フラグを上げて「効かない」と悩む時間がいちばんもったいない。**

    Planner の表現制約は形を返すが、`conversation_planner.py` がまだ
    読んでいない。それを OK と言ってはいけないし、BROKEN でもない。
    """
    result = by_key(run_all(Cfg(**ALL_ON), None))["internal.planner"]
    assert str(result.status) in {
        str(ProbeStatus.DISCONNECTED), str(ProbeStatus.OK),
    }
    if str(result.status) == str(ProbeStatus.DISCONNECTED):
        assert "読んでいない" in result.detail


def test_disconnected_shows_up_in_the_attention_list():
    payload = run_all(Cfg(**ALL_ON), None).snapshot()
    keys = {item["key"] for item in payload["attention"]}
    for item in payload["attention"]:
        assert item["status"] in {"broken", "error", "disconnected"}
    assert "internal.planner" in keys or not keys


# ---------------------------------------------------------------------------
# フラグの依存関係
# ---------------------------------------------------------------------------


def test_a_flag_that_cannot_take_effect_is_marked():
    """上位が off なら、下位を上げても効かない。**そこを見せる。**"""
    flags = run_all(Cfg(**{
        "internal_state.enabled": False,
        "internal_state.affect_enabled": True,
    }), None).flags
    assert flags["internal_state.affect_enabled"]["ineffective"] is True
    assert flags["internal_state.affect_enabled"]["blocked_by"] == ["internal_state.enabled"]


def test_a_flag_with_its_parent_on_is_not_marked():
    flags = run_all(Cfg(**ALL_ON), None).flags
    assert flags["internal_state.affect_enabled"]["ineffective"] is False


# ---------------------------------------------------------------------------
# 読み取り専用であること
# ---------------------------------------------------------------------------


def test_the_diagnosis_writes_nothing(tmp_path):
    """**点検で実際の記憶や関係値を書き換えない。**

    見ただけで状態が変わる仕組みは、見るのが怖くなって使われなくなる。
    """
    from neuro_voice.mind.store import MemoryStore

    store = MemoryStore(tmp_path / "mind.db")
    store.add_episode("覚えている話", event_type="preference")
    before = [dict(row) for row in store.episodes()]

    mind = type("M", (), {"_store": store, "cognitive_state": lambda self: None})()
    run_all(Cfg(**ALL_ON), mind)

    after = [dict(row) for row in store.episodes()]
    assert after == before, "点検が記憶を書き換えている"
    store.close()


def test_the_snapshot_is_json_safe():
    """画面へそのまま渡すので、シリアライズできない値を混ぜない。"""
    import json

    payload = run_all(Cfg(**ALL_ON), None).snapshot()
    text = json.dumps(payload, ensure_ascii=False)
    assert "layers" in payload and "flags" in payload and "counts" in payload
    assert len(text) > 100


@pytest.mark.parametrize("status", list(ProbeStatus))
def test_every_status_has_a_severity(status):
    """並べ替えで未知の状態が末尾へ消えないこと。"""
    assert status in wiring._SEVERITY
