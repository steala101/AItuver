"""Homophone damage control for speech recognition output.

The failure these tests protect against is specific: the sounds are right, the
kanji are wrong, and the assistant then reasons earnestly about a word nobody
said.  Repair happens in three places and each is checked separately.
"""
from __future__ import annotations

import pytest

from neuro_voice.stt.transcript import Transcript, WordConfidence, collect_words
from neuro_voice.stt.transcript_repair import (
    ContextVocabulary, TranscriptRepairer, asr_uncertainty_prompt, collapse_repetitions,
)


class FakeWord:
    def __init__(self, word, probability, start=0.0, end=0.1):
        self.word, self.probability, self.start, self.end = word, probability, start, end


class FakeSegment:
    def __init__(self, text, words=(), avg_logprob=-0.2, no_speech_prob=0.01,
                 compression_ratio=1.2):
        self.text = text
        self.words = list(words)
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio


def make_transcript(text, words, avg_logprob=-0.2):
    return Transcript(
        text=text, avg_logprob=avg_logprob,
        words=[WordConfidence(word=w, probability=p) for w, p in words],
    )


def repairer(**kwargs):
    return TranscriptRepairer(**kwargs)


# ---------------------------------------------------------------------------
# Reading a recognizer result
# ---------------------------------------------------------------------------


def test_segments_are_read_once_keeping_word_probability():
    segments = [
        FakeSegment("今日は", [FakeWord("今日", .98), FakeWord("は", .99)], avg_logprob=-0.3),
        FakeSegment("毎クラの話", [FakeWord("毎クラ", .31), FakeWord("の話", .95)], avg_logprob=-0.5),
    ]
    text, words, avg_logprob, no_speech, _compression = collect_words(segments)
    assert text == "今日は毎クラの話"
    assert len(words) == 4
    assert avg_logprob == pytest.approx(-0.4)
    assert no_speech == pytest.approx(0.01)


def test_a_backend_without_confidence_still_produces_a_transcript():
    plain = Transcript.plain("  こんばんは  ")
    assert plain.text == "こんばんは"
    assert str(plain) == "こんばんは"
    assert bool(plain) is True
    assert plain.reliable is True


def test_empty_transcript_is_falsy():
    assert not Transcript.plain("   ")


# ---------------------------------------------------------------------------
# Context vocabulary and decode-time bias
# ---------------------------------------------------------------------------


def test_vocabulary_prefers_names_and_the_current_activity():
    vocabulary = repairer().build_vocabulary(
        proper_nouns=["ポチ"],
        activity_terms=["マインクラフト"],
        topics=["レッドストーン回路"],
        interests=["音の出る仕組み"],
    )
    assert vocabulary.terms[0] == "ポチ"
    assert vocabulary.terms[1] == "マインクラフト"
    assert "レッドストーン回路" in vocabulary.terms


def test_vocabulary_drops_filler_and_stopwords():
    vocabulary = repairer().build_vocabulary(
        recent_turns=["今日はなんかその、こととかを話した感じ"],
    )
    for junk in ("なんか", "こと", "感じ", "今日"):
        assert junk not in vocabulary.terms


def test_vocabulary_is_bounded():
    vocabulary = repairer(max_terms=5).build_vocabulary(
        topics=[f"話題{i}" for i in range(40)],
    )
    assert len(vocabulary.terms) == 5


def test_initial_prompt_does_not_carry_context_by_default():
    """Whisper emits initial_prompt content, so nothing extra goes in it.

    Regression: injecting "今の会話に出ている言葉: ポッポ、チビ、…" produced
    transcripts like 「さあ、ib ポッポ、ポッポ、チビをカリメと言うと」 — the
    biasing words were spoken back into the text.
    """
    vocabulary = repairer().build_vocabulary(proper_nouns=["ポチ", "マインクラフト"])
    assert vocabulary.initial_prompt("日本語の自然な会話です。") == "日本語の自然な会話です。"


