"""行動を選んでから話す。**文章より先に「何をするか」が決まること。**

これまでの経路は、入力を受けたらすぐ「どう話すか」を決めていた。話す以外の
選択肢——黙る、聞き返す、警告する、中断した話を再開する——が構造として
無かったので、内部状態（感情・関係性・聞き取りの確かさ）は結局のところ
口調にしか影響していなかった。

ここで確かめるのは、**内部状態が行動の点数として効いているか**である。
口調が変わったかどうかではない。
"""
from __future__ import annotations

import pytest

from neuro_voice.cognition import (
    ActionOutcome, ActionType, CognitiveEvent, CognitiveKernel, EventType, build_state,
)


def kernel() -> CognitiveKernel:
    return CognitiveKernel()


def utterance(text: str = "どう思う？", confidence: float = 1.0) -> CognitiveEvent:
    return CognitiveEvent(
        event_type=EventType.USER_UTTERANCE, content=text, confidence=confidence,
    )


def state(**kwargs):
    return build_state(**kwargs)


# ---------------------------------------------------------------------------
# ケース1: 通常質問
# ---------------------------------------------------------------------------


def test_a_plain_question_is_answered():
    decision = kernel().decide(utterance(), state())
    assert decision.selected_action is ActionType.ANSWER
    assert decision.speaks, "話す行動なので Conversation Planner へ渡る"
    assert decision.state_snapshot_id, "どの状態で決めたかを残す"


def test_the_decision_records_what_it_rejected():
    """後から「なぜこの行動だったか」を読めること。長い思考は残さない。"""
    decision = kernel().decide(utterance(), state(relationship={"comfort": .8}))
    assert decision.decision_reason
    dumped = decision.snapshot()
    assert dumped["selected"] == "answer"
    assert "rejected" in dumped


# ---------------------------------------------------------------------------
# ケース2: 感情を含む相談
# ---------------------------------------------------------------------------


def test_distress_outranks_a_bare_answer():
    """「今の設計が全部間違っている気がして不安」——解決策だけを返さない。"""
    troubled = state(user_state={"distress": .8})
    decision = kernel().decide(utterance("全部間違ってる気がして不安"), troubled)
    assert decision.selected_action is ActionType.ACKNOWLEDGE_EMOTION


def test_without_distress_the_emotion_move_is_not_even_proposed():
    """常に共感を挟むのではなく、**信号が立った時だけ**候補にする。"""
    candidates = kernel().propose(utterance(), state())
    assert ActionType.ACKNOWLEDGE_EMOTION not in {c.action_type for c in candidates}


def test_a_joke_is_suppressed_while_the_user_is_distressed():
    troubled = state(user_state={"distress": .8}, relationship={"comfort": .9})
    candidates = kernel().propose(utterance("しんどい"), troubled)
    assert ActionType.MAKE_LIGHT_JOKE not in {c.action_type for c in candidates}


# ---------------------------------------------------------------------------
# ケース3: ASR信頼度が低い
# ---------------------------------------------------------------------------


def test_a_low_confidence_utterance_is_never_answered_outright():
    decision = kernel().decide(
        utterance("……ごにょごにょ", confidence=.3),
        state(uncertainty={"input": .3}),
    )
    assert decision.selected_action in {
        ActionType.ASK_CLARIFICATION, ActionType.REMAIN_SILENT,
    }


def test_answering_is_blocked_rather_than_merely_scored_low():
    """点数の綾で通ってしまわないよう、条件として塞ぐ。"""
    candidates = kernel().propose(
        utterance("？", confidence=.2), state(uncertainty={"input": .2}),
    )
    answer = next(c for c in candidates if c.action_type is ActionType.ANSWER)
    assert answer.blocked
    assert "input_confidence_too_low" in answer.blocking_conditions


# ---------------------------------------------------------------------------
# ケース4: AI発話中の割込み
# ---------------------------------------------------------------------------


def test_an_interruption_with_unfinished_speech_can_resume():
    event = CognitiveEvent(event_type=EventType.AI_INTERRUPTED)
    decision = kernel().decide(
        event, state(world_state={"interrupted_response": "さっきの続きだけど"}),
    )
    assert decision.selected_action is ActionType.RESUME_INTERRUPTED_RESPONSE


def test_an_interruption_with_nothing_pending_does_not_pretend_to_resume():
    event = CognitiveEvent(event_type=EventType.AI_INTERRUPTED)
    decision = kernel().decide(event, state())
    assert decision.selected_action is not ActionType.RESUME_INTERRUPTED_RESPONSE


def test_an_unfinished_utterance_becomes_an_obligation():
    """中断されたら義務が残る。次のターンで選べるようにするため。"""
    event = utterance()
    current = state()
    decision = kernel().decide(event, current)
    updates = CognitiveKernel.apply_outcome(
        decision,
        ActionOutcome(
            action_id=decision.action_id, status="interrupted", interrupted=True,
            observable_effects={"spoken": "途中まで話した"},
        ),
        current,
    )
    assert "finish_interrupted_response" in updates["obligations"]
    assert updates["interrupted_response"] == "途中まで話した"


def test_finishing_clears_the_obligation():
    current = state(obligations=("finish_interrupted_response",))
    decision = kernel().decide(utterance(), current)
    updates = CognitiveKernel.apply_outcome(
        decision, ActionOutcome(action_id=decision.action_id, status="completed"), current,
    )
    assert "finish_interrupted_response" not in updates["obligations"]
    assert updates["interrupted_response"] == ""


