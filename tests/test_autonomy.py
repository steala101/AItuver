import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock

from neuro_voice.autonomy import AutonomousActionSystem, AutonomyEvent, AutonomyEventType
from neuro_voice.autonomy.types import ActionType, PrivacyScope
from neuro_voice.discord_bridge.bot import DiscordBridge
from neuro_voice.pipeline import VoicePipeline


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class AutonomousActionSystemTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _Config({
            "autonomous_action_system.enabled": True,
            "legacy_spontaneous_mode.enabled": False,
            "autonomy.silence_evaluation_min_ms": 0,
            "autonomy.silence_reevaluation_ms": 0,
            # Legacy recall tests opt into the feature explicitly. The shipped
            # application config disables unsolicited past-topic callbacks.
            "autonomy.open_thread_callbacks_enabled": True,
        })

    def test_acknowledgement_prefers_non_speaking_action(self):
        system = AutonomousActionSystem(self.cfg)
        decision = system.publish(AutonomyEvent(
            AutonomyEventType.HUMAN_UTTERANCE_FINALIZED, "test", payload={"text": "うん"},
        ))
        self.assertIn(decision.action_type, {ActionType.DO_NOTHING, ActionType.UPDATE_MEMORY})
        self.assertFalse(decision.should_speak)

    def test_explicit_question_is_answer_candidate(self):
        system = AutonomousActionSystem(self.cfg)
        decision = system.publish(AutonomyEvent(
            AutonomyEventType.HUMAN_UTTERANCE_FINALIZED, "test", payload={"text": "これどうするの？"},
        ))
        self.assertEqual(ActionType.ANSWER, decision.action_type)
        self.assertTrue(decision.should_speak)

    def test_deferred_topic_is_recalled_only_at_a_later_silence(self):
        system = AutonomousActionSystem(self.cfg)
        system.publish(AutonomyEvent(
            AutonomyEventType.HUMAN_UTTERANCE_FINALIZED, "test",
            payload={"text": "設定は後で確認する"}, timestamp=100,
        ))
        self.assertEqual(1, len(system.state.open_threads))
        early = system.publish(AutonomyEvent(
            AutonomyEventType.SILENCE_CONTINUED, "test", timestamp=105,
        ))
        self.assertEqual(ActionType.DO_NOTHING, early.action_type)
        later = system.publish(AutonomyEvent(
            AutonomyEventType.SILENCE_CONTINUED, "test", timestamp=200,
        ))
        self.assertEqual(ActionType.RECALL_OPEN_THREAD, later.action_type)
        self.assertTrue(later.should_call_llm)

    def test_grounded_working_memory_followup_becomes_recallable(self):
        system = AutonomousActionSystem(self.cfg)
        self.assertTrue(system.add_grounded_followup(
            topic="実装中の機能", summary="その後はどうなった？",
            owner_user_id="local:mic", callback_after_s=0,
        ))
        self.assertEqual(1, system.metrics["open_threads_created"])
        thread = system.state.open_threads[0]
        decision = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=thread.callback_earliest_at + 1,
        )
        self.assertEqual(ActionType.RECALL_OPEN_THREAD, decision.action_type)

    def test_open_thread_callback_can_be_disabled_without_losing_thread(self):
        cfg = _Config({
            **self.cfg.values,
            "autonomy.open_thread_callbacks_enabled": False,
        })
        system = AutonomousActionSystem(cfg)
        system.state.open_thread(
            topic="実装中の機能", summary="状態を確認する",
            owner_user_id="local:mic", callback_after_s=0, now=100,
        )
        decision = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=200,
        )
        self.assertEqual(ActionType.DO_NOTHING, decision.action_type)
        self.assertEqual(1, len(system.state.open_threads))

    def test_private_thread_is_not_recalled_in_group(self):
        system = AutonomousActionSystem(self.cfg)
        system.state.open_thread(topic="private", summary="private", owner_user_id="a",
                                 privacy_scope=PrivacyScope.CURRENT_CONVERSATION,
                                 callback_after_s=0, now=100)
        decision = system.publish(AutonomyEvent(
            AutonomyEventType.SILENCE_CONTINUED, "test", timestamp=200,
        ), group=True)
        self.assertEqual(ActionType.DO_NOTHING, decision.action_type)

    def test_stale_event_is_discarded(self):
        system = AutonomousActionSystem(self.cfg)
        event = AutonomyEvent(AutonomyEventType.TOOL_RESULT, "test", payload={"result": "done"},
                              timestamp=0, expires_at=1)
        decision = system.publish(event)
        self.assertEqual(ActionType.DO_NOTHING, decision.action_type)
        self.assertEqual("expired_or_duplicate", decision.reason_code)

    def test_invalid_structured_llm_plan_falls_back_to_none(self):
        self.assertIsNone(AutonomousActionSystem.validate_llm_action("not json"))
        valid = AutonomousActionSystem.validate_llm_action(
            '{"action":{"type":"comment","reason_code":"event"},"speech":{"should_speak":true,"text":"見つけたよ"}}'
        )
        self.assertEqual(ActionType.COMMENT, valid["action_type"])

    def test_vague_past_topic_is_not_registered_for_callback(self):
        system = AutonomousActionSystem(self.cfg)
        self.assertFalse(system.add_grounded_followup(
            topic="さっき言ってたあの話", summary="その後どうなった？",
            owner_user_id="local:mic",
        ))
        self.assertFalse(system.state.open_threads)

    def test_explicit_radio_request_creates_coherent_continuation_before_callbacks(self):
        system = AutonomousActionSystem(self.cfg)
        now = time.monotonic()
        system.state.open_thread(
            topic="古い実装", summary="今どこまで進んだ？",
            owner_user_id="user", callback_after_s=0, now=now - 20,
        )
        self.assertTrue(system.note_conversation_turn(
            "ラジオ風にしばらく喋って", "まずAIの進化の話をしよう。", now=now,
        ))
        system.note_human_speech_ended(now=now)
        decision = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 3,
        )
        self.assertEqual(ActionType.START_TOPIC, decision.action_type)
        self.assertEqual("explicit_continuation", decision.trigger_reason.value)
        self.assertIn("まずAIの進化", decision.details["previous_segment"])
        system.mark_speech_started(decision, now=now + 3)
        system.record_autonomous_response(decision, "次は、人間らしさと間の取り方の話。")
        next_decision = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 6,
        )
        self.assertEqual(ActionType.START_TOPIC, next_decision.action_type)
        self.assertIn("人間らしさと間", next_decision.details["previous_segment"])

    def test_idle_topic_can_be_present_time_reflection_not_open_question(self):
        system = AutonomousActionSystem(self.cfg)
        now = time.monotonic()
        system.update_context({
            "active_theme": "音声AIの自然な会話",
            "interests": ["リアルタイム対話"],
            "time_context": "夜・21時台",
        })
        system.note_human_speech_ended(now=now)
        decision = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 31,
        )
        self.assertEqual(ActionType.START_TOPIC, decision.action_type)
        self.assertEqual("self_initiated_topic", decision.trigger_reason.value)
        self.assertNotEqual("open_thread", decision.details.get("kind"))

    def test_idle_focus_rotates_and_does_not_repeat_consumed_subject(self):
        system = AutonomousActionSystem(self.cfg)
        now = time.monotonic()
        system.update_context({
            "active_theme": "音声AIの自然な会話",
            "interests": ["相互に質問する対話"],
        })
        system.note_human_speech_ended(now=now)
        first = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 31,
        )
        self.assertEqual("音声AIの自然な会話", first.details["focus"])
        system.mark_speech_started(first, now=now + 31)
        system.record_autonomous_response(
            first, "私は応答だけじゃなく、一緒に話題を育てたいな。", now=now + 31,
        )

        second = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 70,
        )
        self.assertEqual(ActionType.START_TOPIC, second.action_type)
        self.assertEqual("相互に質問する対話", second.details["focus"])
        self.assertNotEqual(first.details["focus_signature"], second.details["focus_signature"])

    def test_single_idle_focus_is_not_repeated_after_it_was_spoken(self):
        system = AutonomousActionSystem(self.cfg)
        now = time.monotonic()
        system.update_context({"active_theme": "音声AIの自然な会話"})
        system.note_human_speech_ended(now=now)
        first = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 31,
        )
        system.mark_speech_started(first, now=now + 31)
        system.record_autonomous_response(
            first, "この会話、もっと往復を作りたいな。", now=now + 31,
        )
        repeated = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 70,
        )
        self.assertEqual(ActionType.DO_NOTHING, repeated.action_type)

    def test_grounded_question_move_is_bounded_and_visible_in_prompt(self):
        cfg = _Config({
            **self.cfg.values,
            "autonomy.max_self_initiated_questions_per_5min": 1,
            "autonomy.question_cooldown_ms": 0,
        })
        system = AutonomousActionSystem(cfg)
        now = time.monotonic()
        system.update_context({
            "active_theme": "人間らしい対話",
            "interests": ["会話で何を大切にするか"],
        })
        system.note_human_speech_ended(now=now)
        first = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 31,
        )
        self.assertTrue(first.details["question_allowed"])
        self.assertIn("具体的な質問を一つ", system.autonomous_prompt(first))
        system.mark_speech_started(first, now=now + 31)
        system.record_autonomous_response(
            first, "私は相互に考えを出すのが対話だと思う。きみは何を一番大事にする？",
            now=now + 31,
        )
        second = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=now + 70,
        )
        self.assertFalse(second.details["question_allowed"])

    def test_radio_stop_suppresses_idle_speech_and_clears_continuation(self):
        system = AutonomousActionSystem(self.cfg)
        system.update_context({
            "active_theme": "AIの進化",
            "interests": ["リアルタイム対話"],
        })
        self.assertTrue(system.note_conversation_turn(
            "ラジオ風にしばらく喋って", "一本目の話。", now=100,
        ))
        self.assertFalse(system.note_conversation_turn(
            "いや、もうラジオは終わったよ。", now=110,
        ))
        self.assertFalse(system.continuation_active)
        blocked = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=141,
        )
        self.assertEqual(ActionType.DO_NOTHING, blocked.action_type)
        self.assertTrue(blocked.reason_code.startswith("quiet_after_stop:"))

        # The same grounded present-time context can become eligible again
        # after the configured quiet window, rather than being deleted.
        resumed = system.heartbeat(
            source="local", group=False, human_speaking=False,
            assistant_busy=False, now=411,
        )
        self.assertEqual(ActionType.START_TOPIC, resumed.action_type)


