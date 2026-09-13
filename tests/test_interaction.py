import asyncio
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from neuro_voice.memory.conversation import ConversationManager, InterruptedTurn
from neuro_voice.memory.interaction import (
    InteractionClassifier,
    ResponseAcknowledgementPlanner,
    SpeechIntent,
)
from neuro_voice.tts.style import StyleManager
from neuro_voice.tts.pronunciation import (
    apply_pronunciations, extract_explicit_pronunciation_correction, infer_pronunciation_correction, normalize_pronunciations,
)
from neuro_voice.music_recognition import (
    capture_output_audio, parse_humming_recognition_request, parse_music_recognition_request,
)
from neuro_voice.memory.persona import build_system_prompt, epistemic_turn_prompt
from neuro_voice.utils.emotion import EmotionTagParser
from neuro_voice.emotion.recognizer import OpenSmileSpeechBrainRecognizer
from neuro_voice.search.deepsearch import (
    DeepSearch, conversation_repair_context, is_contextual_clarification,
    build_query, parse_search_plan, should_search,
)
from neuro_voice.discord_bridge.music import (
    extract_music_reference_candidate,
    is_music_reference,
    now_playing_context,
    parse_music_command,
    is_current_track_discussion,
    plan_music_intent,
    should_execute_music_command,
)
from neuro_voice.mind.speakers import SpeakerRegistry
from neuro_voice.mind.mind import Mind
from neuro_voice.dialogue import DialogueIntelligence
from neuro_voice.dialogue.addressing import AddressingAction, AddresseeDetector
from neuro_voice.dialogue.events import ConversationEvent, ConversationEventType
from neuro_voice.dialogue.orchestrator import ConversationOrchestrator
from neuro_voice.dialogue.temporal import TimeAwareness
from neuro_voice.realtime import TurnManager, TurnState, response_can_play, response_is_active
from neuro_voice.audio.loopback import _find_loopback_microphone
from neuro_voice.discord_bridge.audio import UtteranceSegmenter
from neuro_voice.discord_bridge.bot import DiscordBridge
from neuro_voice.discord_bridge.direct_receiver import (
    DirectDAVEReceiver,
    DirectDiscordSpeaker,
    decode_pcm_packet,
    reserve_loopback_port,
)
from neuro_voice.dialogue.curiosity import CuriosityEngine
from neuro_voice.dialogue.user_model import UserModel
from neuro_voice.dialogue.acts import DialogueActPlanner
from neuro_voice.dialogue.response_director import ResponseDirector
from neuro_voice.dialogue.memory_policy import MemoryRecallPolicy
from neuro_voice.dialogue.initiative import InitiativePolicy
from neuro_voice.dialogue.state import ConversationState
from neuro_voice.realtime.playback_tracker import PlayedTextTracker
from neuro_voice.utils.latency import LatencyWindow, TurnMetrics
from neuro_voice.utils.textseg import SentenceSegmenter
from neuro_voice.media import MediaInput, MediaType
from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient
try:
    from neuro_voice.llm.openai_compat import OpenAICompatBackend
except ModuleNotFoundError:  # Minimal test runtime omits optional OpenAI SDK.
    OpenAICompatBackend = None
from neuro_voice.vision.media import hash_distance, prepare_image
from neuro_voice.vision.models import VisualObservation
from neuro_voice.vision.service import PerceptionService, is_direct_vision_turn, video_instruction
from neuro_voice.vision.video import VideoObservationService
from neuro_voice.games import MinecraftKnowledgeBase
from neuro_voice.stt.audio_understanding import AudioUnderstandingResult, Gemma4AudioBackend


