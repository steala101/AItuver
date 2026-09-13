from __future__ import annotations

import asyncio
from types import SimpleNamespace

from neuro_voice.discord_bridge.bot import DiscordBridge
from neuro_voice.pipeline import VoicePipeline
from neuro_voice.utils.config import Config
from neuro_voice.vision.discord_share import (
    DiscordScreenShareSession,
    classify_screen_share_control,
)
from neuro_voice.vision.models import VisualObservation


class _FakePerception:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeVideoService:
    def __init__(self, cfg, perception, is_busy):
        self.cfg = cfg
        self.perception = perception
        self.is_busy = is_busy
        self.is_running = False
        self.last_capture_error = ""
        self.latest_frame = None
        self.last_explicit_frame = None
        self.current_observation = None
        self.priority_requests = 0
        self.analyzed = []
        self.ensure_calls = []

    async def start(self):
        self.is_running = True
        return True

    async def stop(self):
        self.is_running = False

    def request_priority_analysis(self):
        self.priority_requests += 1
        return True

    async def analyze_current_frame(self, *, request_id):
        self.analyzed.append(request_id)

    async def ensure_current_analysis(
        self, *, request_id, timeout_s=None, force_fresh=False,
    ):
        self.ensure_calls.append((request_id, timeout_s, force_fresh))
        self.analyzed.append(request_id)
        return self.current_observation, False

    async def wait_for_first_frame(self, *, timeout_s=3.0):
        return self.latest_frame

    def state_context(self, text=""):
        return "現在の視覚情報:\n- ゲーム内で敵に追われている"

    def debug_status(self):
        return {
            "worker_state": "capturing" if self.is_running else "stopped",
            "latest_observation_age_sec": 0.4,
        }


def _cfg(source="Discord画面共有"):
    return Config(
        {
            "discord": {
                "screen_share": {
                    "enabled": True,
                    "backend": "obs",
                    "obs_source_name": source,
                    "capture_fps": 1.0,
                    "max_frames_per_analysis": 1,
                    "fresh_on_explicit_query": True,
                    "explicit_wait_timeout_sec": 0.5,
                }
            },
            "video": {
                "obs": {"source_name": "ローカルゲーム"},
                "game_profile": "minecraft",
                "capture_max_width": 640,
            },
            "vision": {"visual_memory_seconds": 30},
        }
    )


def test_screen_share_control_start_stop_and_reported_speech():
    assert classify_screen_share_control("画面共有見て").action == "start"
    assert classify_screen_share_control("画面共有を見ながら一緒にやろう").action == "start"
    assert classify_screen_share_control("画面共有はもう見なくていいよ").action == "stop"
    assert classify_screen_share_control("画面共有見てって友達に言った").matched is False
    assert classify_screen_share_control("今の画面どう？").matched is False
    minecraft = classify_screen_share_control("代わりにOBSのマイクラの画面を見て")
    assert minecraft.action == "start"
    assert minecraft.source_kind == "minecraft_obs"
    minecraft_short = classify_screen_share_control("OBSのマイクラを見て")
    assert minecraft_short.action == "start"
    assert minecraft_short.source_kind == "minecraft_obs"