def test_initial_prompt_can_carry_context_when_explicitly_asked():
    vocabulary = repairer().build_vocabulary(proper_nouns=["ポチ", "マインクラフト"])
    prompt = vocabulary.initial_prompt("日本語の自然な会話です。", include_terms=True)
    assert prompt.startswith("日本語の自然な会話です。")
    assert "ポチ" in prompt


def test_initial_prompt_is_unchanged_without_context():
    empty = ContextVocabulary()
    assert empty.initial_prompt("既定のプロンプト") == "既定のプロンプト"
    assert empty.initial_prompt("") == ""


def test_only_verified_proper_nouns_can_steer_the_recognizer():
    """Regression: a previous mis-recognition must not bias the next decode.

    Feeding earlier transcripts back into hotwords made errors self-reinforcing
    — 「ポッポは自分の声聞こえる」 became a biasing phrase and came back.
    """
    vocabulary = repairer().build_vocabulary(
        proper_nouns=["ポッポ"],
        activity_terms=["マインクラフト"],
        recent_turns=["ところで、ポッポは自分の声聞こえると。", "ウィスパーがまた誤認識してる"],
        memory_terms=["エクセルの関数を勉強していた"],
        topics=["レッドストーン回路"],
    )
    assert vocabulary.bias_terms == ["ポッポ", "マインクラフト"]
    for leaked in ("ウィスパー", "声聞", "レッドストーン回路"):
        assert leaked not in vocabulary.bias_terms
    # They remain available for post-hoc reading repair, which cannot mis-decode.
    assert "ウィスパー" in vocabulary.repair_terms


def test_bias_list_is_hard_capped():
    """A long hotword list makes Whisper insert those words into other speech."""
    vocabulary = repairer(max_bias_terms=3).build_vocabulary(
        proper_nouns=[f"名前{i}" for i in range(20)],
    )
    assert len(vocabulary.bias_terms) == 3
    assert len(vocabulary.hotwords("").split()) == 3


def test_japanese_is_split_into_words_not_clauses():
    """Regression: the tokenizer swallowed whole clauses as single 'terms'."""
    vocabulary = repairer().build_vocabulary(
        recent_turns=["ところで、ポッポは自分の声聞こえると。"],
    )
    for clause in ("ポッポは自分の声聞こえる", "多分別の意味でそういう風"):
        assert clause not in vocabulary.terms
    assert all(len(term) <= 16 for term in vocabulary.terms)


def test_hotwords_merge_config_and_context_without_duplicates():
    vocabulary = repairer().build_vocabulary(proper_nouns=["ポチ", "マインクラフト"])
    hotwords = vocabulary.hotwords("ポチ ぽち").split()
    assert hotwords.count("ポチ") == 1
    assert "マインクラフト" in hotwords


def test_appended_context_hotword_list_is_removed_from_question():
    fixer = repairer()
    vocabulary = fixer.build_vocabulary(
        proper_nouns=["ポッポ", "チビ", "ゲスト4"],
        activity_terms=["日本語しりとり"],
    )
    transcript = make_transcript(
        "今は持ってるアイテムで焚き火を作れる？ポッポ チビ ゲスト4 日本語しりとり",
        [],
    )
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "今は持ってるアイテムで焚き火を作れる？"
    assert result.repairs[-1].reason == "context_bias_echo_removed"


def test_two_context_names_in_normal_sentence_are_not_removed():
    fixer = repairer()
    vocabulary = fixer.build_vocabulary(
        proper_nouns=["ポッポ", "チビ"],
    )
    transcript = make_transcript("聞こえる？ポッポ チビ", [])
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "聞こえる？ポッポ チビ"
    assert not any(
        note.reason == "context_bias_echo_removed"
        for note in result.repairs
    )


# ---------------------------------------------------------------------------
# Marking uncertainty
# ---------------------------------------------------------------------------


def test_low_confidence_kanji_word_is_marked_uncertain():
    transcript = make_transcript("毎クラの話", [("毎クラ", .31), ("の話", .95)])
    result = repairer().repair(transcript, None)
    assert "毎クラ" in result.uncertain_surfaces


def test_confident_words_are_never_marked():
    transcript = make_transcript("マインクラフトの話", [("マインクラフト", .97), ("の話", .95)])
    result = repairer().repair(transcript, None)
    assert result.uncertain_surfaces == []
    assert result.reliable is True


