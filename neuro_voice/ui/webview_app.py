"""pywebview ベースの GUI。

バックエンド (音声パイプライン) は別スレッドの asyncio ループで動かし、
GUI とはイベントキュー (JSからのポーリング) で接続する。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import requests
import numpy as np

from neuro_voice.llm.factory import build_llm
from neuro_voice.memory.conversation import ConversationManager
from neuro_voice.memory.persona import (
    PERSONA_FIELDS,
    build_system_prompt,
    get_active_persona,
)
from neuro_voice.dialogue.working_memory import (
    PERSONA_TRAIT_LABELS,
    normalize_persona_traits,
)
from neuro_voice.games import GameProfileRegistry
from neuro_voice.pipeline import VoicePipeline
from neuro_voice.tts.factory import build_tts
from neuro_voice.tts.switchable import SwitchableTTS
from neuro_voice.utils.config import Config

logger = logging.getLogger(__name__)

# GUIから編集できるAPIキー: (環境変数名, 表示ラベル, 実行時に反映するconfigキー)
_API_KEY_DEFS = [
    ("CEREBRAS_API_KEY", "Cerebras (LLM)", "llm.backends.cerebras.api_key"),
    ("OPENAI_API_KEY", "OpenAI (LLM)", "llm.backends.openai.api_key"),
    ("OPENROUTER_API_KEY", "OpenRouter (LLM)", "llm.backends.openrouter.api_key"),
    ("ANTHROPIC_API_KEY", "Anthropic (Claude Vision)", "vision.claude.api_key"),
    ("GOOGLE_VISION_API_KEY", "Google Cloud Vision", "vision.google.api_key"),
    ("AUDD_API_TOKEN", "AudD (曲名認識)", "music_recognition.api_token"),
    ("ACRCLOUD_ACCESS_KEY", "ACRCloud Access Key (鼻歌認識)", "humming_recognition.access_key"),
    ("ACRCLOUD_ACCESS_SECRET", "ACRCloud Access Secret (鼻歌認識)", "humming_recognition.access_secret"),
]


class GuiBackend:
    """パイプラインを別スレッドで動かし、GUIへイベントを供給する。"""

    def __init__(self, cfg: Config, config_path: str | None = None,
                 cognition_test_session: bool = False,
                 cognition_session_mode: str = "disabled"):
        self._cfg = cfg
        self._cognition_test_session_at_start = bool(cognition_test_session)
        self._cognition_session_mode_at_start = str(cognition_session_mode or "disabled")
        self.config_path = Path(config_path) if config_path else None
        self._events: deque[dict[str, Any]] = deque(maxlen=2000)
        self._lock = threading.Lock()
        self.pipeline: VoicePipeline | None = None
        self.tts = None
        self.llm = None
        self.conv = None
        self.stt = None
        self.mind = None
        self.discord = None  # DiscordBridge (接続時に生成)
        self._discord_task = None
        self._stt_switching = False
        self._tts_switching = False
        self._aloop: asyncio.AbstractEventLoop | None = None
        self._backend_thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._shutdown_requested = threading.Event()
        self._shutdown_complete = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._pipeline_task: asyncio.Task | None = None
        self._warmup_task: asyncio.Task | None = None
        backend = cfg.get("llm.backend", "?")
        _, active_persona = get_active_persona(cfg)
        self.info = {
            "persona": active_persona.get("name", "AI"),
            "llm": f"{backend} / {cfg.get(f'llm.backends.{backend}.model', '?')}",
            "tts": cfg.get("audio_output.backend", cfg.get("tts.backend", "?")),
            "stt": cfg.get("stt.model", "?"),
            "stt_device": cfg.get("stt.device", "cuda"),
        }
        backends_cfg = cfg.section("llm.backends")
        self.settings = {
            "backend": cfg.get("llm.backend", "cerebras"),
            "temperature": float(cfg.get("llm.temperature", 0.7)),
            "max_tokens": int(cfg.get("llm.max_tokens", 512)),
            "max_turns": int(cfg.get("conversation.max_turns", 20)),
            "backends": list(backends_cfg.keys()),
            "models": {k: (v or {}).get("model", "") for k, v in backends_cfg.items()},
            "vision_enabled": bool(cfg.get("vision.enabled", False)),
            "vision_mode": str(cfg.get("vision.mode", "on_demand")),
            "vision_interval": int(cfg.get("vision.interval_s", 20)),
            "vision_provider": str(cfg.get("vision.provider", "local")),
            "vision_model": str(cfg.get("vision.model", "gemma4-12b-qat")),
            "video_enabled": bool(cfg.get("video.enabled", False)),
            "video_input": str(cfg.get("video.input", "camera")),
            "video_source": str(cfg.get("video.source", 0)),
            "video_screen_monitor": str(cfg.get("video.screen_monitor", "auto")),
            "video_window_handle": str(cfg.get("video.window_handle", "")),
            "video_obs_host": str(cfg.get("video.obs.host", "127.0.0.1")),
            "video_obs_port": int(cfg.get("video.obs.port", 4455)),
            "video_obs_source": str(cfg.get("video.obs.source_name", "")),
            "video_obs_password_set": bool(str(cfg.get("video.obs.password", "")).strip()),
            "video_game_profile": str(cfg.get("video.game_profile", "")),
            "video_game_profiles": GameProfileRegistry(
                str(cfg.get("game_profiles.root", "") or "") or None
            ).public_summaries(),
            "video_game_commentary": bool(cfg.get("video.game_commentary_enabled", False)),
            "video_game_knowledge": bool(cfg.get(
                f"game_assistant.{str(cfg.get('video.game_profile', 'minecraft')).strip().lower() or 'minecraft'}.enabled",
                cfg.get("game_assistant.minecraft.enabled", True),
            )),
            "video_analysis_interval": float(cfg.get(
                "vision.analysis_min_interval_sec", cfg.get("video.analysis_interval_sec", 2.0),
            )),
            "video_max_frames": int(cfg.get("video.max_frames_per_analysis", 4)),
            "audio_input_backend": str(cfg.get("audio_input.backend", "whisper")),
            "audio_output_backend": str(cfg.get("audio_output.backend", cfg.get("tts.backend", "voicevox"))),
            "auto_load": bool(cfg.get("llm.auto_load", True)),
            "search_enabled": bool(cfg.get("search.enabled", False)),
            "autonomous_research_enabled": bool(cfg.get("autonomous_research.enabled", False)),
            "autonomous_research_mode": str(cfg.get("autonomous_research.mode", "LOW_RISK_AUTO")),
            "music_recognition_enabled": bool(cfg.get("music_recognition.enabled", False)),
            "music_recognition_loopback_device": str(cfg.get("music_recognition.loopback_device", "") or ""),
            "humming_recognition_enabled": bool(cfg.get("humming_recognition.enabled", False)),
            "humming_recognition_host": str(cfg.get("humming_recognition.host", "")),
            "emotion_enabled": bool(cfg.get("emotion.enabled", True)),
            "stt_beam_size": int(cfg.get("stt.beam_size", 5)),
            "proactive_enabled": bool(cfg.get("autonomous_action_system.enabled", True)),
            "proactive_min": int(cfg.get("proactive.min_interval_s", 30)),
            "proactive_max": int(cfg.get("proactive.max_interval_s", 90)),
            "conversation_variant": str(cfg.get("conversation_engine.variant", "enhanced")),
            "conversation_feature_mode": str(cfg.get("conversation_features.mode", "normal")),
            "conversation_developer_ui": bool(cfg.get("conversation_features.developer_ui", False)),
            "conversation_force_feature": str(cfg.get("conversation_features.debug.force_feature", "") or ""),
            "conversation_feature_toggles": {
                key: bool(cfg.get(f"features.{key}", True)) for key in (
                    "humor_engine", "casual_conversation_engine", "story_engine",
                    "imagination_engine", "playful_fantasy_engine", "world_knowledge_engine",
                    "self_growth", "relationship_evolution", "value_system", "experience_memory",
                )
            },
        }

    def ollama_models(self) -> list[str]:
        """Ollama にインストール済みのモデル名一覧を返す (未起動なら空)。

        `ollama list` 相当 (/api/tags) を毎回ライブ取得する。
        取得結果・失敗理由はログに残すので、候補が空のときの切り分けに使える。
        """
        base = str(
            self._cfg.get("llm.backends.ollama.base_url", "http://localhost:11434/v1")
        ).removesuffix("/v1")
        url = f"{base}/api/tags"
        try:
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", []) if m.get("name")]
            # 重複排除しつつ順序を保つ
            names = list(dict.fromkeys(names))
            logger.info("Ollama モデル一覧を取得: %s (%s)", names or "(空)", url)
            return names
        except Exception as e:
            logger.warning("Ollama モデル一覧の取得に失敗 (%s): %s", url, e)
            return []

    @property
    def cfg(self) -> Config:
        return self._cfg

    def start(self) -> None:
        self._backend_thread = threading.Thread(
            target=self._run, name="backend", daemon=True,
        )
        self._backend_thread.start()

    def push(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._events.append({"type": event_type, "data": data or {}})

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:
            logger.exception("バックエンドで致命的エラー")
            self.push("fatal", {"message": str(e)})
        finally:
            self._shutdown_complete.set()

    def request_shutdown(self) -> None:
        """Signal the backend loop without blocking pywebview's UI thread."""
        self._shutdown_requested.set()
        loop, event = self._aloop, self._stop_event
        if loop is not None and event is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)

    def shutdown(self, timeout: float = 15.0) -> None:
        """Request one bounded, ordered shutdown and wait for the backend."""
        with self._shutdown_lock:
            first = not self._shutdown_started
            self._shutdown_started = True
        if first:
            logger.info("GUI終了要求を受信。バックエンドを停止します")
        self.request_shutdown()
        if not self._shutdown_complete.wait(max(0.1, timeout)):
            logger.warning("バックエンド終了が %.1f 秒以内に完了しませんでした", timeout)
        # 自分で起動した VOICEVOX エンジンを終了
        try:
            from neuro_voice.tts.voicevox_launcher import shutdown_engine

            shutdown_engine()
        except Exception:
            pass
        # 自分で起動した Style-Bert-VITS2 サーバーを終了
        try:
            from neuro_voice.tts.style_bert_vits2_launcher import shutdown_server

            shutdown_server()
        except Exception:
            pass
        thread = self._backend_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    async def _shutdown_components(self) -> None:
        """Run on the backend event loop after voice input has stopped."""
        warmup = self._warmup_task
        if warmup is not None and not warmup.done():
            warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warmup
        if self.discord is not None:
            bridge, self.discord = self.discord, None
            try:
                await asyncio.wait_for(bridge.stop(), timeout=5.0)
            except Exception:
                logger.warning("Discord終了処理を打ち切りました", exc_info=True)
        if self.pipeline is not None:
            try:
                await asyncio.wait_for(self.pipeline.shutdown(), timeout=5.0)
            except Exception:
                logger.warning("音声パイプライン終了処理を打ち切りました", exc_info=True)
        if self.mind is not None:
            try:
                # A long final reflection must not keep the CMD alive.  The
                # durable dialogue/personality state is saved again by close().
                await asyncio.wait_for(self.mind.on_session_end(), timeout=3.0)
            except Exception:
                logger.info("終了時の追加内省をスキップしました")
            with contextlib.suppress(Exception):
                await self.mind.close()
        if self.llm is not None:
            try:
                await asyncio.wait_for(self.llm.unload(), timeout=6.0)
            except Exception:
                logger.warning("LLMアンロード処理を打ち切りました", exc_info=True)
        logger.info("GUIバックエンドの終了処理が完了しました")

    async def _main(self) -> None:
        cfg = self._cfg
        self._aloop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._shutdown_requested.is_set():
            self._stop_event.set()
        self.push("status", {"message": "LLMバックエンドに接続中..."})
        llm = build_llm(cfg)
        self.llm = llm
        # 起動時に Ollama のインストール済みモデルをログへ (設定パネルの候補と一致する)
        installed = self.ollama_models()
        active_backend = cfg.get("llm.backend", "")
        cfg_model = str(cfg.get("llm.backends.ollama.model", ""))
        if active_backend == "ollama" and installed and cfg_model not in installed:
            self.push("status", {"message": (
                f"⚠ config のモデル '{cfg_model}' は ollama list に見つかりません。"
                f"インストール済み: {', '.join(installed)}"
            )})
            logger.warning(
                "config の ollama モデル '%s' が未インストール。利用可能: %s",
                cfg_model, installed,
            )
        if self._shutdown_requested.is_set():
            await self._shutdown_components()
            return
        # LLM自動ロード (Ollama のみ実処理)。裏で走らせて起動をブロックしない
        if bool(cfg.get("llm.auto_load", True)) and active_backend == "ollama":
            self.push("status", {"message": "LLMモデルを事前ロード中... (裏で実行)"})

            async def _warmup() -> None:
                ok = await llm.load()
                self.push("status", {
                    "message": "LLMモデルのロード完了" if ok
                    else "⚠ LLMモデルの事前ロードに失敗 (Ollama の起動を確認)"
                })

                fallback_notice = getattr(llm, "fallback_notice", None)
                if fallback_notice:
                    self.info["llm"] = f"ollama / {llm.model} (fallback)"
                    self.push("info_update", {"info": self.info})
                    self.push("status", {"message": fallback_notice})

            self._warmup_task = asyncio.create_task(_warmup())

        # Speech synthesis, recognition and Mind have no dependency on each
        # other, but loading them one after another made startup the *sum* of
        # their costs: the Style-Bert-VITS2 server boot (~19 s) finished before
        # faster-whisper even began importing (~24 s).  Run them together and
        # wait only for the slowest.
        self.push("status", {"message": "音声モデルを並行して読込中... (初回はダウンロードあり)"})
        initial_tts_backend = str(cfg.get("audio_output.backend", cfg.get("tts.backend", "voicevox")))

        def _make_tts():
            return SwitchableTTS(
                build_tts(cfg, on_status=lambda m: self.push("status", {"message": m})),
                initial_tts_backend,
                volume=float(cfg.get("tts.output_volume", 1.0)),
            )

        def _make_stt():
            from neuro_voice.stt.factory import build_stt

            return build_stt(cfg)

        def _make_mind():
            if not bool(cfg.get("mind.enabled", True)):
                return None
            from neuro_voice.mind import Mind

            pkey, pdef = get_active_persona(cfg)
            return Mind(
                cfg, llm, pkey or "default", str(pdef.get("name", "AI")),
                is_busy=lambda: (
                    self.pipeline is not None
                    and self.pipeline.state in ("thinking", "speaking")
                ),
                on_event=self.push_event,
            )

        started_at = time.perf_counter()
        tts, stt, mind = await asyncio.gather(
            asyncio.to_thread(_make_tts),
            asyncio.to_thread(_make_stt),
            asyncio.to_thread(_make_mind),
            # Keep partial results assignable so a failure in one component
            # still lets the others be shut down cleanly.
            return_exceptions=True,
        )
        for component, name in ((tts, "TTS"), (stt, "STT"), (mind, "Mind")):
            if isinstance(component, BaseException):
                logger.error("%s の初期化に失敗しました", name, exc_info=component)
        self.tts = None if isinstance(tts, BaseException) else tts
        self.stt = None if isinstance(stt, BaseException) else stt
        self.mind = None if isinstance(mind, BaseException) else mind
        failure = next(
            (item for item in (tts, stt, mind) if isinstance(item, BaseException)), None,
        )
        if failure is not None:
            await self._shutdown_components()
            raise failure
        logger.info(
            "音声モデルの並行読込が完了 (%.1fs)", time.perf_counter() - started_at,
        )
        if self._shutdown_requested.is_set():
            await self._shutdown_components()
            return
        self.info["stt_device"] = getattr(stt, "device", "cuda")
        conv = ConversationManager(
            system_prompt=build_system_prompt(cfg),
            max_turns=int(cfg.get("conversation.max_turns", 20)),
        )
        self.conv = conv
        self.pipeline = VoicePipeline(
            cfg, llm=llm, tts=tts, conv=conv, stt=stt, on_event=self.push_event,
            mind=self.mind,
        )
        if self._cognition_session_mode_at_start != "disabled":
            self.pipeline.start_cognition_session(self._cognition_session_mode_at_start)
        elif self._cognition_test_session_at_start:
            self.pipeline.start_cognition_test_session(True)
        # ローカル会話でも「〇〇流して」でYouTube再生 (GUI内の埋め込みプレイヤーで鳴らす)
        self._local_music_playing = False
        self._last_local_music_query: str | None = None
        self._local_music_context: dict[str, str] | None = None
        self._recognized_music_context: dict[str, Any] | None = None
        self._recognized_music_at = 0.0
        self._local_humming_armed_until = 0.0
        self._local_humming_play_after = False
        self.pipeline.set_music_hook(self._local_music_hook)
        self.pipeline.set_humming_capture_hook(self._consume_local_humming)
        self.pipeline.set_pronunciation_learning_hook(self._learn_pronunciation_from_speech)
        self.pipeline.set_tts_volume_hook(self.apply_tts_volume_command)
        self.pipeline.set_music_context_provider(self._music_context_for_llm)
        self.push("ready", {"info": self.info})
        self._pipeline_task = asyncio.create_task(
            self.pipeline.run_voice(), name="local-voice-pipeline",
        )
        stop_task = asyncio.create_task(self._stop_event.wait(), name="gui-shutdown-wait")
        try:
            done, _ = await asyncio.wait(
                {self._pipeline_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done and not self._pipeline_task.done():
                self._pipeline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pipeline_task
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
            await self._shutdown_components()

    def _music_context_for_llm(self) -> dict[str, Any] | None:
        """Return exact player metadata first, then a fresh AudD result."""
        if self._local_music_playing and self._local_music_context:
            return self._local_music_context
        if (
            self._recognized_music_context
            and time.monotonic() - self._recognized_music_at <= 60.0
        ):
            return self._recognized_music_context
        return None

    async def _omakase_query(self) -> str:
        """おまかせ再生の検索クエリ。AI自身に気分/好みで選曲させ、
        失敗時は既定クエリにフォールバックする。"""
        pick = None
        if self.mind is not None:
            try:
                pick = await self.mind.suggest_song()
            except Exception:
                logger.warning("おまかせ選曲に失敗", exc_info=True)
        if pick:
            self.push("status", {"message": f"🎵 今の気分だと…「{pick}」がいいかな"})
            return pick
        return str(self._cfg.get("music.default_query", "作業用BGM"))

    async def _learn_pronunciation_from_speech(self, text: str, last_assistant_text: str):
        """口頭訂正を安全に推定できた時だけ、永続するVOICEVOX読み辞書へ保存する。"""
        from neuro_voice.tts.pronunciation import infer_pronunciation_correction

        learned = infer_pronunciation_correction(text, last_assistant_text)
        if not learned:
            return None
        surface, reading = learned
        current = self._cfg.get("tts.voicevox.pronunciations", {}) or {}
        if not isinstance(current, dict):
            current = {}
        if current.get(surface) == reading:
            return None
        current = {**current, surface: reading}
        self._cfg.set("tts.voicevox.pronunciations", current)
        tts = self.tts
        if tts is not None and hasattr(tts, "set_pronunciations"):
            tts.set_pronunciations(current)
        _persist_yaml_mapping(self.config_path, "tts.voicevox.pronunciations", current)
        self.push("status", {"message": f"🔊 読み方を覚えました: {surface} → {reading}"})
        return learned

    def set_tts_volume(self, volume: float) -> float:
        """Apply and persist the backend-independent TTS master volume."""
        value = max(0.0, min(2.0, float(volume)))
        if self.tts is not None and hasattr(self.tts, "set_volume"):
            value = float(self.tts.set_volume(value))
        self._cfg.set("tts.output_volume", value)
        _persist_yaml_value(self.config_path, "tts.output_volume", round(value, 2))
        percent = int(round(value * 100))
        self.push("tts_volume_changed", {"volume": value, "percent": percent})
        self.push("status", {"message": f"🔊 AIの声の音量を {percent}% にしました"})
        return value

    def apply_tts_volume_command(self, command) -> str:
        current = float(getattr(self.tts, "volume", self._cfg.get("tts.output_volume", 1.0)))
        value = command.value if command.mode == "set" else current + command.value
        value = self.set_tts_volume(value)
        return f"声の音量を{int(round(value * 100))}パーセントにしたよ。"

    async def _recognize_local_music(self, text: str) -> str:
        """Identify current playback. Return none/handled/continue_dialogue."""
        from neuro_voice.music_recognition import (
            AudDRecognizer, capture_output_audio, parse_music_recognition_request,
        )

        request = parse_music_recognition_request(text)
        if not request.requested:
            return "none"

        # The embedded YouTube player was created from yt-dlp metadata, so its
        # title is already exact. Fingerprinting the microphone here was both
        # slower and less reliable than using the known player state.
        if self._local_music_playing and self._local_music_context:
            title = str(self._local_music_context.get("title") or "").strip()
            query = str(self._local_music_context.get("query") or title).strip()
            if title or query:
                self.push("status", {"message": f"再生中の曲: {title or query}"})
                return "continue_dialogue"

        settings = self._cfg.section("music_recognition")
        if not bool(settings.get("enabled", False)):
            self.push("status", {"message": "曲名認識は未設定です。設定で有効化し、AUDD_API_TOKEN を指定してください。"})
            return "handled"
        if self.pipeline is None:
            return "handled"
        seconds = max(
            float(settings.get("min_audio_seconds", 5.0)),
            float(settings.get("live_capture_seconds", 8.0)),
        )
        loopback_device = settings.get("loopback_device")
        capture_source = str(settings.get("capture_source", "loopback") or "loopback").lower()
        if capture_source == "microphone":
            audio = self.pipeline.recent_audio(seconds)
        else:
            self.push("status", {"message": f"今流れている再生音を{seconds:g}秒聞いて調べています..."})
            try:
                loop = asyncio.get_running_loop()
                audio = await loop.run_in_executor(
                    None, capture_output_audio, seconds, 16000, loopback_device,
                )
            except Exception as exc:
                logger.exception("曲名認識用ループバック取得でエラー")
                self.push("status", {"message": f"再生音を取得できませんでした: {exc}"})
                return "handled"
        min_samples = int(float(settings.get("min_audio_seconds", 5.0)) * 16000)
        if len(audio) < min_samples:
            self.push("status", {"message": "再生音を十分な長さで取得できませんでした。"})
            return "handled"
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if rms < float(settings.get("min_rms", 0.003)):
            self.push("status", {"message": (
                f"再生音が無音でした（RMS {rms:.5f}）。YouTubeが鳴っているWindows出力を"
                "曲名認識のループバック対象に指定してください。"
            )})
            return "handled"
        recognizer = AudDRecognizer(token_env=str(settings.get("token_env", "AUDD_API_TOKEN")))
        if not recognizer.configured:
            self.push("status", {"message": f"曲名認識のAPIトークンが未設定です: {settings.get('token_env', 'AUDD_API_TOKEN')}"})
            return "handled"
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, recognizer.recognize, audio, 16000,
            )
        except Exception as exc:
            logger.exception("ローカル曲名認識でエラー")
            self.push("status", {"message": f"曲名認識に失敗しました: {exc}"})
            return "handled"
        if result is None:
            self.push("status", {"message": (
                f"AudDに一致する曲がありませんでした（取得音RMS {rms:.5f}）。"
                "ライブ版・カバー・ゲーム内アレンジなどは一致しない場合があります。"
            )})
            return "handled"
        query = result.query
        self._last_local_music_query = query
        self._recognized_music_context = {
            "title": result.title, "artist": result.artist, "query": query,
            "source": "audd",
        }
        self._recognized_music_at = time.monotonic()
        self.push("music_recognized", {
            "title": result.title, "artist": result.artist, "album": result.album,
            "song_link": result.song_link, "query": query,
        })
        self.push("status", {"message": f"曲を特定しました: {query}"})
        if not request.play_after:
            return "continue_dialogue"
        if not bool(self._cfg.get("music.enabled", True)):
            self.push("status", {"message": "曲は特定しましたが、音楽再生は無効です。"})
            return "handled"
        from neuro_voice.discord_bridge.music import resolve_track
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, resolve_track, query)
        except Exception as exc:
            logger.exception("認識後の曲検索に失敗")
            self.push("status", {"message": f"曲は特定しましたが、再生用の検索に失敗しました: {exc}"})
            return "handled"
        if not info or not info.get("video_id"):
            self.push("status", {"message": "曲は特定しましたが、YouTubeで再生可能な候補が見つかりませんでした。"})
            return "handled"
        self._local_music_playing = True
        self._local_music_context = {"title": str(info.get("title", "")), "query": query}
        self.push("local_music", {"action": "play", **info})
        self.push("status", {"message": f"再生します: {info['title']}"})
        return "handled"

    def _arm_local_humming(self, text: str) -> bool:
        from neuro_voice.music_recognition import parse_humming_recognition_request

        request = parse_humming_recognition_request(text)
        if not request.requested:
            return False
        settings = self._cfg.section("humming_recognition")
        if not bool(settings.get("enabled", False)):
            self.push("status", {"message": "鼻歌認識は未設定です。設定で有効化し、ACRCloudの接続情報を設定してください。"})
            return True
        self._local_humming_armed_until = time.monotonic() + float(settings.get("arm_timeout_s", 20.0))
        self._local_humming_play_after = request.play_after
        self.push("status", {"message": "うん、鼻歌か口笛を3〜15秒ほど聞かせてください。"})
        return True

    async def _consume_local_humming(self, audio: np.ndarray, sample_rate: int) -> bool:
        if self._local_humming_armed_until <= 0:
            return False
        if time.monotonic() > self._local_humming_armed_until:
            self._local_humming_armed_until = 0.0
            self.push("status", {"message": "鼻歌の待機時間が切れました。もう一度「鼻歌で曲を探して」と言ってください。"})
            return False
        self._local_humming_armed_until = 0.0
        play_after = self._local_humming_play_after
        self._local_humming_play_after = False
        settings = self._cfg.section("humming_recognition")
        min_samples = int(float(settings.get("min_audio_seconds", 3.0)) * sample_rate)
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if len(audio) else 0.0
        if len(audio) < min_samples or rms < float(settings.get("min_rms", 0.008)):
            self.push("status", {"message": "鼻歌が短すぎるか小さすぎます。3秒以上、なるべく一定の音程で歌ってください。"})
            return True
        from neuro_voice.music_recognition import ACRCloudHummingRecognizer

        recognizer = ACRCloudHummingRecognizer(
            host=str(settings.get("host", "")),
            access_key_env=str(settings.get("access_key_env", "ACRCLOUD_ACCESS_KEY")),
            access_secret_env=str(settings.get("access_secret_env", "ACRCLOUD_ACCESS_SECRET")),
        )
        if not recognizer.configured:
            self.push("status", {"message": "鼻歌認識のACRCloud Host / APIキーが未設定です。"})
            return True
        self.push("status", {"message": "鼻歌から曲を探しています..."})
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, recognizer.recognize, audio, sample_rate)
        except Exception as exc:
            logger.exception("ローカル鼻歌認識でエラー")
            self.push("status", {"message": f"鼻歌認識に失敗しました: {exc}"})
            return True
        if result is None:
            self.push("status", {"message": "候補を特定できませんでした。サビなど特徴的な部分を、伴奏なしで歌ってみてください。"})
            return True
        query = result.query
        self._last_local_music_query = query
        self.push("music_recognized", {"title": result.title, "artist": result.artist, "album": result.album, "query": query, "source": "humming"})
        self.push("status", {"message": f"鼻歌から特定しました: {query}"})
        if play_after and bool(self._cfg.get("music.enabled", True)):
            from neuro_voice.discord_bridge.music import resolve_track
            try:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(None, resolve_track, query)
                if info and info.get("video_id"):
                    self._local_music_playing = True
                    self._local_music_context = {"title": str(info.get("title", "")), "query": query}
                    self.push("local_music", {"action": "play", **info})
                    self.push("status", {"message": f"再生します: {info['title']}"})
                else:
                    self.push("status", {"message": "曲は特定しましたが、YouTubeの再生候補が見つかりませんでした。"})
            except Exception as exc:
                logger.exception("鼻歌認識後の再生に失敗")
                self.push("status", {"message": f"曲は特定しましたが、再生できませんでした: {exc}"})
        return True

    async def _local_music_hook(self, text: str) -> bool:
        """ローカル会話の音楽コマンド。GUI内のYouTubeプレイヤー(音あり)で再生する。

        Discord VC参加中はDiscord側 (VCへFFmpeg再生) が担当するので何もしない。
        """
        if self._arm_local_humming(text):
            return True
        recognition = await self._recognize_local_music(text)
        if recognition == "handled":
            return True
        if recognition == "continue_dialogue":
            return False
        if not bool(self._cfg.get("music.enabled", True)):
            return False
        br = self.discord
        if br is not None and br.is_connected and br.channel_name:
            return False  # VC中はDiscord経路が処理する
        from neuro_voice.discord_bridge.music import (
            extract_music_reference_candidate,
            is_music_reference,
            plan_music_intent,
            parse_music_command,
            resolve_track,
            should_execute_music_command,
        )

        cmd = parse_music_command(text, self._local_music_playing)
        if cmd is None:
            candidate = extract_music_reference_candidate(text)
            if candidate:
                self._last_local_music_query = candidate
            return False
        action, arg = cmd
        plan = None
        if bool(self._cfg.get("music.intent_planner_enabled", True)):
            history = self.conv.messages()[1:] if self.conv is not None else []
            plan = await plan_music_intent(
                self.llm, text,
                parsed_action=action, parsed_query=arg,
                music_playing=self._local_music_playing,
                now_playing=self._local_music_context,
                last_query=self._last_local_music_query,
                conversation_excerpt=history,
                timeout_s=float(self._cfg.get("music.intent_planner_timeout_s", 5.0)),
            )
        if plan is not None:
            if not plan.execute:
                logger.info("ローカル音楽操作をLLMが見送り: action=%s reason=%s text=%s",
                            action, plan.reason, text[:80])
                return False
            if action == "play" and plan.query is not None:
                arg = plan.query
        else:
            # LLM障害時だけ、従来より狭い安全な規則で継続する。
            permitted, reason = should_execute_music_command(
                text, action, assistant_addressed=False,
                one_human_conversation=True, active_exchange=True,
            )
            if not permitted:
                logger.info("ローカル音楽操作を安全規則で見送り: %s", reason)
                return False
        if action == "play":
            if arg is not None and is_music_reference(arg):
                if not self._last_local_music_query:
                    return False
                query = self._last_local_music_query
            elif arg is None:
                # おまかせ → AI自身に今の気分/好みで選曲させる
                query = await self._omakase_query()
            else:
                query = arg
            self._last_local_music_query = query
            self.push("status", {"message": f"🎵 「{query}」を探しています..."})
            loop = asyncio.get_running_loop()
            try:
                info = await loop.run_in_executor(None, resolve_track, query)
            except Exception as e:
                logger.exception("ローカル音楽の検索に失敗")
                self.push("status", {"message": f"⚠ 検索に失敗: {e} (yt-dlpは導入済み?)"})
                return True
            if not info or not info.get("video_id"):
                self.push("status", {"message": (
                    "⚠ YouTubeで見つからなかった (ツール内再生はYouTubeのみ対応)"
                )})
                return True
            self._local_music_playing = True
            self._local_music_context = {"title": str(info.get("title", "")), "query": query}
            self.push("local_music", {"action": "play", **info})
            self.push("status", {"message": f"🎵 再生中: {info['title']} (ツール内プレイヤー)"})
        elif action == "stop":
            self._local_music_playing = False
            self._local_music_context = None
            self.push("local_music", {"action": "stop"})
        elif action in ("seek", "seek_abs", "speed", "volume", "volume_set"):
            # seek/speed は数値、volume は増減量(±0.2)、volume_set は絶対値(0〜1.5)
            self.push("local_music", {"action": action, "value": float(arg)})
        elif action == "skip":
            self.push("status", {"message": (
                "ツール内再生はキュー非対応だよ。曲名で「〇〇流して」と言ってね"
            )})
        elif action == "clear_queue":
            self.push("status", {"message": "ツール内再生はキュー非対応だよ"})
        return True

    def push_event(self, event_type: str, data: dict[str, Any]) -> None:
        # Discord の状態に応じてローカルマイクを制御する
        if event_type == "discord_state":
            br = self.discord
            uses_lb_mic = br is not None and getattr(br, "uses_loopback_mic", False)
            p = self.pipeline
            if p is not None:
                connected = bool(data.get("connected"))
                in_vc = bool(data.get("channel"))
                # Discord VC にいる間の自発発話は DiscordBridge だけが
                # 担当する。ローカル TTS へ二重に流さない。
                p.set_proactive_suspended(in_vc)
                if not in_vc:
                    # Discordから完全切断 → ローカルマイクを戻す
                    p.resume_mic()
                    p.set_muted(False)
                elif uses_lb_mic:
                    # mixは同じマイクをBot側で開くため、Discord接続中は(VC参加前後を
                    # 問わず)ローカルマイクを止めっぱなしにして二重オープン競合を防ぐ。
                    # ※ログイン完了(VC未参加)イベントでうっかり再開して、Bot側が
                    #   マイクを開く瞬間に競合するのを避けるのが肝。
                    p.pause_mic()
                elif in_vc and not p.muted:
                    # 非mix(loopback単体/非loopback)はVC参加中だけミュート
                    p.set_muted(True)
                    self.push("status", {"message": (
                        "🔇 Discord通話中のため、ローカルマイクを自動でOFFにしました "
                        "(二重に聞こえるのを防ぐため。🎤ボタンで戻せます)"
                    )})
        self.push(event_type, data)

    # ---------- Discord ----------

    def start_discord(self) -> dict[str, Any]:
        """Discordブリッジをバックエンドのイベントループ上で起動する。"""
        import os

        if self.discord is not None and self.discord.is_connected:
            return {"ok": False, "message": "すでに接続しています"}
        if self._aloop is None or self.llm is None or self.tts is None or self.stt is None:
            return {"ok": False, "message": "まだ初期化中です。少し待ってから再試行してください"}
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token or "貼り付け" in token:
            return {"ok": False, "message": "Botトークンが未設定です。トークンを入力して適用してください"}
        self._cfg.set("discord.token", token)
        try:
            from neuro_voice.discord_bridge.bot import DiscordBridge
            from neuro_voice.memory.persona import build_system_prompt

            if self.discord is None:
                conv = ConversationManager(
                    system_prompt=build_system_prompt(self._cfg),
                    max_turns=int(self._cfg.get("conversation.max_turns", 20)),
                )
                self.discord = DiscordBridge(
                    self._cfg, llm=self.llm, tts=self.tts, stt=self.stt,
                    mind=self.mind, conv=conv, on_event=self.push_event,
                )
                # ローカル側の発話 (自発モード等がPC既定出力=ループバック対象で鳴る)
                # の間も取り込みをミュートし、自分の声の拾い込みを防ぐ
                self.discord.set_external_busy(
                    lambda: self.pipeline is not None
                    and self.pipeline.state == "speaking"
                )
                # VC参加中の「止めて」等をGUI内ローカルプレイヤーへ転送できるように
                self.discord.set_local_music_probe(
                    lambda: self._local_music_playing
                )
                self.discord.set_pronunciation_learning_hook(
                    self._learn_pronunciation_from_speech
                )
                self.discord.set_tts_volume_hook(self.apply_tts_volume_command)
        except RuntimeError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            logger.exception("Discordブリッジの生成に失敗")
            return {"ok": False, "message": f"Discord初期化エラー: {e}"}

        bridge = self.discord

        # mix取り込みは同じマイクをBot側で開くため、VC参加より前に(=接続時点で)
        # ローカルマイクを止めておく。参加後に止めると開くタイミングで競合し、
        # Bot側マイクにデータが来ず認識が止まることがある(順序の競合対策)。
        if str(self._cfg.get("audio.input_mode", "mic") or "mic").lower() == "mix":
            if self.pipeline is not None:
                self.pipeline.pause_mic()

        async def _run() -> None:
            try:
                await bridge.start()
            except Exception as e:
                logger.exception("Discord接続でエラー")
                msg = str(e)
                if "Improper token" in msg or "LoginFailure" in type(e).__name__:
                    msg = "トークンが正しくありません。Developer Portal で Reset Token して貼り直してください"
                self.push("error", {"message": f"Discord接続エラー: {msg}"})
                self.push("discord_state", {"connected": False, "channel": ""})

        self._discord_task = asyncio.run_coroutine_threadsafe(_run(), self._aloop)
        self.push("status", {"message": "Discord に接続しています..."})
        return {"ok": True}

    def stop_discord(self) -> dict[str, Any]:
        if self.discord is None or self._aloop is None:
            return {"ok": False, "message": "接続していません"}
        bridge = self.discord
        self.discord = None  # 次回接続時に作り直す (会話履歴もリセット)
        future = asyncio.run_coroutine_threadsafe(bridge.stop(), self._aloop)
        try:
            future.result(timeout=10)
        except Exception:
            logger.warning("Discord切断がタイムアウトしました")
        self.push("status", {"message": "Discord から切断しました"})
        return {"ok": True}

    def _shutdown_unused_tts_server(self, *, keep: str) -> None:
        """使わない方のTTSサーバーを落としてメモリ/VRAMを解放する。

        keep が SBV2 系なら VOICEVOX エンジンを、keep が VOICEVOX なら
        SBV2 サーバーを停止する。自分で起動したサーバーだけが対象
        (各 shutdown 関数が内部で判定するため、他プロセスは落とさない)。
        """
        keep = str(keep or "").lower()
        keeps_sbv2 = keep in ("style_bert_vits2", "sbv2")
        try:
            if keeps_sbv2:
                from neuro_voice.tts.voicevox_launcher import shutdown_engine

                shutdown_engine()
                logger.info("TTS切替: VOICEVOXエンジンを停止 (SBV2使用のため)")
            else:
                from neuro_voice.tts.style_bert_vits2_launcher import shutdown_server

                shutdown_server()
                logger.info("TTS切替: SBV2サーバーを停止しVRAMを解放 (VOICEVOX使用のため)")
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
        except Exception:
            logger.warning("未使用TTSサーバーの停止に失敗 (無視)", exc_info=True)

    def reload_stt(self, device: str) -> None:
        """STTを指定デバイスで再読込して差し替える (ワーカースレッドで実行)。"""
        try:
            import gc

            from neuro_voice.stt.factory import build_stt

            # 旧モデルを「先に」解放してからロードする。新旧を同時に載せると
            # (CPU small + GPU large-v3-turbo) ホストRAMを二重に使い、
            # CTranslate2/MKL が確保に失敗する (mkl_malloc: failed to allocate)。
            old = self.stt
            self.stt = None
            if self.pipeline is not None:
                self.pipeline.set_stt(None)
            if self.discord is not None:
                self.discord.set_stt(None)
            del old
            gc.collect()
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass

            new_stt = build_stt(self._cfg, device=device)
            self.stt = new_stt
            if self.pipeline is not None:
                self.pipeline.set_stt(new_stt)
            if self.discord is not None:
                self.discord.set_stt(new_stt)
            gc.collect()
            actual = getattr(new_stt, "device", device)
            _persist_yaml_value(self.config_path, "stt.device", actual)
            self.info["stt_device"] = actual
            self.push("stt_device", {"device": actual})
            self.push("info_update", {"info": self.info})
            self.push("status", {"message": f"音声認識を {actual.upper()} に切り替えました"})
        except Exception as e:
            logger.exception("STT切替に失敗")
            self.push("error", {"message": f"STT切替失敗: {e}"})
        finally:
            self._stt_switching = False

    def request_tts_switch(self, backend: str) -> dict[str, Any]:
        """Build and atomically install a TTS backend without restarting."""
        aliases = {"sbv2": "style_bert_vits2"}
        backend = aliases.get(str(backend).strip().lower(), str(backend).strip().lower())
        allowed = {"voicevox", "style_bert_vits2", "qwen3", "null"}
        if backend not in allowed:
            return {"ok": False, "message": f"未対応の読み上げ音声です: {backend}"}
        if self.tts is None:
            return {"ok": False, "message": "TTSを初期化中です。少し待ってから再試行してください"}
        if self._tts_switching:
            return {"ok": False, "message": "読み上げ音声を切り替え中です"}

        current = getattr(self.tts, "backend_name", str(self.info.get("tts", "voicevox")))
        if backend == current:
            self._cfg.set("audio_output.backend", backend)
            self.settings["audio_output_backend"] = backend
            self.info["tts"] = backend
            self.push("info_update", {"info": self.info})
            return {"ok": True, "changed": False, "backend": backend}

        # build_tts reads the selected name from Config.  Persist before the
        # worker starts so Settings and the header always share one source.
        self._cfg.set("audio_output.backend", backend)
        _persist_yaml_value(self.config_path, "audio_output.backend", backend)
        self._tts_switching = True
        self.push("tts_switching", {"backend": backend})
        self.push("status", {"message": f"読み上げ音声を {backend} に切り替えています..."})

        def _switch() -> None:
            try:
                new_tts = build_tts(
                    self._cfg,
                    on_status=lambda m: self.push("status", {"message": m}),
                )
                if not isinstance(self.tts, SwitchableTTS):
                    raise RuntimeError("実行中TTSがライブ切替に対応していません")
                self.tts.replace(new_tts, backend)
                self.settings["audio_output_backend"] = backend
                self.info["tts"] = backend
                # 切替元 (これから使わない側) のサーバーを落としてVRAM/メモリを解放する。
                # 自分で起動したサーバーだけが対象 (shutdown系は内部で判定)。
                self._shutdown_unused_tts_server(keep=backend)
                self.push("tts_changed", {"backend": backend})
                self.push("info_update", {"info": self.info})
                self.push("status", {"message": f"読み上げ音声を {backend} に切り替えました"})
            except Exception as exc:
                logger.exception("TTS切替に失敗")
                self._cfg.set("audio_output.backend", current)
                _persist_yaml_value(self.config_path, "audio_output.backend", current)
                self.settings["audio_output_backend"] = current
                self.push("tts_switch_failed", {"backend": current, "message": str(exc)})
                self.push("error", {"message": f"読み上げ音声の切替に失敗しました: {exc}"})
            finally:
                self._tts_switching = False

        threading.Thread(target=_switch, daemon=True, name="tts-switch").start()
        return {"ok": True, "changed": True, "backend": backend}