class _ConversationConfig:
    def __init__(self):
        self._values = {
            "discord.respond_all": False,
            "discord.follow_up_s": 15.0,
            "conversation.addressing.respond_threshold": 0.80,
            "conversation.addressing.brief_threshold": 0.55,
            "conversation.addressing.observe_threshold": 0.35,
            "conversation.response_length": "short",
            "conversation.response_depth": 1,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


class ConversationAddressingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _ConversationConfig()
        self.orchestrator = ConversationOrchestrator(
            self.cfg, wake_words=["ポッポ"], assistant_name="ポッポ",
        )

    def _speech(self, text, *, key="transport:a", name="A", at=100, voice_human_count=None):
        metadata = {"speaker_key": key, "speaker_name": name}
        if voice_human_count is not None:
            metadata["voice_human_count"] = voice_human_count
        return self.orchestrator.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL, "discord", text=text, timestamp=at,
            metadata=metadata,
        ))

    def test_explicit_call_responds(self):
        result = self._speech("ポッポ、今日の天気は？")
        self.assertEqual(result.addressing.decision, AddressingAction.RESPOND)
        self.assertTrue(result.addressing.explicit_wake_word)
        self.assertTrue(result.response_plan.should_respond)

    def test_answer_to_assistant_question_responds_without_wake_word(self):
        self.orchestrator.response_started(source="discord", expected_response_from="transport:a",
                                           asked_question=True)
        self.orchestrator.response_finished(source="discord", expected_response_from="transport:a",
                                            asked_question=True)
        result = self._speech("精度が高い方かな", at=101)
        self.assertEqual(result.addressing.decision, AddressingAction.RESPOND)
        self.assertIn("assistant_asked_previous_question", result.addressing.reasons)

    def test_name_mention_is_not_an_invocation(self):
        result = self._speech("ポッポが昨日言ってたやつ、覚えてる？")
        self.assertNotEqual(result.addressing.decision, AddressingAction.RESPOND)
        self.assertFalse(result.addressing.explicit_wake_word)

    def test_human_question_does_not_make_assistant_interrupt(self):
        self.orchestrator.state.participants["transport:b"] = "B"
        result = self._speech("B、今日どうだった？", at=102)
        self.assertFalse(result.response_plan.should_respond)
        self.assertIn("another_participant_is_addressed", result.addressing.reasons)

    def test_question_about_assistant_is_not_suppressed_by_a_human_previous_turn(self):
        self._speech("少し待って", key="transport:b", name="B", at=102)
        result = self._speech("AI、今何しようとした？", key="transport:a", name="A", at=103)
        self.assertTrue(result.response_plan.should_respond)
        self.assertIn("assistant_related_question", result.addressing.reasons)

    def test_respond_all_setting_is_applied_without_recreating_the_orchestrator(self):
        self.orchestrator.configure_addressing(respond_all=True)
        result = self._speech("普通の発話", at=104)
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.reasons, ("respond_all_enabled",))

    def test_direct_expression_is_addressed_when_context_is_strong(self):
        self.orchestrator.response_finished(source="discord", expected_response_from="transport:a",
                                            asked_question=True)
        result = self._speech("これ、ポッポならどう思う？", at=103)
        self.assertTrue(result.response_plan.should_respond)

    def test_event_identity_keeps_voiceprint_and_transport_separate(self):
        event = ConversationEvent(
            ConversationEventType.SPEECH_FINAL, "discord", text="聞こえる？",
            speaker_id="voiceprint:8", metadata={"speaker_key": "transport:123"},
        )
        result = self.orchestrator.on_speech_final(event)
        self.assertEqual(result.state["current_speaker"], "transport:123")

    def test_same_input_stream_continues_a_recent_assistant_turn(self):
        self.orchestrator.response_started(
            source="discord", expected_response_from="transport:a", asked_question=True,
        )
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=True,
        )
        result = self._speech("ああ、今届いた", at=101)
        self.assertTrue(result.response_plan.should_respond)
        self.assertIn("same_input_stream_after_assistant", result.addressing.reasons)

    def test_poppo_stt_variant_is_an_explicit_assistant_call(self):
        result = self._speech("ポチ、聞こえる？", at=105)
        self.assertEqual(result.addressing.decision, AddressingAction.RESPOND)
        self.assertTrue(result.addressing.explicit_wake_word)

    def test_partial_explicit_call_survives_a_final_stt_miss(self):
        result = self.orchestrator.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL, "discord", text="聞こえる？", timestamp=105,
            metadata={"speaker_key": "transport:a", "partial_explicit_call": True},
        ))
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.reasons, ("partial_explicit_assistant_call",))

    def test_unaddressed_single_speaker_question_is_not_assumed_to_be_for_ai(self):
        result = self._speech("もしもし、聞こえる？", at=106)
        self.assertFalse(result.response_plan.should_respond)

    def test_same_stream_continues_after_immediate_follow_up_window(self):
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=True,
        )
        result = self._speech(
            "さっきの続きなんだけど",
            at=self.orchestrator.state.last_assistant_at + 20,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertIn("same_input_stream_continuation", result.addressing.reasons)

    def test_group_speech_in_recent_flow_gets_a_response(self):
        # 3人会話でも、AIが直近まで話していた輪の中の発話には名前なしで反応する
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "来月から始まるゲームが気になる", at=self.orchestrator.state.last_assistant_at + 1,
            voice_human_count=2,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertIn("group_conversation_flow", result.addressing.reasons)

    def test_group_speech_directed_at_another_human_stays_silent(self):
        # 流れの中でも、別の人へ名指しされた発話には割り込まない
        self.orchestrator.state.participants["transport:b"] = "B"
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "B、これどう思う？", at=self.orchestrator.state.last_assistant_at + 1,
            voice_human_count=3,
        )
        self.assertFalse(result.response_plan.should_respond)
        self.assertIn("another_participant_is_addressed", result.addressing.reasons)

    def test_group_flow_expires_outside_follow_up_window(self):
        # AIがしばらく話していなければ、雑談へ勝手に割り込まない
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "来月から始まるゲームが気になる",
            at=self.orchestrator.state.last_assistant_at + 300,
            voice_human_count=2,
        )
        self.assertFalse(result.response_plan.should_respond)

    def test_single_human_voice_channel_is_a_natural_dialogue(self):
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "なんか最近、面白いゲームが増えたよね",
            at=self.orchestrator.state.last_assistant_at + 1,
            voice_human_count=1,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.reasons, ("single_human_conversation",))

    def test_single_human_voice_channel_does_not_need_a_wake_word_to_start(self):
        result = self._speech("今日は何してた？", at=107, voice_human_count=1)
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.reasons, ("single_human_conversation",))

    def test_single_human_question_continues_an_active_exchange(self):
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "それ、どう思う？", at=self.orchestrator.state.last_assistant_at + 1,
            voice_human_count=1,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.reasons, ("single_human_conversation",))

    def test_group_question_in_flow_gets_a_full_response(self):
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech(
            "このゲームどう思う？", at=self.orchestrator.state.last_assistant_at + 1,
            voice_human_count=2,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertEqual(result.addressing.decision, AddressingAction.RESPOND)
        result = self._speech(
            "それ、どう思う？", at=self.orchestrator.state.last_assistant_at + 2,
            voice_human_count=2,
        )
        self.assertTrue(result.response_plan.should_respond)
        self.assertIn("group_topic_follow_up_after_assistant", result.addressing.reasons)

    def test_group_aizuchi_fires_on_ambiguous_speech(self):
        from unittest.mock import patch

        from neuro_voice.dialogue.addressing import AddressingDecision
        from neuro_voice.dialogue.planner import ResponsePlanner, ResponseRole
        from neuro_voice.dialogue.state import ConversationState

        planner = ResponsePlanner(self.cfg)
        state = ConversationState()
        state.voice_human_count = 3
        decision = AddressingDecision(0.4, AddressingAction.OBSERVE, ("ambiguous",), ambiguous=True)
        with patch("neuro_voice.dialogue.planner.random.random", return_value=0.0):
            plan = planner.plan("たしかにそれ面白いかもね", decision, state)
        self.assertTrue(plan.should_respond)
        self.assertEqual(plan.response_role, ResponseRole.ACKNOWLEDGEMENT)
        with patch("neuro_voice.dialogue.planner.random.random", return_value=0.99):
            plan = planner.plan("たしかにそれ面白いかもね", decision, state)
        self.assertFalse(plan.should_respond)

    def test_punctuated_acknowledgement_does_not_start_another_turn(self):
        self.orchestrator.response_finished(
            source="discord", expected_response_from="transport:a", asked_question=False,
        )
        result = self._speech("はーい。", at=self.orchestrator.state.last_assistant_at + 1)
        self.assertFalse(result.response_plan.should_respond)
        self.assertIn("short_acknowledgement", result.addressing.reasons)

    def test_acknowledgement_after_an_answer_does_not_start_another_explanation(self):
        self.orchestrator.response_started(source="discord", expected_response_from="transport:a")
        self.orchestrator.response_finished(source="discord", expected_response_from="transport:a")
        result = self._speech("なるほどね", at=104)
        self.assertFalse(result.response_plan.should_respond)
        self.assertEqual(result.response_plan.reason, "exchange_naturally_closed")


class MemoryRecallPolicyTests(unittest.TestCase):
    def test_retrieval_can_be_internal_without_spoken_mention(self):
        policy = MemoryRecallPolicy(mention_score=0.90)
        result = policy.rank([{
            "id": 1, "text": "Whisperは日本語固定", "score": 0.82,
            "importance": 4, "created_at": 100.0, "access_count": 2,
        }], now=200.0)
        self.assertEqual(result[0].record["id"], 1)
        self.assertFalse(result[0].mention_allowed)

    def test_high_value_recall_can_be_mentioned_naturally(self):
        policy = MemoryRecallPolicy(mention_score=0.88)
        result = policy.rank([{
            "id": 2, "text": "AIアシスタントを開発している", "score": 0.99,
            "importance": 5, "created_at": 100.0, "access_count": 8,
        }], now=101.0)
        self.assertTrue(result[0].mention_allowed)

    def test_current_correction_rejects_conflicting_old_memory(self):
        policy = MemoryRecallPolicy(mention_score=0.88)
        selected, rejected = policy.evaluate([{
            "id": 3,
            "text": "コーヒーを毎日飲むのが好き",
            "score": 0.99,
            "importance": 5,
            "created_at": 100.0,
        }], current_text="今はコーヒーを飲まない", now=101.0)
        self.assertEqual(selected, [])
        self.assertEqual(rejected[0].record["id"], 3)
        self.assertEqual(
            rejected[0].reason, "CURRENT_INPUT_CONTRADICTION",
        )


class InitiativePolicyTests(unittest.TestCase):
    def _policy(self):
        cfg = _ConversationConfig()
        cfg._values.update({
            "proactive.enabled": True,
            "proactive.quiet_s": 10.0,
            "proactive.min_interval_s": 30.0,
            "proactive.min_relevance": 0.70,
        })
        return InitiativePolicy(cfg)

    def test_group_silence_without_a_pending_point_is_not_filled(self):
        state = ConversationState(active_topic="game discussion")
        decision = self._policy().decide(
            state, last_user_at=80, last_proactive_at=0, participant_count=2, now=100,
        )
        self.assertFalse(decision.should_speak)
        self.assertEqual(decision.reason, "group_conversation_may_continue")

    def test_pending_question_can_receive_one_proactive_followup(self):
        state = ConversationState(active_topic="game discussion", unresolved_questions=["how to proceed"])
        decision = self._policy().decide(
            state, last_user_at=80, last_proactive_at=0, participant_count=2, now=100,
        )
        self.assertTrue(decision.should_speak)
        self.assertEqual(decision.reason, "unresolved_topic")

    def test_recent_speech_and_cooldown_block_proactive_turns(self):
        state = ConversationState(active_topic="game discussion", unresolved_questions=["question"])
        recent = self._policy().decide(
            state, last_user_at=95, last_proactive_at=0, participant_count=1, now=100,
        )
        cooldown = self._policy().decide(
            state, last_user_at=80, last_proactive_at=90, participant_count=1, now=100,
        )
        self.assertEqual(recent.reason, "recent_human_speech")
        self.assertEqual(cooldown.reason, "cooldown")

    def test_policy_can_be_enabled_from_the_settings_screen_at_runtime(self):
        cfg = _ConversationConfig()
        policy = InitiativePolicy(cfg)
        state = ConversationState(active_topic="game discussion", unresolved_questions=["question"])
        self.assertEqual(
            policy.decide(state, last_user_at=80, last_proactive_at=0, participant_count=1, now=100).reason,
            "disabled",
        )
        policy.configure(enabled=True, min_interval_s=5)
        self.assertTrue(
            policy.decide(state, last_user_at=80, last_proactive_at=0, participant_count=1, now=100).should_speak
        )


class InteractionClassifierTests(unittest.TestCase):
    def setUp(self):
        self.classifier = InteractionClassifier()

    def test_short_acknowledgements_do_not_interrupt(self):
        for text in ("うん", "はい", "なるほど", "そうですね"):
            with self.subTest(text=text):
                result = self.classifier.classify(text, duration_ms=500)
                self.assertEqual(result.intent, SpeechIntent.ACKNOWLEDGEMENT)

    def test_empty_and_brief_filler_are_false_positives(self):
        self.assertEqual(
            self.classifier.classify("  ", duration_ms=100).intent,
            SpeechIntent.FALSE_POSITIVE,
        )
        self.assertEqual(
            self.classifier.classify("えー", duration_ms=400).intent,
            SpeechIntent.FALSE_POSITIVE,
        )

    def test_substantive_utterance_interrupts(self):
        result = self.classifier.classify("それについて質問してもいい？", duration_ms=1400)
        self.assertEqual(result.intent, SpeechIntent.INTERRUPTION)

class DeferredTopicTests(unittest.TestCase):
    def test_topic_is_kept_for_a_side_question_then_resumed(self):
        conv = ConversationManager("system", max_turns=4)
        conv.add_user("Pythonの非同期処理を説明して")
        conv.add_assistant("まずイベントループについて説明します")
        deferred = conv.hold_current_topic()
        self.assertIsNotNone(deferred)

        conv.add_user("ところでスレッドとの違いは？")
        messages, resumes = conv.messages_for_turn("ところでスレッドとの違いは？")
        self.assertFalse(resumes)
        self.assertIn("今回のユーザー発話を最優先", messages[1]["content"])
        self.assertIn("Pythonの非同期処理", messages[1]["content"])

        _, resumes = conv.messages_for_turn("さっきの続きも聞かせて")
        self.assertTrue(resumes)
        conv.complete_deferred_topic()
        self.assertIsNone(conv.deferred_topic)


class SpeechStyleTests(unittest.TestCase):
    def test_llm_directives_are_removed_and_reported(self):
        emotions, deliveries = [], []
        parser = EmotionTagParser(emotions.append, deliveries.append)
        self.assertEqual(parser.feed("[emotion: joy]"), "")
        self.assertEqual(parser.feed("[style: lively]こんにちは。"), "こんにちは。")
        self.assertEqual(emotions, ["joy"])
        self.assertEqual(deliveries, ["lively"])

    def test_split_inline_emotion_tag_never_leaks_to_ui_or_tts(self):
        emotions = []
        parser = EmotionTagParser(emotions.append)
        output = "".join([
            parser.feed("なに、そんなに驚いた？ "),
            parser.feed("["), parser.feed("fun"), parser.feed("]"),
            parser.feed(" それとも面白いこと見つけた？"), parser.flush(),
        ])
        self.assertEqual(output, "なに、そんなに驚いた？  それとも面白いこと見つけた？")
        self.assertEqual(emotions, ["fun"])

    def test_split_style_directive_updates_style_after_plain_text(self):
        styles = []
        parser = EmotionTagParser(None, styles.append)
        output = parser.feed("前半。 [style:") + parser.feed(" playful]") + parser.feed("後半。")
        self.assertEqual(output, "前半。 後半。")
        self.assertEqual(styles, ["playful"])

    def test_thought_directive_never_reaches_ui_or_tts(self):
        parser = EmotionTagParser()
        output = parser.feed("[thought] なんだか寂しい返事だね。") + parser.flush()
        self.assertEqual(output, " なんだか寂しい返事だね。")

    def test_split_thought_directive_never_reaches_ui_or_tts(self):
        parser = EmotionTagParser()
        output = "".join([
            parser.feed("返事の前 "), parser.feed("["), parser.feed("thought"),
            parser.feed("]"), parser.feed("返事の後"), parser.flush(),
        ])
        self.assertEqual(output, "返事の前 返事の後")

    def test_unrecognized_bracketed_text_is_preserved(self):
        parser = EmotionTagParser()
        output = parser.feed("選択肢は[Python]です。") + parser.flush()
        self.assertEqual(output, "選択肢は[Python]です。")

    def test_unknown_typed_emotion_metadata_is_removed_without_callback(self):
        emotions = []
        parser = EmotionTagParser(emotions.append)
        output = parser.feed("[emotion: joyful]えっ、急に聞くの？") + parser.flush()
        self.assertEqual(output, "えっ、急に聞くの？")
        self.assertEqual(emotions, [])

    def test_style_manager_combines_emotion_and_delivery(self):
        style = StyleManager().resolve("joy", "lively")
        self.assertEqual(style.emotion, "joy")
        self.assertEqual(style.delivery, "lively")
        self.assertGreater(style.speed, 1.0)
        self.assertGreater(style.intonation, 1.0)

    def test_high_confidence_user_emotion_only_slightly_modulates_style(self):
        manager = StyleManager(user_emotion_threshold=0.75)
        ordinary = manager.resolve("neutral", "neutral", user_emotion="sad", user_confidence=0.50)
        adjusted = manager.resolve("neutral", "neutral", user_emotion="sad", user_confidence=1.0)
        self.assertEqual(ordinary.speed, 1.0)
        self.assertLess(adjusted.speed, 1.0)
        self.assertGreater(adjusted.speed, 0.98)


class ResponseAcknowledgementTests(unittest.TestCase):
    def test_leadin_is_chosen_only_after_substantive_turn(self):
        planner = ResponseAcknowledgementPlanner(probability=1.0)
        self.assertIsNone(planner.choose("これどう？", None, 0.0, 0))
        self.assertEqual(
            planner.choose("今日は本当に大変なことがあって、かなり疲れたんだ", "sad", 0.0, 0),
            "うーん",
        )


class UserEmotionRecognizerTests(unittest.TestCase):
    def test_speechbrain_labels_are_normalized_to_internal_emotions(self):
        mapping = OpenSmileSpeechBrainRecognizer._LABEL_MAP
        self.assertEqual(mapping["hap"], "joy")
        self.assertEqual(mapping["exc"], "fun")
        self.assertEqual(mapping["fru"], "angry")


class SearchPlanTests(unittest.TestCase):
    def test_dialogue_clarification_does_not_start_web_search(self):
        for text in ("何？", "ん？さっきの話って何？", "今の何のこと？", "それどういう意味？"):
            with self.subTest(text=text):
                self.assertTrue(is_contextual_clarification(text))
                self.assertFalse(should_search(text))

    def test_assistant_past_claim_is_conversation_repair_not_definition_search(self):
        for text in (
            "え？そんな話してたっけ？おすすめした新作ゲームって何？",
            "ポッポが紹介したゲームってどれ？",
            "さっき言ってた作品って何？",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_contextual_clarification(text))
                self.assertFalse(should_search(text))

    def test_explicit_external_lookup_overrides_conversation_reference(self):
        text = "さっきおすすめしたゲームの発売日を調べて"
        self.assertFalse(is_contextual_clarification(text))
        self.assertTrue(should_search(text))

    def test_repair_context_quotes_only_actual_assistant_evidence(self):
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "さっき何の話してた？"},
            {"role": "assistant", "content": "おすすめした新作ゲームの攻略法について話していたよ。"},
            {"role": "user", "content": "おすすめした新作ゲームって何？"},
        ]
        context = conversation_repair_context(messages[-1]["content"], messages)
        self.assertIn("おすすめした新作ゲームの攻略法", context)
        self.assertIn("具体名が書かれていなければ", context)
        self.assertIn("一般知識や検索結果から穴埋めしない", context)
        self.assertNotIn("Minecraft", context)

    def test_explicit_or_external_question_still_searches(self):
        self.assertTrue(should_search("ハードヒルラについて調べて"))
        self.assertTrue(should_search("今日の天気は？"))
        self.assertTrue(should_search("Pythonって何？"))
        self.assertFalse(should_search("今日の会話は楽しかったね"))

    def test_llm_search_plan_keeps_multiple_disambiguated_queries(self):
        plan = parse_search_plan(
            '{"queries":["メイプルストーリー ハードヒルラ ギミック",'
            '"ハードヒルラ 吸血 ギミック 対処"],'
            '"objective":"ギミックの条件と対処法",'
            '"disambiguation":"メイプル=メイプルストーリー"}',
            "メイプルのヒルラハードのギミックを調べて",
        )
        self.assertGreaterEqual(len(plan.queries), 2)
        self.assertIn("メイプルストーリー", plan.queries[0])
        self.assertIn("条件と対処法", plan.objective)

    def test_invalid_search_plan_falls_back_to_user_query(self):
        plan = parse_search_plan("not json", "ハードヒルラのギミックを調べて")
        self.assertEqual(plan.queries[0], "ハードヒルラのギミック")
        self.assertIn("対処法", plan.queries[1])

    def test_fast_query_keeps_context_and_question_target(self):
        query = build_query("今一番高レベル帯はヤムヤムアイランド。職人種って何？")
        self.assertEqual("ヤムヤムアイランド 職人種", query)

    def test_guide_results_rank_above_general_struggle_posts(self):
        ranked = DeepSearch._rank_results([
            {"title": "ハードヒルラが難しい、みんな苦戦", "body": "感想と雑談"},
            {"title": "ハードヒルラ攻略：吸血ギミックの対処法", "body": "解除条件と手順"},
        ])
        self.assertIn("攻略", ranked[0]["title"])