def test_low_confidence_filler_is_not_marked():
    """A weak score on a particle is not a meaning-breaking substitution."""
    transcript = make_transcript("えっとですね", [("えっと", .22), ("ですね", .30)])
    result = repairer().repair(transcript, None)
    assert result.uncertain_surfaces == []


def test_a_weak_utterance_tightens_the_threshold():
    words = [("水曜", .66), ("の予定", .95)]
    normal = repairer().repair(make_transcript("水曜の予定", words, -0.2), None)
    weak = repairer().repair(make_transcript("水曜の予定", words, -1.1), None)
    assert normal.uncertain_surfaces == []
    assert "水曜" in weak.uncertain_surfaces


# ---------------------------------------------------------------------------
# Silent repair
# ---------------------------------------------------------------------------


def test_katakana_homophone_is_repaired_from_context():
    """「マイクラ」の話中に「まいくら」と書き起こされても直る。"""
    fixer = repairer()
    vocabulary = fixer.build_vocabulary(activity_terms=["マイクラ"])
    transcript = make_transcript("まいくらの話", [("まいくら", .35), ("の話", .95)])
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "マイクラの話"
    assert result.repairs[0].original == "まいくら"
    assert result.repairs[0].corrected == "マイクラ"
    assert result.repairs[0].reason == "context_reading_match"
    # Once repaired it is no longer flagged as unreliable.
    assert result.uncertain_surfaces == []


def test_a_confident_word_is_never_silently_rewritten():
    fixer = repairer()
    vocabulary = fixer.build_vocabulary(activity_terms=["マイクラ"])
    transcript = make_transcript("まいくらの話", [("まいくら", .99), ("の話", .95)])
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "まいくらの話"
    assert result.repairs == []


def test_no_repair_without_context():
    fixer = repairer()
    transcript = make_transcript("まいくらの話", [("まいくら", .30), ("の話", .95)])
    result = fixer.repair(transcript, fixer.build_vocabulary())
    assert result.text == "まいくらの話"
    assert result.repairs == []


def test_ambiguous_reading_is_offered_not_guessed():
    """Two context words share a reading: guessing would just move the error."""
    fixer = repairer()
    vocabulary = ContextVocabulary()
    vocabulary.repair_terms = ["交渉", "考証"]
    # Use the repairer's own normalization so the key matches what it computes.
    vocabulary._by_reading = {fixer._reading_of("こうしょう"): ["交渉", "考証"]}
    transcript = make_transcript("こうしょうの話", [("こうしょう", .30), ("の話", .95)])
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "こうしょうの話"          # unchanged
    assert result.candidates["こうしょう"] == ["交渉", "考証"]


def test_short_hiragana_at_low_confidence_is_treated_as_filler():
    """「かき」程度の短いひらがなを content word 扱いすると誤爆する。"""
    transcript = make_transcript("かきの話", [("かき", .30), ("の話", .95)])
    assert repairer().repair(transcript, None).uncertain_surfaces == []


def test_auto_repair_can_be_disabled_and_still_offers_candidates():
    fixer = repairer(auto_repair=False)
    vocabulary = fixer.build_vocabulary(activity_terms=["マイクラ"])
    transcript = make_transcript("まいくらの話", [("まいくら", .30), ("の話", .95)])
    result = fixer.repair(transcript, vocabulary)
    assert result.text == "まいくらの話"
    assert result.candidates["まいくら"] == ["マイクラ"]


def test_repair_leaves_an_empty_transcript_alone():
    fixer = repairer()
    transcript = Transcript(text="")
    assert fixer.repair(transcript, fixer.build_vocabulary(topics=["何か"])).text == ""


# ---------------------------------------------------------------------------
# Decoder stutter
# ---------------------------------------------------------------------------