def test_session_uses_discord_obs_source_without_mutating_local_source():
    async def run():
        made = []
        previews = []
        perception = _FakePerception()

        def factory(cfg, service_perception, is_busy):
            service = _FakeVideoService(cfg, service_perception, is_busy)
            service.latest_frame = SimpleNamespace(data=b"share-preview")
            made.append(service)
            return service

        cfg = _cfg()
        session = DiscordScreenShareSession(
            cfg,
            object(),
            service_factory=factory,
            perception_factory=lambda: perception,
            on_frame=lambda frame: previews.append(frame.data),
        )
        handled, reply = await session.handle_control("画面共有見て", requester="チビ")
        await asyncio.sleep(0)
        assert handled is True
        assert "見始めた" in reply
        assert session.active is True
        assert made[0].cfg.get("video.input") == "obs"
        assert made[0].cfg.get("video.obs.source_name") == "Discord画面共有"
        assert made[0].cfg.get("vision.capture_fps") == 1.0
        assert made[0].cfg.get("video.game_profile") == ""
        assert made[0].cfg.get("video.capture_max_width") == 1280
        assert cfg.get("video.obs.source_name") == "ローカルゲーム"
        assert previews == [b"share-preview"]

        context = await session.context_for_turn("今どうなってる？", response_id="r1")
        assert "r1" in made[0].analyzed
        assert "Discord画面共有・OBS継続認識" in context
        assert "経過秒=0.4" in context

        handled, reply = await session.handle_control("画面共有を見るのやめて")
        assert handled is True
        assert "やめた" in reply
        assert session.active is False
        await session.close()
        assert perception.closed is True

    asyncio.run(run())


def test_session_can_switch_from_discord_share_to_minecraft_obs_source():
    async def run():
        made = []

        def factory(cfg, perception, is_busy):
            service = _FakeVideoService(cfg, perception, is_busy)
            made.append(service)
            return service

        session = DiscordScreenShareSession(
            _cfg(),
            object(),
            service_factory=factory,
            perception_factory=_FakePerception,
        )
        handled, reply = await session.handle_control(
            "Discordの画面共有を見て", requester="チビ",
        )
        assert handled is True
        assert session.source_kind == "discord_screen_share"
        assert session.source_name == "Discord画面共有"
        assert "Discord画面共有" in reply

        handled, reply = await session.handle_control(
            "代わりにOBSのマイクラの画面を見て", requester="チビ",
        )
        assert handled is True
        assert len(made) == 2
        assert made[0].is_running is False
        assert session.source_kind == "minecraft_obs"
        assert session.source_name == "ローカルゲーム"
        assert made[1].cfg.get("video.obs.source_name") == "ローカルゲーム"
        assert made[1].cfg.get("video.game_profile") == "minecraft"
        assert made[1].cfg.get("video.capture_max_width") == 768
        assert "マイクラ画面" in reply
        await session.close()

    asyncio.run(run())


def test_current_screen_question_uses_fresh_frame_and_skips_second_llm():
    async def run():
        made = []
        previews = []

        def factory(cfg, perception, is_busy):
            service = _FakeVideoService(cfg, perception, is_busy)
            frame = SimpleNamespace(
                data=b"fresh-game-frame",
                metadata={
                    "obs_source_name": "マイクラ",
                    "explicit_request_id": "fresh-r1",
                },
            )
            service.latest_frame = frame
            service.last_explicit_frame = frame
            service.current_observation = VisualObservation(
                summary="草原で村の入口を見ている",
                captured_at=1.0,
                source_name="マイクラ",
            )
            made.append(service)
            return service

        session = DiscordScreenShareSession(
            _cfg(),
            object(),
            service_factory=factory,
            perception_factory=_FakePerception,
            on_frame=lambda frame: previews.append(frame.data),
        )
        ok, _ = await session.start(
            requester="local:mic", source_kind="minecraft_obs",
        )
        assert ok is True
        reply = await session.answer_current_visual_question(
            "今見えてる景色はどんなの？", response_id="fresh-r1",
        )
        assert reply == "草原で村の入口を見ている。"
        assert "撮り直した画面だと" not in reply
        assert ("fresh-r1", 18.0, True) in made[0].ensure_calls
        assert b"fresh-game-frame" in previews
        await session.close()

    asyncio.run(run())


