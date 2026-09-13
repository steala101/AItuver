import unittest

import pytest

from neuro_voice.dialogue.kernel import (
    ConversationKernel, DialogueObligation, ReferenceStatus,
)


class ConversationKernelTests(unittest.TestCase):
    def test_context_reference_detection_ignores_explicit_search(self):
        self.assertTrue(ConversationKernel.is_context_reference(
            "さっきおすすめした新作ゲームって何？",
        ))
        self.assertFalse(ConversationKernel.is_context_reference(
            "さっきのゲームをWebで検索して",
        ))

    def test_repair_outranks_question_and_disables_search(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "違うよ。さっき言ったゲームは何だった？",
            source="local",
            speaker_id="local:mic",
        )
        self.assertEqual(frame.obligations[0], DialogueObligation.REPAIR.value)
        self.assertIn(DialogueObligation.ANSWER.value, frame.obligations)
        self.assertEqual(frame.tool_policy, {
            "allow_search": False,
            "reason": "conversation_repair_first",
        })

    def test_context_reference_uses_memory_before_search(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "おすすめした新作ゲームって何？",
            source="local",
            speaker_id="local:mic",
            memories=[{
                "text": "前の会話でポッポは新作ゲームとしてExample Questを勧めた",
                "score": .91,
                "mention_allowed": True,
            }],
        )
        self.assertIn(DialogueObligation.RESOLVE_REFERENCE.value, frame.obligations)
        self.assertFalse(frame.tool_policy["allow_search"])
        self.assertEqual(
            frame.memory_influences[0].effect,
            "指示対象や過去の事実を照合する",
        )

    def test_explicit_search_remains_available(self):
        policy = ConversationKernel.tool_policy_for("最新情報をWebで検索して")
        self.assertTrue(policy["allow_search"])
        self.assertEqual(policy["reason"], "explicit_search_request")

    def test_activity_state_is_rendered_as_canonical_context(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "お米",
            source="discord",
            speaker_id="discord:1",
            activity={
                "activity_name": "日本語しりとり",
                "status": "WAITING_FOR_AI",
                "current_actor_id": "assistant",
                "public_state": {
                    "previous_word": "こめ",
                    "required_kana": "め",
                },
            },
        )
        prompt = kernel.prompt(frame)
        self.assertIn(DialogueObligation.CONTINUE_ACTIVITY.value, frame.obligations)
        self.assertIn("required=め", prompt)
        self.assertFalse(frame.tool_policy["allow_search"])

    def test_trace_ids_survive_generation_and_final_output(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "今日は何をする？",
            source="local",
            speaker_id="local:mic",
            utterance_id="utt-1",
            response_id="res-1",
            conversation_id="conversation-1",
        )
        self.assertNotEqual(frame.turn_id, frame.decision_id)
        self.assertEqual(frame.utterance_id, "utt-1")
        self.assertTrue(kernel.record_generation(
            source="local", response_id="res-1", generation_id="gen-1",
        ))
        self.assertTrue(kernel.finalize_output(
            "散歩に行きたいな。", source="local", response_id="res-1",
        ))
        trace = kernel.snapshot("local")["decision_trace"]
        self.assertEqual(trace["response_id"], "res-1")
        self.assertEqual(trace["generation_id"], "gen-1")
        self.assertEqual(trace["final_output_summary"], "散歩に行きたいな。")

    def test_ambiguous_reference_requires_clarification_and_never_searches(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "それって何？",
            source="local",
            speaker_id="local:mic",
            reference_candidates=[
                {"id": 1, "assistant_text": "Aの話", "score": 0.9},
                {"id": 2, "assistant_text": "Bの話", "score": 0.87},
            ],
        )
        self.assertEqual(
            frame.reference_resolution.status,
            ReferenceStatus.AMBIGUOUS.value,
        )
        self.assertTrue(frame.reference_resolution.requires_clarification)
        self.assertEqual(frame.response_shape, "clarify_reference")
        self.assertFalse(frame.search_decision.allowed)

    def test_latest_grounded_turn_resolves_when_recency_is_clear(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "それってどんなゲーム？",
            source="local",
            speaker_id="local:mic",
            reference_candidates=[
                {"id": 1, "assistant_text": "古い候補", "score": 0.70},
                {
                    "id": 2,
                    "assistant_text": "「Example Quest」がおすすめだよ",
                    "score": 0.98,
                },
            ],
        )
        self.assertEqual(
            frame.reference_resolution.status,
            ReferenceStatus.RESOLVED.value,
        )
        self.assertIn(
            "Example Quest", frame.reference_resolution.resolved_value,
        )
        self.assertFalse(frame.search_decision.allowed)

    def test_explicit_correction_supersedes_prior_reference(self):
        kernel = ConversationKernel()
        corrected = kernel.build(
            "違う、Bのこと",
            source="local",
            speaker_id="local:mic",
            reference_candidates=[
                {"id": 1, "assistant_text": "Aの話", "score": 0.99},
            ],
        )
        self.assertEqual(
            corrected.reference_resolution.status,
            ReferenceStatus.RESOLVED.value,
        )
        self.assertEqual(corrected.reference_resolution.resolved_value, "B")
        followup = kernel.build(
            "それはどう思う？",
            source="local",
            speaker_id="local:mic",
        )
        self.assertEqual(followup.reference_resolution.resolved_value, "B")
        self.assertFalse(followup.search_decision.allowed)

    def test_underspecified_current_fact_does_not_auto_search(self):
        decision = ConversationKernel.search_decision_for("今いくらなんだろう")
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_clarification)

    def test_protected_decision_cannot_be_overwritten_without_reason(self):
        kernel = ConversationKernel()
        frame = kernel.build(
            "それは何？", source="local", speaker_id="local:mic",
        )
        with self.assertLogs(
            "neuro_voice.dialogue.kernel", level="ERROR",
        ) as captured:
            changed = kernel.guarded_transition(
                {"response_shape": "unrelated"},
                response_id=frame.response_id,
                source="local",
            )
        self.assertFalse(changed)
        self.assertIn("KERNEL_DECISION_OVERRIDDEN", "\n".join(captured.output))
        self.assertNotEqual(frame.response_shape, "unrelated")

    def test_local_and_discord_share_decision_semantics(self):
        kernel = ConversationKernel()
        local = kernel.build(
            "違う、それじゃなくてBのこと",
            source="local", speaker_id="speaker:1",
        )
        discord = kernel.build(
            "違う、それじゃなくてBのこと",
            source="discord", speaker_id="speaker:1",
        )
        self.assertEqual(local.obligations, discord.obligations)
        self.assertEqual(local.decision_priority, discord.decision_priority)
        self.assertEqual(local.response_shape, discord.response_shape)
        self.assertEqual(
            local.search_decision.reason_code,
            discord.search_decision.reason_code,
        )


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 連体詞の部分一致
#
# 「今の」「前の」「さっきの」を裸で並べていたせいで、**ただの感想**が
# 会話参照と判定されていた。Kernelはそれを受けて
# `shape=clarify_reference` / `goal="Ask one concrete clarification question"`
# を「このターンの権威ある決定」として渡す。ポッポがそこで必ず聞き返すのは
# そのためで、同じ入力なら毎回同じ命令になるので**書き出しも固まる**。
#
# 2026-07-29にCodexが直した「話してても」の部分一致と同じ形。
# 連体詞は後ろの名詞まで見ないと、指示語かどうか決まらない。
# ---------------------------------------------------------------------------