def test_observed_stutter_is_collapsed():
    """Both of these reached the chat window verbatim."""
    text, count = collapse_repetitions("そうだね。最初は、最初は、最初は、VLOOKUPの使い方に苦戦したよ。")
    assert text == "そうだね。最初は、VLOOKUPの使い方に苦戦したよ。"
    assert count >= 1

    text, count = collapse_repetitions(
        "ああ、仕事で 仕事ではよく使って、仕事ではよく使ってるし、VLOOKUPも結構使うよ。"
    )
    assert "仕事ではよく使って、仕事ではよく使って" not in text
    assert count >= 1


def test_a_single_deliberate_repeat_is_left_alone():
    assert collapse_repetitions("はい、わかりました") == ("はい、わかりました", 0)
    assert collapse_repetitions("そうそう、それだよ")[1] == 0


def test_normal_speech_is_never_rewritten():
    for text in (
        "今日はVLOOKUPの使い方を教えてほしい",
        "エクセルで条件を設定するところが難しい",
        "",
    ):
        assert collapse_repetitions(text) == (text, 0)


def test_repair_records_the_collapse_as_an_auditable_note():
    transcript = Transcript(text="最初は、最初は、最初は、苦戦したよ")
    result = repairer().repair(transcript, None)
    assert result.text == "最初は、苦戦したよ"
    assert result.repairs[0].reason == "decoder_stutter_collapsed"


def test_collapsing_can_be_disabled():
    transcript = Transcript(text="最初は、最初は、最初は、苦戦したよ")
    result = repairer(collapse_stutter=False).repair(transcript, None)
    assert result.text == "最初は、最初は、最初は、苦戦したよ"


# ---------------------------------------------------------------------------
# Prompt guidance
# ---------------------------------------------------------------------------


def test_no_note_when_recognition_was_clean():
    transcript = make_transcript("マインクラフトの話", [("マインクラフト", .97)])
    assert asr_uncertainty_prompt(repairer().repair(transcript, None)) is None


def test_note_tells_the_model_to_reread_by_sound_not_kanji():
    transcript = make_transcript("毎クラの話", [("毎クラ", .31), ("の話", .95)])
    note = asr_uncertainty_prompt(repairer().repair(transcript, None))
    assert note is not None
    assert "毎クラ" in note
    assert "漢字の意味で解釈しない" in note
    assert "読み替え" in note


def test_note_forbids_talking_about_the_recognition_itself():
    transcript = make_transcript("毎クラの話", [("毎クラ", .31)])
    note = asr_uncertainty_prompt(repairer().repair(transcript, None))
    assert "メタ発言はしない" in note


def test_note_asks_back_only_as_a_last_resort():
    transcript = make_transcript("毎クラの話", [("毎クラ", .31)])
    repaired = repairer().repair(transcript, None)
    asking = asr_uncertainty_prompt(repaired, ask_when_stuck=True)
    assert "毎回は確認しない" in asking
    quiet = asr_uncertainty_prompt(repaired, ask_when_stuck=False)
    assert "聞き返さず" in quiet


def test_note_includes_offered_candidates():
    transcript = make_transcript("かきの話", [("かき", .30)])
    transcript.candidates = {"かき": ["カキ", "牡蠣"]}
    note = asr_uncertainty_prompt(transcript)
    assert "カキ" in note and "牡蠣" in note


def test_a_globally_weak_utterance_is_flagged_even_without_word_scores():
    transcript = Transcript(text="よく聞こえない発話", avg_logprob=-1.4)
    note = asr_uncertainty_prompt(transcript)
    assert note is not None
    assert "認識確度が低い" in note


def test_no_note_for_an_empty_transcript():
    assert asr_uncertainty_prompt(Transcript.plain("")) is None
    assert asr_uncertainty_prompt(None) is None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_snapshot_hides_the_transcript_text_by_default():
    fixer = repairer()
    vocabulary = fixer.build_vocabulary(activity_terms=["マイクラ"])
    result = fixer.repair(
        make_transcript("まいくらの話", [("まいくら", .30), ("の話", .95)]), vocabulary,
    )
    snapshot = result.snapshot()
    assert "text" not in snapshot
    assert snapshot["repairs"][0]["corrected"] == "マイクラ"
    assert snapshot["reliable"] is True
    assert result.snapshot(include_text=True)["text"] == "マイクラの話"
