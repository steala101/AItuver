import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from neuro_voice.dialogue import DialogueIntelligence
from neuro_voice.dialogue.conversation_critic import ConversationCritic
from neuro_voice.dialogue.conversation_planner import ConversationPlanner
from neuro_voice.dialogue.surface_realizer import SurfaceRealizer
from neuro_voice.dialogue.user_model import UserModel
from neuro_voice.dialogue.working_memory import WorkingMemory, normalize_persona_traits
from neuro_voice.llm.context_budget import estimate_tokens
from neuro_voice.dialogue.addressing import AddresseeDetector, AddressingAction
from neuro_voice.dialogue.state import ConversationState
from neuro_voice.privacy import PrivacyManager, Sensitivity, Visibility


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class ConversationPlannerTests(unittest.TestCase):
    def test_builds_four_reasoned_candidates(self):
        planner = ConversationPlanner(_Config())
        profile = {
            "topics": ["Discord", "音声対話"],
            "model": {
                "facts": {"interests": ["AI開発"], "current_projects": [], "preferences": []},
                "interaction_policy": {"response_length": "medium"},
            },
        }
        plan = planner.plan(
            "alice", "音声対話をもっと自然にしたい", profile,
            momentum=.72, memory_snippets=["前にDiscord音声受信を改善していた"],
            allow_follow_up=True, turn_index=1,
        )
        self.assertEqual(4, len(plan.candidates))
        self.assertEqual("current", plan.candidates[0].source)
        self.assertIn("long_memory", {item.source for item in plan.candidates})
        self.assertIn("user_model", {item.source for item in plan.candidates})

    def test_recent_style_and_shape_are_penalized(self):
        planner = ConversationPlanner(_Config())
        profile = {"topics": ["AI"]}
        first = planner.plan("alice", "AIって面白いね", profile, momentum=.7, turn_index=1)
        planner.commit(profile, "面白いね。", {})
        second = planner.plan("alice", "AIって面白いね", profile, momentum=.7, turn_index=2)
        self.assertNotEqual(first.primary_style, second.primary_style)
        self.assertNotEqual(first.response_shape, second.response_shape)

    def test_emotion_changes_with_inertia(self):
        planner = ConversationPlanner(_Config({
            "dialogue.conversation_generation.emotion_inertia": .8,
        }))
        profile = {"topics": []}
        planner.plan("alice", "今日は少し疲れた", profile, momentum=.3, turn_index=1)
        before = profile["conversation_generation"]["ai_emotion"]["valence"]
        planner.plan("alice", "やった、全部成功した！", profile, momentum=.8, turn_index=2)
        after = profile["conversation_generation"]["ai_emotion"]["valence"]
        self.assertGreater(after, before)
        self.assertLess(after - before, .25)  # one turn cannot flip the whole mood

    def test_persona_modules_affect_plan_without_becoming_spoken_labels(self):
        planner = ConversationPlanner(_Config())
        profile = {"topics": ["AI"], "model": {"interaction_policy": {"response_length": "medium"}}}
        traits = normalize_persona_traits({
            "humor": .9, "playfulness": .9, "cheekiness": .8, "spontaneity": .9,
            "contrarian": .85, "suggestion": .9,
        })
        plan = planner.plan(
            "alice", "この設計をどう改善する？", profile, momentum=.7,
            allow_follow_up=True, turn_index=3, persona_traits=traits,
        )
        self.assertEqual(traits, plan.persona_traits)
        # 数値そのものはプロンプトへ渡さない。SurfaceRealizer が同じ数値を読み、
        # 日本語の指示へ変換して渡している（二重に渡していた78tokを削除）。
        self.assertNotIn("人格モジュール", planner.prompt(plan))
        self.assertNotIn("curiosity=", planner.prompt(plan))
        surface = SurfaceRealizer().prompt(plan)
        self.assertIn("子供っぽさ", surface)
        self.assertIn("幼児語", surface)
        self.assertIn("一度だけ", surface)

    def test_childlike_traits_change_style_weights_without_forcing_a_catchphrase(self):
        high = ConversationPlanner._style_weights(
            "statement", "casual",
            normalize_persona_traits({
                "humor": .8, "playfulness": .9, "cheekiness": .8,
                "spontaneity": .9, "emotional_expression": .8,
            }),
        )
        low = ConversationPlanner._style_weights(
            "statement", "casual",
            normalize_persona_traits({
                "humor": .2, "playfulness": .2, "cheekiness": .2,
                "spontaneity": .2, "emotional_expression": .2,
            }),
        )
        self.assertGreater(high["playful"], low["playful"])
        self.assertGreater(high["energetic"], low["energetic"])
        self.assertGreater(high["opinionated"], low["opinionated"])

    def test_serious_context_suppresses_cheekiness(self):
        planner = ConversationPlanner(_Config())
        traits = normalize_persona_traits({
            "humor": .9, "playfulness": .9, "cheekiness": .9, "spontaneity": .9,
        })
        plan = planner.plan(
            "alice", "今日はつらくて落ち込んでる", {"topics": ["体調"]},
            momentum=.3, turn_index=3, persona_traits=traits,
        )
        surface = SurfaceRealizer().prompt(plan)
        self.assertEqual("support", plan.intent)
        self.assertIn("生意気さ", surface)
        self.assertIn("抑え", surface)
        self.assertNotIn("軽く張り合う", surface)

    def test_radio_request_is_planned_as_continuation_not_one_shot_question(self):
        planner = ConversationPlanner(_Config())
        profile = {"topics": ["AI"], "model": {"interaction_policy": {"response_length": "medium"}}}
        plan = planner.plan(
            "alice", "ラジオ風にしばらく喋ってくれない？",
            profile, momentum=.6, allow_follow_up=True, turn_index=4,
        )
        self.assertEqual("monologue", plan.intent)
        self.assertTrue(plan.continuation_requested)
        self.assertEqual("threaded_monologue", plan.response_shape)
        self.assertEqual("long", plan.target_length)
        self.assertFalse(plan.ask_follow_up)
        self.assertGreaterEqual(plan.minimum_moves, 3)

    def test_learned_long_preference_is_a_bias_not_a_permanent_long_reply(self):
        planner = ConversationPlanner(_Config())
        profile = {
            "topics": ["AI"],
            "model": {"interaction_policy": {"response_length": "long"}},
        }
        ordinary = planner.plan(
            "alice", "今日はどうだった？", profile,
            momentum=.5, turn_index=1,
        )
        acknowledgement = planner.plan(
            "alice", "うん", profile,
            momentum=.5, turn_index=2,
        )
        detailed = planner.plan(
            "alice", "その仕組みを詳しく説明して", profile,
            momentum=.5, turn_index=3,
        )
        self.assertEqual("medium", ordinary.target_length)
        self.assertEqual("short", acknowledgement.target_length)
        self.assertEqual("long", detailed.target_length)

    def test_one_off_detail_request_does_not_become_durable_user_preference(self):
        profile = {}
        UserModel.observe(profile, "今回はその仕組みを詳しく教えて")
        self.assertEqual(
            "medium",
            UserModel.ensure(profile)["interaction_policy"]["response_length"],
        )
        UserModel.observe(profile, "今後の説明はいつも長めでお願い")
        self.assertEqual(
            "long",
            UserModel.ensure(profile)["interaction_policy"]["response_length"],
        )