def test_current_screen_question_never_publishes_another_requests_frame():
    async def run():
        made = []
        previews = []

        def factory(cfg, perception, is_busy):
            service = _FakeVideoService(cfg, perception, is_busy)
            service.current_observation = VisualObservation(
                summary="現在の解析結果",
                captured_at=1.0,
                source_name="マイクラ",
            )
            made.append(service)
            return service

        session = DiscordScreenShareSession(
            _cfg(),
            object(),
            service_factory=factory,
            perception_factory=_FakePerception,
            on_frame=lambda frame: previews.append(frame.data),
        )
        ok, _ = await session.start(
            requester="local:mic", source_kind="minecraft_obs",
        )
        assert ok is True
        await asyncio.sleep(0)
        made[0].last_explicit_frame = SimpleNamespace(
            data=b"old-explicit-frame",
            metadata={"explicit_request_id": "old-request"},
        )

        reply = await session.answer_current_visual_question(
            "今、画面見てる？", response_id="new-request",
        )

        assert reply == "現在の解析結果。"
        assert previews == []
        await session.close()

    asyncio.run(run())


def test_minecraft_howto_skips_fresh_vision_but_current_state_still_uses_it():
    async def run():
        made = []

        def factory(cfg, perception, is_busy):
            service = _FakeVideoService(cfg, perception, is_busy)
            service.current_observation = VisualObservation(
                summary="海を泳いでいる", captured_at=1.0,
            )
            made.append(service)
            return service

        session = DiscordScreenShareSession(
            _cfg(),
            object(),
            service_factory=factory,
            perception_factory=_FakePerception,
        )
        ok, _ = await session.start(
            requester="local:mic", source_kind="minecraft_obs",
        )
        assert ok is True
        assert session.wants_current_visual_answer(
            "この豚肉を焼くにはどうすればいい？"
        ) is False
        reply = await session.answer_current_visual_question(
            "この豚肉を焼くにはどうすればいい？", response_id="recipe-r1",
        )
        assert reply is None
        assert made[0].ensure_calls == []

        assert session.wants_current_visual_answer("次はどうすればいい？") is True
        assert session.wants_direct_visual_answer("次はどうすればいい？") is False
        await session.answer_current_visual_question(
            "次はどうすればいい？", response_id="state-r1",
        )
        assert ("state-r1", 6.5, True) in made[0].ensure_calls

        # Asking to inspect the screen explicitly still wins over the stable
        # knowledge classification.
        assert session.wants_current_visual_answer(
            "画面を見て、この豚肉を焼くにはどうすればいい？"
        ) is True
        assert session.wants_direct_visual_answer(
            "画面を見て、この豚肉を焼くにはどうすればいい？"
        ) is False
        await session.close()

    asyncio.run(run())


def test_how_to_cook_is_never_returned_as_raw_scene_narration():
    session = DiscordScreenShareSession(_cfg(), object())
    session._source_kind = "minecraft_obs"
    assert session.wants_current_visual_answer(
        "今この肉をどうやって焼ける？"
    ) is True
    assert session.wants_direct_visual_answer(
        "今この肉をどうやって焼ける？"
    ) is False


def test_current_inventory_question_refreshes_frame_then_uses_normal_reasoning():
    async def run():
        made = []

        def factory(cfg, perception, is_busy):
            service = _FakeVideoService(cfg, perception, is_busy)
            service.current_observation = VisualObservation(
                summary="インベントリを開いている",
                game="minecraft",
                objects=["棒 4個", "原木 6個"],
                player_state_details={
                    "inventory_open": True,
                    "menu_open": True,
                },
                captured_at=1.0,
            )
            made.append(service)
            return service

        session = DiscordScreenShareSession(
            _cfg(),
            object(),
            service_factory=factory,
            perception_factory=_FakePerception,
        )
        ok, _ = await session.start(
            requester="local:mic", source_kind="minecraft_obs",
        )
        assert ok is True
        question = "今は持ってるアイテムで焚き火を作れる？"
        assert session.wants_current_visual_answer(question) is True
        assert session.wants_direct_visual_answer(question) is False

        reply = await session.answer_current_visual_question(
            question, response_id="craft-r1",
        )
        assert reply is None
        assert ("craft-r1", 6.5, True) in made[0].ensure_calls

        context = await session.context_for_turn(
            question, response_id="craft-r1",
        )
        assert "質問に" not in (reply or "")
        assert "視覚要約を言い換えるだけ" in context
        # The same turn must not launch a second vision inference.
        assert made[0].ensure_calls == [("craft-r1", 6.5, True)]
        await session.close()

    asyncio.run(run())


