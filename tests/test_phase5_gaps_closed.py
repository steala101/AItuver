"""Phase 5 で残していた穴を塞いだことを固定する。

どれも「実装はあるが繋がっていない」形だった。**繋いだかどうかは
数えないと分からない**ので、繋がった側から確かめる。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from neuro_voice.cognition.initiative import (
    OpportunityType, TargetScope, opportunities_from_events, scope_for, timing_score,
)
from neuro_voice.cognition.attention import AttentionEvent, AttentionEventType
from neuro_voice.dialogue.conversation_planner import _internal_state_line


def source(*parts: str) -> str:
    return (Path(__file__).resolve().parents[1] / "neuro_voice"
            ).joinpath(*parts).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ① フラグの段階1
# ---------------------------------------------------------------------------


def test_stage_one_is_on_and_stage_two_is_not():
    """**段階1だけ上げる。** 行動が変わるのは speech_enabled から。"""
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "config.yaml")
        .read_text(encoding="utf-8"))
    initiative = raw["initiative"]
    assert initiative["enabled"] is True
    assert initiative["speech_enabled"] is False, "段階2はまだ上げない"
    assert initiative["game_commentary_enabled"] is False
    assert initiative["pre_speech_revalidation_enabled"] is True


def test_the_cognitive_layer_is_still_off():
    """自発発話の判断は cognition が要る。**そこはまだ上げていない。**"""
    import yaml

    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "config.yaml")
        .read_text(encoding="utf-8"))
    assert raw["cognition"]["enabled"] is False


# ---------------------------------------------------------------------------
# timing_score
# ---------------------------------------------------------------------------


def test_speaking_too_soon_after_the_user_scores_low():
    """**食い気味にならない。**"""
    eager = timing_score(seconds_since_user_turn=.2, seconds_since_own_speech=99.0)
    natural = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=99.0)
    assert eager < natural


def test_a_long_gap_makes_it_abrupt():
    """空きすぎたら、いま持ち出しても唐突。"""
    natural = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=99.0)
    stale = timing_score(seconds_since_user_turn=600.0, seconds_since_own_speech=99.0)
    assert stale < natural


def test_speaking_right_after_yourself_looks_like_a_barrage():
    just_spoke = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=.5)
    settled = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=60.0)
    assert just_spoke < settled


def test_a_closed_topic_makes_it_hard_to_bring_up():
    closed = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=99.0,
                          topic_just_closed=True)
    open_topic = timing_score(seconds_since_user_turn=1.5, seconds_since_own_speech=99.0)
    assert closed < open_topic


@pytest.mark.parametrize("kwargs", [
    {"user_speaking": True}, {"assistant_speaking": True},
])
def test_nobody_speaks_over_anybody(kwargs):
    assert timing_score(seconds_since_user_turn=1.5, **kwargs) == 0.0


def test_the_timing_reaches_the_opportunity():
    """**計算しても機会へ届かなければ意味がない。**"""
    event = AttentionEvent(
        event_type=AttentionEventType.GAME_EVENT, summary="ボスエリアに到達した",
        topic_ids=("ボス",), salience=.8, novelty=.8, occurred_at=1000.0)
    good = opportunities_from_events([event], timing=.9, now=1000.0)
    poor = opportunities_from_events([event], timing=.1, now=1000.0)
    assert good and poor
    assert good[0].timing_score == .9
    assert good[0].initiative_score > poor[0].initiative_score


# ---------------------------------------------------------------------------
# target_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [
    (OpportunityType.RESUME_OBLIGATION, TargetScope.DIRECT),
    (OpportunityType.REMIND, TargetScope.DIRECT),
    (OpportunityType.COMMENT_ON_GAME, TargetScope.GROUP),
    (OpportunityType.ACKNOWLEDGE_CHANGE, TargetScope.AMBIENT),
])
def test_the_target_is_decided_by_the_kind(kind, expected):
    """**全部 GROUP のままだと、独り言が呼びかけと同じ重さになる。**"""
    assert scope_for(kind, participant_ids=("chibi",)) is expected


def test_an_unknown_speaker_is_never_addressed_directly():
    """相手が特定できていないのに `DIRECT` にしない。"""
    assert scope_for(OpportunityType.RESUME_OBLIGATION) is TargetScope.GROUP


def test_the_scope_reaches_the_opportunity():
    event = AttentionEvent(
        event_type=AttentionEventType.VISUAL_CHANGE, summary="画面が変わった",
        topic_ids=("画面",), salience=.9, novelty=.9, occurred_at=1000.0)
    out = opportunities_from_events([event], now=1000.0)
    assert out and str(out[0].target_scope) == str(TargetScope.AMBIENT)


# ---------------------------------------------------------------------------
# 内面 → Planner（最後の黄色）
# ---------------------------------------------------------------------------


def test_the_internal_state_becomes_a_prompt_line():
    """**Phase 4 から繋がっていなかった配線。**"""
    line = _internal_state_line({
        "stance": {"mode": "guarded"},
        "expression_constraints": {
            "verbosity": "short", "assertiveness": "hedged",
            "humor_allowed": False, "intensity": "subtle"},
    })
    assert line
    assert "身構え" in line and "短く" in line and "冗談" in line


def test_an_empty_internal_state_adds_nothing():
    """**内面が空なら何も足さない。** 常時汚染しない。"""
    assert _internal_state_line({}) == ""
    assert _internal_state_line(None) == ""


def test_no_raw_numbers_reach_the_prompt():
    """**生の数値を渡さない。** 渡すとモデルが数字を根拠に語り始める。"""
    line = _internal_state_line({
        "affect": {"valence": 0.42, "arousal": 0.77},
        "stance": {"mode": "relaxed", "playfulness": 0.63},
        "expression_constraints": {"verbosity": "medium", "intensity": "subtle"},
    })
    for number in ("0.42", "0.77", "0.63"):
        assert number not in line


def test_the_planner_prompt_actually_calls_it():
    assert "_internal_state_line(plan.internal_state)" in source(
        "dialogue", "conversation_planner.py")


def test_the_planner_receives_it_from_mind():
    """`Mind.build_context` の**両方の分岐**から渡ること。"""
    text = source("mind", "mind.py")
    assert text.count("internal_state=self.internal_planner_view") == 2


# ---------------------------------------------------------------------------
# 実人数
# ---------------------------------------------------------------------------


def test_the_participant_count_is_not_a_boolean():
    """**2値だと3人と5人の差が出ない。**"""
    text = source("pipeline.py")
    assert "participant_count=self._mind.participant_count()" in text
    assert "participant_count=2 if state.is_group else 1" not in text


def test_counting_uses_the_audience_when_known():
    from neuro_voice.mind.mind import Mind

    fake = object.__new__(Mind)
    fake._privacy_context = {"group": True, "audience": {"a", "b", "c"}}
    assert Mind.participant_count(fake) == 3
    fake._privacy_context = {"group": True, "audience": set()}
    assert Mind.participant_count(fake) == 2
    fake._privacy_context = {"group": False, "audience": set()}
    assert Mind.participant_count(fake) == 1


# ---------------------------------------------------------------------------
# Discord parity（第19条）
# ---------------------------------------------------------------------------


def test_the_turn_closure_lives_in_one_place():
    """**手順が2箇所にあると、必ず片方だけ直されて食い違う。**"""
    assert "def close_turn" in source("mind", "mind.py")
    assert "def self_failure_pattern" in source("mind", "mind.py")


def test_discord_uses_the_shared_turn_closure():
    assert "self._mind.close_turn(" in source("discord_bridge", "bot.py")


def test_discord_has_the_same_initiative_layer():
    bot = source("discord_bridge", "bot.py")
    for needle in ("def initiative(self)", "def note_attention_event",
                   "def _proactive_gate", "blocked = self._proactive_gate()"):
        assert needle in bot, needle


def test_discord_feeds_attention_events_too():
    """作る側が無ければ、Discord では機会が永久に0件のまま。"""
    assert 'note_attention_event(\n            "silence_threshold"' in source(
        "discord_bridge", "bot.py")


def test_discord_stays_quiet_when_the_gate_fails():
    """**落ちたら黙る。** 自発発話は落ちた時に喋る方が危ない。"""
    bot = source("discord_bridge", "bot.py")
    start = bot.index("def _proactive_gate")
    assert 'return "gate_error"' in bot[start:start + 1600]


def test_the_shared_failure_pattern_covers_proactive_actions():
    """自発発話（COMMENT/REACT）も割り込まれたら失敗として数える。"""
    from neuro_voice.mind.mind import self_failure_pattern

    decision = type("D", (), {"selected_action": "comment", "confidence": 1.0})()
    outcome = type("O", (), {"status": "interrupted"})()
    assert self_failure_pattern(decision, outcome) == "kept_talking_after_end_signal"


def test_nothing_is_recorded_without_a_decision():
    from neuro_voice.mind.mind import self_failure_pattern

    assert self_failure_pattern(None, None) == ""


# ---------------------------------------------------------------------------
# 診断に出ているか
# ---------------------------------------------------------------------------


class Cfg:
    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def results(**flags):
    from neuro_voice.diagnostics import run_all

    return {r.probe.key: r for r in run_all(Cfg(**flags), None).results}


def test_the_last_yellow_is_gone():
    """**Phase 4 から残っていた「読む側が無い」が消えること。**"""
    from neuro_voice.diagnostics import ProbeStatus

    result = results()["internal.planner"]
    assert str(result.status) == str(ProbeStatus.OK), result.snapshot()


def test_the_diagnostics_cover_the_closed_gaps():
    from neuro_voice.diagnostics import ProbeStatus

    found = results(**{"initiative.enabled": True})
    for key in ("initiative.timing", "initiative.scope",
                "parity.turn_closure", "parity.initiative"):
        assert key in found, key
        assert str(found[key].status) in {
            str(ProbeStatus.OK), str(ProbeStatus.OFF)}, found[key].snapshot()


def test_no_probe_is_broken_or_disconnected():
    """**全部緑か、止めてあるか、起動待ちだけ。**"""
    from neuro_voice.diagnostics import ProbeStatus

    bad = [
        item.snapshot() for item in results(**{"initiative.enabled": True}).values()
        if str(item.status) in {str(ProbeStatus.BROKEN), str(ProbeStatus.ERROR),
                                str(ProbeStatus.DISCONNECTED)}
    ]
    assert not bad, bad
