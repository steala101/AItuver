"""指示書の5テスト: 対応表と guarded_transition 経由の反映。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from neuro_voice.cognition.bridge import ACTION_TO_FRAME, frame_changes
from neuro_voice.cognition.types import ActionDecision, ActionType
from neuro_voice.mind.mind import Mind


@pytest.mark.parametrize("action", list(ACTION_TO_FRAME))
def test_every_mapped_action_yields_all_three_keys(action):
    changes = frame_changes(ActionDecision(selected_action=action))
    assert set(changes) == {"response_goal", "response_shape", "social_mode"}
    assert all(changes.values())


@pytest.mark.parametrize("action", [
    ActionType.REMAIN_SILENT, ActionType.STORE_MEMORY,
    ActionType.ABANDON_INTERRUPTED_RESPONSE,
])
def test_silent_actions_map_to_none(action):
    assert frame_changes(ActionDecision(selected_action=action)) is None


def test_the_returned_dict_is_a_copy():
    changes = frame_changes(ActionDecision(selected_action=ActionType.ANSWER))
    changes["response_goal"] = "書き換え"
    assert ACTION_TO_FRAME[ActionType.ANSWER]["response_goal"] != "書き換え"


def _mind_with_mock_kernel():
    mind = Mind.__new__(Mind)          # __init__ はDB等を要するので通さない
    mind._kernel = MagicMock()
    mind._kernel.guarded_transition.return_value = True
    return mind


def test_apply_calls_guarded_transition_with_a_reason():
    mind = _mind_with_mock_kernel()
    decision = ActionDecision(
        selected_action=ActionType.ANSWER, decision_reason="addressed",
    )
    assert mind.apply_cognitive_decision(decision, source="local", response_id="r1")
    kwargs = mind._kernel.guarded_transition.call_args.kwargs
    assert kwargs["transition_reason"].startswith("cognitive_kernel:")
    assert kwargs["transition_reason"] != "cognitive_kernel:"
    assert kwargs["response_id"] == "r1"


def test_apply_skips_the_kernel_when_there_is_nothing_to_change():
    mind = _mind_with_mock_kernel()
    decision = ActionDecision(selected_action=ActionType.REMAIN_SILENT)
    assert mind.apply_cognitive_decision(decision) is False
    mind._kernel.guarded_transition.assert_not_called()


# ---------------------------------------------------------------------------
# 反映先の修正（2026-08-01 08:50）
#
# bridge の shape が plan_metrics で unverifiable に落ちないこと、
# social_mode が裸のラベルではなく指示としてプロンプトへ出ることを固定する。
# ---------------------------------------------------------------------------


def test_every_bridged_shape_is_measurable():
    """反映されたかどうかを事後に読めない shape を作らない。"""
    from neuro_voice.dialogue.plan_metrics import _SHAPE_CHECKS

    shapes = {changes["response_shape"] for changes in ACTION_TO_FRAME.values()}
    known = set(_SHAPE_CHECKS) | {
        # Kernel自身の語彙。goal の文面が指示の本体で、shape はラベル
        "answer_first", "resolve_then_answer",
        # 遊び心は意味であって事実ではない。規則で判定できるふりをしない
        # （第2条）。意図して unverifiable のまま
        "playful_twist",
    }
    assert shapes <= known, shapes - known


def test_bridged_social_modes_become_instructions_in_the_prompt():
    """supportive と書くだけではモデルに意図が届かない。"""
    from neuro_voice.dialogue.kernel import ConversationKernel

    kernel = ConversationKernel()
    frame = kernel.build(
        "しんどい", source="local", speaker_id="u", momentum=.5, conversation_id="u",
    )
    kernel.guarded_transition(
        {"social_mode": "supportive"}, source="local",
        transition_reason="cognitive_kernel:test",
    )
    text = kernel.prompt(kernel.find(source="local"))
    assert "social_mode指示" in text
    assert "受け止める" in text


def test_an_unknown_social_mode_adds_no_invented_instruction():
    from neuro_voice.dialogue.kernel import ConversationKernel

    kernel = ConversationKernel()
    kernel.build("やあ", source="local", speaker_id="u", momentum=.5, conversation_id="u")
    text = kernel.prompt(kernel.find(source="local"))
    assert "social_mode指示" not in text, "one_to_one 等の場のラベルに指示を捏造しない"


def test_urgent_warning_is_measured_as_short():
    from neuro_voice.dialogue.plan_metrics import evaluate

    class Plan:
        response_shape = "urgent_warning"
        primary_style = ""
        target_length = ""
        ask_follow_up = None
        minimum_moves = 0
        selected_features = ()

    short = evaluate(Plan(), "危ない、下がって！")
    long = evaluate(Plan(), "えっとね、これはですね。まず状況を説明すると。色々あって。つまり。要するに危ないかもしれません。たぶん。")
    def verdict(result):
        return next(c.verdict for c in result.checks if c.name == "response_shape")
    assert verdict(short) == "met"
    assert verdict(long) == "missed"