def test_local_and_discord_use_the_same_verified_recipe_fast_lane():
    class _Knowledge:
        @staticmethod
        def verified_recipe_answer(text):
            return "公式調理回答" if "豚肉" in text else None

    async def run():
        cfg = Config({
            "game_assistant": {
                "minecraft": {
                    "enabled": True,
                    "direct_verified_recipe_answer": True,
                }
            },
            "video": {"game_profile": "minecraft"},
        })
        local = object.__new__(VoicePipeline)
        local._cfg = cfg
        local._minecraft_knowledge = _Knowledge()
        assert await local._minecraft_verified_recipe_answer(
            "豚肉の焼き方を教えて"
        ) == "公式調理回答"

        discord = object.__new__(DiscordBridge)
        discord._cfg = cfg

        async def ensure_knowledge():
            return _Knowledge()

        discord._ensure_minecraft_knowledge = ensure_knowledge
        assert await discord._minecraft_verified_recipe_answer(
            "豚肉の焼き方を教えて"
        ) == "公式調理回答"

    asyncio.run(run())


def test_fresh_answer_never_reads_truncated_json_schema_aloud():
    observation = VisualObservation(
        summary='{"scene_type":"minecraft","summary":"洞窟の入口にいる",',
    )
    reply = DiscordScreenShareSession._format_fresh_observation(
        "今どこにいる？", observation,
    )
    assert reply == "洞窟の入口にいる。"
    assert "scene_type" not in reply


def test_fresh_game_answer_includes_action_and_grounded_next_step():
    observation = VisualObservation(
        summary="雨の海岸で夜を迎えている",
        game="minecraft",
        actions=["生の豚肉を手に持って周囲を見回している"],
        player_state=["health_estimate: 半分", "hunger_estimate: 少ない"],
        suggested_help="敵が来る前に明るい場所へ移動して食料を確保しよう",
        commentary_worthy=True,
    )
    reply = DiscordScreenShareSession._format_fresh_observation(
        "今何をしていて、次はどうすればいい？", observation,
    )
    assert reply.startswith("敵が来る前に明るい場所へ移動")
    assert "生の豚肉を手に持って" in reply
    assert "撮り直した" not in reply


def test_fresh_answer_never_speaks_internal_player_state_keys():
    observation = VisualObservation(
        summary="インベントリを開いている",
        game="minecraft",
        player_state=[
            "inventory_open: True",
            "menu_open: True",
            "health_estimate: 半分",
        ],
        player_state_details={
            "inventory_open": True,
            "menu_open": True,
            "health_estimate": "半分",
            "held_item": "生の豚肉",
        },
    )
    reply = DiscordScreenShareSession._format_fresh_observation(
        "今の状態は？", observation,
    )
    assert "体力は半分" in reply
    assert "手に持っているものは生の豚肉" in reply
    assert "inventory_open" not in reply
    assert "menu_open" not in reply
    assert "True" not in reply


def test_screen_share_session_keeps_game_observation_callback():
    async def callback(observation):
        return observation

    session = DiscordScreenShareSession(
        _cfg(), object(), on_observation=callback,
        service_factory=lambda cfg, perception, is_busy: _FakeVideoService(
            cfg, perception, is_busy,
        ),
        perception_factory=_FakePerception,
    )
    assert session._on_observation is callback


def test_disabled_or_missing_source_fails_without_claiming_to_see():
    async def run():
        cfg = _cfg("")
        session = DiscordScreenShareSession(cfg, object())
        ok, reply = await session.start()
        assert ok is False
        assert "未設定" in reply
        assert session.active is False

        cfg.set("discord.screen_share.enabled", False)
        ok, reply = await session.start()
        assert ok is False
        assert "無効" in reply

    asyncio.run(run())


