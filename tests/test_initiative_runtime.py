"""注意と発話機会を、実際の会話の中で持ち回る。

Phase 5 第三弾。守りたいのは3つ。

1. **既定では何も変わらない。** フラグが off の間は従来どおり
2. **迂回が塞がっている。** 自発発話が Action Selector を通る
3. **「配線したが一度も動いていない」が見える。** 数えていないと分からない

2 がこの弾の本題。`respond_text(internal_event=True)` が
`_cognitive_decide` を丸ごと飛ばしていて、沈黙の決定も記憶の補正も
掛からずに喋っていた。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.cognition.initiative import SpeakingConditions
from neuro_voice.cognition.runtime import InitiativeRuntime


class Cfg:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


ON = {"initiative.enabled": True, "initiative.speech_enabled": True,
      "initiative.game_commentary_enabled": True,
      "initiative.budget.cooldown_s": 0.0}


def runtime(**flags) -> InitiativeRuntime:
    return InitiativeRuntime(Cfg(**flags))


def game(summary="ボスエリアに到達した", **kwargs) -> AttentionEvent:
    kwargs.setdefault("salience", .7)
    kwargs.setdefault("novelty", .8)
    kwargs.setdefault("occurred_at", 1000.0)
    return AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, summary=summary,
        topic_ids=("ボス",), **kwargs)


# ---------------------------------------------------------------------------
# 既定では何も変わらない
# ---------------------------------------------------------------------------


def test_the_default_is_off():
    plain = runtime()
    assert not plain.enabled and not plain.speech_enabled


def test_nothing_is_observed_while_off():
    plain = runtime()
    assert not plain.observe(game(), now=1000.0)
    assert plain.counters["events_seen"] == 0


def test_evaluate_returns_nothing_while_off():
    plain = runtime()
    plain.state.pending_opportunities = (game(),)
    assert plain.evaluate(SpeakingConditions()).empty


def test_speech_needs_its_own_flag():
    """取り込みだけ有効にしても、発話の判断は変わらない。"""
    partial = runtime(**{"initiative.enabled": True})
    assert partial.enabled and not partial.speech_enabled


# ---------------------------------------------------------------------------
# 取り込みと評価
# ---------------------------------------------------------------------------


def test_events_flow_through_and_are_counted():
    live = runtime(**ON)
    assert live.observe(game(), now=1000.0)
    assert live.counters == {**live.counters, "events_seen": 1, "events_accepted": 1}
    assert live.state.pending_opportunities


def test_a_repeated_event_is_counted_but_not_accepted():
    live = runtime(**ON)
    live.observe(game(), now=1000.0)
    live.observe(game(), now=1000.5)
    assert live.counters["events_seen"] == 2
    assert live.counters["events_accepted"] == 1


def test_silence_is_always_in_the_result():
    """**機会があることと話すべきことは別。**"""
    live = runtime(**ON)
    live.observe(game(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    assert any(item.proposed_action.value == "remain_silent"
               for item in result.opportunities)


def test_suppression_reasons_are_kept():
    """**なぜ黙ったかを読めるようにする。**"""
    live = runtime(**ON)
    live.observe(game(), now=1000.0)
    result = live.evaluate(
        SpeakingConditions(user_speaking=True), now=1001.0)
    assert result.suppressed
    assert result.suppressed[0][1] == "user_is_speaking"
    assert live.counters["opportunities_suppressed"] >= 1


def test_speaking_records_the_budget_and_the_dedup_key():
    live = runtime(**ON)
    live.observe(game(), now=1000.0)
    result = live.evaluate(SpeakingConditions(), now=1001.0)
    chosen = result.opportunities[0]
    live.record_spoken(chosen, now=1001.0)
    assert live.counters["spoke"] == 1
    # 同じことを二度言わない。
    again = live.evaluate(SpeakingConditions(), now=1002.0)
    assert all(item.dedup() != chosen.dedup()
               for item in again.opportunities
               if item.proposed_action.value != "remain_silent")


def test_a_crash_in_evaluation_makes_it_stay_quiet(monkeypatch):
    """**評価で落ちたら黙る。** 落ちた時に喋る方が危ない（第17条）。"""
    from neuro_voice.cognition import runtime as module

    live = runtime(**ON)
    live.observe(game(), now=1000.0)
    monkeypatch.setattr(module, "opportunities_from_events",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert live.evaluate(SpeakingConditions(), now=1001.0).empty


def test_the_status_never_contains_conversation_text():
    """**診断へ会話の中身を出さない**（第12条）。

    合言葉に `-` を混ぜてあるのは、**識別子が16進の乱数**だから。
    `1234` のような数字だけの並びは、12桁の16進IDにたまたま現れる
    （実測 5000回に1回）。それで赤くなると、本物の漏れと区別が付かず、
    やがて誰もこのテストを見なくなる。
    """
    import json

    live = runtime(**ON)
    live.observe(game(summary="口座番号は1234-5678だと言っていた"), now=1000.0)
    live.evaluate(SpeakingConditions(), now=1001.0)
    text = json.dumps(live.status(), ensure_ascii=False)
    assert "1234-5678" not in text and "口座番号" not in text


def test_the_counters_show_whether_anything_ever_happened():
    """**「配線したが一度も動いていない」を見分ける。**"""
    live = runtime(**ON)
    assert live.status()["counters"]["events_seen"] == 0
    live.observe(game(), now=1000.0)
    assert live.status()["counters"]["events_seen"] == 1


# ---------------------------------------------------------------------------
# 迂回が塞がっているか
#
# 実際の pipeline はマイクとGPUが要るので組み立てられない。**構造を読む。**
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline_source() -> str:
    return (Path(__file__).resolve().parents[1]
            / "neuro_voice" / "pipeline.py").read_text(encoding="utf-8")


def test_a_separate_entrance_exists(pipeline_source):
    """**内部プロンプトを人の発話として渡さない。** だから別の入口。"""
    assert "def _proactive_decide" in pipeline_source
    assert "def proactive_speech_allowed" in pipeline_source


def test_the_decision_is_carried_into_respond_text(pipeline_source):
    """決定を作るだけでは足りない。**持ち込んで実行ゲートに乗せる。**"""
    assert "proactive_decision=None" in pipeline_source
    assert "if proactive_decision is not None:" in pipeline_source
    assert "self._cognitive_decision = proactive_decision" in pipeline_source


def test_the_carried_decision_reaches_the_execution_gate(pipeline_source):
    """`_execute_or_stay_silent` に乗らないと、沈黙の決定が無視される。"""
    start = pipeline_source.index("if proactive_decision is not None:")
    block = pipeline_source[start:start + 700]
    assert "_execute_or_stay_silent" in block


def test_the_autonomy_call_site_uses_the_new_entrance(pipeline_source):
    assert "proactive_decision=proactive" in pipeline_source
    assert 'self._proactive_decide("spontaneous")' in pipeline_source


def test_the_game_commentary_call_site_uses_it_too(pipeline_source):
    assert 'self._proactive_decide("game")' in pipeline_source


def test_speech_is_revalidated_before_tts(pipeline_source):
    """**取り消した発話を後から突然再生しない。**"""
    assert "proactive_speech_allowed(proactive, opportunity)" in pipeline_source
    assert "runtime.confirm(" in pipeline_source


def test_attention_events_are_fed_from_the_real_path(pipeline_source):
    """作る側が無ければ、機会は永久に0件のまま。"""
    for call in ('note_attention_event(\n            "user_speech_started"',
                 'note_attention_event(\n            "game_event"',
                 'note_attention_event(\n                "silence_threshold"'):
        assert call in pipeline_source, call


def test_proactive_speech_stays_off_when_cognition_is_off(pipeline_source):
    """認知層が止まっているのに自発発話だけ通すと、**沈黙の決定も
    記憶の補正も掛からない発話**が復活する。"""
    start = pipeline_source.index("def _proactive_decide")
    block = pipeline_source[start:start + 1400]
    assert "self.cognition_active()" in block


# ---------------------------------------------------------------------------
# 診断に出ているか
# ---------------------------------------------------------------------------


def test_the_diagnostics_report_the_bypass_as_fixed():
    from neuro_voice.diagnostics import ProbeStatus, run_all

    results = {r.probe.key: r for r in run_all(Cfg(), None).results}
    bypass = results["attention.autonomy_bypass"]
    assert str(bypass.status) == str(ProbeStatus.OK), bypass.snapshot()


def test_the_diagnostics_show_whether_events_are_flowing():
    from neuro_voice.diagnostics import ProbeStatus, run_all

    class FakePipeline:
        def __init__(self, live):
            self._live = live

        def initiative(self):
            return self._live

    live = runtime(**ON)
    results = {r.probe.key: r for r in run_all(Cfg(**ON), None, FakePipeline(live)).results}
    idle = results["initiative.runtime"]
    assert str(idle.status) == str(ProbeStatus.DISCONNECTED)
    assert "1件も届いていない" in idle.detail

    live.observe(game(), now=1000.0)
    results = {r.probe.key: r for r in run_all(Cfg(**ON), None, FakePipeline(live)).results}
    assert str(results["initiative.runtime"].status) == str(ProbeStatus.OK)


def test_the_diagnostics_warn_when_the_speech_flag_cannot_take_effect():
    from neuro_voice.diagnostics import ProbeStatus, run_all

    flags = {"initiative.enabled": True, "initiative.speech_enabled": True}
    results = {r.probe.key: r for r in run_all(Cfg(**flags), None).results}
    result = results["initiative.speech_flag"]
    assert str(result.status) == str(ProbeStatus.DISCONNECTED)
    assert "cognition.enabled" in result.detail


def test_the_flag_panel_shows_the_dependency():
    from neuro_voice.diagnostics import run_all

    flags = run_all(Cfg(**{"initiative.speech_enabled": True}), None).flags
    entry = flags["initiative.speech_enabled"]
    assert entry["ineffective"] is True
    assert set(entry["blocked_by"]) == {"initiative.enabled", "cognition.enabled"}
