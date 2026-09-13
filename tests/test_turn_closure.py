import unittest

from neuro_voice.dialogue.events import ConversationEvent, ConversationEventType
from neuro_voice.dialogue.orchestrator import ConversationOrchestrator


class _Config:
    def __init__(self, **overrides):
        self.values = {
            "discord.respond_all": False,
            "discord.follow_up_s": 15.0,
            "conversation.addressing.respond_threshold": 0.75,
            "conversation.addressing.brief_threshold": 0.45,
            "conversation.addressing.observe_threshold": 0.35,
            "conversation.addressing.continuation_s": 45.0,
            "conversation.response_length": "short",
            "conversation.response_depth": 1,
            "conversation.turn_closure.enabled": True,
            "conversation.turn_closure.max_feedback_chars": 48,
            "conversation.turn_closure.brief_reply_on_gratitude": True,
        }
        self.values.update(overrides)

    def get(self, key, default=None):
        return self.values.get(key, default)


class TurnClosureTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = ConversationOrchestrator(
            _Config(), wake_words=["ポッポ"], assistant_name="ポッポ",
        )

    def speech(self, text, *, at=100.0, activity_active=False):
        return self.orchestrator.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL,
            "local",
            text=text,
            timestamp=at,
            metadata={
                "speaker_key": "local:mic",
                "speaker_name": "ユーザー",
                "activity_active": activity_active,
            },
        ))

    def assistant_said(self, text, *, asked_question=False):
        self.orchestrator.response_started(
            source="local",
            expected_response_from="local:mic",
            asked_question=asked_question,
        )
        self.orchestrator.response_finished(
            source="local",
            expected_response_from="local:mic",
            asked_question=asked_question,
            assistant_text=text,
        )

    def test_compound_understanding_can_end_in_silence(self):
        self.speech("ポッポ、量子化の違いを教えて")
        topic = self.orchestrator.state.active_topic
        self.assistant_said("Q4は軽く、Q6は精度を残しやすいよ。")

        decision = self.speech("なるほど、そういうことか", at=101.0)

        self.assertFalse(decision.response_plan.should_respond)
        self.assertEqual(decision.response_plan.reason, "exchange_naturally_closed")
        self.assertTrue(decision.response_plan.settles_exchange)
        self.assertEqual(self.orchestrator.state.active_topic, topic)
        self.assertFalse(self.orchestrator.state.assistant_turn_open)

    def test_feedback_with_a_reaction_does_not_force_another_explanation(self):
        self.speech("ポッポ、この仕組み面白いね")
        self.assistant_said("入力前に判断層を置くと会話のリズムを制御できるよ。")

        decision = self.speech("たしかに、それ面白いね", at=101.0)

        self.assertFalse(decision.response_plan.should_respond)

    def test_colloquial_agreement_closes_without_rephrasing_previous_reply(self):
        self.speech("ポッポ、明日は仕事なんだ")
        for index, feedback in enumerate(("ほんとそれ。", "マジでそう。"), start=1):
            self.assistant_said(
                "仕事があるって分かってて粘っちゃうの、ちょっとずるいよね。"
                "でも、集中すると時間が溶けるから危ないよ。"
            )
            decision = self.speech(feedback, at=101.0 + index)
            self.assertFalse(decision.response_plan.should_respond, feedback)
            self.assertEqual(
                decision.response_plan.reason, "exchange_naturally_closed",
            )

    def test_question_is_not_suppressed(self):
        self.speech("ポッポ、この仕組みを説明して")
        self.assistant_said("まず終了判定を通すよ。")

        decision = self.speech("なるほど、でも誤判定したらどうする？", at=101.0)

        self.assertTrue(decision.response_plan.should_respond)

    def test_request_is_not_suppressed(self):
        self.speech("ポッポ、候補を出して")
        self.assistant_said("候補は三つあるよ。")

        decision = self.speech("うん、それでお願い", at=101.0)

        self.assertTrue(decision.response_plan.should_respond)

    def test_activity_input_has_priority_over_turn_closure(self):
        self.speech("ポッポ、しりとりしよう")
        self.assistant_said("りんご。次は『ご』だよ。")

        decision = self.speech("うん", at=101.0, activity_active=True)

        self.assertTrue(decision.response_plan.should_respond)
        self.assertNotEqual(decision.response_plan.reason, "exchange_naturally_closed")

    def test_confirmation_of_actionable_question_is_not_swallowed(self):
        self.speech("ポッポ、音楽どうしよう")
        self.assistant_said("この曲を流してみる？", asked_question=True)

        decision = self.speech("うん", at=101.0)

        self.assertTrue(decision.response_plan.should_respond)

    def test_explicit_gratitude_uses_direct_tiny_reply(self):
        self.speech("ポッポ、助かった")
        self.assistant_said("これで設定できたよ。")

        decision = self.speech("ポッポ、ありがとう", at=101.0)

        self.assertTrue(decision.response_plan.should_respond)
        self.assertEqual(decision.response_plan.direct_reply, "どういたしまして。")
        self.assertEqual(decision.response_plan.target_length, "minimal")

    def test_one_to_one_gratitude_also_uses_direct_tiny_reply(self):
        self.speech("ポッポ、設定できたよ")
        self.assistant_said("うまく切り替わってよかった。")

        decision = self.speech("ありがとう", at=101.0)

        self.assertEqual(decision.response_plan.direct_reply, "どういたしまして。")

    def test_local_force_response_still_allows_natural_silence(self):
        orchestrator = ConversationOrchestrator(
            _Config(), wake_words=["ポッポ"], assistant_name="ポッポ",
            force_response=True,
        )
        orchestrator.response_started(source="local")
        orchestrator.response_finished(source="local", assistant_text="これで説明は終わりだよ。")
        result = orchestrator.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL,
            "local",
            text="うん、わかった",
            timestamp=101.0,
            metadata={"speaker_key": "local:mic"},
        ))

        self.assertFalse(result.response_plan.should_respond)


if __name__ == "__main__":
    unittest.main()