class WorkingMemoryTests(unittest.TestCase):
    def test_tracks_grounded_open_thread_and_revisits_only_later(self):
        profile = {}
        WorkingMemory.observe(
            profile, "Discordの音声認識をまだ改善して進めている", ["Discord", "音声認識"],
            now=100, turn_index=1,
        )
        snapshot = WorkingMemory.snapshot(profile)
        self.assertEqual("音声認識", snapshot["active_theme"])
        self.assertTrue(snapshot["open_questions"])
        self.assertIsNone(WorkingMemory.revisit_candidate(
            profile, intent="statement", momentum=.8, turn_index=2, topic_shift=.7, curiosity=.8,
        ))
        candidate = WorkingMemory.revisit_candidate(
            profile, intent="statement", momentum=.8, turn_index=6, topic_shift=.7, curiosity=.8,
        )
        self.assertIn("その後", candidate["question"])
        self.assertIsNone(WorkingMemory.revisit_candidate(
            profile, intent="question", momentum=.8, turn_index=6, topic_shift=.7, curiosity=.8,
        ))

    def test_resolved_topic_is_removed_from_open_questions(self):
        profile = {}
        WorkingMemory.observe(profile, "音声認識をまだ改善している", ["音声認識"], now=100, turn_index=1)
        WorkingMemory.observe(profile, "音声認識が完成した", ["音声認識"], now=101, turn_index=2)
        self.assertFalse(WorkingMemory.snapshot(profile)["open_questions"])

    def test_vague_memory_reference_does_not_become_an_open_question(self):
        profile = {}
        WorkingMemory.observe(
            profile, "さっき言ってたあの話をまた覚えてる",
            ["さっき言ってたあの話"], now=100, turn_index=1,
        )
        self.assertFalse(WorkingMemory.snapshot(profile)["open_questions"])


