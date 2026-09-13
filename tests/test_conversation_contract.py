from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_voice.dialogue import DialogueIntelligence
from neuro_voice.dialogue.conversation_contract import (
    ASK_QUESTION,
    DIRECT_ANSWER,
    HONEST_RECALL,
    SAY_NOT_REMEMBERED,
    ConversationContract,
    choose_conversation_move,
    enforce_reply,
    enforce_sentence,
    recall_requested,
)


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def contract(**overrides):
    values = {
        "response_id": "r1",
        "source": "local",
        "move": DIRECT_ANSWER,
        "allow_question": False,
        "recall_requested": False,
        "recall_grounded": False,
    }
    values.update(overrides)
    return ConversationContract(**values)


def test_question_turn_selects_a_direct_answer_not_an_automatic_follow_up():
    move = choose_conversation_move(
        intent="question", primary_style="explanatory", target_length="medium",
        ask_follow_up=True, recall_is_requested=False, recall_is_grounded=False,
    )
    assert move == DIRECT_ANSWER


def test_curiosity_question_is_an_explicit_move():
    move = choose_conversation_move(
        intent="statement", primary_style="curious", target_length="medium",
        ask_follow_up=True, recall_is_requested=False, recall_is_grounded=False,
    )
    assert move == ASK_QUESTION


def test_recall_move_depends_on_actual_evidence():
    assert recall_requested("前におすすめしたゲームって何？")
    assert choose_conversation_move(
        intent="question", primary_style="direct", target_length="medium",
        ask_follow_up=False, recall_is_requested=True, recall_is_grounded=True,
    ) == HONEST_RECALL
    assert choose_conversation_move(
        intent="question", primary_style="direct", target_length="medium",
        ask_follow_up=False, recall_is_requested=True, recall_is_grounded=False,
    ) == SAY_NOT_REMEMBERED


def test_unplanned_trailing_question_is_removed_from_canonical_reply():
    result = enforce_reply(
        "私はまず原因を切り分けるのがいいと思う。きみはどう思う？",
        contract(),
    )
    assert result.text == "私はまず原因を切り分けるのがいいと思う。"
    assert "question_not_in_plan" in result.reasons


def test_question_lead_chunk_is_not_spoken_when_questions_are_forbidden():
    result = enforce_sentence("ところで、", contract())
    assert result.text == ""
    assert result.reasons == ("dangling_question_lead",)


def test_unsupported_first_person_experience_is_replaced_not_remembered():
    result = enforce_sentence("私もその原作を読んだことがあるよ。", contract())
    assert result.text == "私はそれを実際に体験した記録はないよ。"
    assert result.reasons == ("unsupported_personal_experience",)


def test_present_perception_and_explicit_non_experience_are_preserved():
    current = enforce_sentence("私は今その画面を見ているよ。", contract())
    denied = enforce_sentence("私はその原作を読んだことはないよ。", contract())
    assert current.text == "私は今その画面を見ているよ。"
    assert denied.text == "私はその原作を読んだことはないよ。"


def test_unbacked_memory_claim_is_replaced_but_grounded_recall_is_kept():
    text = "前にその話をしたのを覚えてるよ。"
    missing = enforce_sentence(text, contract(recall_requested=True))
    grounded = enforce_sentence(
        text, contract(recall_requested=True, recall_grounded=True),
    )
    assert missing.text == "そのことを覚えていると言える確かな記録は見つからないよ。"
    assert grounded.text == text


def test_response_id_prevents_a_stale_contract_from_touching_a_new_turn():
    with TemporaryDirectory() as tmp:
        intelligence = DialogueIntelligence(_Config(), Path(tmp) / "dialogue.json")
        intelligence.observe_turn("alice", "どう思う？", now=100)
        intelligence.prompt_context(
            "alice", "どう思う？", response_id="old", source="local",
        )
        unchanged = intelligence.enforce_output_reply(
            "alice", "私はこう思う。次はどうする？",
            source="local",
            response_id="new",
        )
        assert unchanged == "私はこう思う。次はどうする？"


def test_experience_claim_split_across_tts_chunks_is_checked_as_one_sentence():
    with TemporaryDirectory() as tmp:
        intelligence = DialogueIntelligence(_Config(), Path(tmp) / "dialogue.json")
        intelligence.observe_turn("alice", "その原作を読んだ？", now=100)
        intelligence.prompt_context(
            "alice", "その原作を読んだ？", response_id="r-split", source="local",
        )
        first = intelligence.enforce_output_sentence(
            "alice", "私も昔、", source="local", response_id="r-split",
        )
        second = intelligence.enforce_output_sentence(
            "alice", "その原作を読んだことがあるよ。",
            source="local", response_id="r-split",
        )
        assert first == ""
        assert second == "私はそれを実際に体験した記録はないよ。"


def test_grounding_buffer_can_flush_a_final_fragment_without_punctuation():
    with TemporaryDirectory() as tmp:
        intelligence = DialogueIntelligence(_Config(), Path(tmp) / "dialogue.json")
        intelligence.observe_turn("alice", "どう思う？", now=100)
        intelligence.prompt_context(
            "alice", "どう思う？", response_id="r-flush", source="discord",
        )
        assert intelligence.enforce_output_sentence(
            "alice", "私はそう思う", source="discord", response_id="r-flush",
        ) == ""
        assert intelligence.flush_output_sentence(
            "alice", source="discord", response_id="r-flush",
        ) == "私はそう思う"