class Api:
    """JS 側 (window.pywebview.api) に公開するメソッド群。"""

    def __init__(self, backend: GuiBackend):
        self._b = backend

    def poll(self) -> dict[str, Any]:
        """イベントと現在状態をまとめて返す (JSから約80ms間隔で呼ばれる)。"""
        p = self._b.pipeline
        br = self._discord_mic_active()
        # Discordループバック(mix)中は本人マイク=Botの取り込みのミュート状態を表示する
        mic_muted = br.mic_muted if br is not None else (p.muted if p is not None else False)
        return {
            "events": self._b.drain(),
            "state": p.state if p is not None else "loading",
            "level": p.level if p is not None else 0.0,
            "muted": mic_muted,
            "speaker_muted": p.speaker_muted if p is not None else False,
            "vision": p.vision_enabled if p is not None else False,
            "search": p.search_enabled if p is not None else False,
            "proactive": p.proactive_enabled if p is not None else False,
            "cognition_session": (
                p.cognition_session_status() if p is not None else {}),
            "vision_debug": p.vision_debug_status() if p is not None else {"worker_state": "loading"},
            "discord": (
                self._b.discord is not None and self._b.discord.is_connected
            ),
            "discord_channel": (
                self._b.discord.channel_name if self._b.discord is not None else ""
            ),
        }

    def send_text(self, text: str) -> bool:
        p = self._b.pipeline
        if p is None or not text.strip():
            return False
        p.submit_text(text.strip())
        return True

    def _discord_mic_active(self):
        """Discordループバック(mix)で本人マイクを取り込んでいる bridge を返す (なければNone)。"""
        br = self._b.discord
        if br is not None and br.is_connected and getattr(br, "uses_loopback_mic", False):
            return br
        return None

    def toggle_mic(self) -> bool:
        # Discordループバック(mix)中は、Neuroが実際に聞いている「Botの本人マイク取り込み」を
        # ミュート対象にする (ツールのローカルマイクは通話中は使われていないため)。
        br = self._discord_mic_active()
        if br is not None:
            new = not br.mic_muted
            br.set_mic_muted(new)
            if self._b.pipeline is not None:
                self._b.pipeline.set_muted(new)  # ローカル側も整合させる
            return new
        p = self._b.pipeline
        if p is None:
            return False
        p.set_muted(not p.muted)
        return p.muted

    def toggle_speaker(self) -> bool:
        """ローカルスピーカーのミュート切替 (Discordの音声はそのまま)。"""
        p = self._b.pipeline
        if p is None:
            return False
        new_state = not p.speaker_muted
        p.set_speaker_muted(new_state)
        self._b.push("status", {"message": (
            "🔇 ローカルスピーカーをミュートしました (Discord側の声はそのまま出ます)"
            if new_state else "🔊 ローカルスピーカーをONにしました"
        )})
        return new_state

    def interrupt(self) -> None:
        if self._b.pipeline is not None:
            self._b.pipeline.interrupt()

    def reset(self) -> None:
        if self._b.pipeline is not None:
            self._b.pipeline.reset_conversation()

    def info(self) -> dict[str, Any]:
        return self._b.info

    def set_tts_backend(self, backend: str) -> dict[str, Any]:
        """トップ画面から読み上げ音声をライブ切替する。"""
        return self._b.request_tts_switch(backend)

    def get_tts_volume(self) -> dict[str, Any]:
        value = float(getattr(
            self._b.tts, "volume", self._b.cfg.get("tts.output_volume", 1.0),
        ))
        return {"ok": True, "volume": value, "percent": int(round(value * 100))}

    def set_tts_volume(self, percent: Any) -> dict[str, Any]:
        try:
            value = self._b.set_tts_volume(float(percent) / 100.0)
            return {"ok": True, "volume": value, "percent": int(round(value * 100))}
        except (TypeError, ValueError):
            return {"ok": False, "message": "音量は0〜200の数値で指定してください"}

    def get_speakers(self) -> dict[str, Any]:
        """現在のTTSバックエンドの話者一覧と選択値を返す。"""
        tts = self._b.tts
        if tts is None or not hasattr(tts, "list_speakers"):
            return {"ok": False, "message": "この読み上げ音声は話者選択に対応していません"}
        try:
            backend = str(getattr(tts, "backend_name", "voicevox"))
            speakers = []
            for speaker in tts.list_speakers():
                item = dict(speaker)
                key = str(item.get("key", item.get("id", "")))
                item["id"] = key
                item["key"] = key
                item["backend"] = backend
                speakers.append(item)
            return {
                "ok": True,
                "backend": backend,
                "speakers": speakers,
                "current": str(tts.speaker),
            }
        except Exception as e:
            return {"ok": False, "message": f"話者一覧を取得できません: {e}"}

    def set_speaker(self, speaker_id: Any, label: str = "") -> bool:
        """話者を切り替え、現在のペルソナ専用設定として保存する。"""
        tts = self._b.tts
        if tts is None or not hasattr(tts, "set_speaker"):
            return False
        cfg = self._b.cfg
        active, _ = get_active_persona(cfg)
        backend = str(getattr(tts, "backend_name", "voicevox"))
        if backend not in ("voicevox", "style_bert_vits2"):
            self._b.push("error", {"message": f"{backend} は話者選択に対応していません"})
            return False
        if backend == "style_bert_vits2":
            tts.set_speaker(str(speaker_id))
            inner = getattr(tts, "inner", tts)
            model = str(getattr(inner, "model_name", "") or "")
            sid = int(getattr(inner, "speaker_id", 0))
            persona_model_key = (
                f"persona.presets.{active}.style_bert_vits2_model"
                if active else "tts.style_bert_vits2.model_name"
            )
            persona_speaker_key = (
                f"persona.presets.{active}.style_bert_vits2_speaker_id"
                if active else "tts.style_bert_vits2.speaker_id"
            )
            cfg.set(persona_model_key, model)
            cfg.set(persona_speaker_key, sid)
            cfg.set("tts.style_bert_vits2.model_name", model)
            cfg.set("tts.style_bert_vits2.speaker_id", sid)
            saved = all((
                _persist_yaml_value(self._b.config_path, persona_model_key, model),
                _persist_yaml_value(self._b.config_path, persona_speaker_key, sid),
                _persist_yaml_value(self._b.config_path, "tts.style_bert_vits2.model_name", model),
                _persist_yaml_value(self._b.config_path, "tts.style_bert_vits2.speaker_id", sid),
            ))
            selected_key = f"{model}::{sid}"
        else:
            sid = int(speaker_id)
            tts.set_speaker(sid)
            persona_key = f"persona.presets.{active}.voicevox_speaker" if active else "tts.voicevox.speaker"
            cfg.set(persona_key, sid)
            cfg.set("tts.voicevox.speaker", sid)  # 旧設定・非GUI起動との互換用
            saved = (
                _persist_yaml_value(self._b.config_path, persona_key, sid)
                and _persist_yaml_value(self._b.config_path, "tts.voicevox.speaker", sid)
            )
            selected_key = str(sid)
        note = "" if saved else " (config.yaml への保存は失敗)"
        self._b.push("status", {"message": f"話者を {label or speaker_id} に変更しました{note}"})
        self._b.push("speaker_changed", {"speaker": selected_key})
        return True

    def get_settings(self) -> dict[str, Any]:
        """設定パネル用の現在値と選択肢を返す。"""
        s = dict(self._b.settings)
        s["models"] = dict(s["models"])
        # Ollama が未起動だと接続確認に最大 6 秒かかるため、設定表示後に取得する。
        s["ollama_models"] = []
        s["ollama_models_loading"] = True
        s["cerebras_models"] = ["gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7"]
        s["persona"] = self.get_persona()
        pipeline = self._b.pipeline
        s["cognition_test_session"] = (
            pipeline.cognition_session_status() if pipeline is not None else {})
        s["api_keys"] = self.get_api_keys()
        s["discord"] = self.get_discord_settings()
        return s

    def set_cognition_test_session(self, active: bool | None = None) -> dict[str, Any]:
        """Toggle only the live process; config.yaml is intentionally untouched."""
        pipeline = self._b.pipeline
        if pipeline is None:
            return {"ok": False, "message": "まだ初期化中です"}
        if active is None:
            active = not pipeline.cognition_session_status()["effective_cognition_enabled"]
        status = pipeline.start_cognition_test_session(bool(active))
        message = ("認知テストセッションを開始しました"
                   if status["effective_cognition_enabled"]
                   else "認知テストセッションを終了しました")
        if status["pending"]:
            message += "（現在の会話が終わってから反映）"
        self._b.push("status", {"message": message})
        return {"ok": True, **status, "message": message}

    def set_cognition_session(self, mode: str = "disabled") -> dict[str, Any]:
        """Set OFF / TEST / PROD SESSION only for the current process."""
        pipeline = self._b.pipeline
        if pipeline is None:
            return {"ok": False, "message": "まだ初期化中です"}
        mode = str(mode or "disabled")
        if mode not in {"disabled", "test_session", "production_session"}:
            return {"ok": False, "message": "認知セッションの指定が不正です"}
        status = pipeline.start_cognition_session(mode)
        labels = {
            "disabled": "認知をOFFにしました",
            "test_session": "認知テストセッションを開始しました",
            "production_session": "認知PRODセッションを開始しました",
        }
        message = labels[mode]
        if status["pending"]:
            message += "（現在の会話が終わってから反映）"
        self._b.push("status", {"message": message})
        return {"ok": True, **status, "message": message}

    def get_ollama_models(self) -> dict[str, list[str]]:
        """設定画面を開いた後に、Ollama のモデル候補を遅延取得する。"""
        return {"models": self._b.ollama_models()}

    def test_vision_connection(self) -> dict[str, Any]:
        """Probe Ollama/model availability without logging or storing media."""
        from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient

        cfg = self._b.cfg
        base = str(cfg.get("llm.backends.ollama.base_url", "http://localhost:11434/v1"))
        model = str(cfg.get("vision.model", "gemma4-12b-qat"))
        try:
            async def _probe():
                client = OllamaMultimodalClient(base, model)
                try:
                    return await client.probe(check_image=True, check_audio=True)
                finally:
                    await client.aclose()

            caps = asyncio.run(_probe())
            return {
                "ok": caps.connected and caps.model_present,
                "connected": caps.connected, "model_present": caps.model_present,
                "image_input": caps.image_input, "audio_input": caps.audio_input,
                "version": caps.version, "message": caps.detail,
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def list_capture_windows(self) -> dict[str, Any]:
        """List user-selectable visible desktop windows without capturing them."""
        try:
            from neuro_voice.vision.capture import list_capture_windows

            return {"windows": list_capture_windows(), "error": ""}
        except Exception as exc:
            logger.exception("Unable to list capture windows")
            return {"windows": [], "error": str(exc)}

    def list_obs_sources(
        self, host: str = "127.0.0.1", port: int = 4455, password: str = "",
    ) -> dict[str, Any]:
        """List OBS inputs, preferring Game Capture sources in the UI."""
        from neuro_voice.vision.obs import ObsCapture

        cfg = self._b.cfg
        secret = str(password or "") or str(cfg.get("video.obs.password", ""))
        capture = ObsCapture(
            host or str(cfg.get("video.obs.host", "127.0.0.1")),
            int(port or cfg.get("video.obs.port", 4455)), secret, "",
            timeout=float(cfg.get("video.obs.request_timeout_sec", 3.0)),
        )

        async def run() -> dict[str, Any]:
            try:
                version = await capture.client.probe()
                sources = await capture.list_sources()
                return {
                    "ok": True,
                    "sources": sources,
                    "obs_version": version.get("obsVersion") or version.get("obs_version", ""),
                    "websocket_version": version.get("obsWebSocketVersion") or version.get("websocket_version", ""),
                }
            finally:
                await capture.close()

        try:
            return asyncio.run(run())
        except Exception as exc:
            logger.warning("Unable to list OBS sources: %s", exc)
            return {"ok": False, "sources": [], "message": str(exc)}

    def test_obs_connection(
        self, host: str = "127.0.0.1", port: int = 4455,
        password: str = "", source_name: str = "",
    ) -> dict[str, Any]:
        """Validate OBS auth, source selection and a real fresh screenshot."""
        import base64
        from neuro_voice.vision.obs import ObsCapture

        cfg = self._b.cfg
        secret = str(password or "") or str(cfg.get("video.obs.password", ""))
        capture = ObsCapture(
            host or str(cfg.get("video.obs.host", "127.0.0.1")),
            int(port or cfg.get("video.obs.port", 4455)), secret,
            source_name or str(cfg.get("video.obs.source_name", "")),
            max_width=640, jpeg_quality=70,
            timeout=float(cfg.get("video.obs.request_timeout_sec", 3.0)),
        )

        async def run() -> dict[str, Any]:
            try:
                await capture.start()
                media = await capture.capture_media()
                return {
                    "ok": True,
                    "message": f"OBS接続・最新フレーム取得成功: {capture.source_name}",
                    "source": capture.source_name,
                    "image": base64.b64encode(media.data or b"").decode("ascii"),
                    "latency_ms": media.metadata.get("capture_latency_ms", 0),
                }
            finally:
                await capture.close()

        try:
            return asyncio.run(run())
        except Exception as exc:
            logger.warning("OBS connection test failed: %s", exc)
            return {"ok": False, "message": str(exc)}

    def select_foreground_capture_window(self, delay_seconds: float = 3.0) -> dict[str, Any]:
        """Let the user foreground an exclusive-fullscreen game before resolving its HWND."""
        delay = max(1.0, min(10.0, float(delay_seconds)))
        time.sleep(delay)
        try:
            import ctypes
            from ctypes import wintypes
            from neuro_voice.vision.capture import describe_capture_window

            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            handle = user32.GetForegroundWindow()
            if not handle:
                return {"ok": False, "message": "前面ウィンドウを取得できませんでした。"}
            window = describe_capture_window(handle)
            return {"ok": True, "window": window}
        except Exception as exc:
            logger.warning("Unable to select foreground capture window: %s", exc)
            return {"ok": False, "message": str(exc)}

    def set_video_capture_window(self, window_handle: str | int) -> dict[str, Any]:
        """Persist a picker result and swap the running capture source immediately."""
        try:
            from neuro_voice.vision.capture import describe_capture_window

            window = describe_capture_window(window_handle)
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        b = self._b
        handle = str(window["handle"])
        b.cfg.set("video.enabled", True)
        b.cfg.set("video.input", "window")
        b.cfg.set("video.window_handle", handle)
        saved = all([
            _persist_yaml_value(b.config_path, "video.enabled", "true"),
            _persist_yaml_value(b.config_path, "video.input", "window"),
            _persist_yaml_value(b.config_path, "video.window_handle", handle),
        ])
        if b.pipeline is not None and b._aloop is not None and getattr(b.pipeline, "_loop", None) is not None:
            try:
                applied = asyncio.run_coroutine_threadsafe(
                    b.pipeline.refresh_video_observation(), b._aloop,
                ).result(timeout=5)
                if not applied:
                    return {"ok": False, "message": "Game video input could not be started"}
            except Exception as exc:
                logger.warning("Unable to apply selected game window: %s", exc, exc_info=True)
                return {"ok": False, "message": f"Game video input update failed: {exc}"}
        return {"ok": True, "window": window, "saved": saved}

    def video_preview(
        self, input_kind: str = "camera", source: str | int | None = None,
        screen_monitor: str | int | None = None, window_handle: str | int | None = None,
        obs_host: str | None = None, obs_port: int | None = None,
        obs_password: str | None = None, obs_source: str | None = None,
    ) -> dict[str, Any]:
        """Capture one private preview from a camera or the selected screen."""
        kind = str(input_kind).strip().lower()
        if kind == "obs":
            return self.test_obs_connection(
                obs_host or str(self._b.cfg.get("video.obs.host", "127.0.0.1")),
                int(obs_port or self._b.cfg.get("video.obs.port", 4455)),
                obs_password or "",
                obs_source or str(self._b.cfg.get("video.obs.source_name", "")),
            )
        if kind == "window":
            import base64
            try:
                from neuro_voice.vision.capture import WindowCapture

                handle = window_handle if window_handle not in (None, "") else self._b.cfg.get("video.window_handle", "")
                capture = WindowCapture(
                    handle, max_width=960, jpeg_quality=75,
                    backend=str(self._b.cfg.get("video.window_capture_backend", "auto")),
                )
                try:
                    media = capture.capture_media()
                finally:
                    capture.close()
                return {"ok": True, "image": base64.b64encode(media.data or b"").decode("ascii")}
            except Exception as exc:
                logger.warning("Window preview failed: %s", exc)
                return {"ok": False, "message": str(exc)}
        if kind == "screen":
            import base64
            try:
                from neuro_voice.vision.capture import ScreenCapture

                monitor = screen_monitor if screen_monitor not in (None, "") else self._b.cfg.get(
                    "video.screen_monitor", self._b.cfg.get("vision.monitor", "auto"),
                )
                capture = ScreenCapture(monitor=monitor, max_width=960, jpeg_quality=75)
                media = capture.capture_media()
                return {"ok": True, "image": base64.b64encode(media.data or b"").decode("ascii")}
            except Exception as exc:
                logger.warning("Screen preview failed: %s", exc)
                return {"ok": False, "message": str(exc)}
        return self.camera_preview(source)

    def camera_preview(self, source: str | int | None = None) -> dict[str, Any]:
        """Capture one private preview frame; it is not saved to disk/history."""
        import base64

        try:
            import cv2
            from neuro_voice.vision.media import prepare_image

            value = source if source is not None else self._b.cfg.get("video.source", 0)
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
            cap = cv2.VideoCapture(value)
            try:
                if not cap.isOpened():
                    return {"ok": False, "message": "カメラを開けません"}
                ok, frame = cap.read()
                if not ok:
                    return {"ok": False, "message": "カメラからフレームを取得できません"}
                ok, encoded = cv2.imencode(".jpg", frame)
                if not ok:
                    return {"ok": False, "message": "プレビュー画像の変換に失敗"}
                media = prepare_image(bytes(encoded), max_width=640, max_height=480, jpeg_quality=75)
                return {"ok": True, "image": base64.b64encode(media.data or b"").decode("ascii")}
            finally:
                cap.release()
        except ImportError:
            return {"ok": False, "message": "カメラプレビューには opencv-python が必要です"}
        except Exception as exc:
            logger.warning("カメラプレビューに失敗: %s", exc)
            return {"ok": False, "message": str(exc)}

    def get_discord_settings(self) -> dict[str, Any]:
        import os

        cfg = self._b.cfg
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        wake = cfg.get("discord.wake_words") or ["ニューロ", "neuro"]
        b = self._b.discord
        return {
            "token_set": bool(token) and "貼り付け" not in token,
            "respond_all": bool(cfg.get("discord.respond_all", False)),
            "wake_words": ", ".join(str(w) for w in wake),
            "follow_up_s": float(cfg.get("discord.follow_up_s", 30)),
            "echo_text": bool(cfg.get("discord.echo_text", False)),
            "known_voices_only": bool(cfg.get("discord.known_voices_only", False)),
            "greet_on_join": bool(cfg.get("discord.greet_on_join", True)),
            "receive_mode": str(cfg.get("discord.receive_mode", "loopback")),
            "screen_share_enabled": bool(cfg.get("discord.screen_share.enabled", True)),
            "screen_share_obs_source": str(
                cfg.get(
                    "discord.screen_share.obs_source_name",
                    cfg.get("video.obs.source_name", ""),
                )
                or ""
            ),
            "screen_share_status": (
                {
                    "active": b._screen_share_session_obj.active,
                    "source_name": b._screen_share_session_obj.source_name,
                }
                if b is not None and b._screen_share_session_obj is not None
                else {"active": False}
            ),
            "connected": b is not None and b.is_connected,
            "channel": b.channel_name if b is not None else "",
            "members": b.voice_members() if b is not None else [],
        }

    def set_discord_settings(self, s: dict[str, Any]) -> dict[str, Any]:
        """Discord設定を .env / config.yaml へ保存し、接続中なら即反映する。"""
        import os

        b = self._b
        cfg = b.cfg
        s = s or {}
        # トークン (空欄は変更なし)
        token = str(s.get("token", "") or "").strip()
        if token:
            os.environ["DISCORD_BOT_TOKEN"] = token
            cfg.set("discord.token", token)
            env_path = (
                b.config_path.parent.parent / ".env" if b.config_path else Path(".env")
            )
            _persist_env_value(env_path, "DISCORD_BOT_TOKEN", token)

        respond_all = bool(s.get("respond_all", False))
        echo_text = bool(s.get("echo_text", False))
        wake_words = [w.strip() for w in str(s.get("wake_words", "")).replace("、", ",").split(",")
                      if w.strip()] or ["ニューロ", "neuro"]
        try:
            follow_up_s = max(0.0, float(s.get("follow_up_s", 30)))
        except (TypeError, ValueError):
            follow_up_s = 30.0
        known_only = bool(s.get("known_voices_only", False))
        greet_on_join = bool(s.get("greet_on_join", True))
        screen_share_enabled = bool(s.get("screen_share_enabled", True))
        screen_share_obs_source = str(s.get("screen_share_obs_source", "") or "").strip()
        receive_mode = str(s.get("receive_mode", cfg.get("discord.receive_mode", "loopback"))).lower()
        if receive_mode not in ("direct_dave", "loopback", "auto"):
            receive_mode = "loopback"
        cfg.set("discord.respond_all", respond_all)
        cfg.set("discord.echo_text", echo_text)
        cfg.set("discord.wake_words", wake_words)
        cfg.set("discord.known_voices_only", known_only)
        cfg.set("discord.greet_on_join", greet_on_join)
        cfg.set("discord.screen_share.enabled", screen_share_enabled)
        cfg.set("discord.screen_share.obs_source_name", screen_share_obs_source)
        cfg.set("discord.receive_mode", receive_mode)
        cfg.set("discord.follow_up_s", follow_up_s)
        path = b.config_path
        _persist_yaml_value(path, "discord.respond_all", "true" if respond_all else "false")
        _persist_yaml_value(path, "discord.echo_text", "true" if echo_text else "false")
        _persist_yaml_value(path, "discord.known_voices_only", "true" if known_only else "false")
        _persist_yaml_value(path, "discord.greet_on_join", "true" if greet_on_join else "false")
        _persist_yaml_value(
            path, "discord.screen_share.enabled",
            "true" if screen_share_enabled else "false",
        )
        _persist_yaml_value(
            path, "discord.screen_share.obs_source_name", screen_share_obs_source,
        )
        _persist_yaml_value(path, "discord.receive_mode", receive_mode)
        _persist_yaml_value(path, "discord.wake_words", "[" + ", ".join(wake_words) + "]")
        _persist_yaml_value(path, "discord.follow_up_s", follow_up_s)
        if b.discord is not None:
            b.discord.update_settings(
                respond_all=respond_all, wake_words=wake_words, echo_text=echo_text,
                follow_up_s=follow_up_s, greet_on_join=greet_on_join, receive_mode=receive_mode,
            )
        return {"ok": True, "token_saved": bool(token)}

    def discord_connect(self) -> dict[str, Any]:
        return self._b.start_discord()

    def discord_disconnect(self) -> dict[str, Any]:
        return self._b.stop_discord()

    def discord_channels(self) -> dict[str, Any]:
        """参加可能なVC一覧 (Discordタブのドロップダウン用)。"""
        b = self._b.discord
        if b is None or not b.is_connected:
            return {"ok": False, "channels": [], "message": "先にDiscordへ接続してください"}
        return {"ok": True, "channels": b.list_voice_channels(), "current": b.channel_name}

    def _run_discord_coro(self, coro, timeout: float = 20.0) -> dict[str, Any]:
        loop = self._b._aloop
        if loop is None:
            return {"ok": False, "message": "まだ初期化中です"}
        import concurrent.futures

        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return {"ok": False, "message": "タイムアウトしました"}
        except Exception as e:
            logger.exception("Discord操作でエラー")
            return {"ok": False, "message": str(e)}

    def discord_join(self, channel_id: str = "") -> dict[str, Any]:
        """GUIボタンからVCへ参加する。channel_id 空なら人がいるVCへ自動参加。"""
        b = self._b.discord
        if b is None or not b.is_connected:
            return {"ok": False, "message": "先にDiscordへ接続してください"}
        return self._run_discord_coro(b.join_channel(str(channel_id) or None))

    def local_music_ended(self) -> dict[str, Any]:
        """GUI内プレイヤーの再生終了通知 (曲が最後まで到達/停止)。"""
        self._b._local_music_playing = False
        self._b._local_music_context = None
        return {"ok": True}

    def open_music_page(self, url: str) -> dict[str, Any]:
        """再生中の動画をブラウザで開く (Discord画面共有用)。"""
        import webbrowser

        url = str(url or "")
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "message": "URLが不正です"}
        webbrowser.open(url)
        self._b.push("status", {"message": (
            "🖥 ブラウザで開きました。Discordの「画面を共有」でそのウィンドウ/タブを選ぶと"
            "みんなに映像を見せられます (音はニューロが流しているので、共有時は"
            "「音声を共有」をOFFにするか、ニューロ側を「止めて」と言ってね)"
        )})
        return {"ok": True}

    def discord_quick(self) -> dict[str, Any]:
        """🎧ボタン: 状況に応じて 接続→VC参加 / VC参加 / VC退出 を1タップで行う。"""
        b = self._b
        br = b.discord
        if br is not None and br.is_connected:
            if br.channel_name:
                return self._run_discord_coro(br.leave())  # VC参加中 → 退出
            return self._run_discord_coro(br.join_channel(None))  # 接続済み → 参加
        # 未接続 → 接続し、完了したら自動でVCへ
        r = b.start_discord()
        if not r.get("ok"):
            return r
        if b.discord is not None:
            b.discord.request_auto_join()
        return {"ok": True, "message": "Discordに接続後、自動でVCへ参加します"}

    def discord_leave_vc(self) -> dict[str, Any]:
        """GUIボタンからVCを退出する。"""
        b = self._b.discord
        if b is None or not b.is_connected:
            return {"ok": False, "message": "Discordに接続していません"}
        return self._run_discord_coro(b.leave())

    def discord_invite(self) -> dict[str, Any]:
        """サーバーへの再招待リンクを既定ブラウザで開く。

        サーバーからキックされるとBotは自力で戻れないため、OAuth招待リンクを
        開いて手動で再認証してもらう。接続の有無に関わらず動く
        (トークンからClient IDを導出できるため)。
        """
        import os
        import webbrowser

        from neuro_voice.discord_bridge.bot import (
            build_invite_url,
            client_id_from_token,
        )

        url = None
        br = self._b.discord
        if br is not None:
            try:
                url = br.invite_url()
            except Exception:
                logger.exception("招待URLの取得に失敗")
        if not url:
            token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
            if not token or "貼り付け" in token:
                return {"ok": False, "message": "Botトークンが未設定です。トークンを入力して適用してください"}
            cid = client_id_from_token(token)
            if not cid:
                return {"ok": False, "message": "トークンからBotのIDを読み取れませんでした。トークンを確認してください"}
            url = build_invite_url(cid)
        try:
            webbrowser.open(url)
        except Exception:
            logger.exception("ブラウザで招待URLを開けませんでした")
        return {"ok": True, "url": url}

    # ---------- 音声入力ソース (Discord通話のループバック取り込み) ----------

    def list_output_devices(self) -> dict[str, Any]:
        """ループバック取り込み先の出力デバイス一覧 (設定ドロップダウン用)。"""
        try:
            from neuro_voice.audio.loopback import list_output_devices

            return {"ok": True, "devices": list_output_devices()}
        except Exception as e:
            logger.exception("出力デバイス一覧の取得に失敗")
            return {"ok": False, "devices": [], "message": str(e)}

    def list_audio_devices(self) -> dict[str, Any]:
        """Expose output endpoints and the stricter loopback-capable subset separately."""
        try:
            from neuro_voice.audio.loopback import soundcard_inventory

            inventory = soundcard_inventory()
            try:
                import sounddevice as sd

                system = sd.query_devices()
                microphones = [
                    {"id": str(i), "name": str(d["name"]), "channels": int(d["max_input_channels"])}
                    for i, d in enumerate(system) if int(d["max_input_channels"]) > 0
                ]
                playback_outputs = [
                    {"id": str(i), "name": str(d["name"]), "channels": int(d["max_output_channels"])}
                    for i, d in enumerate(system) if int(d["max_output_channels"]) > 0
                ]
            except Exception as exc:
                logger.warning("sounddevice inventory unavailable: %s", exc)
                microphones, playback_outputs = [], []
            return {
                "ok": True, **inventory,
                "physical_microphones": microphones,
                "playback_outputs": playback_outputs,
            }
        except Exception as e:
            logger.exception("音声デバイス一覧の取得に失敗")
            return {
                "ok": False, "outputs": [], "loopback_outputs": [],
                "microphones": [], "physical_microphones": [], "playback_outputs": [],
                "message": str(e),
            }

    def redetect_audio_devices(self) -> dict[str, Any]:
        """Look for the microphone and speaker again, on request.

        The background watcher only recovers a stream that was open and then
        stopped.  A headset that was never seen at start-up, or one Windows
        moved to a new endpoint while the old handle still claims to be alive,
        needs an explicit "look again" — otherwise the only remedy is
        restarting the application.
        """
        from neuro_voice.utils.errors import safe_error_text as _safe_error_text

        pipeline = self._b.pipeline
        loop = self._b._aloop
        if pipeline is None or loop is None or loop.is_closed():
            return {
                "ok": False, "microphone": "", "speaker": "", "used_fallback": False,
                "message": "ニューロが起動していません", "error": "",
            }
        try:
            future = asyncio.run_coroutine_threadsafe(
                pipeline.redetect_audio_devices(), loop,
            )
            return future.result(timeout=20)
        except Exception as e:
            logger.exception("マイクの再認識に失敗")
            return {
                "ok": False, "microphone": "", "speaker": "", "used_fallback": False,
                "message": "マイクの再認識に失敗しました", "error": _safe_error_text(e),
            }

    def get_audio_input_settings(self) -> dict[str, Any]:
        cfg = self._b.cfg
        dev = cfg.get("audio.discord_capture_device") or cfg.get("audio.loopback_device")
        return {
            "mode": str(cfg.get("audio.input_mode", "mic") or "mic"),
            "loopback_device": "" if dev is None else str(dev),
            "discord_capture_device": "" if dev is None else str(dev),
            "microphone_device": "" if cfg.get("audio.input_device") is None else str(cfg.get("audio.input_device")),
            "monitor_output_device": "" if cfg.get("audio.monitor_output_device") is None else str(cfg.get("audio.monitor_output_device")),
            "ai_output_device": "" if (cfg.get("audio.ai_output_device") or cfg.get("audio.output_device")) is None else str(cfg.get("audio.ai_output_device") or cfg.get("audio.output_device")),
            "mute_on_speak": bool(cfg.get("audio.loopback_mute_on_speak", False)),
        }

    def set_audio_input_settings(self, s: dict[str, Any]) -> dict[str, Any]:
        """入力ソースを config.yaml に保存する (反映は再起動時)。"""
        path = self._b.config_path
        mode = str(s.get("mode", "mic") or "mic").lower()
        if mode not in ("mic", "loopback", "mix"):
            mode = "mic"
        dev = s.get("discord_capture_device", s.get("loopback_device"))
        mic_device = s.get("microphone_device")
        monitor_device = s.get("monitor_output_device")
        ai_output_device = s.get("ai_output_device")
        mute = bool(s.get("mute_on_speak", False))
        def _system_device(value):
            if value in (None, "", "null"):
                return None
            text = str(value)
            return int(text) if text.isdigit() else text
        mic_device = _system_device(mic_device)
        monitor_device = _system_device(monitor_device)
        ai_output_device = _system_device(ai_output_device)
        cfg = self._b.cfg
        # soundcardはスピーカー名(文字列)で参照する。空=既定スピーカー
        dev_val = None if dev in (None, "", "null") else str(dev)
        # 実行中の設定にも即反映 (次のDiscord再接続で有効。全体再起動は不要)
        cfg.set("audio.input_mode", mode)
        cfg.set("audio.loopback_device", dev_val)
        cfg.set("audio.discord_capture_device", dev_val)
        cfg.set("audio.input_device", mic_device)
        cfg.set("audio.monitor_output_device", monitor_device)
        cfg.set("audio.ai_output_device", ai_output_device)
        cfg.set("audio.loopback_mute_on_speak", mute)
        ok = _persist_yaml_value(path, "audio.input_mode", mode)
        _persist_yaml_value(path, "audio.loopback_device",
                            "null" if dev_val is None else f'"{dev_val}"')
        _persist_yaml_value(path, "audio.discord_capture_device",
                            "null" if dev_val is None else f'"{dev_val}"')
        _persist_yaml_value(path, "audio.input_device",
                            "null" if mic_device is None else mic_device)
        _persist_yaml_value(path, "audio.monitor_output_device",
                            "null" if monitor_device is None else monitor_device)
        _persist_yaml_value(path, "audio.ai_output_device",
                            "null" if ai_output_device is None else ai_output_device)
        _persist_yaml_value(path, "audio.loopback_mute_on_speak",
                            "true" if mute else "false")
        return {"ok": bool(ok), "restart": True,
                "message": "保存しました。ニューロを再接続すると反映されます"}

    def get_persona(self) -> dict[str, Any]:
        """ペルソナプリセット一覧と現在の選択 (設定パネルのペルソナタブ用)。"""
        cfg = self._b.cfg
        p = cfg.section("persona")
        presets = p.get("presets") or {}
        if not isinstance(presets, dict) or not presets:
            # 旧フラット形式は "default" という単一プリセットとして見せる
            presets = {"default": p}
        active, _ = get_active_persona(cfg)
        active = active or "default"
        out = {}
        for pname, pv in presets.items():
            pv = pv or {}
            out[pname] = {
                "label": str(pv.get("name", pname)),
                "fields": [
                    {"key": key, "label": label, "placeholder": ph,
                     "value": str(pv.get(key, "") or "")}
                    for key, label, ph in PERSONA_FIELDS
                ],
                "system_prompt": str(pv.get("system_prompt", "")),
                "conversation_traits": normalize_persona_traits(
                    pv.get("conversation_traits") if isinstance(pv, dict) else None
                ),
            }
        return {
            "active": active,
            "presets": out,
            "composed": build_system_prompt(cfg),
            "conversation_trait_labels": [
                {"key": key, "label": label} for key, label in PERSONA_TRAIT_LABELS
            ],
        }

    def create_persona(self, data: dict[str, Any]) -> dict[str, Any]:
        """新しいペルソナを1つ作る。**切り替えはしない。**

        作った直後に切り替えると、記憶も関係値も空の人格へいきなり
        入れ替わる。名前と性格を書いてから「適用」で切り替える方が、
        取り消しが効く。

        中身は**空**で作る。既存プリセットから中身を写すと、新しい
        ペルソナが最初から他人の口調と設定を持つことになり、後から
        「なぜこの喋り方なのか」が追えなくなる。
        """
        b = self._b
        cfg = b.cfg
        key = str(data.get("key", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        if not _PERSONA_KEY_RE.match(key):
            return {"ok": False, "message": (
                "IDは半角英小文字で始め、英小文字・数字・_ のみ、32文字までにしてください"
            )}
        if not name:
            return {"ok": False, "message": "表示名を入力してください"}
        presets = cfg.section("persona").get("presets") or {}
        if not isinstance(presets, dict) or not presets:
            return {"ok": False, "message": (
                "config.yaml が旧形式（presets 無し）です。先に presets 形式へ移してください"
            )}
        if key in presets:
            return {"ok": False, "message": f"そのIDは既にあります: {key}"}

        # **既定値は最小限。** 空欄は System Prompt に含まれないので、
        # 書かれていない項目が勝手な人格を作ることはない。
        values: dict[str, Any] = {"name": name}
        for field, _, _ in PERSONA_FIELDS:
            if field != "name":
                values[field] = ""
        values["voicevox_speaker"] = int(cfg.get("tts.voicevox.speaker", 2) or 2)
        values["style_bert_vits2_model"] = str(
            cfg.get("tts.style_bert_vits2.model_name", "") or "")
        values["style_bert_vits2_speaker_id"] = int(
            cfg.get("tts.style_bert_vits2.speaker_id", 0) or 0)

        traits = normalize_persona_traits(None)
        if not _persist_yaml_new_preset(b.config_path, key, values, "", traits):
            return {"ok": False, "message": "config.yaml へ書き込めませんでした"}
        # 実行中の設定にも反映する。再起動を待たずに一覧へ出す。
        base = f"persona.presets.{key}."
        for field, value in values.items():
            cfg.set(base + field, value)
        cfg.set(base + "system_prompt",
                f"あなたは「{name}」という名前のAIアシスタント。")
        cfg.set(base + "conversation_traits", traits)
        logger.info("ペルソナを追加: %s (%s)", name, key)
        return {"ok": True, "key": key,
                "message": f"「{name}」を追加しました。内容を書いてから「適用」で切り替えます",
                "persona": self.get_persona()}

    def delete_persona(self, data: dict[str, Any]) -> dict[str, Any]:
        """ペルソナを一覧から消す。**記憶ファイルは消さない。**

        `data/mind_<key>.db` などをここで消すと、**戻せない。**
        設定を1つ消したつもりで、そのペルソナと話した記録が全部
        無くなる。一覧から外すだけなら、`config.yaml` へプリセットを
        書き戻せば元に戻る——同じ `key` で作り直せば記憶も戻る。

        消さない代わりに、**どのファイルが残っているかを返す。**
        本当に消したい時は、それを見て自分で消せる。

        使用中のペルソナと、最後の1つは消さない。前者は今まさに
        喋っている人格が消えることになり、後者は選べるものが
        無くなる。
        """
        b = self._b
        cfg = b.cfg
        key = str(data.get("key", "")).strip()
        presets = cfg.section("persona").get("presets") or {}
        if not isinstance(presets, dict) or key not in presets:
            return {"ok": False, "message": f"そのペルソナはありません: {key}"}
        active, _ = get_active_persona(cfg)
        if key == str(active):
            return {"ok": False, "message": (
                "使用中のペルソナは削除できません。別のペルソナへ切り替えてからどうぞ"
            )}
        if len(presets) <= 1:
            return {"ok": False, "message": "最後のペルソナは削除できません"}

        if not _remove_yaml_preset(b.config_path, key):
            return {"ok": False, "message": "config.yaml から削除できませんでした"}
        presets.pop(key, None)
        left = _persona_data_files(
            Path(str(cfg.get("mind.data_dir", "data"))), key)
        logger.info("ペルソナを削除: %s（記憶ファイル %s 件は残す）", key, len(left))
        message = f"「{key}」を一覧から削除しました"
        if left:
            message += (
                f"。記憶などのファイル {len(left)} 件は data フォルダに残しています"
                "（同じIDで作り直すと元に戻ります）"
            )
        return {"ok": True, "key": key, "message": message,
                "remaining_files": left, "persona": self.get_persona()}

    def apply_persona(self, data: dict[str, Any]) -> dict[str, Any]:
        """ペルソナの切替・編集を実行時に反映し、config.yaml に保存する。"""
        b = self._b
        cfg = b.cfg
        p = cfg.section("persona")
        presets = p.get("presets") or {}
        has_presets = isinstance(presets, dict) and bool(presets)
        active = str(data.get("active", "")).strip()
        previous_active, _ = get_active_persona(cfg)
        current_tts_backend = str(getattr(b.tts, "backend_name", "voicevox"))
        current_tts_speaker = getattr(b.tts, "speaker", None)
        if has_presets and previous_active and current_tts_speaker is not None:
            # 切替元の現在音声を念のため保存。以後このペルソナへ戻った時に復元する。
            if current_tts_backend == "style_bert_vits2":
                inner = getattr(b.tts, "inner", b.tts)
                current_model = str(getattr(inner, "model_name", "") or "")
                current_sid = int(getattr(inner, "speaker_id", 0))
                cfg.set(f"persona.presets.{previous_active}.style_bert_vits2_model", current_model)
                cfg.set(f"persona.presets.{previous_active}.style_bert_vits2_speaker_id", current_sid)
                _persist_yaml_value(
                    b.config_path, f"persona.presets.{previous_active}.style_bert_vits2_model", current_model,
                )
                _persist_yaml_value(
                    b.config_path, f"persona.presets.{previous_active}.style_bert_vits2_speaker_id", current_sid,
                )
            elif current_tts_backend == "voicevox":
                cfg.set(f"persona.presets.{previous_active}.voicevox_speaker", int(current_tts_speaker))
                _persist_yaml_value(
                    b.config_path,
                    f"persona.presets.{previous_active}.voicevox_speaker",
                    int(current_tts_speaker),
                )
        if has_presets:
            if active not in presets:
                return {"ok": False, "message": f"未定義のペルソナ: {active}"}
            base = f"persona.presets.{active}."
            cfg.set("persona.active", active)
        else:
            base = "persona."
        try:
            valid_keys = {key for key, _, _ in PERSONA_FIELDS}
            for key, value in (data.get("fields") or {}).items():
                if key in valid_keys:
                    cfg.set(base + key, str(value).strip())
            sp = str(data.get("system_prompt", "")).strip()
            if sp:
                cfg.set(base + "system_prompt", sp)
            if "conversation_traits" in data:
                cfg.set(base + "conversation_traits", normalize_persona_traits(
                    data.get("conversation_traits")
                ))
        except Exception as e:
            return {"ok": False, "message": f"ペルソナ設定が不正です: {e}"}

        composed = build_system_prompt(cfg)
        if b.conv is not None:
            # **人格が変わったら履歴も切る**（Phase 7C）。
            #
            # ここが `set_system_prompt()` だけだったのが、実機で見た
            # 「ペルソナ変更後に旧ペルソナらしい反応」の直接の原因。
            # system prompt は新しくなるのに、`_history` に旧ペルソナの
            # AI 発話が残ったまま次のターンへ渡っていた——モデルから
            # 見れば「自分が直前にそう喋った」ので、口調も知識も続く。
            switched = (has_presets
                        and str(active) != str(getattr(b.conv, "persona_id", ""))
                        and bool(cfg.get(
                            "turn_integrity.drop_history_on_persona_switch",
                            True)))
            if switched:
                dropped = b.conv.switch_persona(composed, persona_id=str(active))
                logger.info("ペルソナ切替: 会話履歴 %s 件を破棄", dropped)
            else:
                b.conv.set_system_prompt(composed)

        # config.yaml へ保存 (スカラーは行置換、system_prompt はブロック置換)
        path = b.config_path
        saved = True
        if has_presets and not _persist_yaml_value(path, "persona.active", active):
            saved = False
        for key, _, _ in PERSONA_FIELDS:
            val = str(cfg.get(base + key, "") or "")
            if not _persist_yaml_value(path, base + key, val):
                saved = False
        if sp and not _persist_yaml_block(path, base + "system_prompt", sp):
            saved = False
        if "conversation_traits" in data and not _persist_yaml_mapping(
            path, base + "conversation_traits", cfg.get(base + "conversation_traits", {})
        ):
            saved = False

        _, ap = get_active_persona(cfg)
        name = str(ap.get("name", "AI")) or "AI"
        # 記憶と人格はペルソナごとに育つので、Mind も切り替える
        if b.mind is not None and has_presets:
            try:
                b.mind.switch_persona(active, name)
            except Exception:
                logger.exception("Mindのペルソナ切替に失敗")
        # ペルソナ別に、現在のTTSで最後に選んだ話者へ切り替える。
        if b.tts is not None and hasattr(b.tts, "set_speaker"):
            try:
                if current_tts_backend == "style_bert_vits2":
                    model = str(cfg.get(
                        base + "style_bert_vits2_model",
                        cfg.get("tts.style_bert_vits2.model_name", ""),
                    ) or "")
                    sid = int(cfg.get(
                        base + "style_bert_vits2_speaker_id",
                        cfg.get("tts.style_bert_vits2.speaker_id", 0),
                    ))
                    selected_speaker = f"{model}::{sid}"
                    b.tts.set_speaker(selected_speaker)
                    cfg.set("tts.style_bert_vits2.model_name", model)
                    cfg.set("tts.style_bert_vits2.speaker_id", sid)
                    _persist_yaml_value(path, "tts.style_bert_vits2.model_name", model)
                    _persist_yaml_value(path, "tts.style_bert_vits2.speaker_id", sid)
                elif current_tts_backend == "voicevox":
                    sid = int(cfg.get(base + "voicevox_speaker", cfg.get("tts.voicevox.speaker", 3)))
                    selected_speaker = str(sid)
                    b.tts.set_speaker(sid)
                    cfg.set("tts.voicevox.speaker", sid)
                    _persist_yaml_value(path, "tts.voicevox.speaker", sid)
                else:
                    selected_speaker = None
                if selected_speaker is not None:
                    b.push("speaker_changed", {"speaker": selected_speaker})
            except Exception:
                logger.exception("ペルソナ話者の切替に失敗")
        b.info["persona"] = name
        b.push("info_update", {"info": b.info})
        note = "" if saved else " (config.yaml への保存は一部失敗)"
        b.push("status", {"message": f"ペルソナを「{name}」に設定しました{note}"})
        return {"ok": True, "composed": composed}

    def get_diagnostics(self) -> dict[str, Any]:
        """管理者メニュー用: **配線が生きているかを、その場で一往復させて確かめる。**

        読み取り専用。合成データを流すだけで、記憶・関係値・感情には触らない。

        設定を読むだけの点検にしていないのは、それでは今回の事故を見つけられない
        ため。`CognitiveState.irritation` は「実装がある」「テストが通る」
        「設定も入っている」が全部成り立ったまま、値だけが届いていなかった。
        """
        from neuro_voice.diagnostics import run_all

        try:
            report = run_all(self._b.cfg, self._b.mind, self._b.pipeline)
        except Exception as exc:
            logger.exception("配線診断でエラー")
            return {"error": f"{type(exc).__name__}: {exc}"}
        payload = report.snapshot()
        # スモークテストでは「どの人格の設定を見ているか」が最初の確認点。
        # Mind の内部状態ではなく、切替の正本である設定から読む（読み取り専用）。
        active_persona_id, _ = get_active_persona(self._b.cfg)
        payload["persona"] = {"active_persona_id": str(active_persona_id or "")}
        payload["runtime"] = {
            "mind": self._b.mind is not None,
            "pipeline": self._b.pipeline is not None,
            "discord": self._b.discord is not None,
        }
        # 認知トレースの最新1行。**本文は入っていない**（第12条）。
        payload["trace"] = self._latest_trace()
        payload["migration"] = self.get_migration_status()
        return payload

    # ---------- Identity 移行 (Phase 6F) ----------
    #
    # **新しい管理画面は作らない。** 既存の🩺へ足す。
    # UI から DB を直接触らせない——操作はすべて `Mind` の1メソッドを通る。

    def get_migration_status(self) -> dict[str, Any]:
        """いまどの段階か。**関係値そのものも声紋も出さない**（第12条）。"""
        if not bool(self._b.cfg.get("identity_migration.ui_enabled", False)):
            return {"ui_enabled": False}
        mind = self._b.mind
        if mind is None:
            return {"ui_enabled": True, "available": False}
        try:
            status = mind.relationship_migration_status()
        except Exception as exc:
            logger.exception("移行状態の取得でエラー")
            return {"ui_enabled": True, "available": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        status["ui_enabled"] = True
        status["available"] = True
        status["dry_run_enabled"] = bool(
            self._b.cfg.get("identity_migration.dry_run_enabled", True))
        with contextlib.suppress(Exception):
            status["speakers_detail"] = mind.speaker_status_rows()[:12]
        return status

    def run_migration_operation(self, operation: str, confirmed: bool = False,
                                ) -> dict[str, Any]:
        """UI からの操作。**遷移の可否も昇格条件もバックエンドで見直す。**

        画面のボタンを隠すだけでは足りない。押せなくしても別の経路から
        呼べば通ってしまうので、`Mind.request_migration` が判断する。
        """
        if not bool(self._b.cfg.get("identity_migration.ui_enabled", False)):
            return {"ok": False, "reason": "ui_disabled"}
        if (str(operation) == "dry_run"
                and not bool(self._b.cfg.get(
                    "identity_migration.dry_run_enabled", True))):
            return {"ok": False, "reason": "dry_run_disabled"}
        mind = self._b.mind
        if mind is None:
            return {"ok": False, "reason": "mind_unavailable"}
        try:
            return mind.request_migration(str(operation), confirmed=bool(confirmed))
        except Exception as exc:
            logger.exception("移行操作でエラー")
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    def _latest_trace(self) -> dict[str, Any]:
        """`logs/cognitive_trace.jsonl` の最終行。無ければ空。

        「フラグは上げたが、そもそも認知層を通っていない」を見分けるため。
        """
        import json
        from pathlib import Path

        path = Path(str(self._b.cfg.get("cognition.trace.path", "logs/cognitive_trace.jsonl")))
        if not bool(self._b.cfg.get("cognition.trace.enabled", False)):
            return {"enabled": False}
        try:
            if not path.exists():
                return {"enabled": True, "found": False}
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 8192))
                lines = [x for x in handle.read().decode("utf-8", "ignore").splitlines() if x.strip()]
            if not lines:
                return {"enabled": True, "found": False}
            row = json.loads(lines[-1])
            return {
                "enabled": True, "found": True, "at": row.get("at"),
                "selected": row.get("selected", ""), "status": row.get("status", ""),
                "latency_ms": row.get("latency_ms", {}),
                "memory_trigger": (row.get("memory") or {}).get("trigger", ""),
                "state_version": (row.get("internal_state") or {}).get("version", 0),
                "appraisal": (row.get("internal_state") or {}).get("appraisal", ""),
                "lines": len(lines),
            }
        except Exception as exc:
            return {"enabled": True, "found": False, "error": f"{type(exc).__name__}: {exc}"}

    def get_mind_status(self) -> dict[str, Any]:
        """こころステータス画面用: 人格・気分・好み・親密度・記憶の現在値。"""
        m = self._b.mind
        if m is None:
            return {"enabled": False, "error": "Mindがまだ初期化されていません"}
        p = self._b.pipeline
        try:
            local_runtime = p.runtime_diagnostics() if p is not None else {
                "proactive_enabled": False,
                "last_evaluation": {
                    "final_action": "初期化待ち",
                    "suppression_reasons": ["音声パイプラインが未起動"],
                },
                "heartbeat": {"started": False, "task_alive": False},
            }
        except Exception as exc:
            logger.exception("ローカル稼働状態の取得に失敗")
            local_runtime = {
                "proactive_enabled": False,
                "last_evaluation": {
                    "final_action": "診断失敗",
                    "suppression_reasons": [f"{type(exc).__name__}: {exc}"],
                },
                "heartbeat": {"started": False, "task_alive": False},
            }
        runtime = {"local": local_runtime}
        if self._b.discord is not None:
            try:
                runtime["discord"] = self._b.discord.runtime_diagnostics()
            except Exception as exc:
                logger.exception("Discord稼働状態の取得に失敗")
                runtime["discord"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            status = m.status()
            status["runtime"] = runtime
            return status
        except Exception as e:
            logger.exception("Mindステータス取得に失敗")
            return {
                "enabled": False,
                "error": f"{type(e).__name__}: {e}",
                "runtime": {
                    **runtime,
                    "status_error": f"{type(e).__name__}: {e}",
                },
            }

    def get_research_history(self, limit: int = 100) -> dict[str, Any]:
        """Safe management view: only sanitized/persisted research data."""
        m = self._b.mind
        if m is None:
            return {"ok": False, "items": []}
        return {"ok": True, "items": m.research_history(max(1, min(200, int(limit))))}

    def get_research_detail(self, question_id: str) -> dict[str, Any]:
        m = self._b.mind
        if m is None:
            return {"ok": False}
        detail = m.research_detail(str(question_id))
        return {"ok": detail is not None, "detail": detail}

    def disable_research_topic(self, topic: str) -> dict[str, Any]:
        m = self._b.mind
        if m is None:
            return {"ok": False}
        m.research_disable_topic(str(topic), True)
        return {"ok": True}

    def reset_relationship(self, speaker_key: str = "", temporary_only: bool = False) -> dict[str, Any]:
        """こころ画面から、現在の相手との関係性を初期化/一時回復する。"""
        m = self._b.mind
        if m is None:
            return {"ok": False, "message": "Mindが無効です"}
        try:
            m.reset_relationship(speaker_key or None, temporary_only=bool(temporary_only))
            return {"ok": True, "status": m.status()}
        except Exception as e:
            logger.exception("関係性の初期化に失敗")
            return {"ok": False, "message": str(e)}

    def rename_speaker(self, speaker_id: int, name: str) -> dict[str, Any]:
        """話者の名前を変更する (こころ画面の「はなし相手」から)。"""
        m = self._b.mind
        if m is None:
            return {"ok": False, "message": "Mindが無効です"}
        ok = m.rename_speaker(int(speaker_id), str(name))
        if ok:
            self._b.push("status", {"message": f"話者の名前を「{name}」にしました"})
        return {"ok": ok}

    def forget_speaker(self, speaker_id: int) -> dict[str, Any]:
        """話者プロファイルを削除する。"""
        m = self._b.mind
        if m is None:
            return {"ok": False, "message": "Mindが無効です"}
        return {"ok": m.forget_speaker(int(speaker_id))}

    def merge_speakers(self, source_id: int, target_id: int) -> dict[str, Any]:
        """2つの話者プロファイルを同一人物として統合する。

        ローカルマイクとDiscordで別々に登録された同じ人を1人へまとめる。
        なかよし度・会話プロファイル・学習した好みも一緒に移行される。
        """
        m = self._b.mind
        if m is None:
            return {"ok": False, "message": "Mindが無効です"}
        try:
            result = m.merge_speakers(
                int(source_id), int(target_id),
                source_surface="discord", target_surface="local",
            )
        except Exception as e:
            logger.exception("話者の統合に失敗")
            return {"ok": False, "message": str(e)}
        if result.get("ok"):
            merged = result.get("merged") or {}
            self._b.push("status", {"message": (
                f"「{merged.get('source_name')}」と「{merged.get('target_name')}」を"
                "同一人物として統合しました"
            )})
        return result

    def speaker_merge_candidates(self) -> dict[str, Any]:
        """声紋が非常に近く、同一人物の可能性が高い組み合わせを返す。"""
        m = self._b.mind
        if m is None:
            return {"ok": False, "candidates": []}
        return {"ok": True, "candidates": m.speaker_merge_candidates()}

    # ---------- 人格データのバックアップ ----------

    def create_backup(self) -> dict[str, Any]:
        """設定画面の「今すぐバックアップ」。"""
        m = self._b.mind
        if m is None:
            return {"ok": False, "message": "Mindが無効です"}
        r = m.backup_now()
        if r.get("ok"):
            self._b.push("status", {"message": "人格データをバックアップしました"})
        return r

    def list_backups(self) -> dict[str, Any]:
        m = self._b.mind
        if m is None or m.backup is None:
            return {"ok": False, "backups": [], "message": "バックアップ機能が無効です"}
        try:
            return {"ok": True, "backups": m.backup.list_backups()}
        except Exception as e:
            logger.exception("バックアップ一覧の取得に失敗")
            return {"ok": False, "backups": [], "message": str(e)}

    def restore_backup(self, name: str) -> dict[str, Any]:
        """バックアップから復元 (反映にはアプリ再起動が必要)。"""
        m = self._b.mind
        if m is None or m.backup is None:
            return {"ok": False, "message": "バックアップ機能が無効です"}
        try:
            return m.backup.restore_backup(str(name))
        except Exception as e:
            logger.exception("バックアップの復元に失敗")
            return {"ok": False, "message": str(e)}

    def get_api_keys(self) -> list[dict[str, str]]:
        """APIキーの現在値 (設定パネルのAPIキータブ用)。"""
        import os

        return [
            {"env": env, "label": label, "value": os.environ.get(env, "")}
            for env, label, _ in _API_KEY_DEFS
        ]

    def set_api_keys(self, keys: dict[str, str]) -> dict[str, Any]:
        """APIキーを .env に保存し、実行時にも反映する。空欄は変更しない。"""
        import os

        b = self._b
        env_path = (
            b.config_path.parent.parent / ".env" if b.config_path else Path(".env")
        )
        changed = 0
        saved = True
        changed_envs: list[str] = []
        for env, _, cfg_key in _API_KEY_DEFS:
            value = str((keys or {}).get(env, "") or "").strip()
            if not value or value == os.environ.get(env, ""):
                continue
            os.environ[env] = value
            b.cfg.set(cfg_key, value)
            if not _persist_env_value(env_path, env, value):
                saved = False
            changed += 1
            changed_envs.append(env)
        if changed:
            # Visionキーを新規保存したのにプロバイダがlocalのままなら自動で切り替える
            # (localのままだと画像非対応LLMでエラーになるため)
            if str(b.cfg.get("vision.provider", "local")) == "local":
                target = None
                if "ANTHROPIC_API_KEY" in changed_envs:
                    target = "claude"
                elif "GOOGLE_VISION_API_KEY" in changed_envs:
                    target = "google"
                if target:
                    b.cfg.set("vision.provider", target)
                    b.settings["vision_provider"] = target
                    _persist_yaml_value(b.config_path, "vision.provider", target)
                    label = "Claude Vision" if target == "claude" else "Google Cloud Vision"
                    b.push("status", {"message": f"画像認識のAIを {label} に自動設定しました"})
            # Visionプロバイダはキーを内部に保持するため再生成させる
            if b.pipeline is not None:
                b.pipeline.set_vision_provider(
                    str(b.cfg.get("vision.provider", "local"))
                )
            note = "" if saved else " (.env への保存は一部失敗)"
            b.push("status", {"message": f"APIキーを {changed} 件保存しました{note}"})
        return {"ok": True, "changed": changed}

    def toggle_search(self) -> bool:
        """DeepSearch のクイックON/OFF (フッターの🔍ボタン用)。"""
        b = self._b
        p = b.pipeline
        if p is None:
            return False
        new_state = not p.search_enabled
        p.set_search(new_state)
        _persist_yaml_value(b.config_path, "search.enabled", "true" if new_state else "false")
        b.settings["search_enabled"] = new_state
        b.push("status", {"message": f"Web検索 (DeepSearch) を {'ON' if new_state else 'OFF'} にしました"})
        return new_state

    def toggle_proactive(self) -> bool:
        """自発モードのクイックON/OFF (フッターの💭ボタン用)。"""
        b = self._b
        p = b.pipeline
        if p is None:
            return False
        new_state = not p.proactive_enabled
        p.set_proactive(new_state)
        if b.discord is not None:
            b.discord.set_proactive(
                new_state,
                min_interval_s=float(b.cfg.get("proactive.min_interval_s", 30)),
            )
        b.cfg.set("autonomous_action_system.enabled", new_state)
        _persist_yaml_value(b.config_path, "autonomous_action_system.enabled", "true" if new_state else "false")
        b.settings["proactive_enabled"] = new_state
        b.push("status", {"message": f"自発モードを {'ON' if new_state else 'OFF'} にしました"})
        return new_state

    def get_stt_corrections(self) -> dict[str, list[dict[str, str]]]:
        """設定画面用に、STTの誤変換補正辞書を安定した順番で返す。"""
        raw = self._b.cfg.get("stt.corrections", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "corrections": [
                {"wrong": str(wrong), "right": str(right)}
                for wrong, right in raw.items()
            ]
        }

    def set_stt_corrections(self, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
        """STTの誤変換補正辞書を保存し、実行中の認識器も再読込する。"""
        b = self._b
        corrections: dict[str, str] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            wrong = str(row.get("wrong", "") or "").strip()
            right = str(row.get("right", "") or "").strip()
            if not wrong and not right:
                continue
            if not wrong or not right:
                return {"ok": False, "message": "誤認識と正しい表記を両方入力してください"}
            if len(wrong) > 80 or len(right) > 80:
                return {"ok": False, "message": "1件あたり80文字以内で入力してください"}
            corrections[wrong] = right
        if len(corrections) > 100:
            return {"ok": False, "message": "登録できる補正は100件までです"}

        b.cfg.set("stt.corrections", corrections)
        saved = _persist_yaml_mapping(b.config_path, "stt.corrections", corrections)
        reloading = b.stt is not None and not b._stt_switching
        if reloading:
            b._stt_switching = True
            device = getattr(b.stt, "device", str(b.cfg.get("stt.device", "cuda")))
            threading.Thread(target=b.reload_stt, args=(device,), daemon=True).start()
        note = "音声認識を再読込中です" if reloading else "次回の音声認識開始時に反映されます"
        if not saved:
            note += "（config.yamlへの保存に失敗）"
        b.push("status", {"message": f"誤変換辞書を保存しました。{note}"})
        return {
            "ok": bool(saved),
            "message": note,
            "corrections": [
                {"wrong": wrong, "right": right} for wrong, right in corrections.items()
            ],
        }

    def get_tts_pronunciations(self) -> dict[str, list[dict[str, str]]]:
        raw = self._b.cfg.get("tts.voicevox.pronunciations", {}) or {}
        return {"pronunciations": [
            {"surface": str(surface), "reading": str(reading)}
            for surface, reading in raw.items()
        ]} if isinstance(raw, dict) else {"pronunciations": []}

    def set_tts_pronunciations(self, rows: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Save surface-to-kana overrides without changing displayed text."""
        from neuro_voice.tts.pronunciation import normalize_pronunciations

        candidate = {
            str(row.get("surface", "") or "").strip(): str(row.get("reading", "") or "").strip()
            for row in (rows or []) if isinstance(row, dict) and str(row.get("surface", "") or "").strip()
        }
        pronunciations = normalize_pronunciations(candidate)
        if len(pronunciations) != len(candidate):
            return {"ok": False, "message": "表記と、ひらがな/カタカナだけの読みを両方入力してください"}
        if len(pronunciations) > 100:
            return {"ok": False, "message": "登録できる読み方は100件までです"}
        b = self._b
        b.cfg.set("tts.voicevox.pronunciations", pronunciations)
        tts = b.tts
        if tts is not None and hasattr(tts, "set_pronunciations"):
            tts.set_pronunciations(pronunciations)
        saved = _persist_yaml_mapping(b.config_path, "tts.voicevox.pronunciations", pronunciations)
        b.push("status", {"message": "読み方辞書を保存しました。次の発話から反映されます"})
        return {"ok": bool(saved), "pronunciations": [
            {"surface": surface, "reading": reading} for surface, reading in pronunciations.items()
        ]}

    def apply_settings(self, s: dict[str, Any]) -> dict[str, Any]:
        """LLM設定を実行時に反映し、config.yaml に保存する。"""
        b = self._b
        p = b.pipeline
        if p is None:
            return {"ok": False, "message": "まだ初期化中です"}
        try:
            name = str(s.get("backend", "")).strip()
            model = str(s.get("model", "")).strip()
            temperature = max(0.0, min(2.0, float(s.get("temperature", 0.7))))
            max_tokens = max(64, min(8192, int(s.get("max_tokens", 512))))
            max_turns = max(1, min(100, int(s.get("max_turns", 20))))
        except (TypeError, ValueError):
            return {"ok": False, "message": "設定値が不正です"}

        conversation_variant = str(s.get("conversation_variant", "enhanced"))
        if conversation_variant not in {"baseline", "enhanced", "experimental"}:
            conversation_variant = "enhanced"
        conversation_feature_mode = str(s.get("conversation_feature_mode", "normal"))
        if conversation_feature_mode not in {"conservative", "normal", "expressive", "debug_showcase"}:
            conversation_feature_mode = "normal"
        conversation_developer_ui = bool(s.get("conversation_developer_ui", False))
        conversation_force_feature = str(s.get("conversation_force_feature", "") or "").strip()
        allowed_features = (
            "humor_engine", "casual_conversation_engine", "story_engine",
            "imagination_engine", "playful_fantasy_engine", "world_knowledge_engine",
            "self_growth", "relationship_evolution", "value_system", "experience_memory",
        )
        if conversation_force_feature not in allowed_features:
            conversation_force_feature = ""
        raw_toggles = s.get("conversation_feature_toggles", {})
        feature_toggles = {
            key: bool(raw_toggles.get(key, True)) if isinstance(raw_toggles, dict) else True
            for key in allowed_features
        }

        # Visionプロバイダのキー検証 (画像認識ONで外部プロバイダのとき)
        vp = str(s.get("vision_provider", "local"))
        if bool(s.get("vision_enabled", False)):
            if vp == "claude" and not b.cfg.get("vision.claude.api_key"):
                return {"ok": False, "message": (
                    "Claude Vision を使うには ANTHROPIC_API_KEY が必要です。"
                    "「APIキー」タブで設定してください"
                )}
            if vp == "google" and not b.cfg.get("vision.google.api_key"):
                return {"ok": False, "message": (
                    "Google Cloud Vision を使うには GOOGLE_VISION_API_KEY が必要です。"
                    "「APIキー」タブで設定してください"
                )}

        backends = b.cfg.section("llm.backends")
        if name not in backends:
            return {"ok": False, "message": f"未定義のバックエンド: {name}"}
        bd = backends[name] or {}
        if not model:
            model = bd.get("model", "")
        if not model:
            return {"ok": False, "message": "モデル名を入力してください"}
        api_key = bd.get("api_key", "")
        if not api_key and name != "ollama":
            return {"ok": False, "message": f"{name} の api_key が .env に未設定です"}

        from neuro_voice.llm.openai_compat import OpenAICompatBackend

        try:
            new_llm = OpenAICompatBackend(
                name=name,
                base_url=bd["base_url"],
                api_key=api_key,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=b.cfg.get("llm.reasoning_effort"),
                keep_alive=b.cfg.get("llm.keep_alive"),
                timeout_s=float(b.cfg.get("llm.timeout_sec", 45)),
                ollama_fallback_model=b.cfg.get("llm.ollama_fallback_model"),
                auto_fallback_on_runner_crash=bool(
                    b.cfg.get("llm.auto_fallback_on_runner_crash", True)
                ),
            )
        except Exception as e:
            return {"ok": False, "message": f"バックエンド初期化に失敗: {e}"}

        old = b.llm
        p.set_llm(new_llm)
        b.llm = new_llm
        if b.discord is not None:
            b.discord.set_llm(new_llm)
        if b.conv is not None:
            b.conv.set_max_turns(max_turns)

        # 使わなくなったローカルLLM (Ollama) をアンロード
        if (
            old is not None
            and getattr(old, "name", "") == "ollama"
            and name != "ollama"
            and b._aloop is not None
        ):
            asyncio.run_coroutine_threadsafe(old.unload(), b._aloop)

        # config.yaml へ保存
        path = b.config_path
        saved = all([
            _persist_yaml_value(path, "llm.backend", name),
            _persist_yaml_value(path, "llm.temperature", temperature),
            _persist_yaml_value(path, "llm.max_tokens", max_tokens),
            _persist_yaml_value(path, f"llm.backends.{name}.model", model),
            _persist_yaml_value(path, "conversation.max_turns", max_turns),
        ])

        # Vision設定
        vision_enabled = bool(s.get("vision_enabled", False))
        vision_mode = str(s.get("vision_mode", "on_demand"))
        if vision_mode not in ("on_demand", "auto"):
            vision_mode = "on_demand"
        try:
            vision_interval = max(5, min(600, int(s.get("vision_interval", 20))))
        except (TypeError, ValueError):
            vision_interval = 20
        vision_provider = str(s.get("vision_provider", "local"))
        if vision_provider not in ("ollama", "local", "claude", "google"):
            vision_provider = "ollama"
        vision_model = str(s.get("vision_model", "gemma4-12b-qat")).strip() or "gemma4-12b-qat"
        video_enabled = bool(s.get("video_enabled", False))
        video_game_profile = str(s.get("video_game_profile", "")).strip().lower()
        profile_registry = GameProfileRegistry(
            str(b.cfg.get("game_profiles.root", "") or "") or None
        )
        if video_game_profile and profile_registry.get(video_game_profile) is None:
            video_game_profile = ""
        selected_game_profile = profile_registry.get(video_game_profile)
        if selected_game_profile is not None and not selected_game_profile.allows_vision:
            # Manual-expert profiles (for example KTaNE) must not accidentally
            # receive the player's screen through a previously enabled OBS input.
            video_enabled = False
        video_input = str(s.get("video_input", "camera")).strip().lower()
        if video_input not in ("camera", "screen", "window", "obs"):
            video_input = "obs"
        video_source = str(s.get("video_source", "0")).strip() or "0"
        video_screen_monitor = str(s.get("video_screen_monitor", "auto")).strip() or "auto"
        video_window_handle = str(s.get("video_window_handle", "")).strip()
        # The game-window picker is authoritative for Minecraft.  Without
        # this normalization the UI can show a selected game while the saved
        # pipeline still runs the old ``screen`` source.
        if video_enabled and video_input == "window" and video_window_handle and str(s.get("video_game_profile", "")).strip().lower() == "minecraft":
            video_input = "window"
        if video_enabled and video_input == "window" and not video_window_handle:
            return {"ok": False, "message": "ゲームウィンドウを選択してから設定を適用してください。"}
        video_obs_host = str(s.get("video_obs_host", "127.0.0.1")).strip() or "127.0.0.1"
        try:
            video_obs_port = max(1, min(65535, int(s.get("video_obs_port", 4455))))
        except (TypeError, ValueError):
            video_obs_port = 4455
        video_obs_source = str(s.get("video_obs_source", "")).strip()
        video_obs_password = str(s.get("video_obs_password", "") or "")
        if video_enabled and video_input == "obs" and not video_obs_source:
            return {"ok": False, "message": "OBSのゲームキャプチャソースを選択してください。"}
        video_game_commentary = bool(s.get("video_game_commentary", False))
        if selected_game_profile is not None and not selected_game_profile.allows_vision:
            video_game_commentary = False
        video_game_knowledge = bool(s.get("video_game_knowledge", True))
        try:
            video_interval = max(.5, min(60.0, float(s.get("video_analysis_interval", 2.0))))
            video_max_frames = max(1, min(6, int(s.get("video_max_frames", 4))))
        except (TypeError, ValueError):
            video_interval, video_max_frames = 2.0, 4
        audio_input_backend = str(s.get("audio_input_backend", "whisper"))
        if audio_input_backend not in ("whisper", "gemma4_audio"):
            audio_input_backend = "whisper"
        audio_output_backend = str(s.get("audio_output_backend", "voicevox"))
        if audio_output_backend not in ("voicevox", "style_bert_vits2", "qwen3", "null"):
            audio_output_backend = "voicevox"
        previous_audio_output = str(
            b.cfg.get("audio_output.backend", b.cfg.get("tts.backend", "voicevox"))
        )
        p.set_vision(vision_enabled, vision_mode)
        p.set_vision_provider(vision_provider)
        b.cfg.set("vision.interval_s", vision_interval)
        b.cfg.set("vision.provider", vision_provider)
        b.cfg.set("vision.model", vision_model)
        b.cfg.set("video.enabled", video_enabled)
        b.cfg.set("video.input", video_input)
        b.cfg.set("video.source", video_source)
        b.cfg.set("video.screen_monitor", video_screen_monitor)
        b.cfg.set("video.window_handle", video_window_handle)
        b.cfg.set("video.obs.host", video_obs_host)
        b.cfg.set("video.obs.port", video_obs_port)
        b.cfg.set("video.obs.source_name", video_obs_source)
        if video_obs_password:
            b.cfg.set("video.obs.password", video_obs_password)
            os.environ["OBS_WEBSOCKET_PASSWORD"] = video_obs_password
            env_path = b.config_path.parent.parent / ".env" if b.config_path else Path(".env")
            _persist_env_value(env_path, "OBS_WEBSOCKET_PASSWORD", video_obs_password)
        b.cfg.set("video.game_profile", video_game_profile)
        b.cfg.set("video.game_commentary_enabled", video_game_commentary)
        b.cfg.set("game_assistant.minecraft.enabled", video_game_knowledge)
        if video_game_profile:
            b.cfg.set(f"game_assistant.{video_game_profile}.enabled", video_game_knowledge)
        b.cfg.set("video.analysis_interval_sec", video_interval)
        b.cfg.set("vision.analysis_min_interval_sec", video_interval)
        b.cfg.set("video.max_frames_per_analysis", video_max_frames)
        previous_audio_input = str(b.cfg.get("audio_input.backend", "whisper"))
        b.cfg.set("audio_input.backend", audio_input_backend)
        b.cfg.set("audio_output.backend", audio_output_backend)
        if audio_output_backend != previous_audio_output:
            switch_result = b.request_tts_switch(audio_output_backend)
            if not switch_result.get("ok"):
                b.push("error", {"message": switch_result.get("message", "読み上げ音声を切り替えられませんでした")})
        saved = saved and all([
            _persist_yaml_value(path, "vision.enabled", "true" if vision_enabled else "false"),
            _persist_yaml_value(path, "vision.mode", vision_mode),
            _persist_yaml_value(path, "vision.interval_s", vision_interval),
            _persist_yaml_value(path, "vision.provider", vision_provider),
            _persist_yaml_value(path, "vision.model", vision_model),
            _persist_yaml_value(path, "video.enabled", "true" if video_enabled else "false"),
            _persist_yaml_value(path, "video.input", video_input),
            _persist_yaml_value(path, "video.source", video_source),
            _persist_yaml_value(path, "video.screen_monitor", video_screen_monitor),
            _persist_yaml_value(path, "video.window_handle", video_window_handle),
            _persist_yaml_value(path, "video.obs.host", video_obs_host),
            _persist_yaml_value(path, "video.obs.port", video_obs_port),
            _persist_yaml_value(path, "video.obs.source_name", video_obs_source),
            _persist_yaml_value(path, "video.game_profile", video_game_profile),
            _persist_yaml_value(path, "video.game_commentary_enabled", "true" if video_game_commentary else "false"),
            _persist_yaml_value(path, "game_assistant.minecraft.enabled", "true" if video_game_knowledge else "false"),
            *(
                [_persist_yaml_value(
                    path, f"game_assistant.{video_game_profile}.enabled",
                    "true" if video_game_knowledge else "false",
                )] if video_game_profile else []
            ),
            _persist_yaml_value(path, "video.analysis_interval_sec", video_interval),
            _persist_yaml_value(path, "vision.analysis_min_interval_sec", video_interval),
            _persist_yaml_value(path, "video.max_frames_per_analysis", video_max_frames),
            _persist_yaml_value(path, "audio_input.backend", audio_input_backend),
            _persist_yaml_value(path, "audio_output.backend", audio_output_backend),
        ])

        # 機能トグル (自動ロード / DeepSearch / 疑似感情 / 自発モード)
        auto_load = bool(s.get("auto_load", True))
        search_enabled = bool(s.get("search_enabled", False))
        autonomous_research_enabled = bool(s.get("autonomous_research_enabled", False))
        autonomous_research_mode = str(s.get("autonomous_research_mode", "LOW_RISK_AUTO")).upper()
        if autonomous_research_mode not in {"OFF", "ASK_FIRST", "LOW_RISK_AUTO", "FULL_AUTO_WITH_LIMITS"}:
            autonomous_research_mode = "LOW_RISK_AUTO"
        music_recognition_enabled = bool(s.get("music_recognition_enabled", False))
        music_recognition_loopback_device = str(
            s.get("music_recognition_loopback_device", "") or ""
        ).strip()
        humming_recognition_enabled = bool(s.get("humming_recognition_enabled", False))
        humming_recognition_host = str(s.get("humming_recognition_host", "") or "").strip()
        emotion_enabled = bool(s.get("emotion_enabled", True))
        try:
            stt_beam_size = max(1, min(10, int(s.get("stt_beam_size", 5))))
        except (TypeError, ValueError):
            stt_beam_size = 5
        proactive_enabled = bool(s.get("proactive_enabled", False))
        try:
            proactive_min = max(5, min(3600, int(s.get("proactive_min", 30))))
            proactive_max = max(proactive_min, min(3600, int(s.get("proactive_max", 90))))
        except (TypeError, ValueError):
            proactive_min, proactive_max = 30, 90
        p.set_search(search_enabled)
        p.set_proactive(proactive_enabled)
        if b.discord is not None:
            b.discord.set_proactive(
                proactive_enabled, min_interval_s=proactive_min,
            )
        p.set_emotion_enabled(emotion_enabled)
        b.cfg.set("llm.auto_load", auto_load)
        b.cfg.set("search.enabled", search_enabled)
        b.cfg.set("autonomous_research.enabled", autonomous_research_enabled)
        b.cfg.set("autonomous_research.mode", autonomous_research_mode)
        b.cfg.set("music_recognition.enabled", music_recognition_enabled)
        b.cfg.set(
            "music_recognition.loopback_device",
            music_recognition_loopback_device or None,
        )
        b.cfg.set("humming_recognition.enabled", humming_recognition_enabled)
        b.cfg.set("humming_recognition.host", humming_recognition_host)
        b.cfg.set("emotion.enabled", emotion_enabled)
        previous_beam = int(b.cfg.get("stt.beam_size", 5))
        b.cfg.set("stt.beam_size", stt_beam_size)
        b.cfg.set("autonomous_action_system.enabled", proactive_enabled)
        b.cfg.set("proactive.min_interval_s", proactive_min)
        b.cfg.set("proactive.max_interval_s", proactive_max)
        b.cfg.set("conversation_engine.variant", conversation_variant)
        b.cfg.set("conversation_features.mode", conversation_feature_mode)
        b.cfg.set("conversation_features.developer_ui", conversation_developer_ui)
        b.cfg.set("conversation_features.debug.force_feature", conversation_force_feature or None)
        for key, enabled in feature_toggles.items():
            b.cfg.set(f"features.{key}", enabled)
        if b.mind is not None:
            b.mind.configure_conversation_features()
        # 感情タグ指示は System Prompt 内にあるため再合成する
        if b.conv is not None:
            b.conv.set_system_prompt(build_system_prompt(b.cfg))
        saved = saved and all([
            _persist_yaml_value(path, "llm.auto_load", "true" if auto_load else "false"),
            _persist_yaml_value(path, "search.enabled", "true" if search_enabled else "false"),
            _persist_yaml_value(path, "autonomous_research.enabled", "true" if autonomous_research_enabled else "false"),
            _persist_yaml_value(path, "autonomous_research.mode", autonomous_research_mode),
            _persist_yaml_value(path, "music_recognition.enabled", "true" if music_recognition_enabled else "false"),
            _persist_yaml_value(
                path, "music_recognition.loopback_device",
                "null" if not music_recognition_loopback_device
                else f'"{music_recognition_loopback_device}"',
            ),
            _persist_yaml_value(path, "humming_recognition.enabled", "true" if humming_recognition_enabled else "false"),
            _persist_yaml_value(path, "humming_recognition.host", humming_recognition_host),
            _persist_yaml_value(path, "emotion.enabled", "true" if emotion_enabled else "false"),
            _persist_yaml_value(path, "stt.beam_size", stt_beam_size),
            _persist_yaml_value(path, "autonomous_action_system.enabled", "true" if proactive_enabled else "false"),
            _persist_yaml_value(path, "proactive.min_interval_s", proactive_min),
            _persist_yaml_value(path, "proactive.max_interval_s", proactive_max),
            _persist_yaml_value(path, "conversation_engine.variant", conversation_variant),
            _persist_yaml_value(path, "conversation_features.mode", conversation_feature_mode),
            _persist_yaml_value(path, "conversation_features.developer_ui", "true" if conversation_developer_ui else "false"),
            _persist_yaml_value(path, "conversation_features.debug.force_feature", "null" if not conversation_force_feature else conversation_force_feature),
            *[
                _persist_yaml_value(path, f"features.{key}", "true" if enabled else "false")
                for key, enabled in feature_toggles.items()
            ],
        ])

        b.settings.update(
            {"backend": name, "temperature": temperature,
             "max_tokens": max_tokens, "max_turns": max_turns,
             "vision_enabled": vision_enabled, "vision_mode": vision_mode,
             "vision_interval": vision_interval, "vision_provider": vision_provider, "vision_model": vision_model,
             "video_enabled": video_enabled, "video_input": video_input, "video_source": video_source,
             "video_screen_monitor": video_screen_monitor, "video_window_handle": video_window_handle,
             "video_obs_host": video_obs_host, "video_obs_port": video_obs_port,
             "video_obs_source": video_obs_source,
             "video_obs_password_set": bool(video_obs_password or b.cfg.get("video.obs.password", "")),
             "video_game_profile": video_game_profile, "video_game_commentary": video_game_commentary,
             "video_game_knowledge": video_game_knowledge,
             "video_analysis_interval": video_interval, "video_max_frames": video_max_frames,
             "audio_input_backend": audio_input_backend, "audio_output_backend": audio_output_backend,
             "auto_load": auto_load, "search_enabled": search_enabled,
             "autonomous_research_enabled": autonomous_research_enabled,
             "autonomous_research_mode": autonomous_research_mode,
             "music_recognition_enabled": music_recognition_enabled,
             "music_recognition_loopback_device": music_recognition_loopback_device,
             "humming_recognition_enabled": humming_recognition_enabled,
             "humming_recognition_host": humming_recognition_host,
             "emotion_enabled": emotion_enabled,
             "stt_beam_size": stt_beam_size,
              "proactive_enabled": proactive_enabled,
              "proactive_min": proactive_min, "proactive_max": proactive_max,
              "conversation_variant": conversation_variant,
              "conversation_feature_mode": conversation_feature_mode,
              "conversation_developer_ui": conversation_developer_ui,
              "conversation_force_feature": conversation_force_feature,
              "conversation_feature_toggles": feature_toggles}
        )
        b.settings["models"][name] = model
        b.info["llm"] = f"{name} / {model}"
        # Unlike LLM changes, a game-window selection must take effect at
        # once; otherwise the UI can show the newly selected Minecraft window
        # while the running capture task still reads the old monitor/handle.
        if b.pipeline is not None and b._aloop is not None and getattr(b.pipeline, "_loop", None) is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    b.pipeline.refresh_video_observation(), b._aloop,
                )
                if not future.result(timeout=5):
                    b.push("status", {"message": "Game video input could not be applied"})
            except Exception:
                logger.warning("Unable to refresh game video input after settings change", exc_info=True)
                b.push("status", {"message": "Game video input update failed; restart the app"})
        if (previous_beam != stt_beam_size or previous_audio_input != audio_input_backend) and b.stt is not None and not b._stt_switching:
            b._stt_switching = True
            device = getattr(b.stt, "device", str(b.cfg.get("stt.device", "cuda")))
            threading.Thread(target=b.reload_stt, args=(device,), daemon=True).start()
            b.push("status", {"message": f"beam sizeを {stt_beam_size} に変更。音声認識を再読込しています..."})
        b.push("info_update", {"info": b.info})
        note = "" if saved else " (config.yaml への保存は一部失敗)"
        vision_note = (
            f" / 画面認識: {'自動実況' if vision_mode == 'auto' else '発話時'}"
            if vision_enabled else " / 画面認識: OFF"
        )
        b.push("status", {"message": f"LLMを {name} / {model} に切り替えました{vision_note}{note}"})
        return {"ok": True}

    def toggle_stt(self) -> dict[str, Any]:
        """音声認識のGPU/CPU切替。モデル再読込は別スレッドで行う。"""
        b = self._b
        if b.pipeline is None or b.stt is None:
            return {"ok": False, "message": "まだ初期化中です"}
        if b._stt_switching:
            return {"ok": False, "message": "切替処理中です"}
        current = getattr(b.stt, "device", "cuda")
        target = "cpu" if current.startswith("cuda") else "cuda"
        b._stt_switching = True
        b.push("status", {"message": f"音声認識を {target.upper()} に切替中... (数秒かかります)"})
        threading.Thread(target=b.reload_stt, args=(target,), daemon=True).start()
        return {"ok": True, "target": target}

    def toggle_vision(self) -> bool:
        """画面認識のクイックON/OFF (フッターの👁ボタン用)。"""
        b = self._b
        p = b.pipeline
        if p is None:
            return False
        new_state = not p.vision_enabled
        p.set_vision(new_state)
        _persist_yaml_value(b.config_path, "vision.enabled", "true" if new_state else "false")
        b.settings["vision_enabled"] = new_state
        if b._aloop is not None and getattr(p, "_loop", None) is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    p.refresh_video_observation(), b._aloop,
                )
                if not future.result(timeout=5):
                    b.push("status", {"message": "映像取得の切り替えに失敗しました"})
            except Exception:
                logger.warning("Unable to apply vision capture toggle", exc_info=True)
                b.push("status", {"message": "映像取得の停止に失敗しました。アプリを再起動してください"})
        mode = "自動実況" if p.vision_mode == "auto" else "発話時に画面を見る"
        b.push("status", {
            "message": f"画面認識を {'ON (' + mode + ')' if new_state else 'OFF'} にしました"
        })
        return new_state


def _persist_yaml_value(config_path: Path | None, dotted_key: str, value: Any) -> bool:
    """config.yaml の指定キーの値を、コメント・構造を保ったまま書き換える。

    対象はスカラー値の行のみ (例: "llm.backends.ollama.model")。
    """
    if config_path is None or not config_path.exists():
        return False
    try:
        keys = dotted_key.split(".")
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []  # (インデント幅, キー名)
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if m is None:
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))
            if [k for _, k in stack] == keys:
                rest = m.group(3)
                cm = re.search(r"(\s*#.*?)\s*$", rest)
                comment = cm.group(1) if cm else ""
                lines[i] = f"{m.group(1)}{m.group(2)}: {value}{comment}\n"
                config_path.write_text("".join(lines), encoding="utf-8")
                return True
        return False
    except Exception:
        logger.exception("config.yaml への保存に失敗 (%s)", dotted_key)
        return False


#: 新規ペルソナのキーに使える形。**ファイル名になる**ので厳しくする。
#:
#: `mind_<key>.db` や `relationships_<key>.json` がこのキーから作られる。
#: `..` や `/` を通すと別のペルソナのファイルを指せてしまう。
_PERSONA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _persist_yaml_new_preset(
    config_path: Path | None, key: str, values: dict[str, Any],
    system_prompt: str, traits: dict[str, Any] | None = None,
) -> bool:
    """`persona.presets` の末尾へ新しいプリセットを丸ごと足す。

    既存の `_persist_yaml_value` は**行の置換**しかできない。無い鍵は
    書けないので、新規追加にはこちらが要る。

    `yaml.dump` でファイル全体を書き直さないのは、**コメントが全部
    消える**から。`config.yaml` のコメントは設定の説明そのもので、
    1つ足すたびに失うのは割に合わない。
    """
    if config_path is None or not config_path.exists():
        return False
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []
        start = -1
        indent = 0
        for i, line in enumerate(lines):
            match = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if match is None:
                continue
            depth = len(match.group(1))
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, match.group(2)))
            if [name for _, name in stack] == ["persona", "presets"]:
                start = i
                indent = depth
                break
        if start < 0:
            return False
        # プリセット群の終わりを探す（`presets` より深い行が続く限り）。
        end = start + 1
        last_content = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() == "":
                end += 1
                continue
            if len(line) - len(line.lstrip(" ")) <= indent:
                break
            end += 1
            last_content = end

        pad = " " * (indent + 2)
        inner = " " * (indent + 4)
        # **設定パネルの項目は必ず全部書く。** 呼び出し側に任せない。
        #
        # 1つでも欠けると、「適用」の時に `_persist_yaml_value()` が
        # その行を見つけられず False を返し、「保存は一部失敗」と出る。
        # 呼び出し側が渡し忘れても壊れない形にしておく。
        complete: dict[str, Any] = {
            field: values.get(field, "") for field, _, _ in PERSONA_FIELDS
        }
        complete.update({k: v for k, v in values.items() if k not in complete})
        block = [f"{pad}{key}:\n"]
        for name, value in complete.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                block.append(f"{inner}{name}: {value}\n")
            else:
                text = str(value or "")
                block.append(
                    f"{inner}{name}: {json.dumps(text, ensure_ascii=False)}\n"
                    if text else f"{inner}{name}: \"\"\n")
        # **後で書き換えられる形にしておく。**
        #
        # ここを省くと、「適用」の時に `_persist_yaml_mapping()` が
        # 書き換える対象の行を見つけられず False を返し、
        # 「config.yaml への保存は一部失敗」と出る。**追加した直後の
        # ペルソナだけが保存できない**という分かりにくい形になる。
        deeper = " " * (indent + 6)
        block.append(f"{inner}conversation_traits:\n")
        for trait, score in (traits or {}).items():
            block.append(
                f"{deeper}{json.dumps(str(trait), ensure_ascii=False)}: "
                f"{float(score):.2f}\n")
        if not traits:
            block[-1] = f"{inner}conversation_traits: {{}}\n"
        body = system_prompt.strip() or f"あなたは「{complete.get('name') or key}」という名前のAIアシスタント。"
        block.append(f"{inner}system_prompt: |\n")
        block += [f"{inner}  {row}\n" for row in body.splitlines()]

        lines[last_content:last_content] = block
        config_path.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        logger.exception("config.yaml への新規ペルソナ追加に失敗 (%s)", key)
        return False


def _remove_yaml_preset(config_path: Path | None, key: str) -> bool:
    """`persona.presets.<key>` のブロックを丸ごと消す。

    **`config.yaml` からの削除だけ。** `data/` のファイルには触らない
    ——理由は `delete_persona()` に書いてある。
    """
    if config_path is None or not config_path.exists():
        return False
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            match = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if match is None:
                continue
            depth = len(match.group(1))
            while stack and stack[-1][0] >= depth:
                stack.pop()
            stack.append((depth, match.group(2)))
            if [name for _, name in stack] != ["persona", "presets", key]:
                continue
            end = i + 1
            while end < len(lines):
                row = lines[end]
                if row.strip() == "" or (len(row) - len(row.lstrip(" "))) > depth:
                    end += 1
                    continue
                break
            # 末尾の空行は次のプリセットの前の余白として残す。
            while end > i + 1 and lines[end - 1].strip() == "":
                end -= 1
            del lines[i:end]
            config_path.write_text("".join(lines), encoding="utf-8")
            return True
        return False
    except Exception:
        logger.exception("config.yaml からのペルソナ削除に失敗 (%s)", key)
        return False


#: ペルソナごとに作られるファイルの雛形。**消さないが、場所は伝える。**
PERSONA_DATA_PATTERNS: tuple[str, ...] = (
    "mind_{key}.db", "personality_{key}.json", "dialogue_{key}.json",
    "relationships_{key}.json", "person_relationships_{key}.json",
    "temporal_{key}.json", "temporal_self_{key}.json",
    "activities_{key}.json", "game_session_{key}.json",
    "research_{key}.db", "speakers_{key}.json",
)


def _persona_data_files(data_dir: Path | None, key: str) -> list[str]:
    if data_dir is None or not data_dir.exists():
        return []
    return sorted(
        name for name in (pattern.format(key=key)
                          for pattern in PERSONA_DATA_PATTERNS)
        if (data_dir / name).exists()
    )


def _persist_env_value(env_path: Path, key: str, value: str) -> bool:
    """.env の KEY=VALUE を書き換える (コメントアウト行 "# KEY=" も有効化)。

    キーが無ければ末尾に追記する。ファイルが無ければ新規作成する。
    """
    try:
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}\n"
                env_path.write_text("".join(lines), encoding="utf-8")
                return True
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
        env_path.write_text("".join(lines), encoding="utf-8")
        return True
    except Exception:
        logger.exception(".env への保存に失敗 (%s)", key)
        return False


def _persist_yaml_mapping(
    config_path: Path | None, dotted_key: str, values: dict[str, Any]
) -> bool:
    """config.yaml 内の小さなマッピングを、対象ブロックだけ置き換えて保存する。"""
    if config_path is None or not config_path.exists():
        return False
    try:
        keys = dotted_key.split(".")
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            match = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if match is None:
                continue
            indent = len(match.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, match.group(2)))
            if [key for _, key in stack] != keys:
                continue
            end = i + 1
            while end < len(lines):
                candidate = lines[end]
                if candidate.strip() == "":
                    end += 1
                    continue
                candidate_indent = len(candidate) - len(candidate.lstrip(" "))
                if candidate_indent > indent:
                    end += 1
                    continue
                break
            pad = " " * (indent + 2)
            if values:
                replacement = [f"{match.group(1)}{match.group(2)}:\n"]
                replacement += [
                    f"{pad}{json.dumps(str(wrong), ensure_ascii=False)}: "
                    f"{str(right) if isinstance(right, (int, float)) and not isinstance(right, bool) else json.dumps(str(right), ensure_ascii=False)}\n"
                    for wrong, right in values.items()
                ]
            else:
                replacement = [f"{match.group(1)}{match.group(2)}: {{}}\n"]
            lines[i:end] = replacement
            config_path.write_text("".join(lines), encoding="utf-8")
            return True
        return False
    except Exception:
        logger.exception("config.yaml のマッピング保存に失敗 (%s)", dotted_key)
        return False


def _persist_yaml_block(config_path: Path | None, dotted_key: str, value: str) -> bool:
    """config.yaml のブロックスカラー (key: | の複数行値) を書き換える。

    persona.system_prompt のような複数行テキスト用。コメント・他の構造は保つ。
    """
    if config_path is None or not config_path.exists():
        return False
    try:
        keys = dotted_key.split(".")
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        stack: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)([A-Za-z0-9_]+):(.*)$", line)
            if m is None:
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))
            if [k for _, k in stack] != keys:
                continue
            # ブロック本文の範囲を特定 (キー行より深いインデントの行が続く限り)
            body_indent = indent + 2
            end = i + 1
            while end < len(lines):
                ln = lines[end]
                if ln.strip() == "" or (len(ln) - len(ln.lstrip(" "))) >= body_indent:
                    end += 1
                else:
                    break
            # 末尾の空行はブロック外として残す
            while end > i + 1 and lines[end - 1].strip() == "":
                end -= 1
            pad = " " * body_indent
            block = [f"{m.group(1)}{m.group(2)}: |\n"]
            block += [f"{pad}{ln}\n" for ln in value.splitlines()]
            lines[i:end] = block
            config_path.write_text("".join(lines), encoding="utf-8")
            return True
        return False
    except Exception:
        logger.exception("config.yaml へのブロック保存に失敗 (%s)", dotted_key)
        return False


def run_gui(cfg: Config, config_path: str | None = None,
            cognition_test_session: bool = False,
            cognition_session_mode: str = "disabled") -> None:
    """GUI を起動する (ウィンドウを閉じると終了)。"""
    import webview

    backend = GuiBackend(cfg, config_path, cognition_test_session, cognition_session_mode)
    api = Api(backend)
    html_path = Path(__file__).parent / "assets" / "index.html"
    window = webview.create_window(
        "Neuro Voice AI",
        str(html_path),
        js_api=api,
        width=1000,
        height=780,
        min_size=(720, 560),
        background_color="#0a0d16",
    )
    # Begin stopping audio as soon as the user presses X.  Waiting until
    # webview.start() returns leaves a window where the microphone and model
    # can remain live behind an already closed GUI.
    def _on_closing(*_args) -> None:
        backend.request_shutdown()

    window.events.closing += _on_closing
    backend.start()
    try:
        webview.start()
    finally:
        # Idempotent: the closing event normally started this already.
        backend.shutdown()