class MusicCommandTests(unittest.TestCase):
    def test_now_playing_context_names_the_active_track_without_unsolicited_commentary(self):
        context = now_playing_context({"title": "Overlord OP", "query": "overload オープニングテーマ"})
        self.assertIn("Overlord OP", context)
        self.assertIn("質問されていないのに曲への感想を始めない", context)


class PronunciationDictionaryTests(unittest.TestCase):
    def test_surface_text_is_replaced_only_for_synthesis(self):
        source = "そんな風に話して"
        self.assertEqual(apply_pronunciations(source, {"そんな風": "そんなふう"}), "そんなふうに話して")
        self.assertEqual(source, "そんな風に話して")

    def test_only_kana_readings_are_accepted(self):
        self.assertEqual(normalize_pronunciations({"そんな風": "そんなふう", "bad": "reading"}), {"そんな風": "そんなふう"})

    def test_explicit_surface_and_reading_correction_can_be_learned(self):
        self.assertEqual(extract_explicit_pronunciation_correction("そんな風はそんなふうって読むんだよ"), ("そんな風", "そんなふう"))

    def test_reading_only_correction_is_not_ambiguous_auto_learning(self):
        self.assertIsNone(extract_explicit_pronunciation_correction("そんなかぜじゃなくて、そんなふうって読むんだよ"))

    def test_reading_only_correction_uses_the_last_assistant_surface_when_unique(self):
        self.assertEqual(
            infer_pronunciation_correction("そんなかぜじゃなくて、そんなふうって読むんだよ", "そんな風に話してみよう"),
            ("そんな風", "そんなふう"),
        )

    def test_natural_surface_and_kana_correction_can_be_learned_without_yomu(self):
        self.assertEqual(
            infer_pronunciation_correction("深掘りじゃなくて、ふかぼり", "深掘りしてみよう"),
            ("深掘り", "ふかぼり"),
        )

    def test_canonicalized_repeated_surface_uses_only_vetted_reading(self):
        self.assertEqual(
            infer_pronunciation_correction("違う違う。深掘りじゃなくて深掘り。", "深掘りしてみよう"),
            ("深掘り", "ふかぼり"),
        )
        self.assertIsNone(infer_pronunciation_correction("東京じゃなくて東京。", "東京へ行こう"))


class MindReflectionPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_turn_cancels_only_inflight_reflection_generation(self):
        mind = object.__new__(Mind)
        mind._cancel_reflection_on_activity = True
        mind._reflection_generating = True
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        mind._reflect_task = task

        self.assertTrue(mind.prioritize_realtime_turn())
        with self.assertRaises(asyncio.CancelledError):
            await task

        # Applying a parsed reflection is deliberately not interrupted midway.
        mind._reflection_generating = False
        mind._reflect_task = None
        self.assertFalse(mind.prioritize_realtime_turn())


class MultimodalVisionTests(unittest.TestCase):
    def _image(self, size=(2000, 1000), color=(20, 30, 40)):
        from PIL import Image

        image = Image.new("RGB", size, color)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    def test_large_image_is_normalized_and_bounded(self):
        item = prepare_image(self._image(), max_width=320, max_height=240, jpeg_quality=80)
        self.assertEqual(item.mime_type, "image/jpeg")
        self.assertLessEqual(item.metadata["width"], 320)
        self.assertLessEqual(item.metadata["height"], 240)
        self.assertTrue(item.metadata["fingerprint"])

    def test_ollama_adapter_keeps_media_order_until_boundary(self):
        first = MediaInput(MediaType.IMAGE, data=b"first", mime_type="image/jpeg")
        second = MediaInput(MediaType.VIDEO_FRAME, data=b"second", mime_type="image/jpeg")
        client = OllamaMultimodalClient("http://localhost:11434/v1", "gemma4-12b-qat")
        payload = client._messages([{"role": "user", "content": "describe"}], [first, second])
        self.assertEqual(payload[-1]["content"], "describe")
        self.assertEqual(payload[-1]["images"], ["Zmlyc3Q=", "c2Vjb25k"])

    def test_visual_json_and_invalid_json_are_safe(self):
        parsed = VisualObservation.from_model_text('{"summary":"マグカップ","objects":["青いマグカップ"],"conversation_relevance":"high"}')
        self.assertEqual(parsed.objects, ["青いマグカップ"])
        self.assertEqual(parsed.conversation_relevance, "high")
        fallback = VisualObservation.from_model_text("JSONではない説明")
        self.assertEqual(fallback.summary, "JSONではない説明")

    def test_duplicate_hash_has_no_meaningful_difference(self):
        image = prepare_image(self._image(size=(40, 40)))
        self.assertEqual(hash_distance(image.metadata["fingerprint"], image.metadata["fingerprint"]), 0.0)

    def test_perception_does_not_inject_irrelevant_scene(self):
        class Analyzer:
            pass
        perception = PerceptionService(Analyzer())
        perception.memory.record(VisualObservation(summary="静かな部屋", conversation_relevance="none"))
        self.assertIsNone(perception.context_for("ゲームの攻略を教えて"))
        self.assertIn("現在の視覚情報", perception.context_for("今何が見える？") or "")


class MinecraftVisionTests(unittest.TestCase):
    def _image(self, size=(64, 32), color=(20, 30, 40)):
        from PIL import Image

        image = Image.new("RGB", size, color)
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    def test_raw_image_is_only_attached_for_visual_or_auto_turn(self):
        prompt = "(これは今の画面。短く実況して)"
        self.assertFalse(is_direct_vision_turn("もう修正せんといかんかな"))
        self.assertFalse(is_direct_vision_turn("普通に話そう", mode="auto", auto_prompt=prompt))
        self.assertTrue(is_direct_vision_turn("今の画面を見て"))
        self.assertTrue(is_direct_vision_turn(prompt, mode="auto", auto_prompt=prompt))

    def test_minecraft_observation_fields_and_prompt_are_safe(self):
        observation = VisualObservation.from_model_text(
            '{"summary":"night is falling","game":"minecraft","player_state":["low health"],'
            '"notable_events":["zombie nearby"],"suggested_help":"find shelter",'
            '"commentary_worthy":true,"confidence":0.8}'
        )
        self.assertEqual(observation.game, "minecraft")
        self.assertTrue(observation.commentary_worthy)
        self.assertIn("zombie nearby", observation.prompt_context())
        self.assertIn("Minecraft", video_instruction("minecraft") or "")
        self.assertIsNone(video_instruction(""))

    def test_screen_video_input_wraps_capture_as_video_frame(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "screen",
                    "video.screen_monitor": "auto",
                    "video.game_profile": "minecraft",
                    "video.max_queue_size": 8,
                }.get(key, default)

            def section(self, name):
                return {"max_width": 768, "jpeg_quality": 75} if name == "vision" else {}

        service = VideoObservationService(Config(), object())
        source = prepare_image(self._image(), media_type=MediaType.IMAGE)

        class Screen:
            def capture_media(self):
                return source

        service._capture = Screen()
        frame = service._read_frame()
        self.assertEqual(frame.media_type, MediaType.VIDEO_FRAME)
        self.assertEqual(frame.metadata["source"], "screen")
        self.assertEqual(service.input_kind, "screen")

    def test_explicit_capture_replaces_old_queue_with_fresh_window_frame(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "window",
                    "video.game_profile": "minecraft",
                    "video.max_queue_size": 8,
                }.get(key, default)

            def section(self, name):
                return {"max_width": 768, "jpeg_quality": 75} if name == "vision" else {}

        service = VideoObservationService(Config(), object())
        # This test verifies explicit queue replacement, not live game-window
        # discovery.  Keep an actually running Minecraft window from replacing
        # the injected capture double on developer machines.
        service._auto_game_window = False
        old = prepare_image(self._image(color=(1, 1, 1)), media_type=MediaType.VIDEO_FRAME)
        fresh = prepare_image(self._image(color=(220, 220, 220)), media_type=MediaType.IMAGE)

        class Window:
            def capture_media(self):
                return MediaInput(
                    media_type=fresh.media_type, data=fresh.data, mime_type=fresh.mime_type,
                    timestamp=fresh.timestamp,
                    metadata={**fresh.metadata, "capture_backend": "directx", "capture_sequence": 7},
                )

        service._capture = Window()
        service._frames.append(old)
        frame = asyncio.run(service.capture_current_frame())
        self.assertEqual(frame.metadata["capture_sequence"], 7)
        self.assertEqual(frame.metadata["capture_backend"], "directx")
        self.assertEqual(len(service._frames), 1)
        self.assertEqual(service.latest_frame.data, frame.data)

    def test_background_vision_does_not_block_the_capture_loop(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "screen",
                    "video.max_queue_size": 8,
                    "video.analysis_interval_sec": 2.0,
                    "video.max_frames_per_analysis": 1,
                }.get(key, default)

            def section(self, _name):
                return {}

        class SlowPerception:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def analyze(self, *_args, **_kwargs):
                self.started.set()
                await self.release.wait()
                return VisualObservation(summary="done")

            def cancel_background(self):
                pass

        async def scenario():
            perception = SlowPerception()
            service = VideoObservationService(Config(), perception)
            service._frames.append(prepare_image(self._image(), media_type=MediaType.VIDEO_FRAME))
            await service._maybe_background_analyze()
            self.assertIsNotNone(service._background_analysis_task)
            await asyncio.wait_for(perception.started.wait(), timeout=.2)
            self.assertFalse(service._background_analysis_task.done())
            service.cancel_background()
            try:
                await service._background_analysis_task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())