class GroupConversationAndPrivacyTests(unittest.TestCase):
    def test_group_open_question_waits_but_human_name_is_ignored(self):
        cfg = _Config({"group_conversation.enabled": True})
        detector = AddresseeDetector(cfg, wake_words=["ポッポ"], assistant_name="ポッポ")
        state = ConversationState(voice_human_count=3, participants={"a": "A", "b": "田中"})
        open_question = detector.decide("これってどうしたらいいかな？", state, speaker_key="a", now=10)
        self.assertEqual(AddressingAction.WAIT_FOR_HUMAN, open_question.decision)
        directed = detector.decide("田中、どう思う？", state, speaker_key="a", now=11)
        self.assertEqual(AddressingAction.IGNORE, directed.decision)

    def test_restricted_and_private_memory_never_enters_group_context(self):
        privacy = PrivacyManager(_Config())
        restricted = privacy.classify("パスワードは abc123")
        self.assertEqual(Sensitivity.RESTRICTED, restricted.sensitivity)
        self.assertFalse(restricted.should_store)
        private = {"sensitivity": "high", "visibility": "owner_and_ai"}
        self.assertFalse(privacy.allow_context(private, {"1", "2"}, group=True))
        self.assertEqual("今ここで私から付け加えることはないかな。", privacy.safe_output("token は abc"))


class ConversationCriticTests(unittest.TestCase):
    def test_human_quality_axes_are_explicitly_unmeasured(self):
        result = ConversationCritic().evaluate("短く答えるよ。", [])
        for axis in (
            "goal_focus", "personality_continuity", "memory_naturalness",
            "humor_fit", "emotional_fit", "topic_scope",
        ):
            self.assertIsNone(result.scores[axis])
            self.assertEqual("NOT_MEASURED", result.measurement_status[axis])
        self.assertEqual("HEURISTIC", result.measurement_status["opening_variety"])

    def test_detects_template_and_consecutive_question(self):
        critic = ConversationCritic()
        result = critic.evaluate(
            "そうだね。例えばこういう理由があるよ。きみはどう思う？",
            ["なるほど。だから面白いんだよね。きみはどう思う？"],
        )
        self.assertIn("canned_opening", result.issues)
        self.assertIn("consecutive_question", result.issues)
        self.assertIn("template_structure", result.issues)
        self.assertTrue(result.consecutive_question)

    def test_feedback_changes_next_surface_policy(self):
        critic = ConversationCritic()
        feedback = critic.feedback_prompt({
            "issues": ["repeated_opening", "consecutive_question", "too_similar"],
        })
        self.assertIn("同じ相槌", feedback)
        self.assertIn("質問で締めず", feedback)
        self.assertIn("違う観点", feedback)


