from __future__ import annotations

import asyncio
import io
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from neuro_voice.media import MediaInput, MediaType
from neuro_voice.vision.media import prepare_image
from neuro_voice.vision.models import PerceptionMemory, VisualObservation
from neuro_voice.vision.service import (
    PerceptionService, VisualAnalyzer, compact_reactive_visual_messages,
    is_explicit_vision_query, is_realtime_game_query,
)
from neuro_voice.vision.video import VideoObservationService


class RealtimeVisionTests(unittest.TestCase):
    @staticmethod
    def _image(color=(20, 30, 40)):
        from PIL import Image

        out = io.BytesIO()
        Image.new("RGB", (64, 36), color).save(out, format="JPEG")
        return prepare_image(out.getvalue(), media_type=MediaType.VIDEO_FRAME)

    def test_game_situation_phrases_use_realtime_state(self):
        self.assertTrue(is_realtime_game_query("今ゾンビに襲われてるんだけどどうしたらいい？"))
        self.assertTrue(is_realtime_game_query("今の状況は？"))
        self.assertTrue(is_realtime_game_query("危ない、助けて！"))
        self.assertFalse(is_realtime_game_query("明日の天気は？"))

    def test_screen_text_requests_are_explicit_vision_queries(self):
        self.assertTrue(is_explicit_vision_query("画面の中の文字を読んで"))
        self.assertTrue(is_explicit_vision_query("何て書いてある？"))
        self.assertTrue(is_explicit_vision_query("今、画面見てる？"))

    def test_memory_builds_chronological_flow_with_age(self):
        memory = PerceptionMemory()
        memory.record(VisualObservation(
            summary="森を移動中", captured_at=100.0,
            scene_changes=["森へ入った"], game="minecraft",
        ))
        memory.record(VisualObservation(
            summary="ゾンビと戦闘中", captured_at=102.0,
            scene_changes=["ゾンビが接近"], notable_events=["戦闘開始"],
            game="minecraft",
        ))
        context = memory.timeline_prompt_context(
            now=105.0, latest_frame_at=104.5, motion_score=.31,
        )
        self.assertIn("3.0秒前", context or "")
        self.assertIn("ゾンビと戦闘中", context or "")
        self.assertIn("ゾンビが接近", context or "")
        self.assertIn("画面変化量=0.31", context or "")

    def test_latest_frame_wins_while_short_raw_history_is_separate(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "obs", "video.max_queue_size": 12,
                    "video.obs.capture_fps": 1.0,
                }.get(key, default)

            def section(self, _name):
                return {}

        service = VideoObservationService(Config(), object())
        first = self._image()
        second = self._image()
        second.timestamp = (first.timestamp or time.time()) + 1.0
        service._append(first)
        service._append(second)
        self.assertEqual(service.captured_frame_count, 2)
        self.assertEqual(len(service._frames), 1)
        self.assertEqual(service.latest_frame.data, second.data)
        self.assertEqual(len(service._raw_history), 2)
        self.assertEqual(service.dropped_frame_count, 1)
        self.assertEqual(service.last_motion_score, 0.0)

    def test_visual_memory_expires_old_events_and_deduplicates(self):
        memory = PerceptionMemory(memory_seconds=30)
        first = VisualObservation(
            summary="ゾンビが接近", captured_at=100,
            scene_changes=["右側にゾンビが現れた"], confidence=.8,
        )
        memory.record(first)
        memory.record(VisualObservation(
            summary="まだゾンビがいる", captured_at=101,
            scene_changes=["右側にゾンビが現れた"], confidence=.8,
        ))
        self.assertEqual(len(memory.get_recent_events(30, now=101)), 1)
        memory.record(VisualObservation(
            summary="安全な場所", captured_at=140,
            scene_changes=["ゾンビから逃げ切った"], confidence=.9,
        ))
        events = memory.get_recent_events(30, now=140)
        self.assertEqual([item.summary for item in events], ["ゾンビから逃げ切った"])

    def test_visual_context_is_omitted_for_unrelated_chat(self):
        memory = PerceptionMemory()
        memory.record(VisualObservation(
            summary="洞窟で採掘中", captured_at=time.time(),
            game="minecraft", conversation_relevance="none",
        ))
        self.assertIsNone(memory.get_context_for_dialogue("明日の天気は？"))
        self.assertIn("洞窟で採掘中", memory.get_context_for_dialogue("今の画面どう？") or "")

    def test_malformed_vision_json_falls_back_without_crashing(self):
        observation = VisualObservation.from_model_text("```json\n{broken\n```")
        self.assertIn("broken", observation.summary)

    def test_adaptive_sampling_slows_static_scenes(self):
        class Config:
            def get(self, key, default=None):
                return {
                    "video.input": "obs",
                    "vision.analysis_min_interval_sec": 1.0,
                    "vision.analysis_max_interval_sec": 5.0,
                    "vision.change_threshold": .15,
                    "video.scene_change_threshold": .4,
                }.get(key, default)

            def section(self, _name):
                return {}

        service = VideoObservationService(Config(), object())
        service._last_motion_score = .01
        self.assertEqual(service._adaptive_interval(), 5.0)
        service._last_motion_score = .8
        self.assertEqual(service._adaptive_interval(), 1.0)

    def test_dialogue_timeout_preserves_single_inflight_visual_analysis(self):
        """A short reply deadline must not cancel and restart cold vision."""

        class Config:
            def get(self, key, default=None):
                return {"video.input": "obs"}.get(key, default)

            def section(self, _name):
                return {}

        class Perception:
            def __init__(self):
                self.calls = 0
                self.release = asyncio.Event()
                self.last_was_cache_hit = False
                self.last_metrics = {}

            async def analyze(self, frames, **_kwargs):
                self.calls += 1
                await self.release.wait()
                return VisualObservation(
                    summary="共有画面を解析済み",
                    captured_at=frames[-1].timestamp,
                )

            def cancel_background(self):
                return None

        async def scenario():
            perception = Perception()
            service = VideoObservationService(Config(), perception)
            frame = self._image()

            async def capture(*, request_id=""):
                frame.metadata["explicit_request_id"] = request_id
                service._append(frame)
                return frame

            service.capture_current_frame = capture
            observation, pending = await service.ensure_current_analysis(
                request_id="first", timeout_s=.01,
            )
            self.assertIsNone(observation)
            self.assertTrue(pending)
            self.assertEqual(perception.calls, 1)

            waiter = asyncio.create_task(service.ensure_current_analysis(
                request_id="second", timeout_s=.5,
            ))
            await asyncio.sleep(0)
            self.assertEqual(perception.calls, 1)
            perception.release.set()
            observation, pending = await waiter
            self.assertFalse(pending)
            self.assertEqual(observation.summary, "共有画面を解析済み")
            self.assertEqual(perception.calls, 1)

        asyncio.run(scenario())

    def test_force_fresh_query_cancels_old_inflight_and_analyzes_new_frame(self):
        """A question about 'now' must not join a pre-question analysis."""

        class Config:
            def get(self, key, default=None):
                return {"video.input": "obs"}.get(key, default)

            def section(self, _name):
                return {}

        class Perception:
            def __init__(self):
                self.calls = []
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()
                self.last_was_cache_hit = False
                self.last_metrics = {}

            async def analyze(self, frames, **_kwargs):
                marker = frames[-1].data
                self.calls.append(marker)
                if marker == b"old":
                    self.first_started.set()
                    await self.release_first.wait()
                return VisualObservation(
                    summary=marker.decode(),
                    captured_at=frames[-1].timestamp,
                    frame_hash=str(frames[-1].metadata.get("fingerprint", "")),
                )

            def cancel_background(self):
                return None

        async def scenario():
            perception = Perception()
            service = VideoObservationService(Config(), perception)
            old = MediaInput(
                media_type=MediaType.VIDEO_FRAME,
                data=b"old",
                timestamp=time.time() - 10,
                metadata={
                    "capture_sequence": 1,
                    "sample_index": 1,
                    "fingerprint": "0000",
                },
            )
            fresh = MediaInput(
                media_type=MediaType.VIDEO_FRAME,
                data=b"fresh",
                timestamp=time.time(),
                metadata={
                    "capture_sequence": 2,
                    "sample_index": 2,
                    "fingerprint": "ffff",
                },
            )
            captures = iter((old, fresh))

            async def capture(*, request_id=""):
                frame = next(captures)
                frame.metadata["explicit_request_id"] = request_id
                service._last_explicit_frame = frame
                service._append(frame)
                return frame

            service.capture_current_frame = capture
            _, pending = await service.ensure_current_analysis(
                request_id="old-request", timeout_s=.01,
            )
            self.assertTrue(pending)
            await perception.first_started.wait()

            observation, pending = await service.ensure_current_analysis(
                request_id="fresh-request", timeout_s=.5, force_fresh=True,
            )
            self.assertFalse(pending)
            self.assertEqual(observation.summary, "fresh")
            self.assertEqual(perception.calls, [b"old", b"fresh"])
            self.assertEqual(service.last_explicit_frame.data, b"fresh")

        asyncio.run(scenario())

    def test_failed_explicit_capture_clears_previous_preview(self):
        class Config:
            def get(self, key, default=None):
                return {"video.input": "obs"}.get(key, default)

            def section(self, _name):
                return {}

        async def scenario():
            service = VideoObservationService(Config(), object())
            service._last_explicit_frame = MediaInput(
                media_type=MediaType.VIDEO_FRAME,
                data=b"previous-screen",
                timestamp=time.time() - 30,
                metadata={"explicit_request_id": "previous-request"},
            )

            frame = await service.capture_current_frame(
                request_id="current-request",
            )

            self.assertIsNone(frame)
            self.assertIsNone(service.last_explicit_frame)

        asyncio.run(scenario())

    def test_visual_analyzer_sends_fast_ollama_options(self):
        class Client:
            def __init__(self):
                self.kwargs = None

            async def chat(self, _messages, _media, **kwargs):
                self.kwargs = kwargs
                yield '{"summary":"night combat","game":"minecraft","confidence":0.8}'

        client = Client()
        analyzer = VisualAnalyzer(client, max_output_tokens=120, context_size=3072)
        observation = asyncio.run(analyzer.analyze(
            [self._image()], request_id="vision-1", instruction=None,
        ))
        self.assertEqual(observation.summary, "night combat")
        self.assertEqual(client.kwargs["options"]["num_predict"], 120)
        self.assertEqual(client.kwargs["options"]["num_ctx"], 3072)
        self.assertFalse(client.kwargs["think"])

    def test_next_semantic_pass_receives_previous_state_for_flow(self):
        class Analyzer:
            def __init__(self):
                self.instruction = ""

            async def analyze(self, _frames, **kwargs):
                self.instruction = kwargs.get("instruction") or ""
                return VisualObservation(summary="洞窟へ移動", scene_changes=["洞窟へ入った"])

        analyzer = Analyzer()
        perception = PerceptionService(analyzer)
        perception.memory.record(VisualObservation(summary="森を移動中", captured_at=time.time() - 2))
        asyncio.run(perception.analyze(
            [self._image(color=(180, 20, 20))], request_id="flow-1",
            background=True, instruction="minecraft contract",
        ))
        self.assertIn("森を移動中", analyzer.instruction)
        self.assertIn("scene_changes", analyzer.instruction)
        self.assertIn("洞窟へ入った", perception.memory.flow_summary)

    def test_unchanged_frame_does_not_invoke_vision_model_twice(self):
        class Analyzer:
            def __init__(self):
                self.calls = 0

            async def analyze(self, frames, **_kwargs):
                self.calls += 1
                return VisualObservation(
                    summary="静止中", captured_at=frames[-1].timestamp,
                    confidence=.9,
                )

        analyzer = Analyzer()
        perception = PerceptionService(analyzer)
        frame = self._image()

        async def scenario():
            await perception.analyze([frame], request_id="first", background=True)
            await perception.analyze([frame], request_id="second", background=True)

        asyncio.run(scenario())
        self.assertEqual(analyzer.calls, 1)
        self.assertTrue(perception.last_was_cache_hit)

    def test_reactive_prompt_keeps_persona_and_drops_unrelated_history(self):
        compact = compact_reactive_visual_messages([
            {"role": "system", "content": "あなたはポッポ。少しくだけた話し方をする。"},
            {"role": "system", "content": "無関係な長期記憶" * 500},
            {"role": "system", "content": "ゲーム映像の継続認識状態: ゾンビが接近"},
            {"role": "user", "content": "昨日の話"},
            {"role": "assistant", "content": "昨日の返事"},
            {"role": "user", "content": "今どうすればいい？"},
        ])
        joined = "\n".join(str(item["content"]) for item in compact)
        self.assertIn("あなたはポッポ", joined)
        self.assertIn("ゾンビが接近", joined)
        self.assertIn("今どうすればいい", joined)
        self.assertNotIn("無関係な長期記憶", joined)
        self.assertLess(len(joined), 4096)

    def test_final_multimodal_response_uses_reactive_fast_options(self):
        class Client:
            def __init__(self):
                self.kwargs = None

            async def chat(self, _messages, _media, **kwargs):
                self.kwargs = kwargs
                yield "今は距離を取って、盾で受けよう。"

        fake_openai = SimpleNamespace(AsyncOpenAI=lambda **_kwargs: object())
        with patch.dict(sys.modules, {"openai": fake_openai}):
            from neuro_voice.llm.openai_compat import OpenAICompatBackend

            backend = OpenAICompatBackend(
                name="ollama", base_url="http://127.0.0.1:11434/v1",
                api_key="ollama", model="vision-test",
                multimodal_context_size=4096, multimodal_max_tokens=180,
                multimodal_temperature=.55,
            )
        client = Client()
        backend._multimodal = client

        async def collect():
            return "".join([
                token async for token in backend.generate_with_media(
                    [{"role": "user", "content": "今どうする？"}],
                    [MediaInput(media_type=MediaType.VIDEO_FRAME, data=b"jpeg")],
                    request_id="reactive-1",
                )
            ])

        result = asyncio.run(collect())
        self.assertIn("距離", result)
        self.assertEqual(client.kwargs["options"]["num_ctx"], 4096)
        self.assertEqual(client.kwargs["options"]["num_predict"], 180)
        self.assertEqual(client.kwargs["options"]["temperature"], .55)
        self.assertFalse(client.kwargs["think"])


if __name__ == "__main__":
    unittest.main()