class MinecraftKnowledgeTests(unittest.TestCase):
    def test_local_sqlite_knowledge_is_seeded_and_retrieved(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            database = Path(tmp) / "minecraft.db"
            knowledge = MinecraftKnowledgeBase(database, root / "data" / "minecraft_knowledge.json")
            context = knowledge.context_for("ネザーでガストと溶岩が怖い")
            self.assertTrue(database.exists())
            self.assertIn("ネザー", context or "")
            self.assertIn("Minecraft local knowledge", context or "")

    def test_unrelated_text_does_not_force_progression_context(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            knowledge = MinecraftKnowledgeBase(Path(tmp) / "minecraft.db", root / "data" / "minecraft_knowledge.json")
            self.assertIsNone(knowledge.context_for("今日は何を食べよう"))

    def test_starter_question_has_a_local_fast_answer(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            knowledge = MinecraftKnowledgeBase(Path(tmp) / "minecraft.db", root / "data" / "minecraft_knowledge.json")
            answer = knowledge.quick_answer("始めたばかりだけど最初に何をすればいい?")
            self.assertIn("食料", answer or "")

    def test_immediate_zombie_danger_has_a_local_fast_answer(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as tmp:
            knowledge = MinecraftKnowledgeBase(Path(tmp) / "minecraft.db", root / "data/minecraft_knowledge.json")
            answer = knowledge.quick_answer("今ゾンビに襲われてるんだけどどうしたらいい？")
            self.assertIn("盾", answer or "")
            self.assertIn("食料", answer or "")


class AudioBackendFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemma_audio_never_claims_support_and_uses_whisper_fallback(self):
        class WhisperFallback:
            async def transcribe(self, audio, sample_rate, language=None):
                return AudioUnderstandingResult(text="聞こえた", language=language, backend="whisper")

        backend = Gemma4AudioBackend("gemma4-12b-qat", WhisperFallback())
        result = await backend.transcribe(b"\0" * 16, 16000, "ja")
        self.assertFalse(backend.available)
        self.assertEqual(result.backend, "whisper_fallback")
        self.assertEqual(result.text, "聞こえた")


class MusicRecognitionRequestTests(unittest.TestCase):
    def test_explicit_song_identification_is_detected(self):
        request = parse_music_recognition_request("ポッポ、この曲何？")
        self.assertTrue(request.requested)
        self.assertFalse(request.play_after)

    def test_identify_and_play_request_is_detected(self):
        request = parse_music_recognition_request("この曲を特定して流して")
        self.assertTrue(request.requested)
        self.assertTrue(request.play_after)

    def test_ordinary_music_impression_question_is_not_hijacked(self):
        self.assertFalse(parse_music_recognition_request("この曲どう思う？").requested)

    def test_windows_playback_capture_uses_matching_loopback_endpoint(self):
        class Device:
            def __init__(self, ident, name, loopback=False):
                self.id, self.name, self.isloopback = ident, name, loopback

            def recorder(self, **_kwargs):
                class Recorder:
                    def __enter__(self): return self
                    def __exit__(self, *_args): return None
                    def record(self, numframes):
                        return np.column_stack((
                            np.full(numframes, .2, dtype=np.float32),
                            np.full(numframes, .4, dtype=np.float32),
                        ))
                return Recorder()

        speaker = Device("endpoint-a", "Headphones")
        loopback = Device("endpoint-a", "Headphones loopback", True)
        fake = SimpleNamespace(
            all_speakers=lambda: [speaker], default_speaker=lambda: speaker,
            all_microphones=lambda include_loopback=False: [loopback],
        )
        with patch.dict(sys.modules, {"soundcard": fake}):
            audio = capture_output_audio(1.0, 16000, "endpoint-a")
        self.assertEqual(len(audio), 16000)
        self.assertAlmostEqual(float(audio.mean()), .3, places=4)

    def test_humming_request_arms_a_following_audio_turn(self):
        request = parse_humming_recognition_request("ポッポ、鼻歌で曲を探して流して")
        self.assertTrue(request.requested)
        self.assertTrue(request.play_after)

    def test_humming_word_without_search_does_not_arm(self):
        self.assertFalse(parse_humming_recognition_request("鼻歌が上手いね").requested)


class PersonaTruthfulnessTests(unittest.TestCase):
    class Config:
        def section(self, name):
            if name == "persona":
                return {
                    "active": "test",
                    "presets": {"test": {
                        "name": "ポッポ", "system_prompt": "自然に会話するAI。",
                    }},
                }
            return {}

        def get(self, key, default=None):
            return default

    def test_persona_prioritizes_truth_over_agreement(self):
        prompt = build_system_prompt(self.Config())
        self.assertIn("事実性・非迎合ルール", prompt)
        self.assertIn("数文字の英字", prompt)
        self.assertIn("知らない", prompt)

    def test_response_direction_has_an_agreement_gate(self):
        prompt = ResponseDirector().decide(
            "ODDっていう技術だよ", allow_follow_up=False, momentum=.5,
        ).prompt()
        self.assertIn("Agreement Gate", prompt)
        self.assertIn("正式名称や事実を作らず", prompt)

    def test_relationship_question_uses_independent_social_stance(self):
        direction = ResponseDirector().decide(
            "ポッポは私のこと好き？", allow_follow_up=True, momentum=.8,
        )
        prompt = direction.prompt()
        self.assertEqual(direction.intent, "関係性・感情の確認")
        self.assertFalse(direction.allow_follow_up)
        self.assertIn("Independent Social Stance", prompt)
        self.assertIn("実際の関係状態より盛らない", prompt)

    def test_unknown_acronym_gets_a_turn_specific_truth_guard(self):
        guard = epistemic_turn_prompt("ODDみたいな技術だよ")
        self.assertIn("ODD", guard or "")
        self.assertIn("正式名称", guard or "")
        self.assertIsNone(epistemic_turn_prompt("OBSからGPUで解析する"))


class TimeAwarenessTests(unittest.TestCase):
    class _Config:
        def get(self, key, default=None):
            return {"time_awareness.timezone": "Asia/Tokyo", "time_awareness.max_topics": 24}.get(key, default)

    def test_topic_time_is_persisted_and_injected_into_context(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "temporal.json"
            awareness = TimeAwareness(path, self._Config())
            awareness.record_turn("メイプルのヒルラハードのギミック")
            restored = TimeAwareness(path, self._Config())
            context = restored.prompt_context("ヒルラハードのギミックの続き")
            self.assertIn("時間の状況", context)
            self.assertIn("過去の話題", context)
            self.assertEqual(restored.snapshot()["timezone"], "Asia/Tokyo")

    def test_night_and_morning_period_labels(self):
        self.assertEqual(TimeAwareness._period(8), "朝")
        self.assertEqual(TimeAwareness._period(14), "昼")
        self.assertEqual(TimeAwareness._period(22), "深夜・早朝")

    def test_reference_play_request_is_marked_for_context_resolution(self):
        action, query = parse_music_command("じゃぁそれを再生して", music_playing=False)
        self.assertEqual(action, "play")
        self.assertTrue(is_music_reference(query))

    def test_theme_request_is_kept_as_a_music_context_candidate(self):
        self.assertEqual(
            extract_music_reference_candidate("overloadのオープニングテーマにして"),
            "overloadのオープニングテーマ",
        )


class SttWakeWordCorrectionTests(unittest.TestCase):
    def _stt_with_aliases(self):
        from neuro_voice.stt.faster_whisper_stt import FasterWhisperSTT

        stt = FasterWhisperSTT.__new__(FasterWhisperSTT)
        stt._wake_word_aliases = {"ポチ": ["ポッチ", "おち"]}
        return stt

    def test_corrects_a_misheard_wake_word_at_the_start(self):
        self.assertEqual(self._stt_with_aliases()._apply_wake_word_aliases("おち、聞いて"), "ポチ、聞いて")

    def test_does_not_replace_the_same_common_word_inside_a_sentence(self):
        self.assertEqual(self._stt_with_aliases()._apply_wake_word_aliases("こっちの声聞こえる？"), "こっちの声聞こえる？")

    def test_poppo_and_filler_are_not_used_as_music_search_terms(self):
        self.assertEqual(
            parse_music_command("よし、ポポ有名な音楽流して", music_playing=False),
            ("play", "J-POP 人気曲"),
        )

    def test_popular_one_song_request_is_not_used_as_literal_search_term(self):
        self.assertEqual(
            parse_music_command("なんか有名な曲1曲流して", music_playing=False),
            ("play", "J-POP 人気曲"),
        )

    def test_trending_music_request_uses_the_popular_song_query(self):
        self.assertEqual(
            parse_music_command("流行りの音楽流して", music_playing=False),
            ("play", "J-POP 人気曲"),
        )

    def test_local_music_volume_command_is_parsed_while_playing(self):
        self.assertEqual(
            parse_music_command("音楽の音5%にして", music_playing=True),
            ("volume_set", 0.05),
        )

    def test_queue_clear_is_parsed_even_when_music_is_not_playing(self):
        self.assertEqual(
            parse_music_command("キューに溜まった曲を消して", music_playing=False),
            ("clear_queue", None),
        )

    def test_ambiguous_stop_is_deferred_outside_an_active_one_to_one_turn(self):
        self.assertEqual(
            should_execute_music_command(
                "止めて", "stop", assistant_addressed=False,
                one_human_conversation=False, active_exchange=False,
            ),
            (False, "ambiguous_without_context"),
        )

    def test_explicit_music_instruction_executes_in_group_conversation(self):
        self.assertEqual(
            should_execute_music_command(
                "音楽を止めて", "stop", assistant_addressed=False,
                one_human_conversation=False, active_exchange=False,
            ),
            (True, "explicit_music_target"),
        )

    def test_bare_music_followup_executes_only_during_active_one_to_one_turn(self):
        self.assertEqual(
            should_execute_music_command(
                "止めて", "stop", assistant_addressed=False,
                one_human_conversation=True, active_exchange=True,
            ),
            (True, "active_one_to_one_followup"),
        )

    def test_current_track_question_is_not_a_general_music_search_question(self):
        self.assertTrue(is_current_track_discussion("今の音楽どう思う？"))
        self.assertTrue(is_current_track_discussion("この曲の感想は？"))
        self.assertFalse(is_current_track_discussion("最近の音楽業界どう思う？"))


class MusicIntentPlannerTests(unittest.IsolatedAsyncioTestCase):
    class _LLM:
        def __init__(self, response):
            self._response = response

        async def generate(self, _messages):
            yield self._response

    async def test_planner_accepts_a_real_request_and_refines_a_vague_query(self):
        plan = await plan_music_intent(
            self._LLM('{"execute":true,"action":"play","query":"楽しい J-POP 人気曲","reason":"再生依頼"}'),
            "なんか面白い音楽流して", parsed_action="play", parsed_query="なんか面白い音楽",
            music_playing=False, now_playing=None, last_query=None,
        )
        self.assertTrue(plan.execute)
        self.assertEqual(plan.query, "楽しい J-POP 人気曲")

    async def test_planner_rejects_a_quoted_stop_phrase(self):
        plan = await plan_music_intent(
            self._LLM('{"execute":false,"action":null,"query":null,"reason":"引用表現"}'),
            "止めてって言っても止めないでね", parsed_action="stop", parsed_query=None,
            music_playing=True, now_playing=None, last_query=None,
        )
        self.assertFalse(plan.execute)


class PersonaSpeakerStoreTests(unittest.TestCase):
    def test_speaker_profiles_are_independent_per_persona_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            neuro = SpeakerRegistry(root / "speakers_neuro.json", model_tag="test")
            luna = SpeakerRegistry(root / "speakers_luna.json", model_tag="test")
            neuro.identify(np.array([1.0, 0.0], dtype=np.float32))
            neuro.save()
            self.assertEqual(neuro.count(), 1)
            self.assertEqual(luna.count(), 0)


class DialogueIntelligenceTests(unittest.TestCase):
    class _Config:
        def get(self, _key, default=None):
            return default

    def test_user_model_is_per_user_and_emotional_memory_needs_confidence(self):
        with TemporaryDirectory() as tmp:
            dialogue = DialogueIntelligence(self._Config(), Path(tmp) / "dialogue.json")
            dialogue.observe_turn("alice", "今日は少し疲れた", emotion="sad", emotion_confidence=0.90, now=100)
            dialogue.observe_turn("alice", "眠いかも", emotion="sad", emotion_confidence=0.90, now=110)
            dialogue.observe_turn("bob", "元気", emotion="joy", emotion_confidence=0.20, now=111)
            self.assertEqual(dialogue.snapshot("alice")["user"]["turns"], 2)
            self.assertEqual(dialogue.snapshot("bob")["user"]["turns"], 1)
            self.assertEqual(dialogue.emotional_trend("alice"), "recently_low")
            self.assertIsNone(dialogue.emotional_trend("bob"))

    def test_importance_and_tool_scheduler_are_deterministic(self):
        with TemporaryDirectory() as tmp:
            dialogue = DialogueIntelligence(self._Config(), Path(tmp) / "dialogue.json")
            self.assertGreaterEqual(dialogue.memory_importance("AIアシスタントを開発している"), 45)
            self.assertLess(dialogue.memory_importance("今日ラーメンを食べた", "episode"), 45)
            allowed, _ = dialogue.should_use_tool("search", "ヒルラハードの攻略を調べて", now=100)
            self.assertTrue(allowed)
            dialogue.mark_tool_use("search", "ヒルラハードの攻略を調べて", now=100)
            allowed, reason = dialogue.should_use_tool("search", "ヒルラハードの攻略を調べて", now=105)
            self.assertFalse(allowed)
            self.assertEqual(reason, "duplicate_cooldown")

    def test_prompt_contains_compact_control_signals_not_a_reasoning_trace(self):
        with TemporaryDirectory() as tmp:
            dialogue = DialogueIntelligence(self._Config(), Path(tmp) / "dialogue.json")
            dialogue.observe_turn("alice", "AIを作ってる", now=100)
            prompt = dialogue.prompt_context("alice", "AIを作ってる")
            self.assertIn("User Model", prompt)
            self.assertIn("Confidence Manager", prompt)
            self.assertIn("Internal Thought Layer", prompt)
            self.assertIn("今回の応答設計", prompt)


class TurnManagerTests(unittest.TestCase):
    def test_backchannel_does_not_confirm_a_barge_in(self):
        manager = TurnManager()
        manager.assistant_speaking()
        manager.user_started(assistant_busy=True)
        self.assertEqual(manager.state, TurnState.BARGE_IN_PENDING)
        manager.backchannel(assistant_busy=True)
        self.assertEqual(manager.state, TurnState.AI_SPEAKING)

    def test_substantive_barge_in_has_an_explicit_confirmed_state(self):
        manager = TurnManager()
        manager.user_started(assistant_busy=True)
        manager.barge_in_confirmed()
        self.assertEqual(manager.state, TurnState.BARGE_IN_CONFIRMED)


class UserModelTests(unittest.TestCase):
    def test_explicit_preference_becomes_a_confirmed_fact(self):
        profile = {}
        UserModel.observe(profile, "私は技術的な説明が好きです", now=100)
        facts = UserModel.ensure(profile)["facts"]
        self.assertIn("技術的な説明", facts["interests"])
        self.assertEqual(UserModel.ensure(profile)["interaction_policy"]["technical_depth"], "high")

    def test_curiosity_cooldown_is_consumed_only_by_an_actual_question(self):
        profile = {}
        UserModel.ensure(profile)
        engine = CuriosityEngine(min_turn_interval=3, threshold=0.1)
        decision = engine.decide(
            text="今日は新しい音声対話の仕組みを作っている", topics=["音声対話", "仕組み"],
            profile=profile, momentum=0.8, turn_index=4,
        )
        self.assertTrue(decision.should_ask)
        engine.record_actual_question(profile, "なるほど。", 4)
        self.assertEqual(profile.get("last_curiosity_turn", -99), -99)
        engine.record_actual_question(profile, "どこまでできた？", 4)
        self.assertEqual(profile["last_curiosity_turn"], 4)


class DialogueActTests(unittest.TestCase):
    def test_correction_uses_strategy_without_a_canned_prefix(self):
        act = DialogueActPlanner().classify("それは違う、Whisperの設定を修正して")
        self.assertEqual(act.name, "correction_acceptance")
        self.assertIn("restate", act.strategy)


class ResponseDirectorTests(unittest.TestCase):
    def test_story_uses_a_specific_reaction_and_related_topic(self):
        direction = ResponseDirector().decide("今日、友達とゲームで遊んだ", allow_follow_up=True, momentum=0.8)
        self.assertEqual(direction.intent, "出来事への反応")
        self.assertTrue(direction.allow_follow_up)
        self.assertIn("印象的な一点", direction.prompt())

    def test_support_does_not_turn_into_a_canned_question(self):
        direction = ResponseDirector().decide("今日は疲れていてつらい", allow_follow_up=False, momentum=0.4)
        self.assertEqual(direction.intent, "気持ちの共有と支援")
        self.assertFalse(direction.allow_follow_up)


class PlaybackTrackerTests(unittest.TestCase):
    def test_tracker_distinguishes_generated_synthesized_and_played_text(self):
        tracker = PlayedTextTracker()
        tracker.generated("abcdef")
        tracker.synthesized("abcdef")
        tracker.played("abcdef", 0.5)
        tracker.interrupted("barge_in")
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["generated_text"], "abcdef")
        self.assertEqual(snapshot["synthesized_text"], "abcdef")
        self.assertEqual(snapshot["played_text"], "abc")
        self.assertEqual(snapshot["interrupted_at_character"], 3)
        self.assertEqual(snapshot["interruption_reason"], "barge_in")

    def test_true_barge_in_preserves_played_and_unplayed_chunk_text(self):
        tracker = PlayedTextTracker(response_id="r1")
        tracker.generated("最初の説明です。続きの説明です。")
        first = tracker.queue_chunk("最初の説明です。", total_frames=100, chunk_id="a")
        tracker.queue_chunk("続きの説明です。", total_frames=100, chunk_id="b")
        tracker.started(first.chunk_id)
        tracker.completed(first.chunk_id, 65, 100, False)
        tracker.interrupted("substantive_barge_in")
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["played_text"], "最初の説明")
        self.assertEqual(snapshot["unplayed_text"], "です。続きの説明です。")
        self.assertIn("続きの説明です。", snapshot["queued_text"])

    def test_continuous_barge_in_progress_does_not_duplicate_played_text(self):
        tracker = PlayedTextTracker(response_id="r2")
        tracker.generated("abcdef")
        chunk = tracker.queue_chunk("abcdef", total_frames=100, chunk_id="a")
        tracker.completed(chunk.chunk_id, 50, 100, False)
        tracker.completed(chunk.chunk_id, 50, 100, False)
        self.assertEqual(tracker.snapshot()["played_text"], "abc")


class BargeInFlowTests(unittest.TestCase):
    def test_cancelled_response_id_cannot_accept_late_tts_audio(self):
        self.assertFalse(response_is_active("old-response", "new-response", {"old-response"}))
        self.assertTrue(response_is_active("new-response", "new-response", {"old-response"}))
        # Later queued chunks of a completed response must still play.  Only
        # a confirmed barge-in invalidates them.
        self.assertTrue(response_can_play("completed-response", set()))
        self.assertFalse(response_can_play("old-response", {"old-response"}))

    def test_acknowledgement_returns_to_speaking_without_confirming(self):
        manager = TurnManager()
        manager.assistant_speaking()
        manager.user_started(assistant_busy=True)
        manager.backchannel(assistant_busy=True)
        self.assertEqual(manager.state, TurnState.AI_SPEAKING)

    def test_false_positive_returns_to_speaking_without_history_turn(self):
        manager = TurnManager()
        manager.assistant_speaking()
        manager.user_started(assistant_busy=True)
        manager.speech_ignored()
        self.assertEqual(manager.state, TurnState.AI_SPEAKING)

    def test_interrupted_turn_is_internal_context_not_history(self):
        conv = ConversationManager("system")
        conv.add_user("Pythonについて説明して")
        interrupted = InterruptedTurn(
            response_id="r3", original_user_text="Pythonについて説明して",
            full_assistant_text="最初。続き。", played_text="最初。", unplayed_text="続き。",
            interrupted_at=1.0, interruption_text="別の質問", resume_recommended=True,
        )
        conv.hold_current_topic(interrupted)
        messages, _ = conv.messages_for_turn("別の質問")
        self.assertIn("ユーザーに途中で遮られました", messages[1]["content"])
        self.assertNotIn("最初。続き。", [item["content"] for item in conv.messages()])


class TtsSegmentationTests(unittest.TestCase):
    def test_soft_limit_does_not_split_a_phrase_before_its_punctuation(self):
        segmenter = SentenceSegmenter(max_chars=4, min_chars=1, hard_max_chars=8)
        self.assertEqual(segmenter.feed("面白い"), [])
        self.assertEqual(segmenter.feed("よね。"), ["面白いよね。"])

    def test_short_comma_clause_waits_for_following_context(self):
        segmenter = SentenceSegmenter(max_chars=30, min_chars=8)
        self.assertEqual(segmenter.feed("爆弾処理のゲームって、"), [])
        self.assertEqual(
            segmenter.feed("正解を間違えた瞬間が一番怖いよ。"),
            ["爆弾処理のゲームって、正解を間違えた瞬間が一番怖いよ。"],
        )

    def test_long_comma_clause_remains_a_low_latency_boundary(self):
        segmenter = SentenceSegmenter(max_chars=30, min_chars=8)
        clause = "これは十分な文脈を含んでいる少し長めの前置きなので、"
        self.assertEqual(segmenter.feed(clause), [clause])


class LatencyWindowTests(unittest.TestCase):
    def test_rolling_summary_reports_p50_and_p95(self):
        window = LatencyWindow(max_samples=5)
        for total_ms in (100, 200, 300, 400, 500):
            metrics = TurnMetrics({"speech_end": 0.0, "play_start": total_ms / 1000})
            window.add(metrics)
        total = next(item for item in window.snapshot() if item["label"] == "合計")
        self.assertEqual(total["p50_ms"], 300)
        self.assertEqual(total["p95_ms"], 500)
        self.assertEqual(total["samples"], 5)


class DiscordLoopbackAudioTests(unittest.TestCase):
    def test_quiet_loopback_audio_is_normalized_for_stt(self):
        audio = np.array([0.004, -0.004, 0.003, -0.003], dtype=np.float32)
        prepared, raw_rms, prepared_rms, gain = DiscordBridge._prepare_loopback_for_stt(audio)
        self.assertEqual(prepared.dtype, np.float32)
        self.assertGreater(raw_rms, 0.0025)
        self.assertGreater(gain, 1.0)
        self.assertGreater(prepared_rms, raw_rms)

    def test_silence_is_not_artificially_amplified(self):
        prepared, raw_rms, prepared_rms, gain = DiscordBridge._prepare_loopback_for_stt(
            np.zeros(320, dtype=np.float32)
        )
        self.assertEqual(raw_rms, 0.0)
        self.assertEqual(prepared_rms, 0.0)
        self.assertEqual(gain, 1.0)
        self.assertTrue(np.all(prepared == 0.0))


class DiscordPartialAudioTests(unittest.TestCase):
    def test_in_progress_utterance_is_available_without_finalizing_it(self):
        segmenter = UtteranceSegmenter()
        for _ in range(60):
            self.assertIsNone(segmenter.feed(np.full(320, 0.1, dtype=np.float32)))
        partial = segmenter.current_audio(min_speech_ms=900)
        self.assertIsNotNone(partial)
        self.assertTrue(segmenter.speaking)
        self.assertGreaterEqual(len(partial), 16000 * 0.9)


class DiscordMembershipTests(unittest.TestCase):
    class _Member:
        def __init__(self, member_id, name, bot=False):
            self.id, self.display_name, self.name, self.bot = member_id, name, name, bot

    class _Channel:
        id = 1
        name = "lobby"
        members = []

    class _VoiceClient:
        channel = None

    def test_member_snapshot_excludes_bots(self):
        bridge = DiscordBridge.__new__(DiscordBridge)
        channel = self._Channel()
        channel.members = [self._Member(10, "Alice"), self._Member(11, "Bot", bot=True)]
        vc = self._VoiceClient()
        vc.channel = channel
        bridge._vc = vc
        bridge._voice_members = {}
        bridge._on_event = None
        self.assertEqual(bridge.voice_members(), [{"id": 10, "name": "Alice"}])


class LoopbackEndpointTests(unittest.TestCase):
    class _Device:
        def __init__(self, name, endpoint_id, isloopback=False):
            self.name = name
            self.id = endpoint_id
            self.isloopback = isloopback
            self.channels = 2
            self.samplerate = 48000

    def test_loopback_is_matched_by_endpoint_id_not_name_fragment(self):
        speaker = self._Device("Speakers (Razer BlackShark V2 Pro)", "render-razer")
        wrong = self._Device("Speakers (Razer BlackShark V2 Pro)", "capture-other", True)
        right = self._Device("Unrelated display name", "render-razer", True)
        self.assertIs(_find_loopback_microphone(speaker, [wrong, right]), right)

    def test_same_name_without_matching_endpoint_is_not_usable(self):
        speaker = self._Device("Speakers (Razer BlackShark V2 Pro)", "render-razer")
        same_name = self._Device("Speakers (Razer BlackShark V2 Pro)", "capture-other", True)
        self.assertIsNone(_find_loopback_microphone(speaker, [same_name]))


class DirectDiscordIdentityTests(unittest.TestCase):
    def test_pcm_packet_keeps_transport_account_separate_from_audio(self):
        header = b'{"type":"pcm","encoding":"pcm_s16le","source_account":{"id":"42","name":"DiscordName"}}'
        packet = len(header).to_bytes(4, "big") + header + b"\x01\x02"
        metadata, pcm = decode_pcm_packet(packet)
        self.assertEqual(metadata["source_account"]["id"], "42")
        self.assertEqual(metadata["source_account"]["name"], "DiscordName")
        self.assertEqual(pcm, b"\x01\x02")

    def test_direct_speaker_exposes_account_as_transport_not_profile(self):
        speaker = DirectDiscordSpeaker("42", "DiscordName", ssrc=99)
        self.assertEqual(speaker.source, "discord_direct")
        self.assertEqual(speaker.transport_context(), {"id": "42", "name": "DiscordName", "ssrc": 99})


class DirectDiscordDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_bridge_enqueue_is_not_awaited(self):
        delivered = []
        receiver = DirectDAVEReceiver("test", lambda speaker, audio: delivered.append((speaker, audio)))
        speaker = DirectDiscordSpeaker("42", "DiscordName")
        audio = np.ones(320, dtype=np.float32)
        await receiver._deliver_utterance(speaker, audio)
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0][0], speaker)

    async def test_gui_leave_accepts_a_direct_dave_voice_session(self):
        bridge = DiscordBridge.__new__(DiscordBridge)
        bridge._vc = None
        bridge._direct_channel = object()
        called = []

        async def fake_leave(*, silent=False):
            called.append(silent)

        bridge._cmd_leave = fake_leave
        self.assertEqual(await bridge.leave(), {"ok": True})
        self.assertEqual(called, [True])

    async def test_sidecar_command_error_unblocks_join_immediately(self):
        receiver = DirectDAVEReceiver("test", lambda *_: None)
        receiver._handle_event({"type": "command_error", "message": "gateway is not ready"})
        self.assertTrue(receiver._joined.is_set())
        self.assertEqual(receiver._join_error, "gateway is not ready")

    async def test_sidecar_ready_is_explicitly_observed(self):
        receiver = DirectDAVEReceiver("test", lambda *_: None)
        receiver._handle_event({"type": "sidecar_ready", "bot_id": "123"})
        self.assertTrue(receiver._sidecar_ready.is_set())

    async def test_direct_leave_waits_for_sidecar_confirmation(self):
        receiver = DirectDAVEReceiver("test", lambda *_: None)

        class _Socket:
            async def send(_self, _payload):
                receiver._handle_event({"type": "left", "channel_id": "7"})

        receiver._ws = _Socket()
        await receiver.leave()
        self.assertFalse(receiver._leaving)
        self.assertTrue(receiver._left.is_set())

    async def test_direct_leave_surfaces_sidecar_failure(self):
        receiver = DirectDAVEReceiver("test", lambda *_: None)

        class _Socket:
            async def send(_self, _payload):
                receiver._handle_event({"type": "command_error", "message": "not ready"})

        receiver._ws = _Socket()
        with self.assertRaisesRegex(RuntimeError, "退出に失敗"):
            await receiver.leave()
        self.assertFalse(receiver._leaving)

    async def test_discord_final_input_epoch_increases_for_each_utterance(self):
        """An old queued Discord turn cannot later become the newest reply."""
        bridge = DiscordBridge.__new__(DiscordBridge)
        bridge._last_user_activity = 0.0
        bridge._latest_input_epoch = 0
        bridge._active_response_task = None
        bridge._partial_generation = {}
        bridge._partial_explicit_calls = {}
        bridge._barge_paused = False
        bridge._fd_pause_deadline = 0.0
        bridge._utter_q = asyncio.Queue(maxsize=3)
        user = DirectDiscordSpeaker("42", "DiscordName")
        bridge._enqueue_utterance(user, np.ones(160, dtype=np.float32))
        bridge._enqueue_utterance(user, np.ones(160, dtype=np.float32))
        first = bridge._utter_q.get_nowait()
        second = bridge._utter_q.get_nowait()
        self.assertEqual((first[3], second[3]), (1, 2))
        self.assertEqual(bridge._latest_input_epoch, 2)


class DirectDiscordPortTests(unittest.TestCase):
    def test_sidecars_use_a_valid_ephemeral_loopback_port(self):
        self.assertGreater(reserve_loopback_port(), 0)


@unittest.skipIf(OpenAICompatBackend is None, "optional OpenAI SDK is not installed")
class OllamaRunnerFallbackTests(unittest.TestCase):
    def _backend(self, **overrides):
        params = {
            "name": "ollama",
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "model": "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:Q4_0",
            "ollama_fallback_model": "gemma4-12B-it-Q6:latest",
        }
        params.update(overrides)
        return OpenAICompatBackend(**params)

    def test_windows_runner_crash_uses_runtime_fallback(self):
        backend = self._backend()
        crash = RuntimeError(
            "llama-server process has terminated: exit status 0xc0000409: "
            "The system detected an overrun of a stack-based buffer"
        )

        self.assertTrue(backend._activate_ollama_fallback(crash))
        self.assertEqual(backend.configured_model, "hf.co/google/gemma-4-12B-it-qat-q4_0-gguf:Q4_0")
        self.assertEqual(backend.model, "gemma4-12B-it-Q6:latest")
        self.assertIsNotNone(backend.fallback_notice)
        self.assertFalse(backend._activate_ollama_fallback(crash))

    def test_normal_request_failure_does_not_switch_model(self):
        backend = self._backend()

        self.assertFalse(backend._activate_ollama_fallback(RuntimeError("connection timed out")))
        self.assertEqual(backend.model, backend.configured_model)

    def test_removed_configured_model_uses_runtime_fallback(self):
        backend = self._backend()

        self.assertTrue(backend._activate_ollama_fallback(
            RuntimeError("Error code: 404 - model 'old-qat:latest' not found")
        ))
        self.assertEqual(backend.model, "gemma4-12B-it-Q6:latest")


if __name__ == "__main__":
    unittest.main()
