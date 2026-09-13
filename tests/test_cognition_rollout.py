"""段階的な有効化と、発話の出どころの統制。

Phase 2.5 で沈黙は沈黙になったが、**認知層を通らない経路**（ゲーム警告の
高速経路・自発発話・相槌）はそのまま残っている。経路が分かれていること
自体は正しい——危険警告をLLMの後ろに置くわけにはいかない。問題は、
どこから出た音声か分からないと、通常回答が紛れ込んでも気づけないこと。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition.rollout import (
    BRIEF_ACK_CONSTRAINTS, BypassReason, ClosurePolicy, RolloutMode, SpeechRequest,
    SpeechSource, check_speech, closure_bias, cognition_active, spontaneous_suppressed,
)
from neuro_voice.cognition.types import ActionType


def request(source, action=None, **kwargs) -> SpeechRequest:
    return SpeechRequest(source_type=source, source_action=action, **kwargs)


# ---------------------------------------------------------------------------
# テスト1/2: 段階的な有効化
# ---------------------------------------------------------------------------


def test_a_test_session_turns_it_on_only_while_started():
    assert not cognition_active(RolloutMode.TEST_SESSION, enabled=True, test_session=False)
    assert cognition_active(RolloutMode.TEST_SESSION, enabled=True, test_session=True)


def test_disabled_mode_keeps_the_old_path():
    assert not cognition_active(RolloutMode.DISABLED, enabled=True, test_session=True)


def test_the_master_switch_wins():
    """**止め方は1つ。** enabled が false なら何を設定しても効かない。"""
    for mode in RolloutMode:
        assert not cognition_active(mode, enabled=False, test_session=True)


def test_a_profile_allowlist_is_exact():
    assert cognition_active(
        RolloutMode.PROFILE_ALLOWLIST, enabled=True,
        profile_id="minecraft", allowlist=("minecraft",))
    assert not cognition_active(
        RolloutMode.PROFILE_ALLOWLIST, enabled=True,
        profile_id="minecraft", allowlist=("keep_talking_and_nobody_explodes",))


def test_an_unknown_mode_falls_back_to_disabled():
    assert not cognition_active("なにか新しいモード", enabled=True, test_session=True)


# ---------------------------------------------------------------------------
# テスト3: 認知有効時の Legacy 発話
# ---------------------------------------------------------------------------


def test_legacy_speech_without_a_reason_is_refused():
    result = check_speech(
        request(SpeechSource.LEGACY_PATH), cognition_enabled=True,
    )
    assert not result.allowed
    assert result.reason == "legacy_speech_without_reason"


def test_legacy_speech_with_an_explicit_reason_passes():
    result = check_speech(
        request(SpeechSource.LEGACY_PATH,
                bypass_reason=BypassReason.LEGACY_COMPATIBILITY),
        cognition_enabled=True,
    )
    assert result.allowed


def test_everything_passes_while_cognition_is_off():
    """既定のまま使っている人を黙らせない。"""
    assert check_speech(
        request(SpeechSource.LEGACY_PATH), cognition_enabled=False,
    ).allowed


def test_a_cognitive_request_needs_an_actual_decision():
    result = check_speech(
        request(SpeechSource.COGNITIVE_DECISION), cognition_enabled=True,
        has_valid_decision=False,
    )
    assert not result.allowed


# ---------------------------------------------------------------------------
# テスト4/5: 高速経路
# ---------------------------------------------------------------------------


def test_a_confident_warning_passes_the_fast_path():
    result = check_speech(
        request(SpeechSource.GAME_WARNING_FAST_PATH, ActionType.WARN,
                confidence=.9, bypass_reason=BypassReason.REALTIME_DANGER),
        cognition_enabled=True,
    )
    assert result.allowed


@pytest.mark.parametrize("action", [
    ActionType.ANSWER, ActionType.ASK_CLARIFICATION,
    ActionType.CONTINUE_PREVIOUS_TOPIC, ActionType.MAKE_LIGHT_JOKE,
    ActionType.CHALLENGE_ASSUMPTION, ActionType.ACKNOWLEDGE_EMOTION,
])
def test_ordinary_replies_cannot_leave_through_the_warning_path(action):
    """**通常回答を危険警告の経路から出さない。**"""
    result = check_speech(
        request(SpeechSource.GAME_WARNING_FAST_PATH, action, confidence=.9),
        cognition_enabled=True,
    )
    assert not result.allowed
    assert result.reason == "fast_path_action_not_allowlisted"


def test_a_low_confidence_danger_is_not_a_warning():
    result = check_speech(
        request(SpeechSource.GAME_WARNING_FAST_PATH, ActionType.WARN, confidence=.2),
        cognition_enabled=True,
    )
    assert not result.allowed


def test_a_backchannel_may_not_carry_a_full_action():
    """相槌は短い反応だけ。回答や質問を始めさせない。"""
    assert check_speech(
        request(SpeechSource.BACKCHANNEL), cognition_enabled=True,
    ).allowed
    assert not check_speech(
        request(SpeechSource.BACKCHANNEL, ActionType.ANSWER), cognition_enabled=True,
    ).allowed


# ---------------------------------------------------------------------------
# テスト6: 沈黙直後の自発発話
# ---------------------------------------------------------------------------


def test_the_same_topic_is_not_reopened_after_silence():
    assert spontaneous_suppressed(
        last_status="silent_completed", last_topic_id="設計の話",
        topic_id="設計の話", seconds_since_turn=120.0,
    ) == "same_topic_after_silence"


def test_a_different_topic_is_allowed_once_the_quiet_time_passes():
    """別件で話しかけるのまで止めない。"""
    assert spontaneous_suppressed(
        last_status="silent_completed", last_topic_id="設計の話",
        topic_id="夕飯の話", seconds_since_turn=120.0,
    ) == ""


def test_nothing_is_suppressed_right_after_a_spoken_turn():
    assert spontaneous_suppressed(
        last_status="spoken_completed", last_topic_id="設計の話",
        topic_id="設計の話", seconds_since_turn=1.0,
    ) == ""


def test_even_a_new_topic_waits_a_little_after_silence():
    assert spontaneous_suppressed(
        last_status="silent_completed", last_topic_id="設計の話",
        topic_id="夕飯の話", seconds_since_turn=2.0,
    ) == "too_soon_after_silence"


# ---------------------------------------------------------------------------
# テスト7: 短い了承が通常回答へ膨らまない
# ---------------------------------------------------------------------------


def test_the_brief_acknowledgement_is_constrained():
    assert BRIEF_ACK_CONSTRAINTS["max_sentences"] == 1
    for key in (
        "ask_follow_up", "continue_explanation", "introduce_new_topic",
        "humor", "resume_interrupted_response",
    ):
        assert BRIEF_ACK_CONSTRAINTS[key] is False


def test_the_brief_acknowledgement_goal_forbids_expansion():
    from neuro_voice.cognition.bridge import ACTION_TO_FRAME

    goal = ACTION_TO_FRAME[ActionType.BRIEF_ACKNOWLEDGE]["response_goal"]
    for phrase in ("one short sentence", "No question", "no new topic"):
        assert phrase in goal


# ---------------------------------------------------------------------------
# テスト8: 方針の切り替えは「小さな傾き」であって固定ではない
# ---------------------------------------------------------------------------


def test_each_policy_nudges_a_different_candidate():
    assert closure_bias(ClosurePolicy.SILENT_PREFERRED, ActionType.REMAIN_SILENT) > 0
    assert closure_bias(ClosurePolicy.BRIEF_ACK_PREFERRED, ActionType.BRIEF_ACKNOWLEDGE) > 0
    assert closure_bias(ClosurePolicy.ADAPTIVE, ActionType.REMAIN_SILENT) == 0


def test_the_nudge_is_small_enough_not_to_override():
    """安全警告や明示的な質問を押しのけない大きさに留める。"""
    for policy in ClosurePolicy:
        for action in (ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE):
            assert closure_bias(policy, action) <= .2


def test_both_closure_options_are_offered_regardless_of_policy():
    """**どちらかへ固定しない。** 実機で比べられるよう両方候補に出す。"""
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state

    state = build_state(user_state={"end_signal": .9, "engagement": .1})
    event = CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ")
    for policy in ClosurePolicy:
        actions = {
            item.action_type
            for item in CognitiveKernel(closure_policy=policy).propose(event, state)
        }
        assert ActionType.REMAIN_SILENT in actions
        assert ActionType.BRIEF_ACKNOWLEDGE in actions


def test_the_policy_actually_changes_the_outcome():
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType, build_state

    state = build_state(user_state={"end_signal": .9, "engagement": .1})
    event = CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ")
    silent = CognitiveKernel(closure_policy=ClosurePolicy.SILENT_PREFERRED)
    ack = CognitiveKernel(closure_policy=ClosurePolicy.BRIEF_ACK_PREFERRED)
    assert silent.decide(event, state).selected_action is ActionType.REMAIN_SILENT
    assert ack.decide(event, state).selected_action is ActionType.BRIEF_ACKNOWLEDGE


# ---------------------------------------------------------------------------
# 配線
# ---------------------------------------------------------------------------


def test_the_pipeline_exposes_the_test_session_switch():
    from neuro_voice.pipeline import VoicePipeline

    for name in (
        "cognition_active", "start_cognition_test_session",
        "log_cognition_status", "spontaneous_speech_suppressed",
    ):
        assert hasattr(VoicePipeline, name), name


def test_the_spontaneous_loop_checks_the_suppression():
    import inspect

    from neuro_voice.pipeline import VoicePipeline

    source = inspect.getsource(VoicePipeline)
    assert "spontaneous_speech_suppressed(" in source


def test_the_default_config_is_not_switched_on():
    """**既定値を勝手に本番有効へ変えない。**"""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert data["cognition"]["enabled"] is False
    assert data["cognition"]["rollout_mode"] == "disabled"


# ---------------------------------------------------------------------------
# adaptive: その場の空気で決める
#
# 無言も短い了承も**どちらも正解**なので、決め打ちしない。ただし
# 「拮抗させてコイントス」も判断ではない。何が違えばどちらへ寄るのかを、
# 手がかりごとに固定しておく。
# ---------------------------------------------------------------------------

from neuro_voice.cognition import build_state  # noqa: E402
from neuro_voice.cognition.rollout import (  # noqa: E402
    CLOSURE_BIAS_LIMIT, adaptive_closure_bias,
)


def closing(**kwargs):
    user = {"end_signal": .9, "engagement": .1, **kwargs.pop("user_state", {})}
    return build_state(user_state=user, **kwargs)


def bias_of(state) -> float:
    """正なら「わかった」、負なら無言。"""
    return adaptive_closure_bias(state)[0]


def test_being_cut_off_mid_sentence_leans_towards_acknowledging():
    """黙ると固まったように聞こえる。聞こえていて止めたことを伝える。"""
    assert bias_of(closing(world_state={"interrupted_response": "説明の途中"})) > 0


def test_a_lull_with_nothing_pending_leans_towards_silence():
    assert bias_of(closing(relationship={"comfort": .8})) < 0


def test_an_irritated_user_gets_a_short_word_rather_than_silence():
    """無言は拗ねたようにも取れる。"""
    calm = bias_of(closing(relationship={"comfort": .5}))
    heated = bias_of(closing(
        relationship={"comfort": .5}, user_state={"frustration": .9},
    ))
    assert heated > calm


def test_closeness_makes_silence_comfortable():
    distant = bias_of(closing(relationship={"comfort": .1}))
    close = bias_of(closing(relationship={"comfort": .9}))
    assert close < distant, "親しいほど無言でよい"


def test_an_audience_makes_dead_air_worse():
    alone = bias_of(closing(relationship={"comfort": .5}))
    watched = bias_of(closing(relationship={"comfort": .5}, world_state={"group": True}))
    assert watched > alone


def test_agreement_is_not_dismissal():
    """「もういいよ、それで」は打ち切りではなく同意。無視しない。"""
    dismissal = bias_of(closing(relationship={"comfort": .5}))
    agreement = bias_of(closing(
        relationship={"comfort": .5}, user_state={"closure_is_affirmative": True},
    ))
    assert agreement > dismissal


def test_the_reading_stays_a_nudge():
    """**上書きではない。** 安全警告や明示的な質問を押しのけない。"""
    extreme = closing(
        relationship={"comfort": .0}, user_state={
            "frustration": 1.0, "closure_is_affirmative": True,
        },
        world_state={"interrupted_response": "説明", "group": True},
    )
    assert abs(bias_of(extreme)) <= CLOSURE_BIAS_LIMIT


def test_the_reason_is_recorded():
    """「なぜ黙ったか」を後から読めないと、調整のしようがない。"""
    _bias, reasons = adaptive_closure_bias(
        closing(world_state={"interrupted_response": "説明"}))
    assert "was_mid_utterance" in reasons


def test_adaptive_actually_changes_the_action():
    """空気が違えば選ぶ行動も変わる。"""
    from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType

    kernel = CognitiveKernel(closure_policy=ClosurePolicy.ADAPTIVE)
    event = CognitiveEvent(event_type=EventType.USER_UTTERANCE, content="もういいよ")
    mid_speech = closing(
        relationship={"comfort": .3}, world_state={"interrupted_response": "説明の途中"},
    )
    quiet_lull = closing(relationship={"comfort": .9})
    assert kernel.decide(event, mid_speech).selected_action is ActionType.BRIEF_ACKNOWLEDGE
    assert kernel.decide(event, quiet_lull).selected_action is ActionType.REMAIN_SILENT


def test_an_explicit_policy_still_wins_over_the_reading():
    """聞き比べたい時に、勝手に揺れては困る。"""
    mid_speech = closing(world_state={"interrupted_response": "説明"})
    assert closure_bias(
        ClosurePolicy.SILENT_PREFERRED, ActionType.REMAIN_SILENT, mid_speech,
    ) == CLOSURE_BIAS_LIMIT
    assert closure_bias(
        ClosurePolicy.SILENT_PREFERRED, ActionType.BRIEF_ACKNOWLEDGE, mid_speech,
    ) == 0
