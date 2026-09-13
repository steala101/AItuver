import asyncio
import unittest

from neuro_voice.autonomy import (
    AutonomousActionSystem,
    AutonomyEvent,
    AutonomyEventType,
    AutonomyHeartbeatScheduler,
)
from neuro_voice.autonomy.types import ActionType


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class AutonomyHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, **extra):
        values = {
            "autonomous_action_system.enabled": True,
            "legacy_spontaneous_mode.enabled": False,
            "autonomy.heartbeat_enabled": True,
            "autonomy.heartbeat_interval_ms": 10,
            "autonomy.silence_evaluation_min_ms": 0,
            "autonomy.silence_reevaluation_ms": 0,
            "autonomy.open_thread_default_ttl_minutes": 30,
        }
        values.update(extra)
        return _Config(values)

    async def test_scheduler_continues_after_do_nothing_and_callback_error(self):
        calls = []

        async def callback(heartbeat_id):
            calls.append(heartbeat_id)
            if len(calls) == 1:
                raise RuntimeError("test failure")

        scheduler = AutonomyHeartbeatScheduler(self._config(), callback, name="test")
        scheduler.start()
        await asyncio.sleep(.06)
        await scheduler.stop()
        health = scheduler.health_status()
        self.assertGreaterEqual(health["heartbeat_fired_count"], 2)
        self.assertGreaterEqual(health["heartbeat_failures"], 1)

    async def test_silence_is_reevaluated_after_initial_do_nothing(self):
        system = AutonomousActionSystem(self._config())
        system.note_human_speech_ended(now=100)
        initial = system.heartbeat(source="test", group=False, human_speaking=False,
                                   assistant_busy=False, now=105)
        self.assertEqual(ActionType.DO_NOTHING, initial.action_type)

        system.state.open_thread(
            topic="later", summary="later", owner_user_id="user",
            callback_after_s=0, now=106,
        )
        later = system.heartbeat(source="test", group=False, human_speaking=False,
                                 assistant_busy=False, now=125)
        self.assertEqual(ActionType.RECALL_OPEN_THREAD, later.action_type)
        self.assertTrue(later.should_call_llm)

    async def test_group_idle_waits_for_configured_silence(self):
        system = AutonomousActionSystem(self._config(**{"autonomy.group_idle_min_silence_ms": 30000}))
        system.state.open_thread(topic="later", summary="later", owner_user_id="user",
                                 callback_after_s=0, now=100)
        system.note_human_speech_ended(now=100)
        decision = system.heartbeat(source="test", group=True, human_speaking=False,
                                    assistant_busy=False, now=110)
        self.assertEqual(ActionType.DO_NOTHING, decision.action_type)

    async def test_human_speech_resets_silence_clock(self):
        system = AutonomousActionSystem(self._config())
        system.note_human_speech_ended(now=100)
        system.note_human_speech_started(now=120)
        decision = system.heartbeat(source="test", group=False, human_speaking=False,
                                    assistant_busy=False, now=121)
        self.assertEqual(ActionType.DO_NOTHING, decision.action_type)
        self.assertEqual(0, decision.details.get("silence_duration_ms", 0))

    async def test_requested_tick_wakes_long_interval_scheduler(self):
        calls = []

        async def callback(heartbeat_id):
            calls.append(heartbeat_id)

        scheduler = AutonomyHeartbeatScheduler(
            self._config(**{"autonomy.heartbeat_interval_ms": 10_000}),
            callback, name="test",
        )
        scheduler.start()
        scheduler.request_tick(delay_s=.01)
        await asyncio.sleep(.06)
        await scheduler.stop()
        self.assertTrue(calls)
