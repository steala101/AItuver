from neuro_voice.dialogue.kernel import (
    ConversationKernel, DialogueObligation, ReferenceStatus,
)
from neuro_voice.dialogue.repair import (
    analyze_conversation_repair, normalize_repeated_reply_retry,
    repeated_reply_fallback, repeated_reply_retry_messages,
)
from neuro_voice.memory.conversation import (
    ConversationManager, InterruptedTurn,
)
from neuro_voice.stt.hallucination import is_known_hallucination
from neuro_voice.stt.transcript import Transcript


def test_explicit_meaning_correction_extracts_the_new_fact():
    repair = analyze_conversation_repair(
        "いや、あのー、またゲーム買ったらっていうのは、"
        "キープトーキングを買ったらっていう意味。"
    )
    assert repair.is_repair
    assert repair.supersedes_previous
    assert repair.subject == "またゲーム買ったら"
    assert repair.corrected_meaning == "キープトーキングを買ったら"
    assert "キープトーキングを買ったら" in repair.acknowledgement()


def test_right_hand_correction_is_not_treated_as_normal_chatter():
    repair = analyze_conversation_repair(
        "いや、どんなのって、キープトーキングだよ。"
    )
    assert repair.is_repair
    assert repair.corrected_meaning == "キープトーキング"


def test_kernel_makes_meaning_correction_authoritative_and_blocks_search():
    kernel = ConversationKernel()
    frame = kernel.build(
        "またゲーム買ったらっていうのは、キープトーキングを買ったらっていう意味。",
        source="local",
        speaker_id="local:mic",
    )
    assert frame.obligations[0] == DialogueObligation.REPAIR.value
    assert frame.response_shape == "repair_and_stop"
    assert frame.reference_resolution.status == ReferenceStatus.RESOLVED.value
    assert "キープトーキングを買ったら" in frame.reference_resolution.resolved_value
    assert not frame.search_decision.allowed
    prompt = kernel.prompt(frame)
    assert "authoritative_correction=" in prompt
    assert "Do not repeat or resume that answer" in prompt


def test_correction_discards_interrupted_topic_but_keeps_echo_evidence():
    conversation = ConversationManager("system")
    conversation.add_user("どんなゲーム？")
    interrupted_text = (
        "あはは、ごめん！私、また変な方向に突っ走っちゃったね。"
        "キープトーキングの話を続けようってことだ。"
    )
    conversation.hold_current_topic(InterruptedTurn(
        response_id="r1",
        original_user_text="どんなゲーム？",
        full_assistant_text=interrupted_text,
        played_text="あはは、ごめん！",
        unplayed_text="私、また変な方向に突っ走っちゃったね。",
        interrupted_at=1.0,
        interruption_text="いや、そういう意味じゃない",
    ))
    conversation.add_user(
        "いや、またゲーム買ったらっていうのは、"
        "キープトーキングを買ったらっていう意味。"
    )

    messages, resumed = conversation.messages_for_turn(
        "いや、またゲーム買ったらっていうのは、"
        "キープトーキングを買ったらっていう意味。"
    )

    assert not resumed
    assert conversation.deferred_topic is None
    assert not any("会話制御メモ" in item["content"] for item in messages)
    assert interrupted_text in conversation.recent_assistant_texts()


def test_repeated_reply_uses_correction_safe_fallback_not_search_failure():
    fallback = repeated_reply_fallback(
        "いや、またゲーム買ったらっていうのは、"
        "キープトーキングを買ったらっていう意味。"
    )
    assert "キープトーキングを買ったら" in fallback
    assert "検索" not in fallback


def test_ordinary_repeated_reply_does_not_read_a_diagnostic_apology():
    assert repeated_reply_fallback("ほんとそれ。") == ""


def test_repeated_reply_retry_has_a_real_silence_option():
    messages = [{"role": "user", "content": "ほんとそれ。"}]
    retry = repeated_reply_retry_messages(messages, "ほんとそれ。", "前と同じ返答。")
    assert retry[:-1] == messages
    assert "<NO_REPLY>" in retry[-1]["content"]
    assert normalize_repeated_reply_retry("<NO_REPLY>") == ""
    assert normalize_repeated_reply_retry("じゃあ、ここはいったん休憩だね。") == (
        "じゃあ、ここはいったん休憩だね。"
    )


def test_known_end_card_is_rejected_only_when_timing_or_confidence_is_impossible():
    confident = Transcript(
        text="ご視聴ありがとうございました。",
        avg_logprob=-0.2,
        no_speech_prob=0.01,
    )
    assert is_known_hallucination(confident, duration_ms=384)
    assert not is_known_hallucination(confident, duration_ms=2400)

    weak = Transcript(
        text="ご視聴ありがとうございました。",
        avg_logprob=-1.1,
        no_speech_prob=0.4,
    )
    assert is_known_hallucination(weak, duration_ms=2400)


def test_legitimate_short_phrases_are_never_blacklisted():
    assert not is_known_hallucination(
        Transcript(text="おやすみなさい", avg_logprob=-1.2, no_speech_prob=0.8),
        duration_ms=300,
    )