# ---------------------------------------------------------------------------
# ケース5: ゲーム内危険
# ---------------------------------------------------------------------------


def test_a_confident_danger_outranks_ordinary_conversation():
    event = CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.9)
    decision = kernel().decide(event, state())
    assert decision.selected_action is ActionType.WARN


def test_a_low_confidence_danger_does_not_hijack_the_turn():
    event = CognitiveEvent(event_type=EventType.GAME_DANGER, confidence=.2, content="たぶん敵")
    decision = kernel().decide(event, state())
    assert decision.selected_action is not ActionType.WARN


# ---------------------------------------------------------------------------
# ケース6: 関係性の緩やかな変化
# ---------------------------------------------------------------------------


def test_one_event_does_not_swing_the_relationship():
    current = state(relationship={"comfort": .5, "trust": .5})
    decision = kernel().decide(utterance("しんどい"), state(user_state={"distress": .8}))
    updates = CognitiveKernel.apply_outcome(
        decision, ActionOutcome(action_id=decision.action_id, status="completed"), current,
    )
    delta = updates.get("relationship_delta", {})
    assert all(abs(value) <= .05 for value in delta.values()), delta


def test_low_trust_penalises_challenging_the_user():
    low = kernel()._relationship_fit(ActionType.CHALLENGE_ASSUMPTION, state(
        relationship={"trust": .1}))
    high = kernel()._relationship_fit(ActionType.CHALLENGE_ASSUMPTION, state(
        relationship={"trust": .9}))
    assert low < 0 < high


def test_irritation_pushes_jokes_down():
    calm = kernel()._affect_fit(ActionType.MAKE_LIGHT_JOKE, state(affect={"irritation": .0}))
    annoyed = kernel()._affect_fit(ActionType.MAKE_LIGHT_JOKE, state(affect={"irritation": .8}))
    assert annoyed < calm


def test_repeated_questions_are_penalised():
    """質問の連投を、口調ではなく点数で抑える。"""
    fresh = state(self_state={"curiosity": .9})
    repeated = state(
        self_state={"curiosity": .9},
        recent_actions=("ask_clarification", "ask_clarification"),
    )
    a = kernel()._repetition_penalty(ActionType.ASK_CLARIFICATION, fresh)
    b = kernel()._repetition_penalty(ActionType.ASK_CLARIFICATION, repeated)
    assert b > a


# ---------------------------------------------------------------------------
# ケース7: LLM交換可能性
# ---------------------------------------------------------------------------


def test_the_kernel_never_imports_an_llm():
    """状態と選択はLLMの外にある。差し替えても関係性は残る。

    文章ではなく **import と呼び出し** を見る。説明文に「LLM」と書いてある
    こと自体は問題ではない。
    """
    import ast
    import inspect

    from neuro_voice.cognition import kernel as module

    tree = ast.parse(inspect.getsource(module))
    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any("llm" in name.lower() or "openai" in name.lower() for name in imported), imported


def test_the_state_survives_a_different_llm():
    """LLMの実体を差し替えても、同じ状態からは同じ行動が選ばれる。"""
    current = state(
        relationship={"trust": .8}, affect={"irritation": .1},
        user_state={"distress": .9},
    )
    first = kernel().decide(utterance("つらい"), current)
    second = CognitiveKernel().decide(utterance("つらい"), current)
    assert first.selected_action == second.selected_action


# ---------------------------------------------------------------------------
# 全体の約束
# ---------------------------------------------------------------------------


def test_candidates_stay_within_the_budget():
    """候補は2〜5個。並べるほど良くなるわけではない。"""
    busy = state(
        user_state={"distress": .8}, self_state={"curiosity": .9},
        relationship={"comfort": .9}, obligations=("前の話",),
    )
    assert 2 <= len(kernel().propose(utterance(), busy)) <= 5


def test_weights_live_in_exactly_one_place():
    from neuro_voice.cognition.kernel import WEIGHTS

    assert kernel().weights.keys() >= WEIGHTS.keys()
    assert CognitiveKernel({"safety": 9.0}).weights["safety"] == 9.0


@pytest.mark.parametrize("action", [
    ActionType.REMAIN_SILENT, ActionType.STORE_MEMORY,
    ActionType.ABANDON_INTERRUPTED_RESPONSE,
])
def test_silent_actions_do_not_reach_the_planner(action):
    from neuro_voice.cognition.types import ActionDecision

    assert not ActionDecision(selected_action=action).speaks


def test_the_event_log_keeps_no_conversation():
    """第12条。イベントの記録に本文を入れない。"""
    dumped = utterance("これは秘密の話").summary()
    assert "秘密" not in str(dumped)
    assert dumped["chars"] == len("これは秘密の話")


def test_a_conversation_event_can_be_adapted_without_a_new_entry_point():
    """既存の `ConversationEvent` を包むだけ。入口を二重に作らない。"""
    from neuro_voice.dialogue.events import ConversationEvent, ConversationEventType

    event = CognitiveEvent.from_conversation_event(ConversationEvent(
        event_type=ConversationEventType.SPEECH_FINAL, source="local", text="やあ",
    ))
    assert event.event_type == EventType.USER_UTTERANCE
    assert event.content == "やあ"