def test_local_pipeline_active_share_preempts_monitor_capture():
    """Local-mic visual turns must use the selected OBS share, not monitor 1."""

    class _Conversation:
        def messages_for_turn(self, text):
            return [
                {"role": "system", "content": "persona"},
                {"role": "user", "content": text},
            ], False

    class _ActiveShare:
        active = True
        latest_frame = SimpleNamespace(data=b"obs-frame")

        def __init__(self):
            self.calls = []

        async def context_for_turn(self, text, *, response_id=""):
            self.calls.append((text, response_id))
            return "【Discord画面共有】OBSソースの現在状態"

    async def run():
        pipeline = object.__new__(VoicePipeline)
        pipeline._conv = _Conversation()
        pipeline._screen_share_session_obj = _ActiveShare()
        emitted = []
        pipeline._emit = lambda event_type, **data: emitted.append((event_type, data))

        async def should_not_capture_monitor():
            raise AssertionError("monitor capture must not run during a screen-share session")

        pipeline._capture_frame = should_not_capture_monitor
        messages, resumed, media = await pipeline._build_messages(
            "今の画面共有を見て", response_id="local-r1",
        )

        assert resumed is False
        assert media == []
        assert pipeline._screen_share_session_obj.calls == [
            ("今の画面共有を見て", "local-r1")
        ]
        assert any(
            message["role"] == "system" and "OBSソースの現在状態" in message["content"]
            for message in messages
        )
        # 通常会話のコンテキスト構築では、以前キューに入ったフレームを
        # 「いま見た画面」として再送しない。プレビュー送信はセッション側が
        # 初回取得時または明示質問の request_id と一致した時だけ行う。
        assert emitted == []

    asyncio.run(run())


def test_local_minecraft_share_keeps_official_recipe_context():
    class _Conversation:
        def messages_for_turn(self, text):
            return [
                {"role": "system", "content": "persona"},
                {"role": "user", "content": text},
            ], False

    class _MinecraftShare:
        active = True
        source_kind = "minecraft_obs"
        latest_frame = None

        async def context_for_turn(self, text, *, response_id=""):
            return "【OBS視覚状態】海辺にいる"

    async def run():
        pipeline = object.__new__(VoicePipeline)
        pipeline._conv = _Conversation()
        pipeline._screen_share_session_obj = _MinecraftShare()
        pipeline._emit = lambda *args, **kwargs: None
        pipeline._minecraft_knowledge_context = lambda text: (
            "【公式レシピ】盾は板材6個と鉄インゴット1個"
        )
        messages, resumed, media = await pipeline._build_messages(
            "盾の材料を教えて", response_id="recipe-context-r1",
        )
        assert resumed is False
        assert media == []
        system_text = "\n".join(
            item["content"] for item in messages if item["role"] == "system"
        )
        assert "OBS視覚状態" in system_text
        assert "公式レシピ" in system_text

    asyncio.run(run())


def test_local_pipeline_routes_explicit_start_without_creating_idle_session():
    class _Session:
        def __init__(self):
            self.calls = []

        async def handle_control(self, text, *, requester=""):
            self.calls.append((text, requester))
            return True, "OBS共有を見始めた"

    async def run():
        pipeline = object.__new__(VoicePipeline)
        session = _Session()
        created = []

        def get_session():
            created.append(True)
            return session

        pipeline._get_screen_share_session = get_session

        handled, reply = await pipeline._handle_screen_share_control("普通の雑談")
        assert handled is False
        assert reply is None
        assert created == []

        handled, reply = await pipeline._handle_screen_share_control(
            "Discordの画面共有を見て", requester="local:mic",
        )
        assert handled is True
        assert reply == "OBS共有を見始めた"
        assert created == [True]
        assert session.calls == [("Discordの画面共有を見て", "local:mic")]

    asyncio.run(run())