def _shape(text: str) -> str:
    kernel = ConversationKernel()
    frame = kernel.build(
        text, source="local", speaker_id="u", momentum=.5, conversation_id="u",
    )
    return frame.response_shape


@pytest.mark.parametrize("text", [
    "なんというか、説明しにくいんだけど、今のAIって話しててもAI感が強いんだよね",
    "今のゲームって難しくなったよね",
    "前のパソコンより速い",
    "さっきのラーメン美味しかった",
])
def test_a_demonstrative_before_a_plain_noun_is_not_a_reference(text):
    """「今のAI」の「今の」は会話を指していない。聞き返させない。"""
    assert not ConversationKernel.is_context_reference(text), text
    assert _shape(text) != "clarify_reference", text


@pytest.mark.parametrize("text", [
    "さっきの話の続き",
    "前のやつもう一回",
    "今の、もう一回言って",
    "さっきの説明がよく分からなかった",
    "それってどういうこと？",
])
def test_a_real_reference_still_resolves(text):
    """本物の参照まで落とさない。"""
    assert ConversationKernel.is_context_reference(text), text


def test_the_script_line_that_froze_the_opening():
    """検証台本の1番。ここだけ毎回同じ書き出しになっていた。

    `tools/check_opening_diversity.py` のKernelアームで、詰まった位置は
    8つ中この1つだけだった。
    """
    assert _shape(
        "なんというか、説明しにくいんだけど、今のAIって話しててもAI感が強いんだよね"
    ) == "conversational"