class ConversationGenerationIntegrationTests(unittest.TestCase):
    def test_recall_evidence_and_response_identity_reach_the_plan_contract(self):
        with TemporaryDirectory() as tmp:
            intelligence = DialogueIntelligence(_Config(), Path(tmp) / "dialogue.json")
            intelligence.observe_turn("alice", "前におすすめしたゲームって何？", now=100)
            prompt = intelligence.prompt_context(
                "alice", "前におすすめしたゲームって何？",
                memory_snippets=["前回はゲームAを候補に挙げた"],
                response_id="r-recall", source="discord",
                recall_requested=True, recall_grounded=True,
            )
            plan = intelligence.current_plan("alice")
            self.assertEqual("HONEST_RECALL", plan["conversation_move"])
            self.assertEqual("r-recall", plan["response_id"])
            self.assertEqual("discord", plan["source"])
            self.assertTrue(plan["recall_grounded"])
            self.assertIn("取得できた具体的記録", prompt)

    def test_planner_realizer_critic_are_in_internal_prompt_and_persisted(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "dialogue.json"
            intelligence = DialogueIntelligence(_Config(), path)
            intelligence.observe_turn("alice", "AIの会話をまだ自然に改善している", now=100)
            prompt = intelligence.prompt_context(
                "alice", "AIの会話を自然にしたい",
                memory_snippets=["Discordの会話改善を進めていた"],
            )
            self.assertIn("Conversation Planner", prompt)
            self.assertIn("選択話題=", prompt)
            self.assertIn("Surface Realizer", prompt)
            self.assertIn("Working Memory", prompt)
            self.assertNotIn("Persona Modules:", prompt)
            self.assertIn("上記は発話でなく内部方針", prompt)
            self.assertLessEqual(
                estimate_tokens(prompt), 1500,
                "対話制御コンテキストが再び肥大化している",
            )
            intelligence.record_response("alice", "なるほど。例えばこうできるよ。次はどうする？")
            snapshot = intelligence.snapshot("alice")["conversation_generation"]
            self.assertTrue(snapshot["last_plan"])
            self.assertTrue(snapshot["last_critique"])
            self.assertTrue(snapshot["recent_styles"])
            self.assertTrue(intelligence.snapshot("alice")["working_memory"]["continuing_thought"])
            restored = DialogueIntelligence(_Config(), path)
            restored_snapshot = restored.snapshot("alice")["conversation_generation"]
            self.assertEqual(snapshot["recent_styles"], restored_snapshot["recent_styles"])

    def test_recent_real_reply_openings_are_turn_local_and_bounded(self):
        with TemporaryDirectory() as tmp:
            intelligence = DialogueIntelligence(_Config({
                "dialogue.conversation_generation.recent_reply_avoidance.enabled": True,
                "dialogue.conversation_generation.recent_reply_avoidance.count": 2,
                "dialogue.conversation_generation.recent_reply_avoidance.max_chars": 24,
                "dialogue.conversation_generation.recent_reply_avoidance.max_total_chars": 180,
            }), Path(tmp) / "dialogue.json")
            intelligence.observe_turn("alice", "AIって面白いね", now=100)
            intelligence.prompt_context("alice", "AIって面白いね")
            intelligence.record_response(
                "alice",
                "それ、かなり面白い視点だね。続きを考えると別の可能性もあるよ。",
            )
            prompt = intelligence.prompt_context("alice", "もう少し話そう")
            self.assertIn("直近の自分の発話・今回だけの重複回避材料", prompt)
            self.assertIn("それ、かなり面白い視点だね。", prompt)
            self.assertNotIn("続きを考えると別の可能性もあるよ", prompt)
            guidance = intelligence.snapshot("alice")[
                "conversation_generation"
            ]["recent_reply_guidance"]
            self.assertTrue(guidance["enabled"])
            self.assertEqual(1, guidance["examples"])
            self.assertLessEqual(guidance["chars"], 180)

    def test_recent_reply_avoidance_has_a_clean_control_arm(self):
        with TemporaryDirectory() as tmp:
            intelligence = DialogueIntelligence(_Config({
                "dialogue.conversation_generation.recent_reply_avoidance.enabled": False,
            }), Path(tmp) / "dialogue.json")
            intelligence.observe_turn("alice", "AIって面白いね", now=100)
            intelligence.prompt_context("alice", "AIって面白いね")
            intelligence.record_response("alice", "それ、かなり面白い視点だね。")
            prompt = intelligence.prompt_context("alice", "もう少し話そう")
            self.assertNotIn("直近の自分の発話・今回だけの重複回避材料", prompt)
            guidance = intelligence.snapshot("alice")[
                "conversation_generation"
            ]["recent_reply_guidance"]
            self.assertFalse(guidance["enabled"])
            self.assertEqual(0, guidance["examples"])

    def test_recent_reply_context_is_never_injected_into_group_prompt(self):
        with TemporaryDirectory() as tmp:
            intelligence = DialogueIntelligence(_Config({
                "dialogue.conversation_generation.recent_reply_avoidance.enabled": True,
            }), Path(tmp) / "dialogue.json")
            intelligence.observe_turn("alice", "個別の話", now=100)
            intelligence.prompt_context("alice", "個別の話")
            intelligence.record_response("alice", "個別会話でだけ使う返答。")
            prompt = intelligence.prompt_context(
                "alice", "グループでの発話",
                allow_recent_reply_context=False,
            )
            self.assertNotIn("個別会話でだけ使う返答", prompt)
            guidance = intelligence.snapshot("alice")[
                "conversation_generation"
            ]["recent_reply_guidance"]
            self.assertFalse(guidance["enabled"])
            self.assertEqual(0, guidance["examples"])

    def test_voice_direction_comes_from_planner(self):
        with TemporaryDirectory() as tmp:
            intelligence = DialogueIntelligence(_Config(), Path(tmp) / "dialogue.json")
            intelligence.observe_turn("alice", "やった、成功した！", now=100)
            intelligence.prompt_context("alice", "やった、成功した！")
            direction = intelligence.voice_direction("alice")
            self.assertIn(direction["delivery"], {
                "neutral", "warm", "lively", "playful", "soft", "serious", "gentle", "calm",
            })
            self.assertIn(direction["emotion"], {"neutral", "joy", "fun", "sad", "surprised"})


if __name__ == "__main__":
    unittest.main()
