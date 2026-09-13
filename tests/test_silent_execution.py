"""**沈黙は空文字ではなく、経路を呼ばないこと。**

前回の実装では `REMAIN_SILENT` が最高得点で選ばれても、Conversation Planner
以降が普通に走って喋っていた。決定が参考情報にすぎなかったからである。

ここで固定するのは「呼ばれなかったこと」で、出力が空だったことではない。
空文字は失敗と区別がつかないし、fallback が復活させる。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition.executor import (
    ActionExecutionGate, obligations_to_release,
)
from neuro_voice.cognition.types import (
    ActionDecision, ActionType, ExecutionStatus, SpeechPolicy, speech_policy,
)


def decide(action: ActionType, supporting: ActionType | None = None) -> ActionDecision:
    return ActionDecision(
        selected_action=action, supporting_action=supporting, turn_id="t1",
    )


# ---------------------------------------------------------------------------
# 発話可否が型で決まっている
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [
    ActionType.ANSWER, ActionType.ACKNOWLEDGE_EMOTION, ActionType.ASK_CLARIFICATION,
    ActionType.CHALLENGE_ASSUMPTION, ActionType.CONTINUE_PREVIOUS_TOPIC,
    ActionType.WARN, ActionType.MAKE_LIGHT_JOKE,
    ActionType.RESUME_INTERRUPTED_RESPONSE,
])
def test_speaking_actions_are_allowed(action):
    assert speech_policy(action) is not SpeechPolicy.FORBIDDEN


@pytest.mark.parametrize("action", [
    ActionType.REMAIN_SILENT, ActionType.STORE_MEMORY,
    ActionType.RETRIEVE_MEMORY, ActionType.ABANDON_INTERRUPTED_RESPONSE,
])
def test_non_speaking_actions_are_forbidden(action):
    assert speech_policy(action) is SpeechPolicy.FORBIDDEN


def test_an_unknown_action_defaults_to_silence():
    """既定は deny。知らないものを喋らせない。"""
    assert speech_policy("なにか新しい行動") is SpeechPolicy.FORBIDDEN


# ---------------------------------------------------------------------------
# テスト1: REMAIN_SILENT は経路を呼ばない
# ---------------------------------------------------------------------------


def test_remain_silent_skips_every_speech_stage():
    plan = ActionExecutionGate.plan(decide(ActionType.REMAIN_SILENT))
    assert plan.route == "silent"
    assert not plan.speech_allowed
    assert not plan.planner_allowed
    outcome = ActionExecutionGate.non_speech_outcome(decide(ActionType.REMAIN_SILENT), plan)
    assert outcome.status is ExecutionStatus.SILENT_COMPLETED
    assert not outcome.speech_generated
    assert not outcome.planner_called
    assert not outcome.realizer_called
    assert not outcome.tts_called
    assert outcome.turn_closed


def test_a_silent_turn_is_not_a_failure():
    plan = ActionExecutionGate.plan(decide(ActionType.REMAIN_SILENT))
    outcome = ActionExecutionGate.non_speech_outcome(decide(ActionType.REMAIN_SILENT), plan)
    assert outcome.completed
    assert outcome.status is not ExecutionStatus.FAILED


def test_silence_does_not_close_the_session():
    """ターンは閉じるが、会話は続く。"""
    plan = ActionExecutionGate.plan(decide(ActionType.REMAIN_SILENT), end_signal=.0)
    assert not plan.close_topic, "打ち切りの合図が無ければ話題も閉じない"


# ---------------------------------------------------------------------------
# テスト2: 通常の ANSWER は通る
# ---------------------------------------------------------------------------


def test_answer_goes_through_the_speech_route():
    plan = ActionExecutionGate.plan(decide(ActionType.ANSWER))
    assert plan.route == "speech"
    assert plan.speech_allowed and plan.planner_allowed


# ---------------------------------------------------------------------------
# テスト3: 「もういいよ」＋中断発話
# ---------------------------------------------------------------------------


def test_an_ending_signal_clears_the_pending_response():
    plan = ActionExecutionGate.plan(decide(ActionType.REMAIN_SILENT), end_signal=.9)
    assert plan.clear_pending_response
    assert plan.close_topic, "その話題は休止する"


def test_abandoning_the_interrupted_response_is_internal_not_silent():
    plan = ActionExecutionGate.plan(decide(ActionType.ABANDON_INTERRUPTED_RESPONSE))
    assert plan.route == "internal"
    assert plan.clear_pending_response
    outcome = ActionExecutionGate.non_speech_outcome(
        decide(ActionType.ABANDON_INTERRUPTED_RESPONSE), plan,
    )
    assert outcome.status is ExecutionStatus.INTERNAL_COMPLETED


def test_only_the_related_obligations_are_released():
    """**一括削除しない。** 中断発話を捨てても、他の保留は残る。"""
    obligations = (
        "finish_interrupted_response", "ユーザーへの質問", "次回に回した話題",
    )
    plan = ActionExecutionGate.plan(decide(ActionType.REMAIN_SILENT), end_signal=.9)
    released = obligations_to_release(obligations, plan)
    assert released == ("finish_interrupted_response",)


def test_nothing_is_released_when_the_response_is_kept():
    plan = ActionExecutionGate.plan(decide(ActionType.RESUME_INTERRUPTED_RESPONSE))
    assert not plan.clear_pending_response
    assert obligations_to_release(("finish_interrupted_response",), plan) == ()


# ---------------------------------------------------------------------------
# テスト7 / 矛盾: WARN は通る、矛盾は禁止側へ
# ---------------------------------------------------------------------------


def test_warn_still_speaks():
    plan = ActionExecutionGate.plan(decide(ActionType.WARN))
    assert plan.speech_allowed, "緊急警告を止めない"


def test_a_contradictory_decision_falls_back_to_silence():
    """沈黙と発話が同時に指定されたら、安全側へ倒して痕跡を残す。"""
    decision = decide(ActionType.REMAIN_SILENT, ActionType.ANSWER)
    assert decision.contradictory
    assert not decision.speaks
    plan = ActionExecutionGate.plan(decision)
    assert not plan.speech_allowed
    assert any("contradictory" in w for w in plan.warnings)


def test_a_normal_pair_is_not_contradictory():
    decision = decide(ActionType.ANSWER, ActionType.ACKNOWLEDGE_EMOTION)
    assert not decision.contradictory
    assert decision.speaks


# ---------------------------------------------------------------------------
# 経路そのものが default deny になっているか
# ---------------------------------------------------------------------------


def test_the_pipeline_gate_returns_early_for_silence():
    """`respond_text` が発話前に打ち切れる形になっていること。

    **呼び出しの数だけ確かめる。** 最初の1つだけ見ていると、後から
    足された経路（自発発話など）が打ち切らないまま通ってしまう。
    コメント中の言及に引っかからないよう、`(` 付きで探す。
    """
    import inspect

    from neuro_voice.pipeline import VoicePipeline

    source = inspect.getsource(VoicePipeline.respond_text)
    calls = source.split("_execute_or_stay_silent(")[1:]
    assert calls, "実行ゲートを呼んでいない"
    for index, tail in enumerate(calls):
        assert "return" in tail[:120], f"{index + 1}番目の呼び出しが打ち切っていない"


def test_the_tts_exit_has_a_single_gate():
    import inspect

    from neuro_voice.pipeline import VoicePipeline

    assert "_speech_allowed_now" in inspect.getsource(VoicePipeline._speak_loop)


def test_discord_uses_the_same_gate():
    """第19条。片側だけ黙るのは整合が取れていない。"""
    import inspect

    from neuro_voice.discord_bridge.bot import DiscordBridge

    assert "_speech_allowed_now" in inspect.getsource(DiscordBridge._speak)


def test_the_gate_is_transparent_when_cognition_is_off():
    """既定（無効）では従来どおり喋る。"""
    import inspect

    from neuro_voice.pipeline import VoicePipeline

    source = inspect.getsource(VoicePipeline._speech_allowed_now)
    assert "cognition_active()" in source
    assert "if not active:\n            return True" in source
    assert "CognitionRolloutResolver.resolve" in inspect.getsource(
        VoicePipeline.cognition_active,
    )