class HeartbeatOwnershipTests(unittest.TestCase):
    @staticmethod
    def _autonomy():
        state = SimpleNamespace(active=True)

        def configure(*, enabled):
            state.active = bool(enabled)

        state.configure = configure
        return state

    def test_local_speech_autonomy_off_keeps_research_heartbeat_alive(self):
        heartbeat = Mock()
        host = SimpleNamespace(
            _autonomy=self._autonomy(),
            _proactive_enabled=True,
            _loop=object(),
            _autonomy_heartbeat=heartbeat,
            _schedule_next_proactive=Mock(),
            _emit=Mock(),
        )
        VoicePipeline.set_proactive(host, False)
        self.assertFalse(host._autonomy.active)
        heartbeat.resume.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.pause.assert_not_called()

    def test_discord_speech_autonomy_off_keeps_research_heartbeat_alive(self):
        heartbeat = Mock()
        host = SimpleNamespace(
            _autonomy=self._autonomy(),
            _initiative_policy=SimpleNamespace(configure=Mock()),
            _last_proactive_at=12.0,
            _loop=object(),
            _autonomy_heartbeat=heartbeat,
            _emit=Mock(),
        )
        DiscordBridge.set_proactive(host, False)
        self.assertFalse(host._autonomy.active)
        self.assertEqual(0.0, host._last_proactive_at)
        heartbeat.resume.assert_called_once()
        heartbeat.start.assert_called_once()
        heartbeat.pause.assert_not_called()
