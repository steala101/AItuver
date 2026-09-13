"""asyncio ベースの音声会話パイプライン。

Mic → VAD → STT → LLM → TTS → Speaker を Queue で接続する。
各モジュールは抽象インターフェース経由でのみ利用し、相互に依存しない。
on_event コールバックで GUI 等へ状態を通知できる。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import logging
import random
import re
import time
from uuid import uuid4
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from neuro_voice.audio.capture import MicCapture
from neuro_voice.audio.playback import SpeakerPlayback
from neuro_voice.autonomy import (
    AutonomousActionSystem, AutonomyEvent, AutonomyEventType,
    AutonomyHeartbeatScheduler, PrivacyScope,
)
from neuro_voice.llm.base import LLMBackend
from neuro_voice.memory.conversation import ConversationManager, InterruptedTurn
from neuro_voice.memory.interaction import InteractionClassifier, SpeechIntent
from neuro_voice.dialogue import ConversationEvent, ConversationEventType, ConversationOrchestrator
from neuro_voice.realtime import (
    ContextAssembler, PlayedTextTracker, TurnManager, TurnState, response_can_play, response_is_active,
)
from neuro_voice.stt.base import Transcriber
from neuro_voice.tts.base import TTSBackend
from neuro_voice.tts.style import StyleManager
from neuro_voice.utils.config import Config
from neuro_voice.utils.emotion import EmotionTagParser
from neuro_voice.llm.prompt_layout import insert_before_user_turn
from neuro_voice.utils.errors import safe_error_text, safe_exception_summary
from neuro_voice.cognition.turn_integrity import CommitKind, TextStage
from neuro_voice.cognition.recall import retrieval_trigger
from neuro_voice.utils.latency import LatencyWindow, _PAIRS, SourceType, TurnMetrics
from neuro_voice.utils.textseg import SentenceSegmenter, strip_think
from neuro_voice.vad.segmentation import leading_silence_frames as leading_silence
from neuro_voice.vad.segmentation import trim_range

logger = logging.getLogger(__name__)


def _delivery_hash(value: str | bytes | np.ndarray) -> str:
    """Stable short fingerprint for delivery diagnostics; never log content."""
    if isinstance(value, np.ndarray):
        payload = np.ascontiguousarray(value).tobytes()
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = bytes(value)
    return hashlib.sha256(payload).hexdigest()[:16]

EventCallback = Callable[[str, dict[str, Any]], None]


class VoicePipeline:
    """会話パイプライン全体を管理する。"""

    def __init__(
        self,
        cfg: Config,
        llm: LLMBackend,
        tts: TTSBackend,
        conv: ConversationManager,
        stt: Transcriber | None = None,
        on_event: EventCallback | None = None,
        mind=None,
    ):
        self._cfg = cfg
        self._llm = llm
        self._tts = tts
        self._conv = conv
        self._stt = stt
        self._on_event = on_event
        self._mind = mind  # Mind (記憶+人格)。None なら従来動作
        self._autonomy = AutonomousActionSystem(cfg)
        self._autonomy_heartbeat = AutonomyHeartbeatScheduler(
            cfg, self._on_autonomy_heartbeat, name="local",
        )
        self._autonomy_task: asyncio.Task | None = None
        self._autonomy_last_human_speech_ended_at = time.monotonic()
        self._autonomy_last_status: dict[str, Any] = {
            "evaluated_at": 0.0,
            "final_action": "未評価",
            "suppression_reasons": ["最初のHeartbeat待ち"],
        }
        #: 1ターンを追う所（Phase 7D ②③）。**既定は無効**。
        #: 無効でも `TurnMetrics` の persona/source_type は埋める——
        #: あれは計測の背骨であって、この機能フラグの持ち物ではない。
        from neuro_voice.cognition.turn_tracker import tracker_from_config

        self._turns = tracker_from_config(cfg)
        #: この起動の識別子。ターンをまたいで同じ実行だと分かるように。
        self._turn_session_id = uuid4().hex[:12]
        self._music_hook = None  # ローカル音楽コマンド処理 (GUIが登録。async fn(text)->bool)
        self._humming_capture_hook = None  # async fn(audio, sample_rate)->bool; only consumes an armed hum turn
        self._pronunciation_learning_hook = None  # async fn(user_text, last_assistant_text)->(surface, reading)|None
        self._tts_volume_hook = None  # sync fn(TTSVolumeCommand)->spoken acknowledgement
        self._last_assistant_text = ""
        #: 割り込みで言い終えられなかった発話。認知カーネルが
        #: 「続きを言うか、捨てるか」を選ぶのに要る。
        self._last_interrupted_text = ""
        #: このターンの行動決定。結果を戻す時に照合する。
        self._cognitive_decision = None
        #: このターンの認知トレース。結果が出るまで持ち越す。
        self._cognitive_trace = None
        self._cognitive_trace_writer = None
        #: Trace closure must survive normal shutdown long enough to record a
        #: partial delivery snapshot if physical playback was interrupted.
        self._trace_closure_tasks: set[asyncio.Task] = set()
        self._pending_legacy_trace = None
        #: Local 通常会話の実測を移動集計する。Discord と同じ計測背骨を使う。
        self._latency_window = LatencyWindow()
        self._last_prompt_profile: dict[str, Any] = {}
        self._last_llm_diagnostics: dict[str, Any] = {}
        # Initialize once at startup so configuration/path failures reach the
        # existing application log even when cognition itself is disabled.
        self._trace_writer()
        # Process-local only: it is never written to config.yaml.
        from neuro_voice.cognition.test_session import CognitionTestSession
        self._cognition_test_session = CognitionTestSession()
        #: 注意と発話機会。**遅延生成**——既定では止まっているので、
        #: 起動時に作っても何もしない。
        self._initiative = None
        #: 直前のターンの結果と話題。沈黙直後の蒸し返しを止めるため。
        self._last_outcome_status = ""
        self._last_topic_id = ""
        self._last_turn_at = 0.0
        self._music_context_provider = None  # callable()->再生中の曲情報。会話時だけLLMへ共有
        self._sr = int(cfg.get("audio.sample_rate", 16000))
        self._frame = int(cfg.get("audio.frame_samples", 512))
        self._frame_ms = self._frame * 1000 / self._sr
        self._playback = SpeakerPlayback(
            device=cfg.get("audio.ai_output_device") or cfg.get("audio.output_device"),
            prebuffer_s=0.0,
        )
        self._frame_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=256)
        # Retain only a short rolling window.  It is used solely after an
        # explicit "what song is this" request; normal conversation never
        # exports or persists this buffer.
        history_s = max(5.0, float(cfg.get("music_recognition.capture_seconds", 12.0)))
        self._recent_audio: deque[np.ndarray] = deque(
            maxlen=max(1, int(history_s * self._sr / self._frame) + 2)
        )
        self._respond_task: asyncio.Task | None = None
        self._active_response_id: str | None = None
        self._cancelled_response_ids: set[str] = set()
        self._active_playback_tracker: PlayedTextTracker | None = None
        self._active_response_user_text = ""
        self._input_tasks: set[asyncio.Task] = set()
        self._stt_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._tts_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._interim_busy = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic: MicCapture | None = None
        self._level = 0.0
        self._vision = None
        self._vision_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")
        self._vision_enabled = bool(cfg.get("vision.enabled", False))
        self._vision_mode = str(cfg.get("vision.mode", "on_demand"))
        self._vision_provider = None
        self._vision_provider_name = str(cfg.get("vision.provider", "local"))
        self._local_vision_unsupported = False  # ローカルLLMが画像非対応と判明したらTrue
        self._perception = None
        self._video_service = None
        # Screen-share vision is a conversation source, not a Discord-audio
        # feature.  Keep the same OBS-backed session available to local mic
        # turns so "画面共有を見て" never falls through to monitor capture.
        self._screen_share_session_obj = None
        self._minecraft_knowledge = None
        self._game_commentary_task: asyncio.Task | None = None
        self._game_companion_director = None
        self._pending_game_event = None
        self._last_game_commentary = 0.0
        self._last_game_commentary_text = ""
        self._vision_tasks: set[asyncio.Task] = set()
        self._auto_last = 0.0
        # DeepSearch (Web検索)
        self._search = None
        from neuro_voice.search.contracts import SearchEvidenceCache
        self._search_evidence_cache = SearchEvidenceCache(
            ttl_seconds=600.0, clock=time.monotonic)
        self._search_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search")
        self._search_enabled = bool(cfg.get("search.enabled", False))
        self._search_armed_until = 0.0
        # 疑似感情
        self._emotion_enabled = bool(cfg.get("emotion.enabled", True))
        # Whisperとは独立した、openSMILE + SpeechBrainの音声感情認識。
        self._user_emotion_enabled = bool(cfg.get("user_emotion.enabled", True))
        self._emotion_recognizer = None
        self._emotion_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emotion")
        # 自発モード
        self._proactive_enabled = bool(cfg.get("proactive.enabled", False))
        # Discord VC 中は発話経路を DiscordBridge に一本化する。ローカル
        # パイプラインの自発発話がヘッドホンだけへ流れるのを防ぐ。
        self._proactive_suspended = False
        self._proactive_next = 0.0
        self._last_activity = time.monotonic()
        self._user_speaking = False
        self._classifier = InteractionClassifier(
            acknowledgement_max_chars=int(cfg.get("barge_in.acknowledgement_max_chars", 12)),
        )
        self._style_manager = StyleManager(
            user_emotion_threshold=float(cfg.get("style_manager.user_emotion_confidence_threshold", 0.75)),
        )
        self._turn_manager = TurnManager(self._on_turn_transition)
        # Continuing requests ("keep going until…").  The directive itself is
        # owned by Mind so local voice and Discord share one decision; the
        # pipeline only owns the task that runs a segment on this device.
        self._directive_runtime_obj = None
        #: Measures how much of each prompt repeats the previous one.
        self._prompt_profiler = None
        # True while Discord owns the microphone; the device watcher
        # must not reopen it behind that decision.
        self._mic_paused = False
        # 音声認識の誤変換対策。False = 未構築 (None は「無効」を意味する)。
        self._repairer_obj: Any = False
        self._stt_vocabulary = None
        self._context_assembler = ContextAssembler(cfg, mind, self._emit)
        presets = cfg.get("persona.presets", {}) or {}
        active_persona = str(cfg.get("persona.active", "") or "")
        persona = presets.get(active_persona, {}) if isinstance(presets, dict) else {}
        self._persona_name = str(
            persona.get("name") or cfg.get("persona.name", "") or "AI"
        )
        self._conversation = ConversationOrchestrator(
            cfg,
            wake_words=[str(x) for x in (cfg.get("discord.wake_words") or [])],
            assistant_name=str(persona.get("name", "") or ""),
            on_event=self._on_conversation_event,
            # The local microphone is a single-user conversation; addressing
            # inference is only needed in the multi-party Discord path.
            force_response=True,
        )

    # ---------- 外部公開の状態 (GUI用・スレッドセーフ) ----------

    @property
    def level(self) -> float:
        """マイク入力レベル (0.0〜1.0)。"""
        return self._level

    @property
    def muted(self) -> bool:
        return self._mic.muted if self._mic is not None else False

    @property
    def state(self) -> str:
        """idle / listening / thinking / speaking のいずれか。"""
        turn_state = self._turn_manager.state
        if turn_state is TurnState.AI_SPEAKING or self._playback.is_active:
            return "speaking"
        if turn_state is TurnState.AI_THINKING or self._is_responding():
            return "thinking"
        if self._mic is not None and not self._mic.muted:
            return "listening"
        return "idle"

    @property
    def realtime_state(self) -> str:
        """Detailed Turn Manager state for diagnostics and UI telemetry."""
        return self._turn_manager.state.value

    def runtime_diagnostics(self) -> dict[str, Any]:
        """Expose safe liveness facts, never hidden reasoning or transcripts."""
        return {
            "state": self.state,
            "realtime_state": self.realtime_state,
            "proactive_enabled": self._autonomy.active,
            "proactive_suspended": self._proactive_suspended,
            "user_speaking": self._user_speaking,
            "response_active": self._is_responding(),
            "playback_active": self._playback.is_active,
            "autonomous_turn_alive": (
                self._autonomy_task is not None and not self._autonomy_task.done()
            ),
            "heartbeat": self._autonomy_heartbeat.health_status(),
            "autonomy": self._autonomy.snapshot(),
            "last_evaluation": dict(self._autonomy_last_status),
        }

    def _on_turn_transition(self, transition) -> None:
        self._emit(
            "turn_state",
            previous=transition.previous.value,
            state=transition.current.value,
            reason=transition.reason,
        )

    def _on_conversation_event(self, event: ConversationEvent) -> None:
        data = event.summary(include_text=bool(self._cfg.get("conversation.debug_transcripts", False)))
        data["conversation_event_type"] = data.pop("event_type")
        self._emit("conversation_event", **data)

    def _autonomy_decide(self, event_type: AutonomyEventType, *, text: str = "",
                          payload: dict | None = None, expires_in_s: float | None = None):
        """Record a local event without allowing optional autonomy to delay STT."""
        now = time.monotonic()
        try:
            privacy_scope = PrivacyScope(self._mind.autonomy_privacy_scope(text)) if text else PrivacyScope.CURRENT_CONVERSATION
        except (AttributeError, ValueError):
            privacy_scope = PrivacyScope.CURRENT_CONVERSATION
        event = AutonomyEvent(
            event_type, "local", payload={"text": text, **(payload or {})},
            speaker_id="local:mic", timestamp=now,
            privacy_scope=privacy_scope,
            expires_at=(now + expires_in_s if expires_in_s is not None else None),
        )
        decision = self._autonomy.publish(
            event, group=False, human_speaking=self._user_speaking,
            assistant_busy=self._is_responding() or self._playback.is_active,
        )
        self._emit("autonomy_decision", action=decision.action_type.value,
                   reason=decision.reason_code, utility=decision.utility,
                   autonomy_event_type=event_type.value)
        return decision

    def _local_conversation_decision(self, text: str):
        activity_active = False
        directive_waiting = False
        if self._mind is not None:
            with contextlib.suppress(Exception):
                activity_active = (
                    self._mind.activity_active() or self._mind.game_profile_active()
                )
            with contextlib.suppress(Exception):
                directive_waiting = self._mind.directive_waiting_for_user("local")
        decision = self._conversation.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL,
            source="local",
            text=text,
            metadata={
                "speaker_key": "local:mic",
                "speaker_name": "ユーザー",
                "activity_active": activity_active,
                "directive_waiting_for_user": directive_waiting,
            },
        ))
        self._emit(
            "conversation_decision",
            target_probability=decision.addressing.target_probability,
            action=decision.addressing.decision.value,
            reasons=list(decision.addressing.reasons),
            response_role=decision.response_plan.response_role.value,
            should_respond=decision.response_plan.should_respond,
            plan_reason=decision.response_plan.reason,
            settles_exchange=decision.response_plan.settles_exchange,
            state=decision.state,
        )
        return decision

    def set_muted(self, muted: bool) -> None:
        if self._mic is not None:
            self._mic.muted = muted
            self._emit("mic", muted=muted)

    def pause_mic(self) -> None:
        """ローカルマイクのストリームを完全に停止する。

        Discord(mix)取り込みが同じマイクをもう1本開くと競合してデータが
        来なくなるため、Discord接続中はローカル側のマイクを閉じて1本に集約する。
        """
        # Deliberate: the device watcher must not helpfully reopen it.
        self._mic_paused = True
        mic = self._mic
        if mic is not None and mic.active:
            try:
                mic.stop()
            except Exception:
                logger.exception("ローカルマイクの一時停止に失敗")

    def resume_mic(self) -> None:
        """一時停止したローカルマイクを再開する (Discord切断時)。"""
        self._mic_paused = False
        mic = self._mic
        if mic is not None and not mic.active:
            try:
                mic.start()
                mic.muted = False
                self._emit("mic", muted=False)
            except Exception:
                logger.exception("ローカルマイクの再開に失敗")

    @property
    def speaker_muted(self) -> bool:
        return self._playback.muted

    def set_speaker_muted(self, muted: bool) -> None:
        """ローカルスピーカー再生のON/OFF (Discord側の音声には影響しない)。"""
        self._playback.set_muted(muted)
        self._emit("speaker", muted=self._playback.muted)

    def submit_text(self, text: str) -> None:
        """別スレッドからテキスト発話を投入する (GUI用)。"""
        if self._loop is None or not text:
            return
        self._loop.call_soon_threadsafe(self._start_text_respond, text)

    def interrupt(self) -> None:
        """別スレッドから応答を中断する (GUI用)。"""
        if self._loop is None:
            return

        def _go() -> None:
            if self._is_responding():
                self._respond_task.cancel()
            self._playback.fade_out_and_clear(
                float(self._cfg.get("conversation_audio.interrupt_fade_ms", 280)),
            )
            self._emit("interrupted")

        self._loop.call_soon_threadsafe(_go)

    def reset_conversation(self) -> None:
        self._conv.reset()
        if self._mind is not None:
            self._mind.reset_session()
        self._emit("reset")

    def set_music_hook(self, fn) -> None:
        """「〇〇流して」等をLLMの手前で処理するフックを登録する (async fn(text)->bool)。"""
        self._music_hook = fn

    def set_humming_capture_hook(self, fn) -> None:
        """Register an armed, pre-STT humming capture handler."""
        self._humming_capture_hook = fn

    def set_pronunciation_learning_hook(self, fn) -> None:
        """読み方の口頭訂正を、UI側の永続辞書へ渡すフック。"""
        self._pronunciation_learning_hook = fn

    def set_tts_volume_hook(self, fn) -> None:
        """AI音声の共通音量を変更・保存するフックを登録する。"""
        self._tts_volume_hook = fn

    def set_music_context_provider(self, fn) -> None:
        """再生中の曲情報を返す関数を登録する。"""
        self._music_context_provider = fn

    def recent_audio(self, seconds: float | None = None) -> np.ndarray:
        """Return an in-memory tail of the current input source for explicit music ID."""
        frames = list(self._recent_audio)
        if not frames:
            return np.empty(0, dtype=np.float32)
        limit = max(1, int((seconds or 12.0) * self._sr / self._frame))
        return np.concatenate(frames[-limit:]).astype(np.float32, copy=False)

    def _music_context_message(self) -> dict | None:
        if self._music_context_provider is None:
            return None
        try:
            from neuro_voice.discord_bridge.music import now_playing_context

            content = now_playing_context(self._music_context_provider())
            return {"role": "system", "content": content} if content else None
        except Exception:
            logger.exception("再生中の音楽コンテキスト取得でエラー")
            return None

    def set_llm(self, llm: LLMBackend) -> None:
        """LLMバックエンドを差し替える。次のターンから反映される。"""
        self._llm = llm
        if self._mind is not None:
            self._mind.set_llm(llm)
        self._local_vision_unsupported = False  # 新モデルは画像対応かもしれない
        if self._perception is not None:
            self._perception.cancel_background()
        self._perception = None

    def set_stt(self, stt: Transcriber) -> None:
        """STTを差し替える (GPU/CPU切替用)。次の認識から反映される。"""
        self._stt = stt

    @property
    def vision_enabled(self) -> bool:
        return self._vision_enabled

    def vision_debug_status(self) -> dict:
        share = self._screen_share_session_obj
        if share is not None and share.active:
            try:
                status = share.debug_status()
                status["source_mode"] = "discord_screen_share"
                return status
            except Exception:
                logger.debug("Unable to build screen-share debug status", exc_info=True)
        service = self._video_service
        if service is None:
            return {"worker_state": "stopped", "capture_count": 0, "analysis_count": 0}
        try:
            status = service.debug_status()
            if self._game_companion_director is not None:
                status["reaction_suppressed_reason"] = (
                    self._game_companion_director.last_suppression_reason
                )
            return status
        except Exception:
            logger.debug("Unable to build vision debug status", exc_info=True)
            return {"worker_state": "error"}

    @property
    def vision_mode(self) -> str:
        return self._vision_mode

    def set_vision(self, enabled: bool, mode: str | None = None) -> None:
        """画面認識のON/OFF・モードを切り替える (スレッドセーフ)。"""
        self._vision_enabled = bool(enabled)
        if mode in ("on_demand", "auto"):
            self._vision_mode = mode
        self._auto_last = time.monotonic()
        self._emit("vision", enabled=self._vision_enabled, mode=self._vision_mode)

    def _get_screen_share_session(self):
        """Return the shared OBS screen-share session for local conversation."""
        if self._screen_share_session_obj is None:
            from neuro_voice.vision.discord_share import DiscordScreenShareSession

            self._screen_share_session_obj = DiscordScreenShareSession(
                self._cfg,
                self._llm,
                is_busy=lambda: (
                    self._user_speaking
                    or self._is_responding()
                    or self._playback.is_active
                ),
                on_frame=lambda frame: self._emit(
                    "vision_frame",
                    image=base64.b64encode(frame.data).decode("ascii"),
                    source=(
                        self._screen_share_session_obj.source_kind
                        if self._screen_share_session_obj is not None
                        else "obs_visual"
                    ),
                    source_name=str(
                        (getattr(frame, "metadata", {}) or {}).get(
                            "obs_source_name", "",
                        )
                    ),
                ),
                on_observation=self._on_game_observation,
            )
        return self._screen_share_session_obj

    async def _handle_screen_share_control(
        self, text: str, *, requester: str = "local:mic",
    ) -> tuple[bool, str | None]:
        """Route an explicit local command without constructing idle sessions."""
        from neuro_voice.vision.discord_share import classify_screen_share_control

        if not classify_screen_share_control(text).matched:
            return False, None
        return await self._get_screen_share_session().handle_control(
            text, requester=requester,
        )

    def set_vision_provider(self, name: str) -> None:
        """Visionプロバイダ (local/claude/google) を切り替える。次のキャプチャから反映。"""
        self._vision_provider_name = str(name)
        self._vision_provider = None  # 次回利用時に再生成
        self._local_vision_unsupported = False
        if self._perception is not None:
            self._perception.cancel_background()
        self._perception = None

    @property
    def search_enabled(self) -> bool:
        return self._search_enabled

    def set_search(self, enabled: bool) -> None:
        """DeepSearch (Web検索) のON/OFF。"""
        self._search_enabled = bool(enabled)
        self._emit("search", enabled=self._search_enabled)

    @property
    def proactive_enabled(self) -> bool:
        return self._autonomy.active

    def set_proactive(self, enabled: bool) -> None:
        """自発モードのON/OFF。"""
        self._autonomy.configure(enabled=enabled)
        # The settings control now governs the event-driven system.  Leave the
        # old random scheduler disabled unless an explicit legacy config asks
        # for it at startup.
        self._proactive_enabled = False
        self._schedule_next_proactive()
        # Heartbeat also owns queue maintenance and autonomous research.
        # Disabling unsolicited *speech* must not disable those background
        # responsibilities; _on_autonomy_heartbeat() already returns before
        # speech generation when autonomy.active is false.
        if self._loop is not None:
            self._autonomy_heartbeat.resume()
            self._autonomy_heartbeat.start()
        self._emit("proactive", enabled=self._autonomy.active)

    def set_proactive_suspended(self, suspended: bool) -> None:
        """Temporarily suppress local autonomous speech while Discord owns audio."""
        self._proactive_suspended = bool(suspended)
        self._schedule_next_proactive()
        if suspended:
            self._autonomy_heartbeat.pause()
        else:
            self._autonomy_heartbeat.resume()
        self._emit("proactive", enabled=self._autonomy.active,
                   suspended=self._proactive_suspended)

    def set_emotion_enabled(self, enabled: bool) -> None:
        self._emotion_enabled = bool(enabled)

    # ---------- イベント通知 ----------

    def _emit(self, event_type: str, **data: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, data)
        except Exception:
            logger.exception("イベント通知でエラー")

    # ---------- 音声モード ----------

    async def run_voice(self) -> None:
        """マイク入力による音声会話ループを開始する。"""
        if self._stt is None:
            raise RuntimeError("音声モードには STT が必要です")
        from neuro_voice.vad.silero import SileroVAD

        self._loop = asyncio.get_running_loop()
        vad = SileroVAD(self._sr)
        # VADもウォームアップ (初回呼び出しの遅延で最初の発話の頭を逃さないように)
        vad.prob(np.zeros(self._frame, dtype=np.float32))
        vad.reset()
        self._mic = self._make_capture()
        self._playback.start()
        if not self._mic.start():
            # A headset switched on later is normal.  Keep running and let the
            # device watcher open the microphone when it appears.
            self._emit("status", message=(
                "⚠ マイクが見つかりません。接続すると自動で認識します"
            ))
        self._autonomy_heartbeat.start()
        # Ask the server where the fixed ~1100ms before the first token goes.
        # Runs once, off the conversation path, and only logs.
        if bool(self._cfg.get("llm.probe_on_start", True)):
            task = asyncio.create_task(self._probe_llm_latency(), name="llm-latency-probe")
            self._input_tasks.add(task)
            task.add_done_callback(self._input_tasks.discard)
        from neuro_voice.games import profile_allows_vision

        if (
            self._vision_enabled
            and bool(self._cfg.get("video.enabled", False))
            and profile_allows_vision(self._cfg)
        ):
            from neuro_voice.vision.video import VideoObservationService

            self._video_service = VideoObservationService(
                self._cfg, self._get_perception(),
                is_busy=lambda: self._user_speaking or self._is_responding() or self._playback.is_active,
                on_observation=self._on_game_observation,
            )
            if not await self._video_service.start():
                detail = self._video_service.last_capture_error
                self._emit(
                    "status",
                    message=(
                        f"⚠ 映像認識を開始できませんでした: {detail}"
                        if detail else "⚠ 映像認識を開始できませんでした。音声会話は継続します"
                    ),
                )
        auto_task = asyncio.create_task(self._auto_commentary_loop())
        device_task = asyncio.create_task(
            self._device_watch_loop(), name="audio-device-watch",
        )
        logger.info("パイプライン開始")
        print("🎧 話しかけてください (Ctrl+C で終了)")
        try:
            await self._vad_loop(vad)
        finally:
            auto_task.cancel()
            device_task.cancel()
            await self._autonomy_heartbeat.stop()
            autonomous = self._autonomy_task
            if autonomous is not None and not autonomous.done():
                autonomous.cancel()
            for task in list(self._input_tasks):
                task.cancel()
            for task in list(self._vision_tasks):
                task.cancel()
            if self._video_service is not None:
                await self._video_service.stop()
                self._video_service = None
            if self._screen_share_session_obj is not None:
                await self._screen_share_session_obj.close()
                self._screen_share_session_obj = None
            if self._perception is not None:
                close_perception = getattr(self._perception, "close", None)
                if callable(close_perception):
                    await close_perception()
                self._perception = None
            if self._game_commentary_task is not None:
                self._game_commentary_task.cancel()
            self._mic.stop()
            self._playback.stop()

    async def shutdown(self) -> None:
        """Stop every local realtime component and release worker executors.

        GUI shutdown calls this after cancelling ``run_voice``.  Keep it
        idempotent because the voice loop's ``finally`` block performs part of
        the same cleanup.
        """
        if self._closed:
            return
        self._closed = True
        response_id = self._active_response_id
        if response_id:
            self._cancelled_response_ids.add(response_id)
            self._llm.cancel_request(response_id)
        tasks = [
            self._respond_task, self._game_commentary_task, self._autonomy_task,
            *list(self._input_tasks), *list(self._vision_tasks),
            *list(self._trace_closure_tasks),
        ]
        current = asyncio.current_task()
        for task in tasks:
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None and task is not current and not task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._autonomy_heartbeat.stop()
        if self._video_service is not None:
            with contextlib.suppress(Exception):
                await self._video_service.stop()
            self._video_service = None
        if self._screen_share_session_obj is not None:
            with contextlib.suppress(Exception):
                await self._screen_share_session_obj.close()
            self._screen_share_session_obj = None
        if self._perception is not None:
            close = getattr(self._perception, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()
            self._perception = None
        if self._mic is not None:
            with contextlib.suppress(Exception):
                self._mic.stop()
        with contextlib.suppress(Exception):
            self._playback.stop()
        if self._cognitive_trace_writer is not None:
            with contextlib.suppress(Exception):
                self._cognitive_trace_writer.close(timeout=0.75)
        close_tts = getattr(self._tts, "close", None)
        if callable(close_tts):
            with contextlib.suppress(Exception):
                close_tts()
        for executor in (
            self._stt_ex, self._tts_ex, self._vision_ex, self._search_ex, self._emotion_ex,
        ):
            with contextlib.suppress(Exception):
                executor.shutdown(wait=False, cancel_futures=True)

    async def refresh_video_observation(self) -> bool:
        """Apply a changed camera/game-window setting without an app restart."""
        previous, self._video_service = self._video_service, None
        if previous is not None:
            await previous.stop()
        # The eye/vision switch is the master switch.  Turning it off must
        # release the native game-capture session, not merely stop attaching
        # images to LLM requests.
        from neuro_voice.games import profile_allows_vision

        if (
            not self._vision_enabled
            or not bool(self._cfg.get("video.enabled", False))
            or not profile_allows_vision(self._cfg)
        ):
            logger.info("Game video capture stopped (vision=%s video=%s)", self._vision_enabled,
                        bool(self._cfg.get("video.enabled", False)))
            self._emit("status", message="Game video capture stopped")
            return True
        from neuro_voice.vision.video import VideoObservationService

        service = VideoObservationService(
            self._cfg, self._get_perception(),
            is_busy=lambda: self._user_speaking or self._is_responding() or self._playback.is_active,
            on_observation=self._on_game_observation,
        )
        if not await service.start():
            detail = service.last_capture_error
            self._emit("status", message=detail or "Game video input could not be restarted")
            return False
        self._video_service = service
        self._emit("status", message="Game video input updated")
        return True

    def _make_capture(self):
        """ローカル会話用のマイク取り込み。

        Discord通話の聞き取り(ループバック)は DiscordBridge 側で行う
        (audio.input_mode=loopback/mix)。ローカルパイプラインは常にマイク。
        """
        return MicCapture(
            self._frame_q, self._loop, self._sr, self._frame,
            self._cfg.get("audio.input_device"),
        )

    async def _vad_loop(self, vad) -> None:
        cfg = self._cfg
        thr = float(cfg.get("vad.threshold", 0.5))
        thr_end = float(cfg.get("vad.end_threshold", 0.35))
        start_frames = int(cfg.get("vad.start_frames", 3))
        start_frames_bi = int(cfg.get("vad.start_frames_barge_in", 8))
        end_silence_ms = float(cfg.get("vad.end_silence_ms", 400))
        min_speech_ms = float(cfg.get("vad.min_speech_ms", 300))
        pre_roll_len = max(1, int(float(cfg.get("vad.pre_roll_ms", 224)) / self._frame_ms))
        trim_silence = bool(cfg.get("vad.trim_silence", True))
        keep_leading = max(0, int(float(cfg.get("vad.keep_leading_ms", 80)) / self._frame_ms))
        keep_trailing = max(0, int(float(cfg.get("vad.keep_trailing_ms", 80)) / self._frame_ms))
        interim = bool(cfg.get("stt.interim", False))
        interim_iv = float(cfg.get("stt.interim_interval_ms", 1200)) / 1000

        # A filler like 「うん」「えーと」 often sits below the start threshold,
        # so keep each pre-roll frame's probability and trim by what was
        # actually silent rather than assuming everything pre-trigger was.
        onset_threshold = float(cfg.get("vad.onset_threshold", 0.20))
        pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_len)
        pre_roll_probs: deque[float] = deque(maxlen=pre_roll_len)
        speaking = False
        voiced = 0
        silence_frames = 0
        utter: list[np.ndarray] = []
        last_interim = 0.0
        started_while_busy = False
        leading_silence_frames = 0
        pause_stuck_frames = 0

        while True:
            frame = await self._frame_q.get()
            self._recent_audio.append(np.asarray(frame, dtype=np.float32).copy())
            self._level = min(1.0, float(np.sqrt(np.mean(frame ** 2))) * 6.0)
            prob = vad.prob(frame)

            # 自己修復: barge-in保留の無音化が、応答見送り等の経路で解除され
            # ないまま残ると「発話中のまま固まる+波形停止」になる。発話も
            # 応答処理も走っていないのに一時停止が約1秒続いたら自動復帰する。
            if (self._playback.paused and not self._user_speaking
                    and not self._input_tasks and not self._is_responding()):
                pause_stuck_frames += 1
                if pause_stuck_frames >= 30:  # 512サンプル/フレーム ≈ 32ms × 30 ≈ 1秒
                    logger.warning("barge-in保留が解放されず残っていたため自動復帰します")
                    self._playback.resume_from_pause(
                        float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                    )
                    self._turn_manager.speech_ignored()
                    pause_stuck_frames = 0
            else:
                pause_stuck_frames = 0

            if not speaking:
                pre_roll.append(frame)
                pre_roll_probs.append(prob)
                voiced = voiced + 1 if prob >= thr else 0
                busy = self._playback.is_active or self._is_responding()
                if voiced >= (start_frames_bi if busy else start_frames):
                    speaking = True
                    started_while_busy = busy
                    silence_frames = 0
                    utter = list(pre_roll)
                    # Trim only what was genuinely silent.  A quiet 「えーと」 in
                    # the pre-roll is the start of the sentence, not padding.
                    leading_silence_frames = leading_silence(
                        list(pre_roll_probs),
                        onset_threshold=onset_threshold,
                        keep_frames=keep_leading,
                    )
                    last_interim = time.monotonic()
                    self._on_speech_start(started_while_busy)
                continue

            utter.append(frame)
            silence_frames = silence_frames + 1 if prob < thr_end else 0

            if interim and time.monotonic() - last_interim >= interim_iv:
                last_interim = time.monotonic()
                self._schedule_interim(np.concatenate(utter))

            if silence_frames * self._frame_ms >= end_silence_ms:
                speaking = False
                voiced = 0
                pre_roll.clear()
                pre_roll_probs.clear()
                vad.reset()
                frames_for_stt = utter
                if trim_silence:
                    start, end = trim_range(
                        len(utter),
                        leading_silence=leading_silence_frames,
                        trailing_silence=silence_frames,
                        keep_trailing=keep_trailing,
                    )
                    frames_for_stt = utter[start:end]
                audio = np.concatenate(frames_for_stt)
                utter = []
                self._user_speaking = False
                if len(audio) * 1000 / self._sr >= min_speech_ms:
                    self._start_respond(
                        audio,
                        started_while_busy=started_while_busy,
                        duration_ms=len(audio) * 1000 / self._sr,
                    )
                elif started_while_busy:
                    # No STT task is created for a tiny VAD blip, so resolve
                    # the pending pause here rather than leaving output muted.
                    self._playback.resume_from_pause(
                        float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                    )
                    self._turn_manager.speech_ignored()
                    self._emit("speech_ignored", reason="vad_too_short")

    def _is_responding(self) -> bool:
        return self._respond_task is not None and not self._respond_task.done()

    def _is_response_active(self, response_id: str) -> bool:
        return response_is_active(response_id, self._active_response_id, self._cancelled_response_ids)

    def _is_response_cancelled(self, response_id: str) -> bool:
        """Queued PCM remains valid after LLM/TTS generation has finished."""
        return not response_can_play(response_id, self._cancelled_response_ids)

    def _on_speech_start(self, started_while_busy: bool) -> None:
        """Silence immediately; decide acknowledgement vs interruption after STT."""
        self._last_activity = time.monotonic()
        self._user_speaking = True
        self._autonomy.note_human_speech_started()
        self._autonomy_heartbeat.cancel_pending_tick()
        # A running continuation stops *producing* the moment a human starts,
        # but keeps what is already synthesized.  Whether this was a
        # backchannel or a real interruption is not known until STT finishes,
        # and a backchannel has to resume from where it was (第7条).
        self._pause_directive_for_human(discard_audio=False)
        autonomous = self._autonomy_task
        if autonomous is not None and not autonomous.done():
            response_id = self._active_response_id
            if response_id:
                self._cancelled_response_ids.add(response_id)
                self._llm.cancel_request(response_id)
            autonomous.cancel()
            # Autonomous turns never resume after a human starts speaking;
            # remove already synthesized PCM before normal barge-in handling.
            self._playback.discard_paused_audio()
        self._autonomy_decide(AutonomyEventType.HUMAN_SPEECH_STARTED, expires_in_s=2.0)
        self.note_attention_event(
            "user_speech_started", salience=.9, urgency=.6, dedup="user_speech")
        # A background reflection competes for the same local LLM.  Cancel it
        # at VAD start rather than waiting until STT has completed.
        if self._mind is not None:
            self._mind.prioritize_realtime_turn()
        if self._video_service is not None:
            self._video_service.cancel_background()
        if self._perception is not None:
            self._perception.cancel_background()
        self._turn_manager.user_started(assistant_busy=started_while_busy)
        self._emit("speech_start")
        if started_while_busy:
            self._playback.pause_immediately()
            self._emit("speaker_paused", reason="barge_in_pending")

    def _schedule_interim(self, audio: np.ndarray) -> None:
        """発話途中の暫定認識 (表示のみ)。CPUモードでは本認識を妨げるため行わない。"""
        if self._interim_busy or self._is_responding() or self._stt is None:
            return
        if getattr(self._stt, "device", "cuda") == "cpu":
            return
        self._interim_busy = True
        loop = asyncio.get_running_loop()
        # 途中認識は速度優先 (小さいbeam)。割り込み語検出だけが目的
        future = loop.run_in_executor(self._stt_ex, self._stt.transcribe_partial, audio, self._sr)

        def _done(fut) -> None:
            self._interim_busy = False
            with contextlib.suppress(Exception):
                text = fut.result()
                if text:
                    print(f"\r… {text}", end="", flush=True)
                    self._emit("user_partial", text=text)

        future.add_done_callback(_done)

    def _start_respond(
        self,
        audio: np.ndarray,
        *,
        started_while_busy: bool = False,
        duration_ms: float | None = None,
    ) -> None:
        """STT は既存応答を止めずに実行し、結果で割り込みかを決める。"""
        task = asyncio.create_task(self._respond(
            audio, started_while_busy=started_while_busy, duration_ms=duration_ms,
        ))
        self._input_tasks.add(task)
        task.add_done_callback(self._input_tasks.discard)

    def _start_text_respond(self, text: str) -> None:
        if self._is_responding():
            self._respond_task.cancel()
        self._playback.fade_out_and_clear(
            float(self._cfg.get("conversation_audio.interrupt_fade_ms", 280)),
        )
        self._turn_manager.assistant_thinking()
        self._respond_task = asyncio.create_task(self._respond_from_text(text))

    async def _respond(
        self,
        audio: np.ndarray,
        *,
        started_while_busy: bool = False,
        duration_ms: float | None = None,
    ) -> None:
        metrics = TurnMetrics()
        metrics.mark("speech_end")
        self._begin_turn(metrics)
        loop = asyncio.get_running_loop()
        if self._humming_capture_hook is not None:
            try:
                if await self._humming_capture_hook(audio, self._sr):
                    if started_while_busy:
                        # barge-in保留の無音化を解除しないと再生と状態表示が固まる
                        self._playback.resume_from_pause(
                            float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                        )
                    self._turn_manager.assistant_done()
                    return
            except Exception:
                logger.exception("鼻歌キャプチャ処理でエラー")
        # Bias the recognizer with what this conversation is about *before*
        # decoding.  Fixing what it hears is cheaper and better than fixing
        # what it wrote.
        self._apply_stt_context_bias()
        try:
            from neuro_voice.stt.transcript import detailed_transcribe

            transcript = await loop.run_in_executor(
                self._stt_ex, detailed_transcribe, self._stt, audio, self._sr
            )
            transcript = self._repair_transcript(transcript)
            from neuro_voice.stt.hallucination import is_known_hallucination

            effective_duration_ms = (
                float(duration_ms) if duration_ms is not None
                else (len(audio) / max(1, self._sr) * 1000.0)
            )
            if is_known_hallucination(
                transcript, duration_ms=effective_duration_ms,
            ):
                logger.info(
                    "短音声の既知ASR幻聴を破棄: %s (%.0fms)",
                    transcript.text, effective_duration_ms,
                )
                self._emit(
                    "stt_hallucination_rejected",
                    text=transcript.text,
                    duration_ms=round(effective_duration_ms),
                )
                transcript.text = ""
            text = transcript.text
        except Exception as e:
            logger.exception("STT でエラー")
            self._emit("error", message=f"音声認識エラー: {e}")
            if started_while_busy:
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
                self._turn_manager.speech_ignored()
            return
        metrics.mark("stt_done")
        self._turn_manager.stt_finalizing()
        if not text:
            # 無言で捨てると「無視された」ように見えるためユーザーへ知らせる
            print("\n🎤 (聞き取れませんでした)")
            self._emit("status", message="🎤 うまく聞き取れませんでした。もう一度どうぞ")
            if started_while_busy:
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
                self._turn_manager.speech_ignored()
                self._emit("speech_ignored", reason="empty_stt")
            return
        if started_while_busy:
            classification = self._classifier.classify(text, duration_ms)
            self._emit(
                "speech_classified",
                intent=classification.intent.value,
                text=text,
                reason=classification.reason,
            )
            if classification.intent is SpeechIntent.ACKNOWLEDGEMENT:
                logger.info("barge-in: 相槌として継続 (%s)", classification.normalized_text)
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
                # The directive was paused at VAD onset and nothing else will
                # un-pause it: a backchannel never reaches the reply path.
                self._resume_directive_after_backchannel()
                self._emit("user_acknowledgement", text=text)
                self._turn_manager.backchannel(
                    assistant_busy=self._playback.is_active or self._is_responding()
                )
                return
            if classification.intent is SpeechIntent.FALSE_POSITIVE:
                logger.info("barge-in: 誤検知として破棄 (%s)", classification.reason)
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
                self._resume_directive_after_backchannel()
                self._emit("speech_ignored", reason=classification.reason)
                self._turn_manager.speech_ignored()
                return
            if bool(self._cfg.get("barge_in.enabled", True)):
                self._turn_manager.barge_in_confirmed()
                logger.info("barge-in: 実質的な割り込みを検出 (%s)", text[:40])
                # Confirmed interruption: now the old plan's audio may go.
                self._pause_directive_for_human(discard_audio=True)
                await self._cancel_active_response(interruption_text=text)
                self._emit("interrupted")
            else:
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
                self._resume_directive_after_backchannel()
                self._emit("speech_ignored", reason="barge_in_disabled")
                return
        print(f"\n🗣  {text}")
        self._emit("user_final", text=text)
        self._autonomy_last_human_speech_ended_at = time.monotonic()
        self._autonomy.note_human_speech_ended(now=self._autonomy_last_human_speech_ended_at)
        autonomy = self._autonomy_decide(
            AutonomyEventType.HUMAN_UTTERANCE_FINALIZED, text=text, expires_in_s=5.0,
        )
        conversation_decision = self._local_conversation_decision(text)
        utterance_id = uuid4().hex
        response_id = uuid4().hex
        # A short acknowledgement or status update may be worth retaining as
        # A finalized human utterance is governed by ConversationManager.
        # Autonomy is observational here: it must never veto a normal reply
        # because a classifier missed a Japanese question/command expression.
        # Its speech gate is used only for optional, system-initiated actions.
        if not conversation_decision.response_plan.should_respond:
            if self._mind is not None:
                with contextlib.suppress(Exception):
                    self._mind.begin_conversation_turn(
                        text,
                        source="local",
                        response_id=response_id,
                        utterance_id=utterance_id,
                        conversation_topic=self._conversation.state.active_topic,
                    )
            # Choosing not to speak is still a turn.  A running directive has
            # to hear about it, otherwise it waits for a move that already
            # happened.
            with contextlib.suppress(Exception):
                runtime = self._directive_runtime
                if runtime is not None:
                    runtime.note_suppressed_user_turn(text)
            if started_while_busy:
                # 応答しない場合も、barge-in保留で無音化した再生は必ず戻す。
                # 戻さないと playback が「一時停止のまま再生中」に固まり、
                # 状態が発話中から戻らず左下のマイク波形も止まる。
                self._playback.resume_from_pause(
                    float(self._cfg.get("conversation_audio.resume_fade_ms", 80)),
                )
            self._turn_manager.assistant_done()
            return
        # Emotion analysis is useful enrichment, not a prerequisite for the
        # first token.  Run it alongside speaker resolution instead of adding
        # up to its timeout to every user turn.
        emotion_task = asyncio.create_task(self._analyze_user_emotion(audio))
        self._input_tasks.add(emotion_task)
        emotion_task.add_done_callback(self._input_tasks.discard)
        # 話者識別はLLM準備と並行して裏で走らせる (コンテキスト注入前に合流)
        speaker_task = None
        if self._mind is not None:
            speaker_task = asyncio.ensure_future(self._mind.identify_speaker(audio))

            def _spk_done(fut) -> None:
                with contextlib.suppress(Exception):
                    prof = fut.result()
                    if prof:
                        # Record how this person is known at the microphone, so
                        # a merge with their Discord profile keeps both names.
                        if int(prof.get("id", -1)) >= 0 and not prof.get("auto_name", True):
                            with contextlib.suppress(Exception):
                                self._mind.note_speaker_alias(
                                    int(prof["id"]), "local", str(prof.get("name") or ""),
                                )
                        self._emit("user_speaker", name=prof["name"],
                                   is_new=bool(prof.get("is_new")))
                        # **実経路A**: 声紋が確認できた人だけを参加者にする。
                        self.note_speaker_observation(prof, event_id=utterance_id)
            speaker_task.add_done_callback(_spk_done)
        await self._start_response(
            text, metrics, speaker_task=speaker_task, user_emotion_task=emotion_task,
            replace_active=not started_while_busy, conversation_decision=conversation_decision,
            response_id=response_id, utterance_id=utterance_id, transcript=transcript,
        )


    async def _device_watch_loop(self) -> None:
        """Pick up a microphone or speaker that appears after start-up.

        PortAudio caches its device list, so a headset switched on later is
        invisible until that list is rebuilt.  Polling costs almost nothing and
        removes the only previous remedy: restarting the application.
        """
        from neuro_voice.audio.devices import AudioDeviceMonitor

        configured = float(self._cfg.get("audio.device_watch_interval_s", 3.0))
        if configured <= 0:
            logger.info("オーディオデバイスの監視は無効です")
            return
        interval = max(1.0, configured)
        monitor = AudioDeviceMonitor()
        monitor.poll(refresh=False)          # baseline, without a rebuild
        while True:
            await asyncio.sleep(interval)
            mic = self._mic
            mic_down = mic is not None and not mic.active and not self._mic_paused
            try:
                # PortAudio's _terminate/_initialize pair stops every active
                # stream in the process.  A harmless-looking refresh here was
                # therefore killing both microphone capture and local TTS
                # every three seconds.  Ordinary monitoring is read-only.
                reason, snapshot = await asyncio.to_thread(
                    monitor.poll, refresh=False,
                )
            except Exception:
                logger.debug("オーディオデバイスの監視でエラー", exc_info=True)
                continue
            if not reason and not mic_down:
                continue
            if reason in {"output_changed", "initial"} or mic_down:
                self._playback.request_reopen()
            if mic is None:
                continue
            if not (mic_down or reason in {"input_changed", "input_appeared"}):
                continue
            if (
                self._user_speaking
                or self._is_responding()
                or self._playback.is_active
            ):
                continue                      # never cut a turn in half
            if mic_down:
                # A stopped stream may mean PortAudio's cached endpoint list
                # is stale.  Refresh only in this recovery path, after the
                # input stream has stopped; playback has already been marked
                # for reopen before its next chunk.
                try:
                    reason, snapshot = await asyncio.to_thread(
                        monitor.poll, refresh=True,
                    )
                except Exception:
                    logger.debug(
                        "停止したオーディオデバイスの再取得に失敗", exc_info=True,
                    )
            opened = await asyncio.to_thread(mic.restart)
            if opened:
                logger.info("マイクを開き直しました (%s / %s)", reason or "recovered",
                            snapshot.input_name or "既定デバイス")
                self._emit("status", message=(
                    f"🎤 マイクを認識しました ({snapshot.input_name or '既定デバイス'})"
                ))
            elif reason:
                logger.info("マイク再接続を試行しましたが開けませんでした (%s)", reason)

    async def redetect_audio_devices(self) -> dict[str, Any]:
        """Rebuild the device list and reopen microphone and speaker.

        The watcher only recovers a stream that was open and then stopped.  A
        microphone that was never present at start-up, or one Windows handed
        over under a new endpoint while the old handle still reports itself as
        alive, needs someone to say "look again".  This is that button.

        Unlike the watcher this is allowed to rebuild PortAudio's cached list
        outright: the person asked, so a momentary gap in playback is expected
        rather than a surprise (第17条 — recover once, explicitly, and report).
        """
        from neuro_voice.audio.devices import device_label

        result: dict[str, Any] = {
            "ok": False, "microphone": "", "speaker": "",
            "used_fallback": False, "message": "", "error": "",
        }
        self._playback.request_reopen()
        try:
            rebuilt, snapshot = await asyncio.to_thread(self._rebuild_device_list)
        except Exception as exc:
            result["error"] = safe_error_text(exc)
            result["message"] = "オーディオデバイスの再取得に失敗しました"
            logger.warning("マイク再認識: デバイス一覧の再取得に失敗", exc_info=True)
            return result
        speaker = device_label(snapshot.output_name)
        microphone = device_label(snapshot.input_name)
        result["speaker"] = speaker
        result["microphone"] = microphone
        mic = self._mic
        if mic is None:
            result["ok"] = bool(speaker)
            result["message"] = (
                f"スピーカーを再取得しました（{speaker}）" if result["ok"]
                else "音声デバイスが見つかりません"
            )
            return result
        opened = await asyncio.to_thread(lambda: mic.restart(allow_fallback=True))
        result["ok"] = bool(opened)
        result["used_fallback"] = bool(getattr(mic, "used_fallback", False))
        if not opened:
            result["error"] = safe_error_text(mic.last_error)
            result["message"] = "マイクを開けませんでした。接続と、他のアプリが占有していないかを確認してください"
        elif result["used_fallback"]:
            result["message"] = (
                "設定したマイクが見つからないため既定デバイスで開きました"
                f"（{microphone or '既定デバイス'}）"
            )
        else:
            result["message"] = f"マイクを認識しました（{microphone or '既定デバイス'}）"
        logger.info(
            "マイク再認識: rebuilt=%s opened=%s fallback=%s input=%s output=%s",
            rebuilt, opened, result["used_fallback"], microphone or "-", speaker or "-",
        )
        self._emit("status", message=("🎤 " if opened else "⚠ ") + result["message"])
        return result

    @staticmethod
    def _rebuild_device_list():
        import sounddevice as sd

        from neuro_voice.audio.devices import read_snapshot, refresh_device_list

        rebuilt = refresh_device_list(sd)
        return rebuilt, read_snapshot(sd)

    async def _analyze_user_emotion(self, audio: np.ndarray):
        """Whisperとは独立して、音声感情を確率付きで推定する。"""
        if not self._user_emotion_enabled:
            return None
        try:
            if self._emotion_recognizer is None:
                from neuro_voice.emotion import OpenSmileSpeechBrainRecognizer

                self._emotion_recognizer = OpenSmileSpeechBrainRecognizer(
                    feature_set=str(self._cfg.get("user_emotion.opensmile.feature_set", "eGeMAPSv02")),
                    model_source=str(self._cfg.get("user_emotion.speechbrain.model", "speechbrain/emotion-recognition-wav2vec2-IEMOCAP")),
                    savedir=str(self._cfg.get("user_emotion.speechbrain.savedir", "models/speechbrain_emotion")),
                    device=str(self._cfg.get("user_emotion.speechbrain.device", "cpu")),
                )
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(self._emotion_ex, self._emotion_recognizer.analyze, audio, self._sr),
                timeout=float(self._cfg.get("user_emotion.timeout_s", 4.0)),
            )
            if result is not None:
                self._emit(
                    "user_emotion", label=result.label, confidence=result.confidence,
                    probabilities=result.probabilities,
                )
            return result
        except asyncio.TimeoutError:
            logger.warning("音声感情認識がタイムアウト")
            return None
        except Exception:
            logger.exception("音声感情認識でエラー")
            return None

    async def _respond_from_text(self, text: str) -> None:
        metrics = TurnMetrics()
        metrics.mark("speech_end")
        metrics.mark("stt_done")
        self._begin_turn(metrics)
        self._emit("user_final", text=text)
        decision = self._local_conversation_decision(text)
        utterance_id = uuid4().hex
        response_id = uuid4().hex
        if not decision.response_plan.should_respond:
            if self._mind is not None:
                with contextlib.suppress(Exception):
                    self._mind.begin_conversation_turn(
                        text,
                        source="local",
                        response_id=response_id,
                        utterance_id=utterance_id,
                        conversation_topic=self._conversation.state.active_topic,
                    )
            # Same as the spoken path: choosing not to speak is still a turn,
            # and a running directive has to hear about it.
            with contextlib.suppress(Exception):
                runtime = self._directive_runtime
                if runtime is not None:
                    runtime.note_suppressed_user_turn(text)
            return
        await self._start_response(
            text, metrics, conversation_decision=decision,
            response_id=response_id, utterance_id=utterance_id,
            # Typed input has no recognition hypothesis: there is nothing to be
            # uncertain about.  This used to pass `transcript`, a name that does
            # not exist here, and every typed message raised NameError.
            transcript=None,
        )

    async def _start_response(
        self,
        text: str,
        metrics: TurnMetrics,
        *,
        user_emotion=None,
        user_emotion_task: asyncio.Task | None = None,
        speaker_task: asyncio.Task | None = None,
        replace_active: bool = False,
        conversation_decision=None,
        response_id: str | None = None,
        utterance_id: str | None = None,
        transcript=None,
    ) -> None:
        """新しい応答を一つだけ起動する。通常ターンは古い応答を置き換える。"""
        if replace_active:
            await self._cancel_active_response()
        response_id = response_id or uuid4().hex
        self._active_response_id = response_id
        self._cancelled_response_ids.discard(response_id)
        self._active_response_user_text = text
        self._turn_manager.assistant_thinking()
        if conversation_decision is not None:
            self._conversation.response_started(
                source="local", expected_response_from="local:mic",
            )
        self._respond_task = asyncio.create_task(
            self.respond_text(
                text, metrics, user_emotion=user_emotion,
                user_emotion_task=user_emotion_task, speaker_task=speaker_task,
                conversation_decision=conversation_decision, response_id=response_id,
                utterance_id=utterance_id, transcript=transcript,
            ),
        )
        await self._respond_task

    async def _cancel_active_response(self, interruption_text: str | None = None) -> None:
        task = self._respond_task
        response_id = self._active_response_id
        self._turn_manager.barge_in_confirmed()
        if response_id:
            self._cancelled_response_ids.add(response_id)
            self._llm.cancel_request(response_id)
        tracker = self._active_playback_tracker
        if tracker is not None:
            tracker.interrupted("substantive_barge_in")
            topic = self._conv.hold_current_topic(InterruptedTurn(
                response_id=response_id or tracker.response_id,
                original_user_text=self._active_response_user_text,
                full_assistant_text=tracker.generated_text,
                played_text=tracker.played_text,
                unplayed_text=tracker.unplayed_text,
                interrupted_at=time.monotonic(),
                interruption_text=interruption_text,
                resume_recommended=bool(tracker.unplayed_text.strip()),
            ))
            if topic is not None:
                self._emit("topic_deferred", topic=topic.summary,
                           played_text=tracker.played_text, unplayed_text=tracker.unplayed_text)
        # This is intentionally immediate.  The playback loop reports the
        # partial frame count through its completion callback.
        self._playback.discard_paused_audio()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---------- 共通応答処理 (音声/テキスト両モード) ----------

    async def respond_text(
        self,
        user_text: str,
        metrics: TurnMetrics | None = None,
        user_emotion=None,
        user_emotion_task: asyncio.Task | None = None,
        speaker_task: asyncio.Task | None = None,
        conversation_decision=None,
        response_id: str | None = None,
        utterance_id: str | None = None,
        internal_event: bool = False,
        internal_event_kind: str = "",
        transcript=None,
        proactive_decision=None,
    ) -> str:
        """ユーザー発話に対して LLM 応答を生成し、文単位でTTS再生する。

        user_emotion はopenSMILE + SpeechBrainによる音声感情推定結果。
        """
        metrics = metrics or TurnMetrics()
        prompt_build_started = time.perf_counter()
        self._last_prompt_profile = {}
        self._last_llm_diagnostics = {}
        response_id = response_id or uuid4().hex
        utterance_id = utterance_id or uuid4().hex
        self._begin_turn(metrics)
        # **1つの発話から出る最終応答は1件**（Phase 7D ③）。
        # 同じ `utterance_id` から2つ目の応答が立ち上がったら、それが
        # 重複の正体。既定では止めず、どの層かだけ記録する。
        self._active_response_id = response_id
        self._cancelled_response_ids.discard(response_id)
        self._active_response_user_text = user_text
        if user_emotion is None and user_emotion_task is not None and user_emotion_task.done():
            with contextlib.suppress(Exception):
                user_emotion = user_emotion_task.result()
        game_correction = None
        direct_reply = None
        activity_context = None
        if not internal_event:
            share_handled, share_reply = await self._handle_screen_share_control(
                user_text, requester="local:mic",
            )
            if share_handled:
                direct_reply = share_reply
            elif (
                self._screen_share_session_obj is not None
                and self._screen_share_session_obj.active
            ):
                if (
                    self._screen_share_session_obj.source_kind == "minecraft_obs"
                    and not self._screen_share_session_obj.needs_visual_reasoning(
                        user_text,
                    )
                ):
                    direct_reply = await self._minecraft_verified_recipe_answer(
                        user_text,
                    )
                if direct_reply is None:
                    direct_reply = await (
                        self._screen_share_session_obj.answer_current_visual_question(
                            user_text, response_id=response_id,
                        )
                    )
            elif self._video_service is not None:
                direct_reply = await self._minecraft_verified_recipe_answer(
                    user_text,
                )

            from neuro_voice.tts.volume import parse_tts_volume_command

            volume_command = parse_tts_volume_command(user_text)
            if (
                direct_reply is None
                and volume_command is not None
                and self._tts_volume_hook is not None
            ):
                direct_reply = self._tts_volume_hook(volume_command)
            elif direct_reply is None:
                # Only a speaker-resolved STT final may enter canonical activity
                # state. ActivityStateManager is the sole source of game truth.
                activity_outcome = None
                if self._mind is not None:
                    game_outcome = self._mind.game_profile_handle_final_input(
                        user_text, actor_id="local:mic", source="local",
                        utterance_id=utterance_id,
                    )
                    activity_outcome = (
                        game_outcome if game_outcome.handled
                        else self._mind.activity_handle_final_input(
                            user_text, actor_id="local:mic", source="local",
                            utterance_id=utterance_id,
                        )
                    )
                    contexts = [
                        (
                            self._mind.game_profile_grounded_context()
                            if self._mind.game_profile_active() else None
                        ),
                        self._mind.activity_grounded_context(),
                    ]
                    activity_context = "\n\n".join(x for x in contexts if x) or None
                if activity_outcome is not None and activity_outcome.handled:
                    direct_reply = activity_outcome.reply
                    # 会話でプロファイルが変わったら、映像取得もその場で
                    # 付け替える。設定値だけ変えても、起動時に始まった
                    # Minecraftのキャプチャは動いたまま残る。
                    if str(activity_outcome.reason or "").startswith("game_profile_switched"):
                        await self._apply_switched_game_profile()
        if (
            direct_reply is None
            and conversation_decision is not None
            and conversation_decision.response_plan.direct_reply
        ):
            direct_reply = conversation_decision.response_plan.direct_reply
        directive_created = None
        if not internal_event and self._mind is not None:
            try:
                frame = self._mind.begin_conversation_turn(
                    user_text,
                    source="local",
                    response_id=response_id,
                    utterance_id=utterance_id,
                    conversation_topic=self._conversation.state.active_topic,
                ) or {}
            except Exception:
                logger.exception("Conversation Kernel turn initialization failed")
                frame = {}
            try:
                directive_created = self._handle_directive_turn(user_text, frame)
            except Exception:
                logger.exception("Behavior directive handling failed")
        # 自発発話は `_proactive_decide` で既に決めてある。**決定を持ち込む**
        # ことで、内部イベントも `_execute_or_stay_silent` と ActionOutcome の
        # 経路に乗る——ここが以前の迂回の実体だった。
        if proactive_decision is not None:
            self._cognitive_decision = proactive_decision
            if self._mind is not None:
                self._mind.apply_cognitive_decision(
                    proactive_decision, source="local", response_id=response_id)
            if not self._execute_or_stay_silent(response_id, metrics):
                self._finish_cognition_turn(metrics)
                return ""
        # 文章を作る前に「何をするか」を決める（既定では無効）。
        if direct_reply is None and not internal_event:
            response_required, requirement_source = self._response_requirement(
                conversation_decision, user_text)
            self._cognitive_decision = self._cognitive_decide(
                user_text, transcript, metrics,
                response_required=response_required,
                response_requirement_source=requirement_source,
                turn_frame=frame,
            )
            if self._cognitive_decision is not None and self._mind is not None:
                self._mind.apply_cognitive_decision(
                    self._cognitive_decision, source="local", response_id=response_id)
                from neuro_voice.cognition.types import ActionType
                if self._cognitive_decision.selected_action is ActionType.EXECUTE_TOOL:
                    direct_reply = await self._execute_production_read_only_tool(
                        user_text, metrics, response_id)
                # **既定は「話さない」。** 発話が許可された決定でなければ、
                # ここで打ち切る。Planner も Realizer も応答LLMもTTSも呼ばない。
                if (direct_reply is None
                        and not self._execute_or_stay_silent(response_id, metrics)):
                    self._finish_cognition_turn(metrics)
                    return
                if self._cognitive_decision.selected_action is ActionType.BRIEF_ACKNOWLEDGE:
                    # This constrained closure route intentionally bypasses the
                    # normal response prompt: one acknowledgement, no question
                    # and no new topic.
                    direct_reply = self._brief_acknowledgement_text()
        if direct_reply is None and not internal_event and self._pronunciation_learning_hook is not None:
            try:
                learned = await self._pronunciation_learning_hook(user_text, self._last_assistant_text)
                if learned:
                    surface, reading = learned
                    self._emit("pronunciation_learned", surface=surface, reading=reading)
            except Exception:
                logger.exception("読み方の口頭訂正処理でエラー")
        if not internal_event:
            game_correction = self._minecraft_spoken_correction(
                user_text, self._last_assistant_text,
            )
        # 音楽コマンド (「〇〇流して」等) はLLMに渡さずフックで処理する
        if direct_reply is None and not internal_event and self._music_hook is not None:
            try:
                if await self._music_hook(user_text):
                    if conversation_decision is not None:
                        self._conversation.response_finished(source="local")
                    return
            except Exception:
                logger.exception("音楽フックでエラー")
        if user_emotion is not None:
            annotated = (
                f"{user_text}\n(音声感情推定: {user_emotion.label} / "
                f"確信度: {user_emotion.confidence:.0%})"
            )
        else:
            annotated = user_text
        if not internal_event:
            self._conv.add_user(annotated)
            # A silent cognition decision must not become a SpeechRequest.
            speech_commit = self._turns.commit(
                metrics, CommitKind.SPEECH_REQUEST, response_id,
                decision_id=utterance_id,
            )
            if self._turns.blocks(speech_commit):
                logger.warning("同じ発話から2件目の応答が立ち上がったため中止: %s", response_id)
                return ""
            fallback_state = getattr(metrics, "_cognitive_fallback_state", None)
            if fallback_state is not None and speech_commit.accepted:
                fallback_state.speech_request_accepted = True
            if direct_reply is not None:
                if self._mind is not None:
                    self._mind.mark_kernel_generation(
                        source="local", response_id=response_id,
                        generation_id=response_id,
                    )
                    self._mind.finalize_kernel_output(
                        direct_reply, source="local", response_id=response_id,
                    )
                await self._respond_direct_text(direct_reply, metrics, response_id)
                if conversation_decision is not None:
                    self._conversation.response_finished(
                        source="local",
                        asked_question=direct_reply.rstrip().endswith(("?", "？")),
                        expected_response_from="local:mic",
                        assistant_text=direct_reply,
                    )
                return
            observation = self._perception.memory.current_scene if self._perception is not None else None
            quick_answer = self._minecraft_quick_answer(user_text, observation)
            if quick_answer and bool(self._cfg.get("game_assistant.minecraft.direct_quick_answer", False)):
                if self._mind is not None:
                    self._mind.mark_kernel_generation(
                        source="local", response_id=response_id,
                        generation_id=response_id,
                    )
                    self._mind.finalize_kernel_output(
                        quick_answer, source="local", response_id=response_id,
                    )
                await self._respond_fast_game_guidance(quick_answer, metrics, response_id)
                if conversation_decision is not None:
                    self._conversation.response_finished(
                        source="local", asked_question=False, expected_response_from="local:mic",
                        assistant_text=quick_answer,
                    )
                return
        segmenter = SentenceSegmenter(
            max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
        )
        sentence_q: asyncio.Queue[str | None] = asyncio.Queue()
        def _resolve_voice(emotion_value, delivery_value):
            style = self._style_manager.resolve(
                emotion_value, delivery_value,
                user_emotion=getattr(user_emotion, "label", None),
                user_confidence=float(getattr(user_emotion, "confidence", 0.0)),
            )
            if self._mind is not None:
                style = self._mind.smooth_voice_style(style, source="local", generation_id=response_id)
            return style
        # 感情はTTSにも渡して声の抑揚・大きさ・速さを変える。文合成前に確定させたいので先に用意
        turn_emotion: dict[str, str | None] = {"v": None}
        turn_style: dict[str, object] = {
            "delivery": None,
            "speech": _resolve_voice(None, None),
        }
        playback_tracker = PlayedTextTracker(response_id=response_id)
        self._active_playback_tracker = playback_tracker
        # State-changing activities are buffered until the proposed move has
        # passed validation. Normal conversation keeps its low-latency stream.
        activity_guarded = activity_context is not None
        speak_task = None if activity_guarded else asyncio.create_task(
            self._speak_loop(sentence_q, metrics, turn_emotion, turn_style, playback_tracker, response_id),
        )
        reply = ""
        reply_suppressed_reason = ""
        rejected_reply = ""
        recall_contract: dict[str, str] = {}
        messages, resumed_deferred_topic, media = await self._build_messages(
            user_text, response_id=response_id, allow_live_image=not internal_event,
        )
        if not internal_event:
            from neuro_voice.dialogue.repair import analyze_conversation_repair

            conversation_repair = analyze_conversation_repair(user_text)
            if conversation_repair.is_repair:
                messages = insert_before_user_turn(
                    messages, conversation_repair.system_context(),
                )
        if game_correction is not None:
            messages = insert_before_user_turn(
                messages, game_correction.system_context(),
            )
        if activity_context is not None:
            # This is a separate, high-priority state channel. It is not a
            # conversation-history message and always wins on contradiction.
            position = next(
                (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
                len(messages),
            )
            messages = [
                *messages[:position],
                {"role": "system", "content": activity_context},
                *messages[position:],
            ]
        if internal_event:
            if internal_event_kind == "autonomy":
                # An autonomous prompt is not a human utterance, but the
                # resulting speech is part of the same conversation. Preserve
                # recent dialogue so the move can refer to the actual present
                # exchange. The internal prompt itself is never persisted.
                messages = self._conv.messages_for_autonomous_turn(user_text)
            else:
                # A visual/game event is not a user utterance. Supply it for
                # this response only and keep it out of durable conversation
                # history. Omit long chat history for low-latency commentary.
                systems = [item for item in messages if item.get("role") == "system"]
                messages = [*systems[:4], {"role": "user", "content": user_text}]
        if conversation_decision is not None:
            plan = conversation_decision.response_plan
            policy = (
                "【会話方針】"
                f"役割={plan.response_role.value}; 長さ={plan.target_length}; 深掘り={plan.depth}; "
                f"話題={conversation_decision.state.get('active_topic') or 'なし'}。"
                "相槌だけなら説明を始めず、無意味な締めや質問を足さない。"
            )
            messages = insert_before_user_turn(messages, policy)
        music_context = None if internal_event else self._music_context_message()
        if music_context is not None:
            messages = insert_before_user_turn(
                messages, str(music_context.get("content") or ""),
            )
        from neuro_voice.discord_bridge.music import is_current_track_discussion
        if not internal_event and is_current_track_discussion(user_text):
            # 「今の曲どう思う？」は再生コンテキストへの質問。音楽業界全般の
            # Web検索にすり替えず、再生情報（なければ不明）に基づき返答させる。
            logger.info("Tool Scheduler: 再生中の曲についての会話のためWeb検索を見送る")
            if music_context is None:
                messages = insert_before_user_turn(messages, (
                    "ユーザーは現在または直前の曲について尋ねている。再生中の曲情報が"
                    "ないため、知っているふりをせず、曲名や再生状態を確認してから答えること。"
                ))
        elif not internal_event and activity_context is None:
            messages = await self._maybe_search(
                user_text, messages, response_id=response_id,
            )
        if not internal_event:
            messages = await self._inject_mind(
                user_text, messages, speaker_task,
                user_emotion=user_emotion, metrics=metrics,
                response_id=response_id, utterance_id=utterance_id,
            )
            if self._mind is not None:
                recall_contract = self._mind.recall_response_contract(user_text)
                if recall_contract:
                    self._emit(
                        "memory_recall_contract",
                        status=recall_contract.get("status", ""),
                        forced_reply=bool(recall_contract.get("reply")),
                    )
            if self._cognitive_trace is None:
                self._prepare_legacy_turn_trace(metrics)
            else:
                self._bind_trace_turn(self._cognitive_trace, metrics)
            if self._mind is not None:
                planned_voice = self._mind.conversation_voice_direction("local")
                turn_emotion["v"] = planned_voice.get("emotion")
                turn_style["delivery"] = planned_voice.get("delivery")
                turn_style["speech"] = _resolve_voice(turn_emotion["v"], turn_style["delivery"])
                self._emit(
                    "conversation_plan",
                    plan=self._mind.conversation_plan_snapshot("local"),
                    emotion=planned_voice.get("ai_emotion"),
                    delivery=planned_voice.get("delivery"),
                )
                expression_plan = planned_voice.get("expression_plan")
                if isinstance(expression_plan, dict):
                    self._emit(
                        "expression_plan",
                        source="local",
                        plan=expression_plan,
                        avatar_connected=False,
                    )
        if media and bool(media[0].metadata.get("reactive_visual", False)):
            from neuro_voice.vision.service import compact_reactive_visual_messages

            messages = compact_reactive_visual_messages(messages)
        if not internal_event:
            # All three change every turn, so they go next to the user turn:
            # anything in front of them stays byte-identical and the server
            # can reuse it.  This is what the comments already said; the code
            # used to insert at index 1 and invalidate the whole prompt.
            messages = insert_before_user_turn(
                messages, self._directive_prompt_block(directive_created),
            )
            # Closest to the user turn: the model must read the words as a
            # hypothesis, not as authoritative kanji.
            messages = insert_before_user_turn(
                messages, self._asr_uncertainty_block(transcript),
            )
            # Whether there is anything to hesitate about is the code's call;
            # where in the sentence the difficulty sits is the model's.
            messages = insert_before_user_turn(
                messages, self._hesitation_block(transcript),
            )

        def _emo_cb(emo: str) -> None:
            turn_emotion["v"] = emo
            if self._mind is not None:
                self._mind.observe_affect(emo, reason="llm_expression")
            turn_style["speech"] = _resolve_voice(emo, turn_style.get("delivery"))
            if self._emotion_enabled:
                self._emit("emotion", emotion=emo)

        def _style_cb(delivery: str) -> None:
            turn_style["delivery"] = delivery
            turn_style["speech"] = _resolve_voice(turn_emotion["v"], delivery)
            self._emit("speech_style", delivery=delivery)

        emotion = EmotionTagParser(_emo_cb, _style_cb)
        if self._mind is not None and not internal_event:
            self._mind.mark_kernel_generation(
                source="local", response_id=response_id,
                generation_id=response_id,
            )
        self._emit("assistant_start")
        print("🤖 ", end="", flush=True)
        try:
            messages = self._fit_prompt(messages)
            self._profile_prompt(messages)
            metrics.mark("prompt_ready")
            self._last_llm_diagnostics = {
                "prompt_build_ms": round((time.perf_counter() - prompt_build_started) * 1000, 1),
                "prompt_token_count": self._last_prompt_profile.get("total_tokens"),
                "prompt_reusable_prefix_tokens": self._last_prompt_profile.get("reusable_prefix_tokens"),
                "prompt_reuse_ratio": self._last_prompt_profile.get("reuse_ratio"),
                "retrieved_memory_count": len(getattr(self._mind, "last_recall", ()) if self._mind is not None else ()),
                "llm_queue_wait_ms": None,
                "model_load_ms": None,
                "prompt_eval_ms": None,
                "provider_first_token_ms": None,
                "llm_invoked": False,
            }
            if recall_contract.get("reply"):
                async def forced_recall_reply():
                    yield recall_contract["reply"]
                stream = forced_recall_reply()
            else:
                self._last_llm_diagnostics["llm_invoked"] = True
                stream = (
                    self._llm.generate_with_media(messages, media, request_id=response_id)
                    if media else self._llm.generate(messages)
                )
            # A turn that adds nothing (「マジでそう。」) leaves the model with its
            # own previous answer as the most salient text in front of it, and
            # it says it again word for word.  The opening is held back until
            # the first sentence is complete so a repeat can be stopped before
            # it reaches the screen or the speaker; after that, streaming is
            # unchanged.  Holding costs one sentence of display latency and
            # nothing at all in audio, since TTS waits for a sentence anyway.
            echo_guard = self._reply_echo_guard(segmenter)
            if echo_guard is not None:
                self._record_response_delivery_trace(echo_guard_checked=True)
            async for token in strip_think(stream):
                token = emotion.feed(token)
                if not token:
                    continue
                if not reply:
                    metrics.mark("llm_first")
                    self._last_llm_diagnostics.update(
                        getattr(self._llm, "last_generation_metrics", {}) or {})
                reply += token
                if echo_guard is not None:
                    verdict, ready, shown = echo_guard.feed(token)
                    if verdict == "holding":
                        continue
                    if verdict == "repeat":
                        match = echo_guard.match
                        reason = f"repeat_{match.kind}" if match is not None else "repeat_assistant"
                        logger.info(
                            "重複候補を発話前に抑止: kind=%s score=%.2f candidate=%s",
                            match.kind if match is not None else "unknown",
                            match.score if match is not None else 0.0,
                            reply.strip()[:40],
                        )
                        self._emit("reply_suppressed", reason=reason)
                        rejected_reply = reply.strip()
                        self._record_response_delivery_trace(
                            echo_match_scope="HISTORICAL_RESPONSE",
                            primary_similarity_score=(
                                match.score if match is not None else 0.0),
                        )
                        reply = ""
                        reply_suppressed_reason = reason
                        # The verdict is final for this draft.  Leaving the
                        # guard alive makes the post-stream flush classify and
                        # log the same draft a second time.
                        echo_guard = None
                        break
                    echo_guard = None
                    playback_tracker.generated(shown)
                    print(shown, end="", flush=True)
                    if not activity_guarded:
                        self._emit("assistant_token", token=shown)
                        for sentence in ready:
                            sentence_q.put_nowait(sentence)
                    continue
                playback_tracker.generated(token)
                print(token, end="", flush=True)
                if not activity_guarded:
                    self._emit("assistant_token", token=token)
                    for sentence in segmenter.feed(token):
                        sentence_q.put_nowait(sentence)
            leftover = emotion.flush()
            if leftover and echo_guard is None:
                reply += leftover
                playback_tracker.generated(leftover)
                if not activity_guarded:
                    self._emit("assistant_token", token=leftover)
                    for sentence in segmenter.feed(leftover):
                        sentence_q.put_nowait(sentence)
            elif leftover and echo_guard is not None:
                reply += leftover
                echo_guard.feed(leftover)
            if echo_guard is not None:
                # A short reply can end without a sentence terminator, so the
                # guard never got its verdict from ``feed``.
                verdict, ready, shown = echo_guard.flush()
                if verdict == "repeat":
                    match = echo_guard.match
                    reason = f"repeat_{match.kind}" if match is not None else "repeat_assistant"
                    logger.info(
                        "重複候補を発話前に抑止: kind=%s score=%.2f candidate=%s",
                        match.kind if match is not None else "unknown",
                        match.score if match is not None else 0.0,
                        reply.strip()[:40],
                    )
                    self._emit("reply_suppressed", reason=reason)
                    rejected_reply = reply.strip()
                    self._record_response_delivery_trace(
                        echo_match_scope="HISTORICAL_RESPONSE",
                        primary_similarity_score=(
                            match.score if match is not None else 0.0),
                    )
                    reply = ""
                    reply_suppressed_reason = reason
                else:
                    playback_tracker.generated(shown)
                    print(shown, end="", flush=True)
                    if not activity_guarded:
                        self._emit("assistant_token", token=shown)
                        for sentence in ready:
                            sentence_q.put_nowait(sentence)
                echo_guard = None
            tail = "" if reply_suppressed_reason else segmenter.flush()
            if tail and not activity_guarded and reply:
                sentence_q.put_nowait(tail)
            if activity_guarded:
                validation, reason = self._mind.activity_validate_assistant_text(
                    reply, generation_id=response_id,
                )
                if validation != "VALID":
                    # First invalid proposal is retried in the *same* turn.  Do
                    # not make the human speak again merely because formatting or
                    # an invalid candidate slipped through the model output.
                    logger.warning("Activity output rejected; retrying once: %s (%s)", validation, reason)
                    retry_context = self._mind.activity_retry_context() or ""
                    retry_messages = [*messages, {"role": "system", "content": (
                        f"Your previous game proposal was rejected ({reason}). "
                        "Retry now: output one legal quoted hiragana word only, with no rule explanation.\n"
                        + retry_context
                    )}]
                    retry_reply = ""
                    async for retry_token in strip_think(self._llm.generate(retry_messages)):
                        retry_reply += retry_token
                    if retry_reply.strip():
                        reply = retry_reply.strip()
                        validation, reason = self._mind.activity_validate_assistant_text(
                            reply, generation_id=response_id,
                        )
                if validation != "VALID":
                    logger.warning("Activity output blocked before TTS after retry: %s (%s)", validation, reason)
                    self._emit("activity_output_blocked", validation=validation, reason=reason)
                    reply = (
                        "同じ手番で有効な言葉を確定できなかったから、しりとりはいったん止めるね。"
                        if reason == "max_reproposals_exceeded" else
                        "最初の言葉と手番は確認できたよ。今は私の番だから、もう一度だけ言葉を選び直すね。"
                    )
                # 爆弾解除では、ポッポが規則を読んで自分で出した答えを、
                # 発話の**前に**コード側の判定と突き合わせる。全文を保持して
                # から話す経路なので、ここで差し替えれば間違った指示は
                # 耳に届かない（第2条）。判定できない時は触らない。
                reply = self._verified_defusal_reply(reply)
                self._emit("assistant_token", token=reply)
                guarded_segmenter = SentenceSegmenter(
                    max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
                )
                for sentence in guarded_segmenter.feed(reply):
                    sentence_q.put_nowait(sentence)
                tail = guarded_segmenter.flush()
                if tail:
                    sentence_q.put_nowait(tail)
                speak_task = asyncio.create_task(
                    self._speak_loop(
                        sentence_q, metrics, turn_emotion, turn_style,
                        playback_tracker, response_id,
                        enforce_conversation_contract=False,
                    ),
                )
            if not reply.strip():
                # A local model can spend its entire output budget on an
                # internal-looking response after a large retrieval prompt.
                # Retry once with compact evidence instead of leaving a silent
                # assistant bubble.
                web_blocks = [
                    str(message.get("content", ""))
                    for message in messages
                    if message.get("role") == "system" and "Web検索結果" in str(message.get("content", ""))
                ]
                if reply_suppressed_reason.startswith("repeat_"):
                    from neuro_voice.dialogue.repair import (
                        normalize_repeated_reply_retry,
                        repeated_reply_fallback,
                        repeated_reply_retry_messages,
                    )

                    # Corrections have a deterministic acknowledgement.  For
                    # ordinary conversation, retry the *same turn* once with
                    # a silence option instead of reading a diagnostic apology
                    # to the user.
                    recovered = repeated_reply_fallback(user_text)
                    recovered_from_safe_fallback = bool(recovered)
                    regenerated_match = None
                    if not recovered:
                        self._record_response_delivery_trace(regeneration_attempted=True)
                        retry_parser = EmotionTagParser(_emo_cb, _style_cb)
                        retry_raw = ""
                        retry_messages = repeated_reply_retry_messages(
                            messages, user_text, rejected_reply,
                        )
                        async for retry_token in strip_think(self._llm.generate(retry_messages)):
                            retry_raw += retry_parser.feed(retry_token)
                        retry_raw += retry_parser.flush()
                        recovered = normalize_repeated_reply_retry(retry_raw)
                        retry_guard = self._reply_echo_guard(SentenceSegmenter(
                            max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))),
                            min_chars=8,
                        ))
                        if (
                            recovered and retry_guard is not None
                            and retry_guard.repeats(recovered)
                        ):
                            regenerated_match = retry_guard.match
                            match = regenerated_match
                            logger.warning(
                                "重複応答の再生成も抑止: kind=%s score=%.2f",
                                match.kind if match is not None else "unknown",
                                match.score if match is not None else 0.0,
                            )
                            self._record_response_delivery_trace(
                                echo_match_scope="HISTORICAL_RESPONSE",
                                regenerated_similarity_score=(
                                    match.score if match is not None else 0.0),
                            )
                        self._emit(
                            "reply_regenerated",
                            reason=reply_suppressed_reason,
                            success=bool(recovered),
                        )
                    from neuro_voice.dialogue.echo import resolve_historical_echo
                    echo_choice = resolve_historical_echo(
                        rejected_reply, recovered,
                        regenerated_matches_history=regenerated_match is not None,
                        regenerated_is_safe_fallback=recovered_from_safe_fallback,
                    )
                    reply = echo_choice.text
                    if reply:
                        self._record_response_delivery_trace(
                            echo_resolution=echo_choice.resolution,
                        )
                        playback_tracker.generated(reply)
                        self._emit("assistant_token", token=reply)
                        segmenter = SentenceSegmenter(
                            max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))),
                            min_chars=8,
                        )
                        for sentence in segmenter.feed(reply):
                            sentence_q.put_nowait(sentence)
                        recovery_tail = segmenter.flush()
                        if recovery_tail:
                            sentence_q.put_nowait(recovery_tail)
                elif web_blocks:
                    compact_evidence = web_blocks[-1][-1600:]
                    retry_messages = [
                        message for message in messages
                        if not (message.get("role") == "system" and "Web検索結果" in str(message.get("content", "")))
                    ]
                    retry_messages.insert(-1, {
                        "role": "system",
                        "content": (
                            "直前の回答生成は本文が空で終了した。内部の考察や英語は出さず、"
                            "以下の根拠だけを使って日本語で直接1〜3文回答すること。"
                            "根拠が不足なら、不明な点を明示すること。\n\n" + compact_evidence
                        ),
                    })
                    self._emit("search_response_retry", reason="empty_primary_response")
                    logger.warning("検索後の本文が空だったため、コンパクトな根拠で再生成します")
                    async for retry_token in strip_think(self._llm.generate(retry_messages)):
                        retry_token = emotion.feed(retry_token)
                        if not retry_token:
                            continue
                        if not reply:
                            metrics.mark("llm_first_retry")
                        reply += retry_token
                        playback_tracker.generated(retry_token)
                        self._emit("assistant_token", token=retry_token)
                        for sentence in segmenter.feed(retry_token):
                            sentence_q.put_nowait(sentence)
                if not reply.strip():
                    fallback = (
                        "検索結果は取れたけど、回答の生成が途中で切れちゃった。"
                        "もう一度短く聞いてくれたら、検索なしで答えるね。"
                        if web_blocks else
                        "回答の生成が途中で切れちゃった。もう一度短く言ってくれたら、すぐ答えるね。"
                    )
                    reply = fallback
                    self._record_response_delivery_trace(echo_resolution="SAFE_FALLBACK")
                    playback_tracker.generated(fallback)
                    self._emit("assistant_token", token=fallback)
                    for sentence in segmenter.feed(fallback):
                        sentence_q.put_nowait(sentence)
            # **生成された文章そのもの**。ここが繰り返していたら、
            # 下流をいくら見ても直す場所は見つからない（`duplicate_site()`
            # が最初に見るのもここ）。本文は残さず指紋だけ。
            self._turns.note_text(metrics, TextStage.LLM_OUTPUT, reply)
            sentence_q.put_nowait(None)
            if speak_task is not None:
                await speak_task
            self._record_response_delivery_trace(final_response_empty=not bool(reply.strip()))
            print()
            if (
                reply.strip()
                and self._mind is not None
                and not internal_event
                and not activity_guarded
            ):
                reply = self._mind.enforce_conversation_reply(
                    reply, source="local", response_id=response_id,
                )
            self._turns.note_text(metrics, TextStage.SURFACE_REALIZED, reply)
            if not reply.strip() and not reply_suppressed_reason:
                # 無言の吹き出しを避け、原因の当たりを付けられるよう通知する
                msg = "モデルが空の応答を返しました (VRAM不足でロード不完全、または思考のみで本文なしの可能性)"
                logger.warning(msg)
                self._emit("status", message=f"⚠ {msg}")
            self._emit("assistant_done", text=reply)
            if self._mind is not None and not internal_event:
                self._mind.finalize_kernel_output(
                    reply, source="local", response_id=response_id,
                )
            self._emit("assistant_playback", **playback_tracker.snapshot())
            if reply.strip() and resumed_deferred_topic:
                self._conv.complete_deferred_topic()
                self._emit("topic_resumed")
            self._report_latency(metrics)
        except asyncio.CancelledError:
            playback_tracker.interrupted("response_cancelled")
            if speak_task is not None:
                speak_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await speak_task
            self._playback.discard_paused_audio()
            print(" …(中断)")
            self._emit("assistant_cancelled", text=reply)
            self._emit("assistant_playback", **playback_tracker.snapshot())
            raise
        except Exception as e:
            if speak_task is not None:
                speak_task.cancel()
            logger.exception("応答生成でエラー")
            error_text = str(e).lower()
            if (
                "multimodal" in error_text
                or "does not support image input" in error_text
                or "does not advertise vision" in error_text
            ):
                # ローカルLLMが画像非対応 (例: Ollamaの400)。次ターンから画像を付けない
                self._local_vision_unsupported = True
                self._emit("error", message=(
                    "このローカルLLMは画像に対応していません。"
                    "設定 ⚙ →「機能」タブで画像認識のAIを Claude Vision か "
                    "Google Cloud Vision に切り替えてください。"
                    "(次の発話からは画像なしで応答します)"
                ))
            else:
                self._emit("error", message=f"応答生成エラー: {e}")
        finally:
            current = asyncio.current_task()
            if self._respond_task is None or self._respond_task is current:
                self._turn_manager.assistant_done()
            interrupted_response = response_id in self._cancelled_response_ids
            # 閉ループの最後。やってみてどうなったかを状態へ戻す。
            # 中断なら、言い終えていない発話と義務が次のターンへ残る。
            # ActionOutcome / Speech Gate の実エラーは隠さない。ただしその場合も
            # セッション境界の後始末は必ず行い、次のターンへ古い epoch を残さない。
            try:
                outcome_status = "interrupted" if interrupted_response else "completed"
                delivery_failure_stage = ""
                delivery_completed = False
                if not interrupted_response:
                    await self._await_delivery_finalization(metrics)
                    outcome_status, delivery_failure_stage, delivery_completed = (
                        self._cognitive_answer_delivery_result(reply, metrics)
                    )
                self._cognitive_outcome(
                    reply, status=outcome_status, error=delivery_failure_stage,
                    speech_generated=delivery_completed,
                    tts_called=delivery_completed,
                    turn_closed=delivery_completed,
                    metrics=metrics,
                )
            finally:
                if self._active_response_id == response_id:
                    self._active_response_id = None
                    self._active_playback_tracker = None
                self._finish_cognition_turn(metrics)
            self._auto_last = time.monotonic()
            self._last_activity = time.monotonic()
            self._schedule_next_proactive()
            if reply and internal_event and internal_event_kind != "autonomy":
                self._last_game_commentary_text = reply.strip()
            heard_text = ""
            if reply and internal_event and internal_event_kind == "autonomy":
                # Autonomous speech is a real assistant turn. Without this
                # commit, the user's next reply has no preceding utterance to
                # attach to and the assistant appears to forget what it asked.
                heard_text = playback_tracker.played_text if interrupted_response else reply
                if heard_text:
                    self._conv.add_assistant(heard_text)
                    self._last_assistant_text = heard_text
            if reply and not internal_event:
                # Never write words that were cancelled before playback into
                # normal conversation history.  The full text is retained in
                # InterruptedTurn's internal system context instead.
                heard_text = playback_tracker.played_text if interrupted_response else reply
                if heard_text:
                    self._conv.add_assistant(heard_text)
                    self._last_assistant_text = heard_text
                if self._mind is not None:
                    try:
                        # トーン付きで記録 → 内省が「ユーザーの気持ち」も考慮できる
                        topic = ""
                        if conversation_decision is not None:
                            topic = str(conversation_decision.state.get("active_topic") or "")
                        self._mind.record_turn(
                            annotated, heard_text, turn_emotion["v"], topic=topic,
                            source="local", event_id=str(metrics.turn_id),
                        )
                        self._flush_legacy_turn_trace(metrics)
                        # record_turn is what drives the planner's commit, so
                        # the measurement is only available afterwards.
                        self._record_plan_metrics()
                        # Deferred research is scheduled only after this turn;
                        # it cannot delay the user's immediate answer.
                        self._mind.maybe_queue_user_research(user_text)
                    except Exception:
                        logger.exception("Mindのターン記録でエラー")
                runtime = None if self._mind is None else self._directive_runtime
                if runtime is not None and runtime.enabled:
                    # BehaviorDirective replaces the old regex continuation.
                    # Running both would be a second request classifier.
                    settled = await runtime.settle(timeout=float(
                        self._cfg.get("behavior_directive.plan_settle_timeout_s", 8.0)
                    ))
                    directive_created = directive_created or settled
                    if directive_created is not None and not interrupted_response:
                        runtime.note_opening_segment(directive_created, heard_text)
                    if runtime.active() is not None:
                        self._autonomy_heartbeat.request_tick(delay_s=runtime.tick_delay())
                else:
                    continuation_started = self._autonomy.note_conversation_turn(
                        user_text, heard_text,
                    )
                    if continuation_started:
                        self._autonomy_heartbeat.request_tick(delay_s=float(
                            self._cfg.get("autonomy.explicit_continuation_interval_s", 2.5)
                        ))
                        self._emit(
                            "autonomy_continuation_started",
                            remaining=self._autonomy.snapshot()["continuation"]["remaining"],
                        )
            if conversation_decision is not None:
                self._conversation.response_finished(
                    source="local", interrupted=interrupted_response or not bool(reply.strip()),
                    asked_question=reply.rstrip().endswith(("?", "？")),
                    expected_response_from="local:mic",
                    assistant_text=heard_text,
                )
        return reply


    def _fit_prompt(self, messages: list[dict], *, label: str = "会話") -> list[dict]:
        """Leave room in the context window for the reply itself.

        Trimming history by turn count ignores how long each turn was.  A story
        session fills a 4096-token window in a few exchanges and the model then
        stops after a handful of characters (finish_reason=length), which reads
        as the assistant cutting itself off mid-sentence.
        """
        from neuro_voice.llm.context_budget import fit_messages, resolve_reserve_tokens

        context_tokens = int(self._cfg.get("llm.num_ctx", 4096))
        # The reserve follows max_tokens: they are two halves of one decision.
        reserve = resolve_reserve_tokens(
            self._cfg.get("llm.context_reserve_tokens", None),
            max_tokens=int(self._cfg.get("llm.max_tokens", 1024)),
            context_tokens=context_tokens,
        )
        result = fit_messages(
            messages, context_tokens=context_tokens, reserve_tokens=reserve,
            # Cut with room to spare so the boundary can stay put for several
            # turns.  A history whose oldest message changes every turn cannot
            # be reused by the server (measured: reuse stuck at 30-35%).
            slack_ratio=float(self._cfg.get("llm.trim_slack_ratio", 0.15)),
        )
        if result.trimmed:
            self._emit(
                "context_trimmed", label=label, dropped=result.dropped,
                estimated_tokens=result.estimated_tokens, budget=result.budget,
            )
        return result.messages

    async def _inject_mind(
        self,
        user_text: str,
        messages: list[dict],
        speaker_task: asyncio.Task | None = None,
        user_emotion=None,
        metrics: TurnMetrics | None = None,
        response_id: str = "",
        utterance_id: str = "",
    ) -> list[dict]:
        """Mind (話者・人格状態・中期要約・想起した長期記憶) を system として注入する。"""
        assembled = await self._context_assembler.build(
            user_text, messages, speaker_task=speaker_task,
            user_emotion=user_emotion, metrics=metrics,
            conversation_topic=self._conversation.state.active_topic,
            response_id=response_id,
            utterance_id=utterance_id,
        )
        if self._mind is not None:
            # ContextAssembler has just updated Working Memory.  Promote only
            # its grounded unfinished/project follow-ups so the next idle
            # heartbeat can revisit them without inventing a random topic.
            followup_provider = getattr(self._mind, "autonomy_followups", None)
            followups = followup_provider() if callable(followup_provider) else []
            for item in followups:
                self._autonomy.add_grounded_followup(
                    topic=item["topic"], summary=item["question"],
                    owner_user_id="local:mic",
                    callback_after_s=float(
                        self._cfg.get("autonomy.followup_callback_after_s", 600)
                    ),
                    source_excerpt=str(item.get("source_excerpt") or ""),
                )
        if self._mind is not None and self._mind.last_recall:
            recalled = self._mind.last_recall
            self._conversation.events.publish(ConversationEvent(
                ConversationEventType.MEMORY_RECALLED, "local", metadata={"memories": recalled},
            ))
            self._emit("memory_recalled", memories=recalled)
        return assembled

        if self._mind is None:
            return messages
        started = time.perf_counter()
        deadline_s = max(0.01, float(self._cfg.get("realtime.context_deadline_ms", 150)) / 1000)
        speaker_wait_s = min(
            deadline_s,
            max(0.0, float(self._cfg.get("realtime.speaker_context_wait_ms", 120)) / 1000),
        )
        if speaker_task is not None:
            # 話者識別の完了を待ってからコンテキストを作る (上限あり)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(speaker_task), timeout=speaker_wait_s)
            if speaker_task.done():
                if metrics is not None:
                    metrics.mark("speaker_ready")
            else:
                if metrics is not None:
                    metrics.mark("speaker_deferred")
                self._emit("context_deferred", source="speaker", deadline_ms=round(speaker_wait_s * 1000))
        try:
            self._mind.observe_dialogue_turn(
                user_text,
                getattr(user_emotion, "label", None),
                float(getattr(user_emotion, "confidence", 0.0)),
            )
            # Retrieval is deliberately a lexical/state-only gate.  Do not
            # use an LLM here: most ordinary turns should not touch Memory.
            trigger_reason = retrieval_trigger(user_text)
            include_recall = bool(trigger_reason)
            remaining = max(0.01, deadline_s - (time.perf_counter() - started))
            ctx = await asyncio.wait_for(
                self._mind.build_context(
                    user_text,
                    include_recall=include_recall,
                    retrieval_trigger_reason=(trigger_reason if include_recall else "not_applicable"),
                ), timeout=remaining
            )
        except asyncio.TimeoutError:
            self._emit("context_deferred", source="mind", deadline_ms=round(deadline_s * 1000))
            if metrics is not None:
                metrics.mark("context_deferred")
                metrics.mark("context_ready")
            return messages
        except Exception:
            logger.exception("Mindコンテキスト生成でエラー")
            return messages
        if metrics is not None:
            metrics.mark("context_ready")
        if not ctx:
            return messages
        messages = list(messages)
        # ベースの system prompt の直後に差し込む
        pos = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(pos, {"role": "system", "content": ctx})
        return messages

    # ---------- DeepSearch (Web検索) ----------

    async def _maybe_search(
        self, user_text: str, messages: list[dict], *,
        response_id: str = "",
    ) -> list[dict]:
        """LLMで検索設計→Web取得→根拠抽出を行い、回答用コンテキストへ注入する。"""
        from neuro_voice.search.deepsearch import (
            conversation_repair_context, is_contextual_clarification,
        )

        if re.search(r"(?:あとで|後で|ついでに|自動で)?(?:調べて|検索して|学習して)(?:おいて|おいてね|ほしい|欲しい)", user_text):
            logger.info("Tool Scheduler: deferred research request; immediate Web search skipped")
            return messages
        if is_contextual_clarification(user_text):
            logger.info("Tool Scheduler: dialogue clarification; Web search skipped")
            repaired = list(messages)
            position = 1 if repaired and repaired[0].get("role") == "system" else 0
            repaired.insert(position, {
                "role": "system",
                "content": conversation_repair_context(user_text, messages),
            })
            return repaired
        if not self._search_enabled:
            return messages
        # 「今の画面」はローカル映像ソースでしか答えられない。OBSが未接続
        # でもWeb検索へすり替えず、映像取得エラーをそのまま会話へ渡す。
        from neuro_voice.vision.service import is_explicit_vision_query

        if is_explicit_vision_query(user_text):
            logger.info("Tool Scheduler: current-screen question; Web search skipped")
            return messages
        from neuro_voice.search.deepsearch import (
            DeepSearch, is_search_preface, parse_search_plan, should_search,
        )

        now = time.monotonic()
        if is_search_preface(user_text):
            self._search_armed_until = now + float(self._cfg.get("search.armed_timeout_s", 30.0))
            self._emit("status", message="🔍 次に話す内容を検索する準備ができたよ")
            return messages
        armed = now < self._search_armed_until
        if not should_search(user_text) and not armed:
            return messages
        if self._mind is not None:
            allowed, reason = self._mind.dialogue_allows_tool(
                "search", user_text, source="local",
            )
            if not allowed:
                logger.info("Tool Scheduler: search skipped (%s)", reason)
                return messages
        self._search_armed_until = 0.0
        started_at = time.perf_counter()
        # The acknowledgement must not hold Web retrieval behind TTS synthesis.
        asyncio.create_task(self._speak_search_notice(), name="search-notice")
        max_queries = int(self._cfg.get("search.max_queries", 3))
        fallback_plan = parse_search_plan("", user_text, max_queries=max_queries)
        if self._search is None:
            s = self._cfg.section("search")
            self._search = DeepSearch(
                max_results=int(s.get("max_results", 4)),
                region=str(s.get("region", "jp-jp")),
                timeout=float(s.get("timeout_s", 7.0)),
                max_pages=int(s.get("fetch_pages", 2)),
                page_max_chars=int(s.get("page_max_chars", 2400)),
                parallelism=int(s.get("parallelism", 3)),
            )
        loop = asyncio.get_running_loop()
        query = " / ".join(fallback_plan.queries)
        if self._mind is not None:
            self._mind.mark_kernel_search_execution(
                source="local", response_id=response_id,
                query=query,
            )
        self._emit("search_start", query=query)
        print(f"🔍 Web検索中: {query}")
        search_task = loop.run_in_executor(
            self._search_ex, self._search.search_many, fallback_plan.queries,
        )
        planner_task = asyncio.create_task(self._generate_search_text([
            {
                "role": "system",
                "content": (
                    "あなたは日本語Web検索のクエリ設計者。ユーザーの曖昧な固有名詞を正式名称へ補い、"
                    "質問で知りたい条件・手順・例外を明確化する。検索語は日本語で、攻略・仕様質問では"
                    "作品名、対象名、知りたい要素（例: ギミック、解除条件、対処法）を必ず含める。"
                    "JSONだけを返す: {\"queries\":[\"検索語1\",\"検索語2\"],\"objective\":\"答える内容\","
                    "\"disambiguation\":\"曖昧語をどう解いたか\"}。手順・攻略・ギミック質問では"
                    "異なる観点のqueriesを3件返す（正式名称、対処法、条件・手順）。"
                ),
            },
            {"role": "user", "content": user_text},
        ], limit=int(self._cfg.get("search.planner_max_chars", 480))))
        plan = fallback_plan
        plan_source = "fallback"
        try:
            plan_raw = await asyncio.wait_for(
                asyncio.shield(planner_task),
                timeout=float(self._cfg.get("search.planner_timeout_s", 0.8)),
            )
            candidate = parse_search_plan(plan_raw, user_text, max_queries=max_queries)
            if candidate.queries != fallback_plan.queries:
                # The generic query already has a head start. When planning is
                # unusually quick, issue the refined query concurrently.
                search_task = asyncio.create_task(
                    asyncio.to_thread(self._search.search_many, candidate.queries),
                    name="planned-web-search",
                )
                query = " / ".join(candidate.queries)
                plan_source = "planner"
            else:
                plan_source = "planner_same_query"
            plan = candidate
        except TimeoutError:
            planner_task.cancel()
            logger.info("検索計画は時間予算を超過。即時クエリで継続します")
        except Exception:
            logger.debug("検索計画の高速生成に失敗。即時クエリで継続します", exc_info=True)
        logger.info("検索計画: queries=%s objective=%s disambiguation=%s", plan.queries, plan.objective, plan.disambiguation)
        try:
            results = await search_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Retrieval is optional. A provider/network failure must never
            # turn a normal conversation turn into an empty assistant bubble.
            logger.exception("Web検索の取得に失敗。検索なしで会話を継続します")
            self._emit("search_error", query=query, error=f"{type(exc).__name__}: {exc}"[:240])
            if self._mind is not None:
                self._mind.mark_kernel_search_execution(
                    source="local", response_id=response_id,
                    query=query, found=False,
                    error_code=type(exc).__name__,
                )
            return messages
        web_ms = round((time.perf_counter() - started_at) * 1000)
        if self._mind is not None:
            self._mind.mark_dialogue_tool_use("search", user_text)
        if not results:
            self._emit("search_done", query=query, found=False)
            if self._mind is not None:
                self._mind.mark_kernel_search_execution(
                    source="local", response_id=response_id,
                    query=query, found=False,
                )
            return messages
        # Avoid replacing one LLM round-trip with an oversized final prompt.
        # Page text still has room for concrete procedures, but generation can
        # start promptly on local models as well.
        extracted = results[:int(self._cfg.get("search.final_context_max_chars", 2400))]
        if bool(self._cfg.get("search.preextract_enabled", False)):
            extracted = await self._generate_search_text([
            {
                "role": "system",
                "content": (
                    "あなたは検索結果の根拠抽出担当。ユーザーの質問に直接必要な事実だけを抽出し、"
                    "手順・条件・例外・注意点を優先する。与えられた検索結果にないことを補完・推測しない。"
                    "『難しい』『みんなが苦戦している』等の感想・問題報告だけは回答根拠に使わない。"
                    "各要点の末尾に根拠番号 [1] のように付ける。不足なら『検索結果だけでは確認不能』と明示する。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"質問: {user_text}\n"
                    f"検索の目的: {plan.objective}\n"
                    f"曖昧さの解消: {plan.disambiguation or 'なし'}\n\n"
                    f"検索結果:\n{results}"
                ),
            },
            ], limit=int(self._cfg.get("search.extract_max_chars", 1400)))
        self._emit("search_done", query=query, found=True)
        if self._mind is not None:
            self._mind.mark_kernel_search_execution(
                source="local", response_id=response_id,
                query=query, found=True,
            )
        self._emit("search_timing", query=query, source=plan_source, web_ms=web_ms,
                   total_ms=round((time.perf_counter() - started_at) * 1000))
        logger.info("検索高速化: source=%s web=%dms total=%dms", plan_source, web_ms,
                    round((time.perf_counter() - started_at) * 1000))
        messages = list(messages)
        messages.insert(-1, {
            "role": "system",
            "content": (
                "以下は、LLMが質問を明確化してから検索・根拠抽出したメモ。"
                "このメモを最優先の根拠として、ユーザーの質問に直接答えること。"
                "根拠にない断定はせず、必要なら不足も伝えること。\n"
                "【重要・出所の区別】これは今この瞬間にWebから取得した外部情報であり、"
                "あなたが過去にやったこと・調べたこと・経験したことではない。"
                "『〜を調べてたよ』『〜を勉強してたんだ』のように、この検索結果を"
                "自分の過去の行動や体験として語ってはいけない。"
                "自分の行動について聞かれているなら、検索結果ではなく実際の会話履歴と"
                "記憶だけを根拠にし、思い出せないなら正直にそう言うこと。\n\n"
                f"検索目的: {plan.objective}\n"
                f"根拠抽出:\n{extracted or results}"
            ),
        })
        return messages

    async def _generate_search_text(self, messages: list[dict], limit: int) -> str:
        """検索用の小さなLLMタスクを収集する。失敗時は空文字へフォールバック。"""
        try:
            chunks: list[str] = []
            async for token in strip_think(self._llm.generate(messages)):
                chunks.append(token)
                if sum(map(len, chunks)) >= limit:
                    break
            return "".join(chunks)[:limit]
        except Exception:
            logger.exception("検索用LLM処理でエラー")
            return ""

    async def _speak_search_notice(self) -> None:
        """検索待ちの無音を避ける、会話用の短い通知。履歴には残さない。"""
        if not bool(self._cfg.get("search.speak_notice", True)):
            return
        text = str(self._cfg.get("search.notice_text", "ちょっと調べてみるね。"))
        try:
            loop = asyncio.get_running_loop()
            audio, sr = await loop.run_in_executor(self._tts_ex, self._tts.synthesize, text, None)
            self._playback.play(audio, sr)
            self._emit("search_notice", text=text)
        except Exception:
            logger.exception("検索開始通知のTTSに失敗")

    # ---------- 画面認識 (Vision) ----------

    def _minecraft_knowledge_context(self, user_text: str, observation=None) -> str | None:
        """Retrieve local gameplay guidance only while the Minecraft profile is active."""
        if not bool(self._cfg.get("game_assistant.minecraft.enabled", True)):
            return None
        if str(self._cfg.get("video.game_profile", "")).strip().lower() != "minecraft":
            return None
        try:
            if self._minecraft_knowledge is None:
                from neuro_voice.games import MinecraftKnowledgeBase

                self._minecraft_knowledge = MinecraftKnowledgeBase.from_config(self._cfg)
            return self._minecraft_knowledge.context_for(
                user_text, observation,
                limit=max(1, min(5, int(self._cfg.get("game_assistant.minecraft.max_context_entries", 3)))),
            )
        except Exception:
            logger.exception("Minecraft local knowledge retrieval failed")
            return None

    def _minecraft_quick_answer(self, user_text: str, observation=None) -> str | None:
        if not bool(self._cfg.get("game_assistant.minecraft.enabled", True)):
            return None
        if str(self._cfg.get("video.game_profile", "")).strip().lower() != "minecraft":
            return None
        try:
            if self._minecraft_knowledge is None:
                from neuro_voice.games import MinecraftKnowledgeBase

                self._minecraft_knowledge = MinecraftKnowledgeBase.from_config(self._cfg)
            return self._minecraft_knowledge.quick_answer(user_text, observation)
        except Exception:
            logger.exception("Minecraft local quick answer failed")
            return None

    async def _minecraft_verified_recipe_answer(self, user_text: str) -> str | None:
        """Return official cooking guidance without running vision or the LLM."""
        if not bool(
            self._cfg.get(
                "game_assistant.minecraft.direct_verified_recipe_answer", True,
            )
        ):
            return None
        if not bool(self._cfg.get("game_assistant.minecraft.enabled", True)):
            return None
        if str(self._cfg.get("video.game_profile", "")).strip().lower() != "minecraft":
            return None
        try:
            if self._minecraft_knowledge is None:
                from neuro_voice.games import MinecraftKnowledgeBase

                self._minecraft_knowledge = MinecraftKnowledgeBase.from_config(self._cfg)
            return await asyncio.to_thread(
                self._minecraft_knowledge.verified_recipe_answer,
                user_text,
            )
        except Exception:
            logger.exception("Minecraft verified recipe retrieval failed")
            return None

    def _minecraft_spoken_correction(self, user_text: str, last_assistant_text: str):
        """Validate recipe corrections before allowing them to mutate local knowledge."""
        if not bool(self._cfg.get("game_assistant.minecraft.enabled", True)):
            return None
        if str(self._cfg.get("video.game_profile", "")).strip().lower() != "minecraft":
            return None
        try:
            if self._minecraft_knowledge is None:
                from neuro_voice.games import MinecraftKnowledgeBase

                self._minecraft_knowledge = MinecraftKnowledgeBase.from_config(self._cfg)
            result = self._minecraft_knowledge.maybe_apply_spoken_correction(
                user_text, last_assistant_text,
            )
            if result is None:
                return None
            if result.verified:
                self._emit(
                    "status", message=(
                        f"✅ Minecraft DBを公式データで訂正しました: "
                        f"{result.item_name}（Java {self._minecraft_knowledge.active_version}）"
                    ),
                )
                self._emit(
                    "minecraft_knowledge_corrected", item=result.item_name,
                    fact=result.canonical_fact,
                    version=self._minecraft_knowledge.active_version,
                )
            elif result.status == "rejected":
                self._emit("status", message=f"⚠ 訂正はDBへ保存していません: {result.reason}")
            return result
        except Exception:
            logger.exception("Minecraft spoken correction verification failed")
            return None

    async def _respond_fast_game_guidance(
        self, text: str, metrics: TurnMetrics, response_id: str,
    ) -> None:
        """Speak a verified local gameplay answer without waiting for the 12B model."""
        await self._respond_direct_text(text, metrics, response_id, error_label="Minecraft攻略")

    async def _respond_direct_text(
        self, text: str, metrics: TurnMetrics, response_id: str, *, error_label: str = "応答",
    ) -> None:
        """Speak deterministic text through the normal tracked delivery route."""
        self._emit("assistant_start")
        self._emit("assistant_token", token=text)
        self._turns.note_text(metrics, TextStage.LLM_OUTPUT, text)
        delivered = False
        delivery_error = ""
        playback_tracker = PlayedTextTracker(response_id=response_id)
        self._active_playback_tracker = playback_tracker
        try:
            sentences: asyncio.Queue[str | None] = asyncio.Queue()
            sentences.put_nowait(text)
            sentences.put_nowait(None)
            # Tool presenters, control replies, and verified game answers use
            # exactly the streaming coordinator: one SpeechRequest, one
            # logical playback session, and callback-owned physical segments.
            await self._speak_loop(
                sentences, metrics, playback_tracker=playback_tracker,
                response_id=response_id, enforce_conversation_contract=False,
            )
            delivery_state, delivery_error = await self._await_delivery_finalization(metrics)
            delivered = delivery_state == "completed"
            if delivered:
                self._conv.add_assistant(text)
                self._last_assistant_text = text
                self._emit("assistant_done", text=text)
            self._report_latency(metrics)
        except asyncio.CancelledError:
            self._emit("assistant_cancelled", text=text)
            raise
        except Exception as exc:
            delivery_error = "direct_tts_or_playback_failed"
            logger.exception("Direct response TTS failed")
            self._emit("error", message=(
                f"{error_label}の読み上げに失敗: {safe_error_text(exc)}"
            ))
        finally:
            if self._cognitive_decision is not None:
                delivered_snapshot = self._turns.delivery_snapshot(metrics)
                tts_called = bool(
                    delivered_snapshot.get("tts_chunk_count", 0)
                    or delivered_snapshot.get("tts_job", {}).get("accepted", 0))
                self._cognitive_outcome(
                    text,
                    status=("completed" if delivered else
                            ("interrupted" if delivery_error == "playback_interrupted"
                             else "failed")),
                    error=delivery_error,
                    speech_generated=delivered,
                    tts_called=tts_called,
                    turn_closed=delivered,
                    metrics=metrics,
                )
            if self._active_response_id == response_id:
                self._active_response_id = None
                self._active_playback_tracker = None
            self._turn_manager.assistant_done()
            self._finish_cognition_turn(metrics)
            self._last_activity = time.monotonic()

    @staticmethod
    def _brief_acknowledgement_text() -> str:
        """The sole surface form for a constrained terminal acknowledgement."""
        return "わかりました。"

    async def _build_messages(
        self, user_text: str = "", *, response_id: str | None = None,
        allow_live_image: bool = True,
    ) -> tuple[list[dict], bool, list]:
        """Build text messages plus backend-neutral media, never base64 history."""
        messages, resumed_deferred_topic = self._conv.messages_for_turn(user_text)
        from neuro_voice.memory.persona import epistemic_turn_prompt

        truth_guard = epistemic_turn_prompt(user_text)
        if truth_guard:
            messages.insert(1, {"role": "system", "content": truth_guard})

        # Once a user has explicitly selected Discord screen sharing, that OBS
        # source owns visual grounding for the session.  Do this before the
        # ordinary video/monitor branches so a local-mic request cannot
        # silently capture monitor 1 instead.
        share = self._screen_share_session_obj
        if share is not None and share.active:
            visual_context = await share.context_for_turn(
                user_text, response_id=response_id or "",
            )
            if visual_context:
                messages.insert(1, {"role": "system", "content": visual_context})
            if getattr(share, "source_kind", "") == "minecraft_obs":
                game_context = self._minecraft_knowledge_context(user_text)
                if game_context:
                    messages.insert(1, {"role": "system", "content": game_context})
            # The share session publishes its initial preview and exact
            # request-bound frame itself. Re-emitting queued latest_frame here
            # made an old image look like a newly inspected screen.
            return messages, resumed_deferred_topic, []

        video_frame = None
        live_video_frame = False
        live_game_frame = False
        from neuro_voice.vision.service import is_direct_vision_turn

        auto_prompt = str(self._cfg.get("vision.auto_prompt", ""))
        direct_visual_turn = is_direct_vision_turn(
            user_text, mode=self._vision_mode, auto_prompt=auto_prompt,
        )
        if self._video_service is not None:
            from neuro_voice.vision.service import is_explicit_vision_query, is_realtime_game_query

            selected_live_source = str(self._cfg.get("video.input", "")).lower() in {"window", "obs"}
            if selected_live_source:
                visual_context_started = time.perf_counter()
                # Do not attach a 640px image to every conversational turn.
                # On the local 12B model that repeated vision prefill cost was
                # ~28 seconds before the first response token.  OBS is already
                # sampled continuously; use its structured state timeline and
                # let the normal text generation start immediately.
                self._video_service.cancel_background()
                perception = self._get_perception()
                latest = self._video_service.latest_frame
                current = perception.memory.current_scene
                wants_current_state = is_realtime_game_query(user_text)
                if latest is not None and wants_current_state:
                    frame_b64 = base64.b64encode(latest.data or b"").decode("ascii")
                    self._emit("vision_frame", image=frame_b64)
                state_context = self._video_service.state_context(user_text)
                if state_context:
                    messages.insert(1, {"role": "system", "content": state_context})
                elif wants_current_state:
                    detail = self._video_service.last_capture_error
                    messages.insert(1, {"role": "system", "content": (
                        "OBSは1秒ごとにフレームを取得しているが、意味解析済みの状態はまだない。"
                        "画像が見えたふりをせず、ユーザー自身が説明した現在状況を最優先に、"
                        "Minecraftの安全な対処だけを短く答えること。"
                        + (f" 取得エラー: {detail}" if detail else "")
                    )})
                game_context = self._minecraft_knowledge_context(user_text, current)
                if game_context:
                    messages.insert(1, {"role": "system", "content": game_context})
                frame_age = (
                    float("inf") if latest is None
                    else max(0.0, time.time() - float(latest.timestamp or time.time()))
                )
                state_age = (
                    float("inf") if current is None
                    else max(0.0, time.time() - float(current.captured_at or time.time()))
                )
                stale_after = max(.5, float(self._cfg.get("vision.stale_observation_sec", 2.0)))
                if wants_current_state and state_age > stale_after:
                    self._video_service.request_priority_analysis()
                    if current is not None:
                        messages.insert(1, {"role": "system", "content": (
                            f"視覚状態は{state_age:.1f}秒前の解析結果で、最新フレームの優先解析を裏で予約した。"
                            "解析完了を待たず、保存済み状態を暫定情報として短く答える。"
                            "古い情報を現在形で断定せず、ユーザー自身の説明を優先する。"
                        )})
                # Deliberately never attach the raw OBS image to the main
                # conversation turn. A 12B image prefill caused 11-28 second
                # first-token stalls; the background Vision worker owns image
                # inference and dialogue only reads its text state.
                reactive_image = False
                context_ms = round((time.perf_counter() - visual_context_started) * 1000)
                logger.info(
                    "Realtime visual context: frames=%s motion=%.3f state_age=%s "
                    "direct_image=%s frame_age_ms=%s context_build_ms=%s",
                    self._video_service.captured_frame_count,
                    self._video_service.last_motion_score,
                    "none" if current is None else round(max(0.0, time.time() - current.captured_at), 1),
                    reactive_image,
                    "none" if latest is None else round(frame_age * 1000),
                    context_ms,
                )
                return messages, resumed_deferred_topic, []
            if is_explicit_vision_query(user_text):
                video_frame = await self._video_service.capture_current_frame()
            perception = self._get_perception()
            # Past perception may describe a different moment in the game.
            # For a direct "look at the screen" request, the attached capture
            # is authoritative and old scene memory must not override it.
            context = None if live_video_frame else perception.context_for(user_text)
            if context:
                messages.insert(1, {"role": "system", "content": context})
            game_context = self._minecraft_knowledge_context(
                user_text, None if live_video_frame else perception.memory.current_scene,
            )
            if game_context:
                messages.insert(1, {"role": "system", "content": game_context})
        if not self._vision_enabled:
            return messages, resumed_deferred_topic, []
        # Merely enabling image recognition must not attach a desktop/camera
        # image to every ordinary conversational turn. Local 12B image prefill
        # can add 10-20 seconds. Direct images are reserved for an explicit
        # visual request or the dedicated auto-commentary prompt.
        if not allow_live_image or not direct_visual_turn:
            return messages, resumed_deferred_topic, []
        # When a selected game window is active, explicit visual questions use
        # the exact fresh frame from that window.  Do not silently replace it
        # with the desktop/foreground monitor capture.
        frame = video_frame or await self._capture_frame()
        if frame is None:
            return messages, resumed_deferred_topic, []
        messages = list(messages)
        last = messages[-1]
        frame_b64 = base64.b64encode(frame.data or b"").decode("ascii")
        self._emit("vision_frame", image=frame_b64)  # UI preview only; never stored in history

        if live_video_frame and not live_game_frame:
            messages.insert(1, {"role": "system", "content": (
                "Minecraft is not detected yet. The attached image is a fresh desktop frame "
                "captured for this turn. Describe only what is visible in that desktop image."
            )})
        if live_game_frame:
            captured = time.strftime("%H:%M:%S", time.localtime(frame.timestamp or time.time()))
            messages.insert(1, {"role": "system", "content": (
                "このターンでは、選択されたゲームウィンドウから今取得した画像が添付されている。"
                f"取得時刻は{captured}、capture_sequence={frame.metadata.get('capture_sequence', '-') }、"
                f"backend={frame.metadata.get('capture_backend', 'unknown')}。"
                "『画面を直接見られない』とは答えない。画像から確実に分からないことだけを、"
                "見えない・判別できないと具体的に説明すること。"
            )})

        # 外部プロバイダ (claude/google): 画像→テキスト説明に変換して注入する。
        # ローカルLLMが画像非対応でも画面認識が使える。
        if self._vision_provider_name not in ("", "local", "ollama", "none"):
            desc = await self._describe_frame(frame_b64)
            if desc:
                messages[-1] = {
                    "role": "user",
                    "content": f"{last['content']}\n\n(今のPC画面の内容: {desc})",
                }
            return messages, resumed_deferred_topic, []

        # Local Gemma/Ollama receives an explicit frame directly.  This avoids
        # a second 12B inference merely to build a summary and guarantees that
        # the response is grounded in the image just previewed in the UI.
        if self._local_vision_unsupported:
            logger.info("ローカルLLMが画像非対応のため、画像添付をスキップ")
            return messages, resumed_deferred_topic, []
        perception = self._get_perception()
        from neuro_voice.vision.service import is_explicit_vision_query

        explicit = is_explicit_vision_query(user_text)
        if live_video_frame:
            return messages, resumed_deferred_topic, [frame]
        if explicit:
            timeout = max(0.1, float(self._cfg.get("vision.explicit_query_wait_timeout_sec", 3.0)))
            try:
                await asyncio.wait_for(
                    perception.analyze([frame], request_id=response_id or uuid4().hex), timeout=timeout,
                )
            except asyncio.TimeoutError:
                messages.insert(1, {"role": "system", "content": "カメラ映像の解析はまだ終わっていない。古い映像を現在の状況として断定しない。"})
            except Exception as exc:
                logger.warning("ローカル視覚解析をスキップ: %s", exc)
                error_text = str(exc).lower()
                self._local_vision_unsupported = (
                    "unsupported" in error_text or "does not support" in error_text or "rejected image" in error_text
                )
        else:
            task = asyncio.create_task(
                perception.analyze([frame], request_id=uuid4().hex, background=True),
                name="background-screen-observation",
            )
            self._vision_tasks.add(task)
            def _vision_done(done: asyncio.Task) -> None:
                self._vision_tasks.discard(done)
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    done.result()
            task.add_done_callback(_vision_done)
        context = perception.context_for(user_text)
        if context:
            messages.insert(1, {"role": "system", "content": context})
        return messages, resumed_deferred_topic, []

    def _get_perception(self):
        if self._perception is None:
            from neuro_voice.vision.service import PerceptionService, VisualAnalyzer

            vision_backend = self._llm
            if getattr(self._llm, "name", "") == "ollama":
                from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient

                vision_backend = OllamaMultimodalClient(
                    str(self._cfg.get("llm.backends.ollama.base_url", "http://localhost:11434/v1")),
                    str(self._cfg.get("vision.model", "gemma4-12b-qat")),
                    timeout_s=float(self._cfg.get("vision.request_timeout_sec", 45)),
                    keep_alive=self._cfg.get("llm.keep_alive"), retries=1,
                )

            self._perception = PerceptionService(
                VisualAnalyzer(
                    vision_backend,
                    timeout_s=float(self._cfg.get("vision.request_timeout_sec", 45)),
                    max_output_tokens=int(self._cfg.get("vision.realtime.max_output_tokens", 160)),
                    context_size=int(self._cfg.get("vision.realtime.context_size", 4096)),
                ),
                memory_seconds=float(self._cfg.get("vision.visual_memory_seconds", 30.0)),
            )
        return self._perception

    async def _describe_frame(self, jpeg_b64: str) -> str | None:
        """外部 Vision API でスクリーンショットをテキスト説明に変換する。"""
        try:
            if self._vision_provider is None:
                from neuro_voice.vision.providers import build_vision_provider

                self._cfg.set("vision.provider", self._vision_provider_name)
                self._vision_provider = build_vision_provider(self._cfg)
            if self._vision_provider is None:
                return None
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._vision_ex, self._vision_provider.describe, jpeg_b64
            )
        except Exception as e:
            logger.exception("Vision プロバイダでエラー")
            self._emit("error", message=f"画像認識エラー: {e}")
            return None

    async def _capture_frame(self):
        try:
            if self._vision is None:
                from neuro_voice.vision.capture import ScreenCapture

                v = self._cfg.section("vision")
                self._vision = ScreenCapture(
                    monitor=v.get("monitor", "auto"),
                    max_width=int(v.get("max_width", 1024)),
                    jpeg_quality=int(v.get("jpeg_quality", 70)),
                    save_last=bool(v.get("save_last", True)),
                    save_dir=str(self._cfg.get("logging.dir", "logs")),
                )
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._vision_ex, self._vision.capture_media
            )
        except Exception as e:
            logger.exception("画面キャプチャに失敗")
            self._emit("error", message=f"画面キャプチャ失敗: {e}")
            return None

    async def _on_game_observation(self, observation) -> None:
        """Queue meaningful gameplay changes for a short co-player reaction."""
        if not bool(self._cfg.get("video.game_commentary_enabled", False)):
            return
        if not bool(self._cfg.get("vision.proactive_reaction_enabled", True)):
            return
        if str(self._cfg.get("video.game_profile", "")).lower() != "minecraft":
            return
        if self._game_companion_director is None:
            from neuro_voice.games import GameCompanionDirector

            self._game_companion_director = GameCompanionDirector(
                minimum_confidence=float(self._cfg.get("video.game_commentary_confidence", .55)),
                minimum_gap_sec=float(self._cfg.get(
                    "vision.proactive_reaction_cooldown_sec",
                    self._cfg.get("video.game_commentary_cooldown_sec", 8.0),
                )),
                urgent_gap_sec=float(self._cfg.get("video.game_commentary_urgent_gap_sec", 1.25)),
                duplicate_ttl_sec=float(self._cfg.get("video.game_commentary_duplicate_ttl_sec", 30.0)),
                minimum_importance=float(self._cfg.get("vision.important_event_threshold", .55)),
                reaction_probability=float(self._cfg.get("vision.proactive_reaction_probability", .85)),
            )
        # **実経路B**: 映像の解析結果から「いまどの画面か」だけを取り込む。
        # `people` / `objects` は確信が安定しないので、まだ入れない。
        with contextlib.suppress(Exception):
            self.initiative().observe_vision(observation)
        event = self._game_companion_director.observe(observation)
        if event is None:
            logger.debug(
                "Visual reaction suppressed: %s",
                self._game_companion_director.last_suppression_reason,
            )
            return
        # A commentary directive owns the reaction to this event.  Routing it
        # through the directive keeps one voice on the floor instead of the
        # companion loop and the continuation both deciding to speak.
        if self.note_directive_environment_event(
            f"{event.kind}: {event.summary}", importance=float(event.priority),
        ):
            logger.info(
                "Game event routed to active directive: kind=%s priority=%.2f",
                event.kind, event.priority,
            )
            return
        # 注意層へも流す。**ここでは重要かどうかを判断しない**——
        # 間引きと分類は `EventIntake` と `classify_game_event` の仕事で、
        # 呼び出し側ごとに閾値を持つと場所によって基準が変わる。
        self.note_attention_event(
            "game_event", summary=event.summary, topic_ids=(str(event.kind),),
            salience=float(event.priority), novelty=float(event.priority),
            dedup=f"game:{event.kind}", ttl=8.0,
        )
        # **実経路C**: 既存の分類器が付けた `kind` をそのまま状況にする。
        # 架空のイベント型は足さない。
        with contextlib.suppress(Exception):
            self.initiative().observe_game_event(event)
        autonomy = self._autonomy_decide(
            AutonomyEventType.GAME_EVENT,
            payload={"summary": event.summary, "priority": event.priority, "kind": event.kind},
            expires_in_s=float(self._cfg.get("autonomy.speech_event_max_age_ms", 5000)) / 1000,
        )
        if not autonomy.should_speak:
            logger.info("Game companion event kept as state only: action=%s", autonomy.action_type.value)
            return
        pending = self._pending_game_event
        if pending is None or event.priority >= pending.priority or event.created_at > pending.created_at + 2.0:
            self._pending_game_event = event
        logger.info(
            "Game companion event queued: kind=%s priority=%.2f evidence=%s",
            event.kind, event.priority, " / ".join(event.evidence)[:160],
        )
        if self._game_commentary_task is None or self._game_commentary_task.done():
            self._game_commentary_task = asyncio.create_task(
                self._game_companion_loop(), name="minecraft-companion-commentary",
            )

    async def _game_companion_loop(self) -> None:
        """Deliver a recent event when speech has a natural opening."""
        current_task = asyncio.current_task()
        ttl = max(2.0, float(self._cfg.get("video.game_commentary_pending_ttl_sec", 10.0)))
        try:
            while self._pending_game_event is not None:
                event = self._pending_game_event
                self._pending_game_event = None
                deadline = event.created_at + ttl
                while self._is_responding() or self._playback.is_active or self._user_speaking:
                    newer = self._pending_game_event
                    if newer is not None and newer.priority >= event.priority:
                        event = newer
                        self._pending_game_event = None
                        deadline = event.created_at + ttl
                    if time.monotonic() >= deadline:
                        event = None
                        break
                    await asyncio.sleep(.1)
                if event is None or time.monotonic() >= deadline:
                    logger.info("Game companion event expired while conversation was busy")
                    continue

                from neuro_voice.games import companion_prompt

                # The OBS screen-share session owns a separate PerceptionService.
                # Retrieve guidance from the actual event rather than the local
                # monitor pipeline's potentially unrelated current scene.
                knowledge_query = " ".join(
                    item for item in (
                        event.summary, *event.evidence, event.suggested_help,
                    ) if item
                )
                knowledge = self._minecraft_knowledge_context(knowledge_query)
                prompt = companion_prompt(
                    event, previous_comment=self._last_game_commentary_text,
                    knowledge=knowledge or "",
                )
                self._last_game_commentary = time.monotonic()
                # From this point it is a normal cancellable live response.
                # Before this point the scheduler must not make _is_responding
                # true, otherwise it would wait on itself forever.
                self._respond_task = current_task
                self._turn_manager.assistant_thinking()
                # 実況も同じ入口を通す。**逐次反応は実況ではなく読み上げ。**
                proactive, opportunity = self._proactive_decide("game")
                if self.initiative().speech_enabled and proactive is None:
                    logger.info("実況は見送り: 認知カーネルが沈黙を選んだ")
                    continue
                if proactive is not None and self.proactive_speech_allowed(
                        proactive, opportunity):
                    continue
                logger.info("Game companion reaction started: kind=%s", event.kind)
                reply = await self.respond_text(
                    prompt, internal_event=True, proactive_decision=proactive)
                if proactive is not None and reply:
                    self.initiative().record_spoken(
                        opportunity, cost=1.0 + min(1.5, len(reply) / 120.0))
                if self._respond_task is current_task:
                    self._respond_task = None
        except asyncio.CancelledError:
            raise
        finally:
            if self._respond_task is current_task:
                self._respond_task = None
            if self._game_commentary_task is current_task:
                self._game_commentary_task = None

    async def _auto_commentary_loop(self) -> None:
        """自動実況 + 自発モードのループ。

        - vision.mode == auto: 一定間隔で画面を見て実況コメント
        - proactive.enabled : ランダム間隔で自発的に話題振り・ツッコミ
        """
        self._schedule_next_proactive()
        while True:
            await asyncio.sleep(1.0)
            if self._is_responding() or self._playback.is_active:
                continue

            # --- 自動実況 (Vision auto) ---
            if self._vision_enabled and self._vision_mode == "auto":
                interval = max(5.0, float(self._cfg.get("vision.interval_s", 20)))
                if time.monotonic() - self._auto_last >= interval:
                    self._auto_last = time.monotonic()
                    prompt = str(self._cfg.get(
                        "vision.auto_prompt",
                        "(これは今の画面のスクリーンショット。実況者として状況に短く一言コメントして)",
                    ))
                    logger.info("自動実況コメントを生成")
                    self._respond_task = asyncio.create_task(self.respond_text(prompt))
                    continue

            # --- 自発モード ---
            if self._autonomy.active or not self._proactive_enabled or self._proactive_suspended:
                continue
            quiet = float(self._cfg.get("proactive.quiet_s", 10))
            if time.monotonic() - self._last_activity < quiet:
                continue  # 会話直後はしばらく黙る
            if time.monotonic() < self._proactive_next:
                continue
            self._schedule_next_proactive()
            # 黙ると決めた直後に、同じ話題を自分から蒸し返さない。
            # ここを通さないと、沈黙の決定が事実上無かったことになる。
            self.note_attention_event(
                "silence_threshold", salience=.4,
                topic_ids=(self._conversation.state.active_topic,)
                if self._conversation.state.active_topic else (),
                dedup="silence", ttl=30.0)
            suppression = self.spontaneous_speech_suppressed(
                self._conversation.state.active_topic,
            )
            if suppression:
                logger.info("自発発話を抑制: %s", suppression)
                self._emit("spontaneous_suppressed", reason=suppression)
                continue
            prompt = self._pick_proactive_prompt()
            logger.info("自発発話を生成: %s", prompt[:40])
            self._emit("proactive_fire")
            self._respond_task = asyncio.create_task(self.respond_text(prompt))

    async def _on_autonomy_heartbeat(self, heartbeat_id: str) -> None:
        """Evaluate idle state only; expensive generation runs in its own task."""
        if self._mind is not None:
            # Queue maintenance only. It never invents a research topic or
            # spends an LLM call merely because the room is quiet.
            self._mind.research_heartbeat()
        # An explicitly requested continuation is not unsolicited speech, so it
        # is evaluated before the autonomy suppressors and is not blocked by
        # the "no spontaneous talk" switch.  Human floor priority still applies.
        if await self._maybe_run_directive_segment():
            return
        if not self._autonomy.active or self._proactive_suspended:
            self._autonomy_last_status = {
                "evaluated_at": time.time(),
                "final_action": "wait",
                "suppression_reasons": [
                    "自発モードOFF" if not self._autonomy.active else "Discord参加中"
                ],
            }
            return
        if self._mind is not None:
            self._autonomy.update_context(self._mind.autonomy_context("local"))
        now = time.monotonic()
        silence_ms = round((now - self._autonomy_last_human_speech_ended_at) * 1000)
        if self._user_speaking or self._is_responding() or self._playback.is_active:
            self._autonomy_last_status = {
                "evaluated_at": time.time(),
                "final_action": "wait",
                "suppression_reasons": ["発話権が使用中"],
            }
            self._emit("autonomy_heartbeat", heartbeat_id=heartbeat_id, final_action="wait",
                       silence_duration_ms=max(0, silence_ms), suppression_reasons=["floor_busy"])
            if self._autonomy.continuation_active:
                self._autonomy_heartbeat.request_tick(delay_s=1.0)
            return
        decision = self._autonomy.heartbeat(
            source="local", group=False, human_speaking=False, assistant_busy=False, now=now,
        )
        suppression = [] if decision.should_speak else [
            "発話価値のある現在文脈・関心・未完了事項がない"
            if decision.action_type.value == "do_nothing"
            else decision.reason_code
        ]
        self._autonomy_last_status = {
            "evaluated_at": time.time(),
            "final_action": decision.action_type.value,
            "utility": decision.utility,
            "reason_code": decision.reason_code,
            "suppression_reasons": suppression,
        }
        self._emit(
            "autonomy_heartbeat", heartbeat_id=heartbeat_id,
            silence_duration_ms=max(0, silence_ms), final_action=decision.action_type.value,
            candidate_count=1 if decision.action_type.value != "do_nothing" else 0,
            top_candidate_type=decision.action_type.value, top_candidate_utility=decision.utility,
            speech_threshold=float(self._cfg.get("autonomy.min_speech_utility", .68)),
            suppression_reasons=suppression,
        )
        if not decision.should_speak or not decision.should_call_llm:
            return
        task = self._autonomy_task
        if task is not None and not task.done():
            return
        self._autonomy_task = asyncio.create_task(
            self._run_local_autonomous_turn(decision, heartbeat_id),
            name=f"local-autonomy-turn:{heartbeat_id}",
        )

    async def _run_local_autonomous_turn(self, decision, heartbeat_id: str) -> None:
        """Pre-speech gate shared with heartbeat-created autonomous turns."""
        response_started = False
        reply = ""
        interrupted = False
        try:
            if (not self._autonomy.active or self._proactive_suspended or self._user_speaking
                    or self._is_responding() or self._playback.is_active):
                self._emit("autonomy_speech_cancelled", heartbeat_id=heartbeat_id, reason="pre_speech_gate")
                return
            # **認知層を通す。** これまでここは `_cognitive_decide` を丸ごと
            # 飛ばしていて、沈黙の決定も記憶の補正も内面の補正も掛からずに
            # 喋っていた。内部プロンプトを人の発話として渡すわけにはいかないので、
            # 機会として別の入口（`_proactive_decide`）から通す。
            proactive, opportunity = self._proactive_decide("spontaneous")
            if self.initiative().speech_enabled and proactive is None:
                self._emit("autonomy_speech_cancelled", heartbeat_id=heartbeat_id,
                           reason="cognitive_kernel_chose_silence")
                return
            if proactive is not None:
                blocked = self.proactive_speech_allowed(proactive, opportunity)
                if blocked:
                    self._emit("autonomy_speech_cancelled",
                               heartbeat_id=heartbeat_id, reason=blocked)
                    return
            self._autonomy.mark_speech_started(decision)
            self._conversation.response_started(source="local")
            response_started = True
            reply = await self.respond_text(
                self._autonomy.autonomous_prompt(decision),
                internal_event=True,
                internal_event_kind="autonomy",
                proactive_decision=proactive,
            )
            if proactive is not None and reply:
                self.initiative().record_spoken(
                    opportunity, cost=1.0 + min(1.5, len(reply) / 120.0))
            self._autonomy.record_autonomous_response(
                decision, reply,
            )
            self._emit("autonomy_turn_finished", heartbeat_id=heartbeat_id,
                       action=decision.action_type.value)
            if self._autonomy.continuation_active:
                self._autonomy_heartbeat.request_tick(delay_s=float(
                    self._cfg.get("autonomy.explicit_continuation_interval_s", 2.5)
                ))
        except asyncio.CancelledError:
            interrupted = True
            self._emit("autonomy_speech_cancelled", heartbeat_id=heartbeat_id, reason="human_speech")
            raise
        except Exception:
            logger.exception("Local autonomous turn failed: heartbeat=%s", heartbeat_id)
        finally:
            if response_started:
                self._conversation.response_finished(
                    source="local",
                    interrupted=interrupted or not bool(reply.strip()),
                    asked_question=self._autonomy.response_asked_question(reply),
                    expected_response_from=(
                        "local:mic"
                        if self._autonomy.response_asked_question(reply)
                        else None
                    ),
                    assistant_text=reply,
                )
            if self._autonomy_task is asyncio.current_task():
                self._autonomy_task = None

    # ---------- 音声認識の誤変換対策 ----------

    @property
    def _transcript_repairer(self):
        if self._repairer_obj is False:
            from neuro_voice.stt.factory import build_transcript_repairer

            self._repairer_obj = build_transcript_repairer(self._cfg)
        return self._repairer_obj

    def _apply_stt_context_bias(self) -> None:
        """Push the current conversation's vocabulary into the recognizer."""
        repairer = self._transcript_repairer
        if repairer is None or self._mind is None:
            return
        try:
            context = self._mind.speech_recognition_context("local")
            vocabulary = repairer.build_vocabulary(**context)
            self._stt_vocabulary = vocabulary
            self._stt.set_context_bias(
                initial_prompt=vocabulary.initial_prompt(
                    str(self._cfg.get("stt.initial_prompt", "") or ""),
                    include_terms=bool(self._cfg.get(
                        "stt.transcript_repair.bias_via_initial_prompt", False,
                    )),
                ),
                hotwords=vocabulary.hotwords(
                    str(self._cfg.get("stt.hotwords", "") or "")
                ),
            )
        except Exception:
            logger.debug("STT文脈バイアスの更新に失敗 (認識は継続)", exc_info=True)

    def _repair_transcript(self, transcript):
        """Mark unreliable words and repair unambiguous homophone damage."""
        repairer = self._transcript_repairer
        if repairer is None or transcript is None:
            return transcript
        try:
            repaired = repairer.repair(transcript, self._stt_vocabulary)
        except Exception:
            logger.exception("STT文脈補正に失敗 (元の認識結果を使用)")
            return transcript
        if repaired.repairs or repaired.uncertain_words:
            self._emit("stt_repair", **repaired.snapshot())
        return repaired

    def _asr_uncertainty_block(self, transcript) -> str:
        if self._transcript_repairer is None:
            return ""
        from neuro_voice.stt.transcript_repair import asr_uncertainty_prompt

        return asr_uncertainty_prompt(
            transcript,
            ask_when_stuck=bool(
                self._cfg.get("stt.transcript_repair.ask_when_unresolvable", True)
            ),
        ) or ""

    async def _synthesize_with_delivery(self, loop, sentence: str, emotion, style):
        """Synthesize one sentence, honouring the pauses written into it.

        Style-Bert-VITS2 reads 「うーん……そうだなあ」 at an even pace, so a written
        pause is not a heard one.  The sentence is cut at its ellipses, each
        part synthesized on its own, and real silence spliced between them.
        A sentence without hesitation takes the original single-call path.
        """
        from neuro_voice.tts.delivery import plan_delivery

        section = self._cfg.section("conversation") or {}
        settings = (section.get("hesitation") or {}) if isinstance(section, dict) else {}
        if not bool(settings.get("enabled", True)):
            return await loop.run_in_executor(
                self._tts_ex, self._tts.synthesize, sentence, emotion, style,
            )
        pieces = plan_delivery(
            sentence,
            unit_pause_ms=int(settings.get("unit_pause_ms", 320)),
            max_pause_ms=int(settings.get("max_pause_ms", 900)),
            trailing_pause_ms=int(settings.get("trailing_pause_ms", 220)),
            filler_speed=float(settings.get("filler_speed", 0.88)),
        )
        if len(pieces) <= 1 and (not pieces or pieces[0].pause_after_ms <= 0):
            return await loop.run_in_executor(
                self._tts_ex, self._tts.synthesize, sentence, emotion, style,
            )
        chunks: list[np.ndarray] = []
        sample_rate = 0
        for piece in pieces:
            piece_style = style
            if piece.speed_scale != 1.0 and style is not None:
                with contextlib.suppress(Exception):
                    piece_style = dataclasses.replace(
                        style, speed=float(style.speed) * piece.speed_scale,
                    )
            audio, rate = await loop.run_in_executor(
                self._tts_ex, self._tts.synthesize, piece.text, emotion, piece_style,
            )
            sample_rate = rate or sample_rate
            chunks.append(np.asarray(audio, dtype=np.float32))
            if piece.pause_after_ms > 0 and sample_rate:
                frames = int(sample_rate * piece.pause_after_ms / 1000)
                chunks.append(np.zeros(frames, dtype=np.float32))
        if not chunks or not sample_rate:
            return await loop.run_in_executor(
                self._tts_ex, self._tts.synthesize, sentence, emotion, style,
            )
        logger.debug("間を入れて発話: %d分割 / %s", len(pieces), sentence[:40])
        return np.concatenate(chunks), sample_rate

    def _record_plan_metrics(self) -> None:
        """Append this turn's plan-adherence measurement to its own log.

        Separate file because mixing it into the running log makes it
        impossible to aggregate, and because the comparison this exists for —
        the same utterances with the planner on and off — is done by diffing
        two sessions of it.  No conversation text is written (第12条).
        """
        if self._mind is None:
            return
        try:
            metrics = self._mind.last_plan_metrics
        except Exception:
            return
        if not metrics:
            return
        window = metrics.get("window") or {}
        logger.info(
            "計画反映率: %.0f%% (厳格 %.0f%% / 検証可能 %.0f%%) 違反=%s | "
            "直近%d turn: 連続質問=%d 冒頭重複=%.0f%% shape連続=%d",
            100 * float(metrics.get("adherence") or 0),
            100 * float(metrics.get("strict") or 0),
            100 * float(metrics.get("checkable_ratio") or 0),
            metrics.get("violations") or "なし",
            int(window.get("window") or 0),
            int(window.get("consecutive_questions") or 0),
            100 * float(window.get("opening_repeat_ratio") or 0),
            int(window.get("shape_streak") or 0),
        )
        self._emit("plan_metrics", **metrics)
        path = str(self._cfg.get(
            "conversation.metrics.path", "logs/conversation_metrics.jsonl",
        ) or "").strip()
        if not path:
            return

        def _append() -> None:
            import json
            from pathlib import Path

            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(metrics, ensure_ascii=False) + "\n")

        task = asyncio.create_task(asyncio.to_thread(_append), name="plan-metrics")
        self._input_tasks.add(task)
        task.add_done_callback(self._input_tasks.discard)

    def _profile_prompt(self, messages: list[dict]) -> None:
        """Record what the prompt is made of and how much of it repeats.

        Measurement only — nothing here changes what is sent.  See
        ``llm/prompt_metrics.py`` for why the reusable prefix is the number
        that matters.
        """
        if not bool(self._cfg.get("llm.profile_prompt", True)):
            return
        try:
            if self._prompt_profiler is None:
                from neuro_voice.llm.prompt_metrics import PromptProfiler

                self._prompt_profiler = PromptProfiler()
            profile = self._prompt_profiler.profile(messages)
        except Exception:
            logger.debug("プロンプト計測に失敗", exc_info=True)
            return
        logger.info("プロンプト内訳: %s", profile.summary())
        detail = profile.section_summary()
        if detail:
            logger.info("プロンプト内訳(詳細): %s", detail)
        if profile.total_tokens > int(self._cfg.get("llm.num_ctx", 4096)):
            # fit_messages never drops system messages, so a large enough
            # context block pushes the prompt past the window and the server
            # truncates from the front — which is where the persona lives.
            logger.warning(
                "プロンプトがnum_ctxを超えています (%dtok > %dtok)。"
                "system側が切り詰め対象外のため、先頭が落ちる可能性があります",
                profile.total_tokens, int(self._cfg.get("llm.num_ctx", 4096)),
            )
        self._emit("prompt_profile", **profile.snapshot())
        self._last_prompt_profile = profile.snapshot()

    async def _probe_llm_latency(self) -> None:
        """One-off measurement of where the per-request fixed cost goes.

        The OpenAI-compatible endpoint discards the timings that would explain
        it; Ollama's native ``/api/chat`` reports them.  Runs with and without
        the per-request ``options`` we normally send, so both suspects are
        tested in a single pass instead of a config edit and a restart each.
        """
        backend = str(getattr(self._llm, "name", "") or "").lower()
        if backend != "ollama":
            logger.info("LLM遅延の切り分けは Ollama 専用です (現在=%s)", backend or "不明")
            return
        # The backends live under llm.backends.<name>, not llm.<name>.
        settings = {}
        with contextlib.suppress(Exception):
            settings = (self._cfg.section("llm.backends") or {}).get("ollama") or {}
        base_url = str(settings.get("base_url") or "").strip()
        model = str(settings.get("model") or "").strip()
        if not base_url or not model:
            logger.info(
                "LLM遅延の切り分けを省略: llm.backends.ollama の base_url/model が読めません",
            )
            return
        from neuro_voice.llm.ollama_probe import probe_ollama

        try:
            # A moment after start-up, so this does not race the first turn.
            await asyncio.sleep(float(self._cfg.get("llm.probe_delay_s", 3.0)))
            result = await asyncio.to_thread(
                probe_ollama, base_url, model,
                num_ctx=int(self._cfg.get("llm.num_ctx", 4096)),
                max_tokens=int(self._cfg.get("llm.max_tokens", 1024)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("LLM遅延の切り分けに失敗", exc_info=True)
            return
        logger.info(
            "LLM遅延の切り分け (keep_alive=%s):\n%s",
            self._cfg.get("llm.keep_alive") or "(未設定)", result.summary(),
        )
        self._emit("status", message=f"⏱ 応答遅延の内訳: {result.verdict()}")

    def _reply_echo_guard(self, segmenter):
        """Guard this reply against being the previous one again, or None."""
        if not bool(self._cfg.get("conversation.suppress_repeated_reply", True)):
            return None
        # Self-repeat and user parroting are different failures.  Label their
        # evidence so the guard can use a stricter threshold for the latter
        # and telemetry can say which one actually fired.
        previous: list[str] = []
        user_said = ""
        with contextlib.suppress(Exception):
            recent = getattr(self._conv, "recent_assistant_texts", None)
            if callable(recent):
                # The failure is a turn-level loop.  Comparing four old
                # replies also catches harmless recurring openers such as
                # 「えっ、なに？」 and caused false positives.
                previous.extend(recent(limit=2))
            for message in reversed(self._conv.messages()):
                role = message.get("role")
                if role == "user" and not user_said:
                    user_said = str(message.get("content") or "")
                if previous and user_said:
                    break
        if (
            self._last_assistant_text.strip()
            and self._last_assistant_text not in previous
        ):
            previous.append(self._last_assistant_text)
        from neuro_voice.dialogue.echo import EchoSource, ReplyEchoGuard

        sources = [
            EchoSource(item, "assistant")
            for item in previous if str(item or "").strip()
        ]
        if user_said.strip():
            sources.append(EchoSource(user_said, "user"))
        if not sources:
            return None

        guard = ReplyEchoGuard(sources, segmenter)
        return guard if guard.active else None

    def _record_response_delivery_trace(self, **values: Any) -> None:
        """Record privacy-safe answer/delivery state on the active Trace only."""
        trace = getattr(self, "_cognitive_trace", None)
        if trace is None:
            trace = getattr(self, "_pending_legacy_trace", None)
        if trace is None:
            return
        for name, value in values.items():
            if hasattr(trace, name):
                setattr(trace, name, value)

    # -- 自分から話す (Phase 5 第三弾) -----------------------------------
    #
    # **迂回の修正はここ。** これまで autonomy とゲーム実況は
    # `respond_text(internal_event=True)` で `_cognitive_decide` を飛ばしていた。
    # かといって内部プロンプトを `_cognitive_decide` へ渡すと、
    # **自分が自分に話しかけた**ことになる（内部文から相手の感情を推定し、
    # 内部文で記憶を引く）。だから**別の入口**を作る。

    def initiative(self):
        """自発発話と世界状態の入れ物。既定では止まっている。

        初回に作った時点で**DBを繋ぎ、前回の状態を復元する**。
        一時的な事実は復元しても古い扱いになる（`hydrate`）。
        """
        if self._initiative is None:
            from neuro_voice.cognition.runtime import InitiativeRuntime

            self._initiative = InitiativeRuntime(self._cfg, source="local")
            store = getattr(self._mind, "_store", None) if self._mind is not None else None
            if store is not None:
                self._initiative.attach_store(store)
                with contextlib.suppress(Exception):
                    result = self._initiative.hydrate()
                    if result.get("hydrated"):
                        logger.info("世界状態を復元: %s", result)
                        self._emit("world_state_hydrated", **result)
            # **実経路E**: ゲームの出来事を世界状態へ。会話由来のみ。
            if self._mind is not None:
                with contextlib.suppress(Exception):
                    self._mind.attach_game_observer(self._note_game_session_event)
                # 関係値の移行 (Phase 6E)。**人物の解決はこちらが正本。**
                # 繋がないと Resolver が人物を引けず、`disabled` のまま。
                with contextlib.suppress(Exception):
                    self._mind.attach_identity_runtime(self._initiative)
                # 道具 (Phase 7)。**繋がないと、部品はあるのに誰も呼ばない。**
                with contextlib.suppress(Exception):
                    self._bind_tool_runtime()
        return self._initiative

    def _bind_tool_runtime(self) -> None:
        """**最初に繋ぐ実ツールは検索1つだけ**（第27項）。

        `dialogue_allows_tool` は**検索専用の判断**（ゲーム中は検索しない、
        等の既存の意味論が入っている）で、汎用の権限層ではない。Tool Gate
        で置き換えるのではなく、**検索の追加条件として残す**。
        """
        if self._mind is None:
            return
        runtime = self._mind.tool_runtime()
        if runtime.executor.bound("web_search", "search"):
            return

        def _search(*, query: str, max_results: int = 4):
            from neuro_voice.search.deepsearch import DeepSearch

            if self._search is None:
                section = self._cfg.section("search")
                self._search = DeepSearch(
                    max_results=int(section.get("max_results", 4)),
                    region=str(section.get("region", "jp-jp")),
                    timeout=float(section.get("timeout_s", 7.0)),
                    max_pages=int(section.get("fetch_pages", 2)),
                    page_max_chars=int(section.get("page_max_chars", 2400)),
                    parallelism=int(section.get("parallelism", 3)))
            from neuro_voice.cognition.search_result_presenter import normalise_search_results

            clean_query = str(query).strip()
            raw_results = self._search.evidence_results_strict(
                clean_query, max_results=max_results, deadline_seconds=7.0)
            # Tool output crosses the trust boundary as compact, sanitized
            # evidence only.  HTML and raw provider payload never enter it.
            return {
                "search_result_views": [item.snapshot() for item in normalise_search_results(
                    raw_results, limit=max_results, query=clean_query)],
                "source_type": "web_search",
                "search_query": clean_query,
            }

        runtime.bind_search(_search)
        # 試験用の実書き込み (Phase 7B)。**root が空なら繋がない。**
        with contextlib.suppress(Exception):
            runtime.bind_scratch()

    def _note_game_session_event(self, *, event: str, **payload) -> None:
        """ゲームセッションの出来事 → 世界状態と目標。**発話しない。**"""
        with contextlib.suppress(Exception):
            self.initiative().observe_ktane_event(event=event, **payload)

    def note_speaker_observation(self, profile, *, event_id: str = "") -> int:
        """**実経路A**: 話者確認 → いま誰が居て、誰が話しているか。

        `auto_name` は「声紋では確定できず、仮の名前を付けた」印。
        **確認できていない声を既知の人物として扱うと、別人の関係値が動く。**
        取り違えは後から分離できないので、確認できた時だけ参加者にする。

        コールバックの中に直接書かず1つのメソッドにしてあるのは、
        ここが**実際に通るかを試せるようにする**ため。
        """
        if not profile:
            return 0
        try:
            return self.initiative().observe_speaker(
                speaker_id=int(profile.get("id", -1)),
                name=str(profile.get("name") or ""),
                confirmed=not bool(profile.get("auto_name", True)),
                event_id=str(event_id or ""))
        except Exception:  # noqa: BLE001 — 世界状態の失敗で会話を止めない（第17条）
            logger.debug("話者観測の取り込みに失敗", exc_info=True)
            return 0

    def flush_world_state(self) -> dict:
        """溜まった状態をまとめて書く。**応答の待ち時間には入れない。**

        映像1フレームごとにSQLiteを叩くと音声応答が止まるので、
        ターンの区切りでまとめて呼ぶ。
        """
        runtime = self.initiative()
        result = runtime.flush_world()
        goals = runtime.flush_goals()
        if not result.get("persisted") and result.get("reason") not in {
            "disabled", "no_store",
        }:
            # **未保存を成功扱いしない。**
            self._emit("world_state_persist_failed", reason=result.get("reason", ""))
        return {**result, "goals": goals}

    def note_attention_event(
        self, event_type: str, *, summary: str = "", topic_ids: tuple[str, ...] = (),
        salience: float = .5, novelty: float = .5, urgency: float = .0,
        confidence: float = 1.0, dedup: str = "", ttl: float = 0.0,
        participant_ids: tuple[str, ...] = (),
    ) -> bool:
        """出来事を注意層へ入れる。**間引かれたら False。**

        全フレーム・全ASR断片をここへ流してよい——`EventIntake` が落とす。
        呼び出し側で「これは重要か」を判断しないのが狙いで、
        判断が散らばると閾値が場所ごとに違う状態になる。
        """
        runtime = self.initiative()
        if not runtime.enabled:
            return False
        try:
            from neuro_voice.cognition.attention import AttentionEvent

            now = time.monotonic()
            return runtime.observe(AttentionEvent(
                event_type=event_type, source="local", summary=summary[:180],
                topic_ids=tuple(topic_ids)[:3], participant_ids=tuple(participant_ids),
                salience=salience, novelty=novelty, urgency=urgency,
                confidence=confidence, deduplication_key=dedup,
                occurred_at=now, expires_at=(now + ttl) if ttl > 0 else None,
            ), now=now)
        except Exception:
            logger.exception("注意イベントの取り込みでエラー")
            return False

    def _speaking_conditions(self):
        """いま話してよいかの材料を集める。**判定はここでしない。**"""
        from neuro_voice.cognition.initiative import SpeakingConditions

        closure_age = 999.0
        if self._last_turn_at:
            closure_age = max(0.0, time.monotonic() - self._last_turn_at)
        return SpeakingConditions(
            user_speaking=bool(self._user_speaking),
            awaiting_end_of_turn=bool(self._turn_manager.waiting_for_user_end())
            if hasattr(self._turn_manager, "waiting_for_user_end") else False,
            assistant_speaking=bool(self._is_responding() or self._playback.is_active),
            handling_interruption=bool(self._last_interrupted_text),
            seconds_since_turn_closure=closure_age,
            last_outcome_status=str(self._last_outcome_status or ""),
            last_topic_id=str(self._last_topic_id or ""),
            focus_mode=bool(self._proactive_suspended),
        )

    def _proactive_decide(self, kind: str = "spontaneous"):
        """自分から話す機会を作り、**通常会話と同じ Action Selector へ通す。**

        `_cognitive_decide` と別の関数にしてあるのは、渡すイベントが違うため。
        こちらは「自分へ向けられていない出来事」なので、答えるものが無い
        （`_UNADDRESSED_EVENTS`）。**内部プロンプトを人の発話として渡さない。**

        返すのは `(decision, opportunity)`。`decision` が `None`、または
        発話を許さない決定なら、呼び出し側は黙る。
        """
        runtime = self.initiative()
        if not (runtime.enabled and runtime.speech_enabled) or self._mind is None:
            return None, None
        if not self.cognition_active():
            # 認知層が止まっている時に自発発話だけ通すと、**沈黙の決定も
            # 記憶の補正も掛からない発話**が復活する。止めておく。
            return None, None
        try:
            from neuro_voice.cognition import (
                CognitiveEvent, CognitiveKernel, EventType,
            )
            from neuro_voice.cognition.initiative import SOURCE_PROACTIVE
            from neuro_voice.cognition.trace import CognitiveTrace

            trace = CognitiveTrace()
            started = time.perf_counter()
            state = self._mind.cognitive_state(source="local")
            self._schedule_due_obligation_attention(state)
            # 記憶と内面は**候補づくりの前に**引く。記憶をそのまま喋らせず、
            # 機会にしてから同じ列へ並べるため。
            recall = self._mind.recall_for_action("", state, source="local")
            internal = self._mind.internal_state("local")
            bias = self._mind.internal_action_bias(internal, source="local")
            result = runtime.evaluate(
                self._speaking_conditions(),
                obligations=tuple(state.unresolved_obligations),
                relationship_fit=state.comfort - .5,
                internal_state=internal,
                participant_id=self._mind.speaker_display_name("local") or "",
                memories=recall.scored,
                # **実人数を数える。** 2値だと3人と5人の差が出ない。
                participant_count=self._mind.participant_count(),
                others_talking=bool(state.is_group and self._user_speaking),
                seconds_since_user_turn=max(
                    0.0, time.monotonic() - (self._last_activity or 0.0)),
                seconds_since_own_speech=max(
                    0.0, time.monotonic() - (self._last_turn_at or 0.0)),
            )
            trace.mark("proactive_total_ms", (time.perf_counter() - started) * 1000)
            for name, value in (result.latency_ms or {}).items():
                trace.mark(name, value)
            if result.empty:
                return None, None

            event = CognitiveEvent(
                event_type=EventType.VISUAL_CHANGE if kind == "game"
                else EventType.SILENCE_TIMEOUT,
                source="local", content="", confidence=1.0,
                metadata={"initiative": kind},
            )
            # 共有している目標。**上書きではなく小さな補正。**
            # 打ち切りの合図が出ていれば `goal_bias` 側で何も返らない。
            runtime.sweep_world()
            goal_bias = runtime.goal_bias(end_signal=state.end_signal)
            kernel = CognitiveKernel(
                dict(self._cfg.get("cognition.weights", {}) or {}),
                closure_policy=str(
                    self._cfg.get("cognition.closure_response_policy", "adaptive")),
                memory_influence=recall.influence,
                state_bias=bias if not bias.empty else None,
                goal_bias=goal_bias,
            )
            self._record_world_trace(trace, runtime, goal_bias)
            candidates = kernel.propose(event, state, result.opportunities)
            decision = kernel.decide(event, state, candidates)
            self._mind.record_cognitive_action(str(decision.selected_action))

            trace.input_event_ids = (event.event_id,)
            trace.state_snapshot_summary = state.summary()
            trace.generated_candidates = [item.snapshot() for item in candidates]
            trace.selected_action = str(decision.selected_action)
            trace.decision_reason = decision.decision_reason
            trace.initiative_summary = result.snapshot()
            self._record_memory_trace(trace, recall)
            self._record_persona_trace(trace, recall)
            self._record_state_trace(trace, internal, bias)
            self._cognitive_trace = trace
            self._emit("initiative_decision", **decision.snapshot(),
                       opportunities=len(result.opportunities))

            if decision.source_type != SOURCE_PROACTIVE or not decision.speaks:
                # 沈黙が選ばれた／通常候補が勝った。**どちらも正常。**
                logger.info("自発発話は見送り: %s (%s)",
                            decision.selected_action, decision.decision_reason)
                self._trace_writer().emit(trace)
                self._cognitive_trace = None
                return None, None
            chosen = next(
                (item for item in result.opportunities
                 if item.proposed_action is decision.selected_action), None)
            return decision, chosen
        except Exception:
            # 落ちたら黙る。**自発発話は落ちた時に喋る方が危ない。**
            logger.exception("自発発話の判断でエラー（今回は黙る）")
            return None, None

    def _schedule_due_obligation_attention(self, state) -> int:
        """Route due prior work to Initiative, never into a user reply's recall.

        Only a stable obligation ID is sent to Attention; question/topic text
        remains with its owner and is not copied into logs or Trace.
        """
        if state is None:
            return 0
        from neuro_voice.cognition.attention import AttentionEventType

        scheduled = 0
        now = time.time()
        for item in tuple(getattr(state, "obligation_contexts", ()) or ()):
            due_at = float(item.get("due_at", 0.0) or 0.0)
            if due_at <= 0 or due_at > now:
                continue
            if str(item.get("status", "open") or "open").lower() in {
                "resolved", "cancelled", "expired",
            }:
                continue
            obligation_id = str(item.get("obligation_id", "") or "")
            if not obligation_id:
                continue
            if self._observe_attention(
                event_type=AttentionEventType.OBLIGATION_DUE,
                summary="due_obligation", salience=.8, novelty=.4, urgency=1.0,
                confidence=1.0, dedup=f"obligation_due:{obligation_id}",
            ):
                scheduled += 1
        return scheduled

    def proactive_speech_allowed(self, decision, opportunity) -> str:
        """TTSへ入れる直前の最終確認。空文字なら出してよい。

        Speech Gate と再検証の両方を通す。候補を選んでから音が出るまでに
        相手が話し始めることがあるので、**入口で1度見るだけでは足りない**。
        """
        if decision is None:
            return "no_decision"
        try:
            from neuro_voice.cognition.rollout import (
                SpeechRequest, SpeechSource, check_speech,
            )

            runtime = self.initiative()
            conditions = self._speaking_conditions()
            cancelled = runtime.confirm(opportunity or decision, conditions)
            if cancelled:
                self._emit("initiative_cancelled", reason=cancelled)
                return cancelled
            result = check_speech(
                SpeechRequest(
                    source_type=SpeechSource.PROACTIVE_OPPORTUNITY,
                    source_action=decision.selected_action,
                    confidence=decision.confidence,
                    topic_id=decision.topic_id,
                    source_event_ids=decision.source_event_ids,
                    expires_at=decision.expires_at,
                    user_speaking=conditions.user_speaking,
                    closure_suppressed=self.spontaneous_speech_suppressed(
                        decision.topic_id) or "",
                ),
                cognition_enabled=self.cognition_active(),
                has_valid_decision=True,
                decision_allows_speech=decision.speaks,
            )
            if not result.allowed:
                self._emit("initiative_cancelled", reason=result.reason)
                return result.reason
            trace = self._cognitive_trace
            if trace is not None:
                trace.speech_gate_result = result.reason
                trace.mark("pre_speech_revalidation_ms", runtime.last_revalidation_ms)
            return ""
        except Exception:
            logger.exception("自発発話のゲート判定でエラー（今回は黙る）")
            return "gate_error"

    @staticmethod
    def _response_requirement(conversation_decision, user_text: str = "") -> tuple[bool, str]:
        """Use the existing ConversationKernel intent, not another text rule."""
        from neuro_voice.dialogue.intent_plan import is_stop_request
        if is_stop_request(user_text):
            return False, "closure_signal"
        plan = getattr(conversation_decision, "response_plan", None)
        if plan is None or not bool(getattr(plan, "should_respond", False)):
            return False, ""
        role = str(getattr(plan, "response_role", "") or "")
        if role in {"remain_silent", "acknowledgement"}:
            return False, str(getattr(plan, "reason", "") or role)[:80]
        required = getattr(plan, "requires_response_contract", None)
        if required is None:
            required = (
                str(getattr(plan, "reason", "") or "") == "addressed"
                and role in {"answer_and_expand", "continue_previous_topic"}
            )
        if not bool(required):
            return False, str(getattr(plan, "reason", "") or role)[:80]
        return True, str(getattr(plan, "reason", "addressed") or "addressed")[:80]

    def _cognitive_decide(
        self, user_text: str, transcript, metrics: TurnMetrics | None = None,
        *, response_required: bool = False,
        response_requirement_source: str = "",
        turn_frame: dict[str, Any] | None = None,
    ) -> object | None:
        """文章を作る前に、何をするかを決める。

        既定では無効（`cognition.enabled`）。有効にすると、通常発話が
        認知カーネルを通ってから Conversation Planner へ渡る。
        反射（VAD・割り込み・TTS停止）はここを通さない——待たせたら
        割り込みが効かなくなる。
        """
        if self._mind is None or not self._turn_cognition_active(metrics):
            return None
        from neuro_voice.cognition.fallback import FallbackState, legacy_fallback_allowed
        fallback_state = getattr(metrics, "_cognitive_fallback_state", None)
        if fallback_state is None:
            fallback_state = FallbackState()
            if metrics is not None:
                metrics._cognitive_fallback_state = fallback_state
        try:
            from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType
            from neuro_voice.cognition.trace import CognitiveTrace

            trace = CognitiveTrace()
            if metrics is not None:
                self._bind_trace_turn(trace, metrics)
                trace.action_selector_used = True
            started = time.perf_counter()
            confidence = 1.0
            with contextlib.suppress(Exception):
                value = getattr(transcript, "confidence", None)
                if value is not None:
                    confidence = float(value)
            event = CognitiveEvent(
                event_type=EventType.USER_UTTERANCE, source="local",
                content=user_text, confidence=confidence,
            )
            trace.mark("event_normalization_ms", (time.perf_counter() - started) * 1000)

            mark = time.perf_counter()
            state = self._mind.cognitive_state(
                user_text, source="local", input_confidence=confidence,
                interrupted_response=self._last_interrupted_text,
                response_required=response_required,
                response_requirement_source=response_requirement_source,
            )
            from dataclasses import replace
            from neuro_voice.cognition.action_constraints import ActionEligibility
            from neuro_voice.dialogue.turn_closure import TurnClosurePolicy
            frame = dict(turn_frame or {})
            eligibility = ActionEligibility.from_turn(
                closure_signal=state.end_signal > .5,
                closure_confidence=state.end_signal,
                closure_is_affirmative=state.closure_is_affirmative,
                direct_question=str(frame.get("decision_reason", "")) == "direct_question",
                explicit_answer_request=TurnClosurePolicy.has_explicit_answer_request(user_text),
                new_task_instruction=bool(frame.get("plan_candidate", False)),
                safety_warning=float(state.world_state.get("danger", 0.0) or 0.0) > .5,
                closure_response_policy=str(
                    self._cfg.get("cognition.closure_response_policy", "adaptive")),
            )
            state = replace(
                state,
                working_context={
                    **state.working_context,
                    "action_eligibility": eligibility.snapshot(),
                },
            )
            trace.mark("state_snapshot_ms", (time.perf_counter() - mark) * 1000)

            # 過去の経験を、**探す理由がある時だけ**引く。挨拶では引かない。
            # 引けたら行動候補の点数へ効く（プロンプトへ足すだけにしない）。
            recall = self._mind.recall_for_action(user_text, state, source="local")
            self._record_memory_trace(trace, recall)
            self._record_persona_trace(trace, recall)

            # 持続している内面。**上書きではなく小さな補正**として渡す。
            mark = time.perf_counter()
            internal = self._mind.internal_state("local")
            bias = self._mind.internal_action_bias(internal, source="local")
            trace.mark("internal_state_ms", (time.perf_counter() - mark) * 1000)
            self._record_state_trace(trace, internal, bias)

            kernel = CognitiveKernel(
                dict(self._cfg.get("cognition.weights", {}) or {}),
                closure_policy=str(
                    self._cfg.get("cognition.closure_response_policy", "adaptive")),
                memory_influence=recall.influence,
                state_bias=bias if not bias.empty else None,
            )
            mark = time.perf_counter()
            candidates = kernel.propose(event, state)
            if getattr(metrics, "cognition_rollout_mode", "") == "production_session":
                from neuro_voice.search.contracts import SearchDisposition
                from neuro_voice.search.intent import route_search_request

                cached = self._search_evidence_cache.get(
                    persona_id=metrics.persona_id, surface="local",
                    audience="local:mic", epoch=metrics.persona_epoch)
                search_route = route_search_request(
                    user_text, enabled=self._search_enabled,
                    has_reusable_evidence=cached is not None)
                metrics._search_route_decision = search_route
                if search_route.disposition in {
                    SearchDisposition.EXECUTE,
                    SearchDisposition.BLOCKED,
                    SearchDisposition.CLARIFY,
                    SearchDisposition.REUSE_EVIDENCE,
                }:
                    from neuro_voice.cognition.types import ActionCandidate, ActionType
                    candidates.append(ActionCandidate(
                        action_type=ActionType.EXECUTE_TOOL,
                        target="web_search.search",
                        parameters={
                            "tool_id": "web_search", "operation_id": "search",
                            "search_disposition": search_route.disposition.value,
                        },
                        reasons=(search_route.reason_code,),
                        score_components={"explicit_request": 2.0},
                        total_score=2.0, confidence=1.0,
                        source_type="cognitive_tool_request",
                        source_event_ids=(event.event_id,),
                    ))
            trace.mark("candidate_generation_ms", (time.perf_counter() - mark) * 1000)

            mark = time.perf_counter()
            decision = kernel.decide(event, state, candidates)
            trace.mark("action_selection_ms", (time.perf_counter() - mark) * 1000)

            self._mind.record_cognitive_action(str(decision.selected_action))
            trace.input_event_ids = (event.event_id,)
            trace.state_snapshot_summary = state.summary()
            trace.generated_candidates = [item.snapshot() for item in candidates]
            trace.selected_action = str(decision.selected_action)
            trace.action_decision_id = str(decision.action_id)
            trace.decision_source = "ACTION_SELECTOR"
            trace.response_required = bool(state.response_required)
            trace.response_requirement_source = state.response_requirement_source
            trace.supporting_action = (
                str(decision.supporting_action) if decision.supporting_action else ""
            )
            trace.decision_scores = {
                str(item.action_type): item.total_score for item in candidates
            }
            trace.decision_reason = decision.decision_reason
            constraint = dict(getattr(decision, "parameters", {}).get("action_constraint", {}) or {})
            trace.closure_signal = eligibility.closure_signal
            trace.closure_confidence = eligibility.closure_confidence
            trace.direct_question = eligibility.direct_question
            trace.explicit_answer_request = eligibility.explicit_answer_request
            trace.new_task_instruction = eligibility.new_task_instruction
            trace.safety_warning = eligibility.safety_warning
            trace.response_obligation = eligibility.response_obligation
            trace.eligible_actions = list(constraint.get("eligible_actions", ()))
            trace.candidate_action_scores = dict(trace.decision_scores)
            trace.initially_selected_action = str(constraint.get("initially_selected_action", ""))
            trace.action_constraint_result = str(constraint.get("action_constraint_result", ""))
            trace.corrected_action = str(constraint.get("corrected_action", ""))
            trace.final_selected_action = str(constraint.get("final_selected_action", decision.selected_action))
            trace.closure_response_policy = eligibility.closure_response_policy
            trace.action_selection_reason = eligibility.action_selection_reason
            self._cognitive_trace = trace
            logger.info(
                "認知カーネル: %s%s (%s) 状態=%s",
                decision.selected_action,
                f"+{decision.supporting_action}" if decision.supporting_action else "",
                decision.decision_reason, state.summary(),
            )
            self._emit("cognitive_decision", **decision.snapshot())
            return decision
        except Exception:
            # 認知層で落ちても会話は続ける（第17条）。旧経路と同じ動きになる。
            logger.exception("認知カーネルでエラー。従来の経路で続行します")
            allowed, reason = legacy_fallback_allowed(
                fallback_state, technical_error=True,
                stale_epoch=not self._speech_allowed_now(metrics=metrics),
            )
            if metrics is not None:
                metrics.cognitive_fallback_count = fallback_state.fallback_count
                metrics.cognitive_fallback_reason = reason
                metrics.cognitive_side_effect_state = {
                    name: bool(getattr(fallback_state, name))
                    for name in (
                        "action_decision_committed", "tool_execution_started",
                        "tool_side_effect_committed", "memory_write_committed",
                        "speech_request_accepted", "playback_started",
                        "irreversible_effect_started",
                    )
                }
            return None if allowed else self._cognitive_decision

    def _record_persona_trace(self, trace, recall) -> None:
        """取れた記憶の**持ち主**をトレースへ（Phase 7D 観測点）。

        分離が効いているかは、件数ではなく持ち主を見ないと分からない。
        `retrieved_memory_persona_ids` に他人が混ざっていたら漏れている。
        """
        if trace is None or self._mind is None:
            return
        with contextlib.suppress(Exception):
            ids = [item.memory.memory_id for item in (recall.scored or ())] if recall else []
            for name, value in self._mind.persona_trace_fields(ids).items():
                setattr(trace, name, value)

    @staticmethod
    def _record_memory_trace(trace, recall) -> None:
        """想起の内容をトレースへ。**要約と数値だけ、本文は入れない**（第12条）。"""
        if trace is None or recall is None:
            return
        with contextlib.suppress(Exception):
            # A missing trigger means no retrieval was attempted.  Record that
            # explicitly so trace consumers do not need to infer it from an
            # empty string or an empty result list.
            trace.memory_retrieval_trigger = recall.trigger or "not_applicable"
            trace.memory_trigger_result = (
                "RETRIEVE" if recall.trigger else "NOT_APPLICABLE"
            )
            trace.retrieval_query_summary = (
                recall.query.summary() if recall.query is not None else {}
            )
            trace.retrieved_memory_ids = [item.memory.memory_id for item in recall.scored]
            trace.memory_relevance_scores = {
                str(item.memory.memory_id): item.score for item in recall.scored
            }
            if recall.influence is not None and not recall.influence.empty:
                trace.memory_effect_on_actions = recall.influence.snapshot()
            gate = getattr(recall, "obligation_gate", None)
            if gate is not None:
                trace.obligation_retrieval_considered = bool(gate.considered)
                trace.obligation_retrieval_triggered = bool(gate.triggered)
                trace.obligation_retrieval_reason = str(gate.reason)
                trace.obligation_candidate_count = int(gate.candidate_count)
                trace.obligation_relevance_score = float(gate.relevance_score)
                trace.obligation_effect_on_action = (
                    recall.influence.snapshot()
                    if gate.triggered and recall.influence is not None and not recall.influence.empty
                    else {}
                )
            trace.reflection_ids_used = [r.reflection_id for r in recall.reflections]
            for name, value in (recall.latency_ms or {}).items():
                trace.mark(name, value)

    @staticmethod
    def _record_state_trace(trace, internal, bias) -> None:
        """内面の要約と、どの軸がどの行動を動かしたかをトレースへ。

        **粗い数値とラベルだけ。** 生の状態履歴もプロンプトも残さない（第12条）。
        """
        if trace is None or internal is None:
            return
        with contextlib.suppress(Exception):
            summary = internal.summary()
            trace.internal_state_version = int(summary.get("version", 0))
            trace.internal_state_summary = {
                "affect": summary.get("affect", {}),
                "bands": summary.get("bands", {}),
                "stance": summary.get("stance", {}),
                "preferences": summary.get("preferences", {}),
            }
            trace.applied_state_deltas = summary.get("deltas", [])
            if bias is not None and not bias.empty:
                trace.state_dimensions_used = list(bias.dimensions_used)
                trace.state_effect_on_actions = bias.snapshot()

    @staticmethod
    def _record_world_trace(trace, runtime, goal_bias) -> None:
        """いまの状況と目標をトレースへ。**画面全文も本文も入れない**（第12条）。"""
        if trace is None or runtime is None:
            return
        with contextlib.suppress(Exception):
            if runtime.world_enabled:
                trace.world_state_summary = runtime.world.summary()
                trace.world_state_deltas = [
                    item.snapshot() for item in runtime.world.recent_changes[-6:]]
            if runtime.goals_enabled:
                trace.goal_summary = [
                    item.snapshot() for item in runtime.goals.values()][:6]
                trace.goal_effect_on_actions = dict(goal_bias or {})
                trace.permission_required = [
                    item.kind for item in runtime.next_actions()
                    if str(item.permission) == "needs_approval"]

    def observe_world_entity(self, name: str, **kwargs):
        """対象を1つ観測する。**確信が足りなければ既知へ寄せない。**"""
        return self.initiative().observe_entity(name, **kwargs)

    def observe_world_fact(self, subject_id: str, predicate: str, value=True, **kwargs):
        """事実を1つ観測する。**古い事実は根拠にしない。**"""
        return self.initiative().observe_fact(subject_id, predicate, value, **kwargs)

    def propose_goal(self, goal, *, source: str = "", approved: bool = False):
        """目標を登録してよいか。**勝手な長期目標は ACTIVE にしない。**"""
        return self.initiative().propose_goal(goal, source=source, approved=approved)

    #: 認知決定と実行結果から読み取れる、自分の失敗。
    #:
    #: **これが無いと同じ失敗を繰り返す。** 自由文にすると同じ失敗が
    #: 別物として溜まるので、短い識別子で持つ。
    def _self_failure_pattern(self, decision, outcome) -> str:
        action = str(getattr(decision, "selected_action", ""))
        if str(getattr(outcome, "status", "")) == "interrupted":
            if action in {"continue_previous_topic", "answer"}:
                # 言い終える前に割り込まれた。話しすぎている可能性。
                return "kept_talking_after_end_signal"
        if float(getattr(decision, "confidence", 1.0)) < 0.55 and action == "answer":
            return "answered_on_low_confidence"
        return ""

    def _cognitive_answer_delivery_result(
        self, reply: str, metrics: TurnMetrics | None,
    ) -> tuple[str, str, bool]:
        """Return the real completion state for a speech-permitted ANSWER."""
        decision = getattr(self, "_cognitive_decision", None)
        if decision is None or metrics is None or not self._turn_cognition_active(metrics):
            return "completed", "", False
        if str(getattr(decision, "selected_action", "")) != "answer":
            return "completed", "", False
        if not bool(getattr(self, "_cognitive_speech_allowed", False)):
            return "completed", "", False
        if not str(reply or "").strip():
            return "failed", "response_empty", False
        delivery = self._turns.delivery_snapshot(metrics)
        sessions = int(delivery.get("logical_playback_session_count", 0) or 0)
        if sessions < 1:
            return "failed", "tts_or_playback", False
        segments = list(delivery.get("playback_segments", ()) or ())
        if not segments:
            return "failed", "playback_not_started", False
        if any(not bool(segment.get("completed", False)) for segment in segments):
            return "interrupted", "playback_interrupted", False
        return "completed", "", True

    async def _await_delivery_finalization(
        self, metrics: TurnMetrics | None, *, timeout_s: float = 30.0,
    ) -> tuple[str, str]:
        """Wait for shared Playback Coordinator terminal state without retrying."""
        if metrics is None:
            return "failed", "missing_turn_metrics"
        finalized = getattr(self._turns, "playback_finalized", None)
        if finalized is None:
            return "failed", "delivery_tracker_missing"
        deadline = time.monotonic() + max(.1, float(timeout_s))
        try:
            while not finalized(metrics) and time.monotonic() < deadline:
                await asyncio.sleep(.02)
        except asyncio.CancelledError:
            return "interrupted", "playback_cancelled"
        except Exception:
            logger.exception("Playback delivery finalization failed")
            return "failed", "delivery_finalizer_error"
        if not finalized(metrics):
            return "failed", "delivery_finalizer_timeout"
        delivery = self._turns.delivery_snapshot(metrics)
        sessions = int(delivery.get("logical_playback_session_count", 0) or 0)
        segments = list(delivery.get("playback_segments", ()) or ())
        if sessions < 1 or not segments:
            return "failed", "tts_or_playback"
        if any(not bool(segment.get("completed", False)) for segment in segments):
            return "interrupted", "playback_interrupted"
        return "completed", ""

    async def _execute_production_read_only_tool(
        self, user_text: str, metrics: TurnMetrics, response_id: str,
    ) -> str:
        """Run the sole Phase-8 probe without changing persistent config.

        Tool output is never interpolated into instructions or speech.  The
        result planner uses only verified status, and execution has no Legacy
        fallback once it starts.
        """
        from neuro_voice.cognition.tool_exec import ToolStatus
        from neuro_voice.cognition.tool_runtime import search_intent
        from neuro_voice.cognition.types import ActionDecision, ActionType

        self._bind_tool_runtime()
        runtime = self._mind.tool_runtime()
        runtime.set_read_only_session(True)
        from neuro_voice.search.contracts import SearchDisposition
        from neuro_voice.search.intent import route_search_request

        route = getattr(metrics, "_search_route_decision", None)
        if route is None:
            cached = self._search_evidence_cache.get(
                persona_id=metrics.persona_id, surface="local",
                audience="local:mic", epoch=metrics.persona_epoch)
            route = route_search_request(
                user_text, enabled=self._search_enabled,
                has_reusable_evidence=cached is not None)
        if route.disposition is SearchDisposition.REUSE_EVIDENCE:
            cached = self._search_evidence_cache.get(
                persona_id=metrics.persona_id, surface="local",
                audience="local:mic", epoch=metrics.persona_epoch)
            if cached is not None:
                from neuro_voice.cognition.search_result_presenter import present_search_outcome
                presentation = present_search_outcome(
                    cached.outcome, answer_mode=route.answer_mode)
                original = self._cognitive_decision
                self._cognitive_decision = ActionDecision(
                    selected_action=ActionType.REPORT_TOOL_SUCCESS,
                    decision_reason="reused_recent_search_evidence",
                    state_snapshot_id=str(getattr(original, "state_snapshot_id", "")),
                    event_ids=tuple(getattr(original, "event_ids", ()) or ()),
                    confidence=.9,
                    turn_id=str(getattr(original, "turn_id", "") or metrics.turn_id),
                    source_type="cognitive_tool_outcome",
                )
                return presentation.response
        if route.disposition is SearchDisposition.BLOCKED:
            original = self._cognitive_decision
            self._cognitive_decision = ActionDecision(
                selected_action=ActionType.REPORT_TOOL_FAILURE,
                decision_reason=route.reason_code,
                state_snapshot_id=str(getattr(original, "state_snapshot_id", "")),
                event_ids=tuple(getattr(original, "event_ids", ()) or ()),
                confidence=1.0,
                turn_id=str(getattr(original, "turn_id", "") or metrics.turn_id),
                source_type="cognitive_tool_outcome",
            )
            return "今は検索機能を使えないよ。"
        if route.disposition is SearchDisposition.CLARIFY:
            original = self._cognitive_decision
            self._cognitive_decision = ActionDecision(
                selected_action=ActionType.REPORT_TOOL_FAILURE,
                decision_reason=route.reason_code,
                state_snapshot_id=str(getattr(original, "state_snapshot_id", "")),
                event_ids=tuple(getattr(original, "event_ids", ()) or ()),
                confidence=1.0,
                turn_id=str(getattr(original, "turn_id", "") or metrics.turn_id),
                source_type="cognitive_tool_outcome",
            )
            return "何を検索するか、対象をもう少し具体的に教えて。"
        intent = search_intent(route.query or user_text, person_id="local:mic", resolution="local",
                               rationale="explicit_read_only_tool_request")
        turn = runtime.propose(intent)
        trace = self._cognitive_trace
        if trace is not None:
            trace.note_plan(turn.plan, turn.admission)
        fallback_state = getattr(metrics, "_cognitive_fallback_state", None)
        if fallback_state is not None:
            fallback_state.tool_execution_started = True
        turn = await asyncio.to_thread(
            runtime.execute, turn, wait=7.0, user_awaiting=True)
        if trace is not None:
            trace.note_permission(turn.permission)
            trace.note_tool_gate(turn.gate_verdict)
            trace.note_tool_outcome(turn)
        result = turn.result
        from neuro_voice.cognition.search_result_presenter import (
            build_search_outcome, normalise_search_results, present_search_outcome,
        )
        presenter_started = time.perf_counter()
        structured = getattr(result, "structured_output", {}) or {}
        result_status = "SUCCEEDED" if str(getattr(result, "status", "")) == "succeeded" else "FAILED"
        search_outcome = build_search_outcome(
            route, structured.get("search_result_views"), status=result_status)
        presentation = present_search_outcome(search_outcome)
        self._search_evidence_cache.put(
            search_outcome, persona_id=metrics.persona_id, surface="local",
            audience="local:mic", epoch=metrics.persona_epoch,
            source_turn_id=metrics.turn_id)
        normalized_count = len(normalise_search_results(
            structured.get("search_result_views"), limit=4,
            query=route.query, answer_mode=route.answer_mode))
        if presentation.ui_result is not None:
            self._emit("tool_search_result", result=presentation.ui_result)
        verified = bool(turn.verification and turn.verification.verified)
        succeeded = bool(result and str(result.status) == str(ToolStatus.SUCCEEDED))
        if trace is not None:
            # The result can choose only the success/failure report route.  Its
            # content never reaches a prompt or the spoken response.
            trace.note_tool_result_usage(
                used_by_planner=bool(succeeded and verified))
            trace.note_search_result_presentation(
                presentation, normalized_count=normalized_count,
                elapsed_ms=(time.perf_counter() - presenter_started) * 1000)
        original = self._cognitive_decision
        self._cognitive_decision = ActionDecision(
            selected_action=(ActionType.REPORT_TOOL_SUCCESS if succeeded and verified
                             else ActionType.REPORT_TOOL_FAILURE),
            decision_reason=("verified_read_only_tool_result" if succeeded and verified
                             else "read_only_tool_result_unavailable"),
            state_snapshot_id=str(getattr(original, "state_snapshot_id", "")),
            event_ids=tuple(getattr(original, "event_ids", ()) or ()),
            confidence=.9 if succeeded and verified else .7,
            parameters={"tool_execution_id": str(getattr(result, "execution_id", ""))},
            turn_id=str(getattr(original, "turn_id", "") or metrics.turn_id),
            source_type="cognitive_tool_outcome",
        )
        return presentation.response

    def _cognitive_outcome(
        self, reply: str, *, status: str = "completed", error: str = "",
        speech_generated: bool = False, tts_called: bool = False,
        turn_closed: bool = False, metrics: TurnMetrics | None = None,
        affected_obligation_ids: tuple[str, ...] = (),
    ) -> None:
        """やってみてどうなったかを状態へ戻す。**これが無いと閉ループにならない。**

        中断されたなら、言い終えていない発話と義務が残る。次のターンで
        「続きを言うか、捨てるか」を選べるようにする。
        """
        decision, self._cognitive_decision = self._cognitive_decision, None
        trace, self._cognitive_trace = self._cognitive_trace, None
        if decision is None or self._mind is None:
            return
        from neuro_voice.cognition import ActionOutcome, CognitiveKernel

        started = time.perf_counter()
        interrupted = status == "interrupted"
        outcome = ActionOutcome(
            action_id=decision.action_id,
            turn_id=str(getattr(decision, "turn_id", "") or getattr(metrics, "turn_id", "")),
            status=status,
            interrupted=interrupted,
            error=error,
            observable_effects={"spoken": reply if interrupted else ""},
            speech_generated=speech_generated,
            tts_called=tts_called,
            turn_closed=turn_closed,
            affected_obligation_ids=affected_obligation_ids,
        )
        state = self._mind.cognitive_state(
            source="local", interrupted_response=self._last_interrupted_text,
        )
        updates = CognitiveKernel.apply_outcome(decision, outcome, state)
        # `ActionOutcome` is the one authoritative payload after the kernel has
        # settled the obligations.  Event and Trace consumers receive this same
        # snapshot; they must not append a second keyword source.
        if not outcome.affected_obligation_ids:
            outcome.affected_obligation_ids = tuple(
                str(item) for item in updates.get("obligations", ()) if str(item)
            )
        self._last_interrupted_text = str(updates.get("interrupted_response", ""))
        # **結果が確定してから**内面を動かし、自分の失敗を覚える。
        # 手順は `Mind.close_turn` の1箇所だけ——Discord と揃えるため
        # （第19条。片側だけ実装して完了としない）。
        # ターンの区切りでまとめて書く。**1件ずつ同期で書かない。**
        self.flush_world_state()
        deltas = self._mind.observe_internal_outcome(
            decision=decision, outcome=outcome, cognitive_state=state,
            source="local",
            event_id=decision.event_ids[0] if decision.event_ids else decision.action_id,
        )
        from neuro_voice.mind.mind import self_failure_pattern

        pattern = self_failure_pattern(decision, outcome)
        if pattern:
            self._mind.record_self_failure(
                pattern, source="local",
                detail=f"{decision.selected_action}:{outcome.status}",
            )
        if trace is not None and deltas:
            trace.proposed_state_deltas = [d.snapshot() for d in deltas]
            trace.applied_state_deltas = [
                d.snapshot() for d in deltas if d.applied
            ]
            trace.delta_clamp_reasons = [
                d.clamp_reason for d in deltas if d.clamp_reason
            ]
            trace.event_appraisal = self._mind.internal_state_status().get(
                "appraisal", "")
        # 関係性の差分は `apply_outcome` が上限を掛けて返している
        # （一度の出来事で大きく動かさない）。書き込みの持ち主は
        # `Relationships` のままなので、ここでは観測イベントだけ残す。
        emit_payload = self._emit_cognitive_outcome(outcome)
        if trace is not None:
            trace.mark("outcome_update_ms", (time.perf_counter() - started) * 1000)
            trace.execution_status = status
            trace.action_outcome_status = str(outcome.status)
            trace.turn_closure_completed = bool(outcome.turn_closed)
            trace.speech_generation_completed = bool(outcome.speech_generated)
            trace.final_response_empty = not bool(str(reply or "").strip())
            trace.delivery_failure_stage = str(error or "")
            trace.outcome_summary = emit_payload
            trace.state_changes = list(updates.get("state_changes", []))
            if metrics is not None:
                trace.turn_latency = metrics.snapshot()
                trace.speech_delivery = self._turns.delivery_snapshot(metrics)
                trace.speech_request_count = int(
                    trace.speech_delivery.get("speech_request", {}).get("accepted", 0))
            self._emit_trace_after_playback(trace, metrics)
            logger.debug("認知トレース: %s", trace.latency_breakdown)

    def _emit_cognitive_outcome(self, outcome) -> dict[str, Any]:
        """Construct the cognitive outcome event exactly once from its owner."""
        emit_payload = outcome.snapshot()
        self._emit("cognitive_outcome", **emit_payload)
        return emit_payload

    def _emit_trace_safely(self, trace) -> None:
        """Trace failures are diagnostic-only; cognition state failures are not."""
        try:
            self._trace_writer().emit(trace)
        except Exception:
            logger.exception("認知トレースのenqueueでエラー")

    def _capture_delivery_finalization(self, trace, metrics: TurnMetrics,
                                       *, finalized: bool, error: str = "") -> None:
        """Take the terminal delivery snapshot once, without speech content."""
        delivery = self._turns.delivery_snapshot(metrics)
        segments = list(delivery.get("playback_segments", ()) or ())
        sessions = int(delivery.get("logical_playback_session_count", 0) or 0)
        speech_count = int(delivery.get("speech_request_count", 0) or 0)
        if error:
            terminal = "interrupted" if error == "cancelled" else "failed"
        elif speech_count == 0:
            terminal = "not_applicable"
        elif sessions < 1 or not segments:
            terminal = "failed"
        elif all(bool(item.get("completed", False)) for item in segments):
            terminal = "completed"
        else:
            terminal = "interrupted"
        delivery["trace_delivery_finalized"] = bool(finalized)
        trace.speech_delivery = delivery
        trace.speech_request_count = int(
            delivery.get("speech_request", {}).get("accepted", 0))
        trace.delivery_finalize_called = True
        trace.delivery_terminal_state = terminal
        trace.playback_started = any(
            float(item.get("started_at", 0.0) or 0.0) > 0 for item in segments)
        trace.playback_completed = bool(segments) and all(
            bool(item.get("completed", False)) for item in segments)
        trace.trace_delivery_finalized = bool(finalized)
        trace.delivery_finalize_error = str(error or "")[:80]

    def _emit_trace_after_playback(self, trace, metrics: TurnMetrics | None) -> None:
        """Write after physical playback, without delaying speech or its start."""
        finalized = getattr(self._turns, "playback_finalized", None)
        if metrics is not None and finalized is None:
            self._capture_delivery_finalization(
                trace, metrics, finalized=False, error="delivery_tracker_missing")
            self._emit_trace_safely(trace)
            return
        try:
            ready = metrics is None or finalized(metrics)
        except Exception:
            logger.exception("Playback trace finalizer readiness failed")
            if metrics is not None:
                self._capture_delivery_finalization(
                    trace, metrics, finalized=False, error="finalizer_error")
            self._emit_trace_safely(trace)
            return
        if ready:
            if metrics is not None:
                self._capture_delivery_finalization(trace, metrics, finalized=True)
            self._emit_trace_safely(trace)
            return

        async def _finish() -> None:
            interrupted = False
            finalizer_error = ""
            try:
                deadline = time.monotonic() + 30.0
                while not self._turns.playback_finalized(metrics) and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                # Shutdown is not a reason to lose the only diagnostic
                # evidence.  Preserve its partial completion state, then let
                # normal cancellation continue.
                interrupted = True
            except Exception:
                logger.exception("Playback trace finalizer failed")
                finalizer_error = "finalizer_error"
            finally:
                trace.turn_latency = metrics.snapshot()
                self._capture_delivery_finalization(
                    trace, metrics,
                    finalized=not interrupted and not finalizer_error,
                    error="cancelled" if interrupted else finalizer_error)
                self._emit_trace_safely(trace)
            if interrupted:
                raise asyncio.CancelledError

        task = asyncio.create_task(_finish(), name=f"trace-playback-close-{metrics.turn_id}")
        tasks = getattr(self, "_trace_closure_tasks", None)
        if tasks is None:
            tasks = self._trace_closure_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _execute_or_stay_silent(self, response_id: str | None,
                                 metrics: TurnMetrics | None = None) -> bool:
        """発話してよいターンか。False なら**このターンは何も生成しない**。

        空文字を作るのでも、生成してから捨てるのでもない。
        Planner / Realizer / 応答LLM / TTS を**呼ばずに**ターンを閉じる。
        セッションは閉じない——次のユーザー発話は普通に処理される。
        """
        decision = self._cognitive_decision
        if decision is None:
            return True
        from neuro_voice.cognition.executor import (
            ActionExecutionGate, obligations_to_release,
        )

        started = time.perf_counter()
        state = self._mind.cognitive_state(source="local")
        plan = ActionExecutionGate.plan(decision, end_signal=state.end_signal)
        trace = self._cognitive_trace
        if trace is not None:
            trace.planner_input = plan.snapshot()
            trace.speech_gate_active = self._turn_cognition_active(metrics)
        self._cognitive_speech_allowed = bool(plan.speech_allowed)
        if plan.speech_allowed:
            return True

        released = obligations_to_release(state.unresolved_obligations, plan)
        if plan.clear_pending_response:
            # 予約済みの音声と、言い終えていない発話を無効化する。
            # 「もういいよ」の後に古い説明が流れてこないようにするため。
            self._last_interrupted_text = ""
            if response_id:
                self._cancel_response(response_id)
        silence = ActionExecutionGate.silence_contract(decision, plan)
        if trace is not None and plan.route == "silent":
            trace.action_decision_id = str(silence["action_decision_id"])
            trace.decision_source = str(silence["decision_source"])
            trace.silence_reason_code = str(silence["silence_reason_code"])
            trace.suppression_reason = str(silence["suppression_reason"])
        if plan.route == "silent" and not bool(silence["valid"]):
            # An incomplete cognitive decision is a technical failure, never a
            # successful silence. No irreversible effect exists at this point,
            # so the Phase 8 fallback may take the one legacy route.
            from neuro_voice.cognition.fallback import legacy_fallback_allowed

            fallback_state = getattr(metrics, "_cognitive_fallback_state", None)
            stale_epoch = bool(
                metrics is not None and metrics.effective_cognition_enabled
                and metrics.cognition_session_epoch
                != self._cognition_test_session.snapshot().epoch
            )
            allowed, reason = legacy_fallback_allowed(
                fallback_state, technical_error=True,
                stale_epoch=stale_epoch,
            ) if fallback_state is not None else (False, "missing_fallback_state")
            if metrics is not None:
                metrics.cognitive_fallback_count = int(
                    getattr(fallback_state, "fallback_count", 0))
                metrics.cognitive_fallback_reason = reason
            if allowed:
                if trace is not None:
                    trace.fallback_used = True
                    trace.fallback_count = int(fallback_state.fallback_count)
                    trace.cognitive_fallback_reason = reason
                self._cognitive_decision = None
                self._cognitive_trace = None
                return True
            self._cognitive_outcome(
                "", status="failed", error="silence_contract_invalid",
                turn_closed=True, metrics=metrics,
                affected_obligation_ids=released,
            )
            return False
        elapsed = (time.perf_counter() - started) * 1000
        status = "internal_completed" if plan.route == "internal" else "silent_completed"
        if trace is not None:
            trace.mark("decision_to_closure_ms", elapsed)
            trace.planner_input = {
                **plan.snapshot(),
                "planner_skipped": True, "realizer_skipped": True,
                "tts_skipped": True,
                "turn_closure_reason": plan.route,
            }
        self._cognitive_outcome(
            "", status=status, turn_closed=True, metrics=metrics,
            affected_obligation_ids=released,
        )
        for warning in plan.warnings:
            logger.warning("認知決定の矛盾: %s", warning)
        logger.info(
            "認知カーネル: %s のため発話しない (%s) 取消した義務=%d %.2fms",
            decision.selected_action, status, len(released), elapsed,
        )
        self._last_outcome_status = status
        self._last_topic_id = str(state.current_topic or "")
        self._last_turn_at = time.monotonic()
        return False

    def spontaneous_speech_suppressed(self, topic_id: str = "") -> str:
        """自発発話を止める理由。空なら止めない。

        黙ると決めた直後に同じ話題を自分から蒸し返すと、沈黙の決定が
        事実上無かったことになる。**時間だけで決めず、話題も見る**——
        別件で話しかけるのまで止める必要はない。
        """
        if not self.cognition_active():
            return ""
        from neuro_voice.cognition.rollout import spontaneous_suppressed

        return spontaneous_suppressed(
            last_status=self._last_outcome_status,
            last_topic_id=self._last_topic_id,
            topic_id=str(topic_id or ""),
            seconds_since_turn=max(0.0, time.monotonic() - self._last_turn_at),
            quiet_seconds=float(self._cfg.get("cognition.silence_quiet_seconds", 20)),
        )

    def cognition_active(self) -> bool:
        """このセッションで認知層を効かせるか。**止め方は `enabled` の1つ。**"""
        from neuro_voice.cognition.rollout import CognitionRolloutResolver

        return CognitionRolloutResolver.resolve(
            self._cfg, session=self._cognition_test_session.snapshot(),
        ).effective_enabled

    def start_cognition_test_session(self, active: bool = True) -> dict[str, Any]:
        """Request a process-local override; apply it only between turns."""
        return self.start_cognition_session("test_session" if active else "disabled")

    def start_cognition_session(self, mode: str = "test_session") -> dict[str, Any]:
        """Set TEST/PRODUCTION session mode without persisting config."""
        self._cognition_test_session.request_mode(
            mode, idle=self._cognition_turn_idle())
        self.log_cognition_status()
        result = self.cognition_session_status()
        self._emit("cognition_session", **result)
        return result

    def cognition_session_status(self) -> dict[str, Any]:
        snapshot = self._cognition_test_session.snapshot()
        from neuro_voice.cognition.rollout import CognitionRolloutResolver
        resolved = CognitionRolloutResolver.resolve(self._cfg, session=snapshot)
        return {
            "effective_cognition_enabled": resolved.effective_enabled,
            "rollout_mode": resolved.resolved_mode,
            "requested_rollout_mode": resolved.requested_mode,
            "execution_path": resolved.execution_path,
            "activation_source": resolved.activation_source,
            "config_fingerprint": resolved.config_fingerprint,
            "warning": resolved.warning,
            "cognition_session_epoch": snapshot.epoch,
            "state": snapshot.state,
            "pending": snapshot.state.endswith("starting") or snapshot.state.endswith("stopping"),
        }

    def _cognition_turn_idle(self) -> bool:
        return not self._is_responding() and not self._user_speaking

    def _finish_cognition_turn(self, _metrics: TurnMetrics | None = None) -> None:
        # Called by the completing turn itself; `_respond_task` can still be
        # alive for a few instructions, so checking it here would deadlock a
        # requested stop/start until another turn arrives.
        if self._cognition_test_session.apply_if_idle():
            self.log_cognition_status()

    def _turn_cognition_active(self, metrics: TurnMetrics | None) -> bool:
        return (bool(metrics.effective_cognition_enabled) if metrics is not None
                else self.cognition_active())

    def log_cognition_status(self) -> None:
        """有効かどうか分からない状態を作らない。起動時とセッション開始時に出す。"""
        from neuro_voice.cognition.rollout import CognitionRolloutResolver
        resolved = CognitionRolloutResolver.resolve(
            self._cfg, session=self._cognition_test_session.snapshot())
        active = resolved.effective_enabled
        logger.info(
            "[Cognition] enabled=%s rollout_mode=%s speech_gate=%s "
            "active_profile=%s closure_policy=%s legacy_fallback=%s",
            resolved.effective_enabled,
            resolved.resolved_mode,
            "active" if active else "inactive",
            self._cfg.get("video.game_profile", "") or "-",
            self._cfg.get("cognition.closure_response_policy", "adaptive"),
            "on" if not active else "off",
        )
        self._emit("cognition_status", active=active,
                   rollout_mode=resolved.resolved_mode,
                   execution_path=resolved.execution_path,
                   activation_source=resolved.activation_source)

    def _speech_allowed_now(
        self, response_id: str | None = None, request=None,
        metrics: TurnMetrics | None = None,
    ) -> bool:
        """いま音声を出してよいか。**最終出口の判定**。

        入口で1度見るだけでは足りない。fallback も古い非同期タスクも、
        別経路の相槌も、最後はここを通る。出どころ（`SpeechRequest`）が
        分からない要求は、認知層が有効な間は通さない。
        """
        from neuro_voice.cognition.rollout import (
            BypassReason, SpeechRequest, SpeechSource, check_speech,
        )

        # The legacy path keeps the direct call so its transparent behavior is
        # explicit; a TurnMetrics snapshot otherwise freezes this decision.
        active = (self.cognition_active() if metrics is None
                  else self._turn_cognition_active(metrics))
        if (metrics is not None and metrics.effective_cognition_enabled
                and metrics.cognition_session_epoch
                != self._cognition_test_session.snapshot().epoch):
            logger.warning("stale cognition epoch rejected: turn=%s epoch=%s current=%s",
                           metrics.turn_id, metrics.cognition_session_epoch,
                           self._cognition_test_session.snapshot().epoch)
            return False
        if not active:
            return True
        if response_id is not None and self._is_response_cancelled(response_id):
            return False
        decision = self._cognitive_decision
        if request is None:
            request = SpeechRequest(
                source_type=(
                    SpeechSource.COGNITIVE_DECISION if decision is not None
                    else SpeechSource.LEGACY_PATH
                ),
                source_action=decision.selected_action if decision else None,
                turn_id=str(response_id or ""),
                bypass_reason=(
                    BypassReason.NONE if decision is not None
                    else BypassReason.LEGACY_COMPATIBILITY
                ),
            )
        result = check_speech(
            request, cognition_enabled=active,
            has_valid_decision=decision is not None,
            decision_allows_speech=bool(decision and decision.speaks),
        )
        if not result.allowed:
            logger.warning(
                "発話を拒否: %s / %s", result.reason, request.summary(),
            )
            self._emit("speech_rejected", **request.summary(), reason=result.reason)
        return result.allowed

    def _cancel_response(self, response_id: str) -> None:
        """予約済みの出力を無効化する。既存のキャンセル機構を使う。"""
        with contextlib.suppress(Exception):
            self._cancelled_response_ids.add(response_id)
        with contextlib.suppress(Exception):
            self._playback.stop()

    def _trace_writer(self):
        """Trace startup diagnostics and the non-blocking JSONL writer."""
        if self._cognitive_trace_writer is None:
            from neuro_voice.cognition.async_trace_writer import TraceWriter

            self._cognitive_trace_writer = TraceWriter(
                self._cfg.get("cognition.trace.path", "logs/cognitive_trace.jsonl"),
                enabled=bool(self._cfg.get("cognition.trace.enabled", False)),
                project_root=Path(__file__).resolve().parents[1],
            )
            status = self._cognitive_trace_writer.status()
            logger.info(
                "Cognitive Trace startup: cognitive_trace_enabled=%s "
                "trace_writer_initialized=%s trace_output_path=%s "
                "trace_queue_enabled=%s trace_last_error=%s project_root=%s "
                "config_source_path=%s runtime_source_root=%s",
                status["cognitive_trace_enabled"], status["trace_writer_initialized"],
                status["trace_output_path"], status["trace_queue_enabled"],
                status["trace_last_error"], status["project_root"],
                getattr(self._cfg, "path", None), Path(__file__).resolve().parents[1],
            )
        return self._cognitive_trace_writer

    def _bind_trace_turn(self, trace, metrics: TurnMetrics) -> None:
        trace.turn_id = str(metrics.turn_id)
        trace.input_event_ids = (trace.turn_id,)
        if not trace.active_persona_id:
            trace.active_persona_id = str(metrics.persona_id)
            trace.active_persona_version = str(metrics.persona_version)
            trace.persona_epoch = int(metrics.persona_epoch or 0)
        trace.cognition_enabled = bool(metrics.effective_cognition_enabled)
        trace.rollout_mode = str(metrics.cognition_rollout_mode)
        trace.requested_rollout_mode = str(metrics.cognition_requested_rollout_mode)
        trace.execution_path = str(metrics.cognition_execution_path)
        trace.cognition_session_epoch = int(metrics.cognition_session_epoch or 0)
        trace.cognition_activation_source = str(metrics.cognition_activation_source)
        trace.cognition_config_fingerprint = str(metrics.cognition_config_fingerprint)
        trace.transport = str(metrics.transport)
        trace.speech_gate_active = bool(metrics.effective_cognition_enabled)
        trace.legacy_response_path_used = not bool(metrics.effective_cognition_enabled)

    def _prepare_legacy_turn_trace(self, metrics: TurnMetrics) -> None:
        """Trace the normal (cognition-disabled) conversation path once."""
        writer = self._trace_writer()
        if not writer.enabled:
            return
        from neuro_voice.cognition.trace import CognitiveTrace

        trace = CognitiveTrace(
            turn_id=str(metrics.turn_id),
            input_event_ids=(str(metrics.turn_id),),
            selected_action="legacy_conversation",
            execution_status="turn_committed",
            memory_retrieval_trigger="legacy_context",
            active_persona_id=str(metrics.persona_id),
            active_persona_version=str(metrics.persona_version),
            persona_epoch=int(metrics.persona_epoch or 0),
        )
        self._bind_trace_turn(trace, metrics)
        trace.fallback_count = int(metrics.cognitive_fallback_count or 0)
        trace.fallback_used = bool(trace.fallback_count)
        trace.cognitive_fallback_reason = str(metrics.cognitive_fallback_reason or "")
        trace.side_effect_state = dict(metrics.cognitive_side_effect_state or {})
        if self._mind is not None:
            for name, value in self._mind.last_retrieval_trace.items():
                if hasattr(trace, name):
                    setattr(trace, name, value)
            if trace.memory_trigger_result:
                trace.memory_retrieval_trigger = trace.memory_trigger_result
            memory_ids = [
                int(item["id"]) for item in self._mind.last_recall
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            ]
            trace.retrieved_memory_ids = memory_ids
            with contextlib.suppress(Exception):
                for name, value in self._mind.persona_trace_fields(memory_ids).items():
                    setattr(trace, name, value)
        self._pending_legacy_trace = trace

    def _flush_legacy_turn_trace(self, metrics: TurnMetrics | None = None) -> None:
        trace = self._pending_legacy_trace
        self._pending_legacy_trace = None
        if trace is None:
            return
        if self._mind is not None:
            with contextlib.suppress(Exception):
                trace.memory_write_decisions = list(
                    self._mind._episodes.snapshot().get("last_write_decisions", []))
        if metrics is not None:
            trace.turn_latency = metrics.snapshot()
            trace.latency_summary = self._latency_window.totals()
            trace.latency_breakdown = {
                label: round(value, 2)
                for start, end, label in _PAIRS
                if (value := metrics.delta_ms(start, end)) is not None
            }
            trace.speech_delivery = self._turns.delivery_snapshot(metrics)
            trace.speech_request_count = int(
                trace.speech_delivery.get("speech_request", {}).get("accepted", 0))
        trace.llm_diagnostics = dict(self._last_llm_diagnostics)
        if trace.llm_diagnostics.get("llm_invoked"):
            trace.llm_diagnostics.update(
                getattr(self._llm, "last_generation_metrics", {}) or {})
        if metrics is not None:
            provider_first = metrics.delta_ms("prompt_ready", "llm_first")
            if provider_first is not None and trace.llm_diagnostics.get("provider_first_token_ms") is None:
                trace.llm_diagnostics["provider_first_token_ms"] = round(provider_first, 1)
        self._emit_trace_after_playback(trace, metrics)

    def _verified_defusal_reply(self, reply: str) -> str:
        """規則と食い違っていたら差し替える。判定できなければ触らない。"""
        if self._mind is None or not str(reply or "").strip():
            return reply
        try:
            verdict, correction = self._mind.game_profile_verify_reply(reply)
        except Exception:
            logger.exception("爆弾解除の検算に失敗")
            return reply
        if verdict == "unclear":
            return reply
        self._emit("ktane_verified", verdict=verdict)
        if not correction:
            logger.info("爆弾解除: ポッポの判断と規則が一致")
            return reply
        # 本文は残さない（第12条）。食い違ったという事実だけ。
        logger.warning("爆弾解除: ポッポの判断が規則と食い違ったため差し替え")
        return correction

    async def _apply_switched_game_profile(self) -> None:
        """会話で選ばれたプロファイルへ、映像取得を合わせる。

        `refresh_video_observation` は `profile_allows_vision` を見るので、
        映像を使わないプロファイル（爆弾解除）へ移ればキャプチャは止まり、
        使うプロファイル（Minecraft）へ戻れば立ち上がる。

        ここで失敗しても会話は続ける。切り替えの返事はもう返っているし、
        映像が付かないことは会話が止まる理由にはならない（第17条）。
        """
        try:
            await self.refresh_video_observation()
        except Exception:
            logger.exception("ゲームプロファイル切り替え後の映像更新に失敗")
        profile = ""
        with contextlib.suppress(Exception):
            profile = str(self._cfg.get("video.game_profile", "") or "")
        self._emit("game_profile_switched", profile=profile)

    def _without_internal_leak(self, sentence: str) -> str:
        """Drop a sentence that reads our own internals back out.

        The prompt used to forbid this in seven places, about 300 tokens, and
        it still happened.  ``response_shape=`` in a spoken Japanese sentence
        is a fact about the text, so code decides it (第2条) and the prompt
        keeps one short line.
        """
        from neuro_voice.dialogue.leakage import leak_markers, strip_internal_leak

        markers = leak_markers(sentence)
        if not markers:
            return sentence
        kept = strip_internal_leak(sentence)
        # Log the marker, never the sentence: the surrounding text is
        # conversation (第12条).
        logger.warning("内部値が発話へ漏れたため落としました: %s", "、".join(markers[:3]))
        self._emit("internal_leak_dropped", markers=markers[:3])
        return kept

    def _hesitation_block(self, transcript) -> str:
        """Permission to let real uncertainty show, scoped to this turn."""
        if self._mind is None:
            return ""
        try:
            block, snapshot = self._mind.hesitation_permission(
                "local", transcript=transcript,
            )
        except Exception:
            logger.debug("確信度の評価に失敗", exc_info=True)
            return ""
        if snapshot and snapshot.get("allowance"):
            logger.info("今回の確信度: %s", snapshot)
            self._emit("hesitation", **snapshot)
        return block

    # ---------- 継続依頼 (BehaviorDirective) ----------

    def _build_directive_runtime(self):
        """Compose the shared runtime with the local-voice half of the work."""
        from neuro_voice.dialogue.directive_runtime import DirectiveHost, DirectiveRuntime

        host = DirectiveHost(
            speak=self._directive_speak,
            # Deliberately excludes our own playback.  The next segment has to
            # be written *while* the current one is being heard, or every gap
            # between segments costs a full model round-trip of dead air.  How
            # far ahead we may run is playback_ahead_s's job, not this one.
            floor_busy=lambda: bool(self._user_speaking or self._is_responding()),
            playback_ahead_s=lambda: float(
                getattr(self._playback, "pending_seconds", 0.0)
            ),
            request_tick=lambda delay: self._autonomy_heartbeat.request_tick(delay_s=delay),
            ready=lambda: not self._proactive_suspended,
            run_tool=self._directive_run_tool,
            conversation_tail=self._directive_conversation_tail,
            environment_available=lambda: self._video_service is not None,
            cancel_generation=self._directive_cancel_generation,
            discard_pending_audio=self._playback.discard_paused_audio,
            search_enabled=lambda: bool(self._search_enabled),
            on_user_stop=self._on_directive_user_stop,
        )
        return DirectiveRuntime(
            self._cfg, mind=self._mind, llm=self._llm, source="local",
            host=host, emit=self._emit, persona_name=lambda: self._persona_name,
        )

    @property
    def _directive_runtime(self):
        runtime = self._directive_runtime_obj
        if runtime is None or runtime._mind is not self._mind or runtime._llm is not self._llm:
            # Mind and the LLM backend can both be swapped at runtime.
            runtime = self._build_directive_runtime()
            self._directive_runtime_obj = runtime
        return runtime

    def _directive_conversation_tail(self) -> list[str]:
        return [
            f"{'ユーザー' if item.get('role') == 'user' else '自分'}: "
            f"{str(item.get('content', ''))[:120]}"
            for item in self._conv.messages()[-4:]
            if item.get("role") in {"user", "assistant"}
        ]

    def _directive_cancel_generation(self, response_id_prefix: str) -> None:
        response_id = self._active_response_id
        if response_id and response_id.startswith(response_id_prefix):
            self._cancelled_response_ids.add(response_id)
            self._llm.cancel_request(response_id)

    def _on_directive_user_stop(self, _user_text: str) -> None:
        """Stop requested talk and keep optional autonomy quiet for a while."""
        quiet_s = max(
            0.0, float(self._cfg.get("autonomy.post_stop_quiet_seconds", 300)),
        )
        self._autonomy.suppress_speech(
            duration_s=quiet_s, reason="directive_user_stop",
        )
        self._autonomy_heartbeat.cancel_pending_tick()
        task = self._autonomy_task
        if task is not None and not task.done():
            task.cancel()
        self._emit(
            "autonomy_suppressed", source="local",
            reason="directive_user_stop", quiet_seconds=quiet_s,
        )

    async def _directive_speak(self, text: str, segment_id: str, state_version: int) -> bool:
        """Play one segment sentence by sentence.

        A segment is a paragraph, not a control phrase.  Synthesizing it in one
        request produced a query string long enough to be rejected outright,
        and the failure surfaced as a percent-encoded URL in the chat window.
        Splitting also lets a human interrupt mid-segment.
        """
        directive = self._directive_runtime.active()
        if directive is None:
            return False
        response_id = self._directive_runtime.response_id_for(directive, segment_id)
        self._active_response_id = response_id
        self._cancelled_response_ids.discard(response_id)
        segmenter = SentenceSegmenter(
            max_chars=min(60, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
        )
        sentences = [*segmenter.feed(text), *([segmenter.flush()] or [])]
        sentences = [item.strip() for item in sentences if item and item.strip()]
        if not sentences:
            return False
        self._emit("assistant_start")
        self._emit("assistant_token", token=text)
        loop = asyncio.get_running_loop()
        spoken_any = False
        try:
            for sentence in sentences:
                # A directive revision or a human turn invalidates the rest of
                # this paragraph; stop rather than finish reading it out.
                if not directive.accepts(state_version=state_version):
                    break
                if not self._is_response_active(response_id) or self._user_speaking:
                    break
                audio, sample_rate = await loop.run_in_executor(
                    self._tts_ex, self._tts.synthesize, sentence, None, None,
                )
                if not self._is_response_active(response_id):
                    break
                self._playback.play(audio, sample_rate)
                spoken_any = True
        except asyncio.CancelledError:
            self._emit("assistant_cancelled", text=text)
            raise
        except Exception as exc:
            # Report the failure without pasting a synthesis URL into the chat.
            logger.exception("継続トークの読み上げに失敗")
            self._emit("error", message=(
                f"継続トークの読み上げに失敗: {safe_error_text(exc)}"
            ))
            return False
        finally:
            if self._active_response_id == response_id:
                self._active_response_id = None
            self._last_activity = time.monotonic()
        if spoken_any:
            self._conv.add_assistant(text)
            self._last_assistant_text = text
            self._emit("assistant_done", text=text)
        return spoken_any and response_id not in self._cancelled_response_ids

    def _handle_directive_turn(self, user_text: str, frame: dict[str, Any]):
        """Instant part only; plan/revision run alongside the reply."""
        if self._mind is None:
            return None
        return self._directive_runtime.begin_user_turn(user_text, frame)

    def _directive_prompt_block(self, directive) -> str:
        if self._mind is None:
            return ""
        return self._directive_runtime.prompt_block(directive)

    def _note_directive_opening_segment(self, directive, spoken_text: str) -> None:
        if self._mind is not None:
            self._directive_runtime.note_opening_segment(directive, spoken_text)

    async def _maybe_run_directive_segment(self) -> bool:
        if self._mind is None:
            return False
        return await self._directive_runtime.tick()

    def _pause_directive_for_human(self, *, discard_audio: bool = True) -> None:
        if self._mind is not None:
            self._directive_runtime.pause_for_human(discard_audio=discard_audio)

    def _resume_directive_after_backchannel(self) -> None:
        if self._mind is not None:
            with contextlib.suppress(Exception):
                self._directive_runtime.resume_after_backchannel()

    def note_directive_environment_event(self, summary: str, *, importance: float = 0.6) -> bool:
        """Feed a game/screen event to a directive that asked to watch for them."""
        if self._mind is None:
            return False
        return self._directive_runtime.note_environment_event(summary, importance=importance)

    async def _directive_run_tool(self, request: dict[str, Any]) -> str:
        """Execute a directive tool request under the existing permission gates."""
        tool = str(request.get("tool") or "")
        query = str(request.get("query") or "")
        if tool == "web_search":
            if not self._search_enabled or not query:
                return "検索は許可されていない"
            if self._mind is not None:
                # SearchDecision / PrivacyManager stay authoritative.  A
                # directive never widens its own search permission.
                allowed, reason = self._mind.dialogue_allows_tool(
                    "search", query, source="local",
                )
                if not allowed:
                    return f"検索は許可されていない ({reason})"
            try:
                from neuro_voice.search.deepsearch import DeepSearch

                if self._search is None:
                    section = self._cfg.section("search")
                    self._search = DeepSearch(
                        max_results=int(section.get("max_results", 4)),
                        region=str(section.get("region", "jp-jp")),
                        timeout=float(section.get("timeout_s", 7.0)),
                        max_pages=int(section.get("fetch_pages", 2)),
                        page_max_chars=int(section.get("page_max_chars", 2400)),
                        parallelism=int(section.get("parallelism", 3)),
                    )
                self._emit("search_start", query=query)
                result = await asyncio.get_running_loop().run_in_executor(
                    self._search_ex, self._search.search, query,
                )
                return str(result or "")[:600]
            except Exception:
                logger.exception("Directive web search failed")
                return "検索に失敗した"
        if tool == "game_state_read_only" and self._video_service is not None:
            return str(self._video_service.state_context(query) or "")[:600]
        if tool == "vision_read_only" and self._perception is not None:
            scene = self._perception.memory.current_scene
            return str(getattr(scene, "summary", "") or "")[:600]
        return ""


    def _schedule_next_proactive(self) -> None:
        """次の自発発話タイミングをランダムに決める。"""
        lo = max(5.0, float(self._cfg.get("proactive.min_interval_s", 30)))
        hi = max(lo, float(self._cfg.get("proactive.max_interval_s", 90)))
        self._proactive_next = time.monotonic() + random.uniform(lo, hi)

    def _pick_proactive_prompt(self) -> str:
        """状況に応じた自発発話プロンプトを選ぶ。Vision有効なら画面ネタも混ぜる。"""
        vision_ok = self._vision_enabled
        vision_prompts = [
            "(自発発話: 今の画面を見て、気になったところに短くツッコミを入れて。1〜2文)",
            "(自発発話: 今の画面の状況を見て、実況風に一言コメントして)",
            "(自発発話: 今の画面に映っているものについて、ユーザーに軽く質問してみて)",
        ]
        chat_prompts = [
            "(自発発話: これまでの会話に関連する雑談を自分から短く振って。1〜2文)",
            "(自発発話: ユーザーが静かなので、自分から気になる話題を振ってみて。1〜2文)",
            "(自発発話: ユーザーに軽い質問をしてみて。1文)",
            "(自発発話: 最近考えていたことを独り言のように短くつぶやいて。1〜2文)",
        ]
        pool = chat_prompts + (vision_prompts if vision_ok else [])
        return random.choice(pool)

    def _begin_turn(self, metrics: TurnMetrics, *, kind=None):
        """ターンの入口。**`turn_id` はここで決めた1つを最後まで使う。**

        入口が1箇所に絞れない（マイク／打ち込み／自発発話／内部イベント）
        ので冪等にしてある。2回目以降は最初に作った枠をそのまま返す。

        persona と session_id の埋め込みは `turn_frame_enabled` の外側で
        やる。あれは Phase 7D の計測の背骨に属していて、🩺 の
        ペルソナ欄が空のままだったのは単に呼ばれていなかったから。
        """
        from neuro_voice.cognition.turn_integrity import ConversationKind

        if metrics is None:
            return None
        if not metrics.session_id:
            metrics.session_id = self._turn_session_id
        metrics.source_type = SourceType.LOCAL_MIC
        # A request made while a turn is active remains pending until this
        # boundary.  The values below are immutable for the life of the turn.
        if self._cognition_turn_idle():
            self._cognition_test_session.apply_if_idle()
        session = self._cognition_test_session.snapshot()
        from neuro_voice.cognition.rollout import CognitionRolloutResolver
        resolved = CognitionRolloutResolver.resolve(self._cfg, session=session)
        metrics.effective_cognition_enabled = resolved.effective_enabled
        metrics.cognition_rollout_mode = resolved.resolved_mode
        metrics.cognition_requested_rollout_mode = resolved.requested_mode
        metrics.cognition_execution_path = resolved.execution_path
        metrics.cognition_activation_source = resolved.activation_source
        metrics.cognition_config_fingerprint = resolved.config_fingerprint
        metrics.cognition_session_epoch = session.epoch
        metrics.transport = "LOCAL"
        if self._mind is not None:
            with contextlib.suppress(Exception):
                metrics.bind_persona(self._mind.persona_context)
        return self._turns.begin(
            metrics, kind=kind or ConversationKind.NORMAL_CONVERSATION,
        )

    def _report_latency(self, metrics: TurnMetrics) -> None:
        report = metrics.report()
        if report:
            print(report)
            logging.getLogger("latency").info(report.replace("\n", " | "))
        items = []
        for start, end, label in _PAIRS:
            delta = metrics.delta_ms(start, end)
            if delta is not None:
                items.append({"label": label, "ms": round(delta)})
        for marker, label in (
            ("speaker_deferred", "Speaker deferred"),
            ("context_deferred", "Context deadline miss"),
        ):
            delta = metrics.delta_ms("stt_done", marker)
            if delta is not None:
                items.append({"label": label, "ms": round(delta)})
        summary = self._latency_window.add(metrics)
        if items:
            self._emit("latency", source="local", items=items,
                       summary=summary)

    async def _speak_loop(self, sentence_q: asyncio.Queue, metrics: TurnMetrics,
                          turn_emotion: dict | None = None,
                          turn_style: dict | None = None,
                          playback_tracker: PlayedTextTracker | None = None,
                          response_id: str | None = None,
                          enforce_conversation_contract: bool = True) -> None:
        """文を受け取り順次TTS合成して再生キューへ送る。感情で声色を変える。"""
        loop = asyncio.get_running_loop()
        # Keep short synthesis chunks cancellable, but do not hand the first
        # one to the output device until there is enough real PCM behind it.
        # This buffers *synthesized* audio, unlike SpeakerPlayback's short
        # queue wait, and therefore prevents VOICEVOX gaps mid-response.
        opening_buffer_s = max(0.25, float(self._cfg.get("tts.prebuffer_s", 1.0)))
        pending_playback: list[tuple[np.ndarray, int, Callable | None, Callable | None]] = []
        pending_duration_s = 0.0
        playback_started = False
        # **この呼び出しの中で 0 から数える**（Phase 7D ③）。
        # 同じ応答に発話ループが2つ立ち上がると両方が 0 番から始まるので、
        # そこで気づける。断片ごとに新しい ID を振ると常に通ってしまい、
        # 検査しているつもりで何も見ていないことになる。
        chunk_index = 0
        speak_job_id = str(response_id or metrics.turn_id)
        playback_session_id = (
            playback_tracker.playback_session_id if playback_tracker is not None
            else f"{speak_job_id}:playback"
        )

        def _queue_for_playback(item: tuple[np.ndarray, int, Callable | None, Callable | None]) -> None:
            item_audio, item_sr, item_start, item_complete = item
            self._playback.play(
                item_audio, item_sr, on_start=item_start, on_complete=item_complete,
                should_play=(
                    # ``respond_text`` can finish synthesizing before the
                    # playback queue has rendered its later chunks.  Those
                    # chunks are valid unless this response was explicitly
                    # cancelled by a substantive barge-in.
                    (lambda _response_id=response_id: not self._is_response_cancelled(_response_id))
                    if response_id is not None else None
                ),
            )

        try:
            while True:
                sentence = await sentence_q.get()
                finish_after_sentence = False
                if sentence is None:
                    if (
                        enforce_conversation_contract
                        and self._mind is not None
                        and response_id is not None
                    ):
                        sentence = self._mind.flush_conversation_sentence(
                            source="local", response_id=response_id,
                        )
                    if not sentence:
                        for item in pending_playback:
                            _queue_for_playback(item)
                        break
                    finish_after_sentence = True
                if response_id is not None and not self._is_response_active(response_id):
                    if finish_after_sentence:
                        pending_playback.clear()
                        break
                    continue
                if not self._speech_allowed_now(response_id, metrics=metrics):
                    continue
                sentence = self._without_internal_leak(sentence)
                if (
                    sentence and enforce_conversation_contract and not finish_after_sentence
                    and self._mind is not None and response_id is not None
                ):
                    sentence = self._mind.enforce_conversation_sentence(
                        sentence, source="local", response_id=response_id,
                    )
                if not sentence:
                    if finish_after_sentence:
                        for item in pending_playback:
                            _queue_for_playback(item)
                        pending_playback.clear()
                        break
                    continue
                self._turns.note_text(metrics, TextStage.SPEECH_GATE_PASSED, sentence)
                sentence_index = chunk_index
                verdict = self._turns.commit_chunk(metrics, speak_job_id, sentence_index)
                chunk_index += 1
                if self._turns.blocks(verdict):
                    logger.warning("同じ断片が2回TTSへ来たため飛ばします: %s", verdict.key)
                    if finish_after_sentence:
                        break
                    continue
                emo = turn_emotion.get("v") if turn_emotion else None
                style = turn_style.get("speech") if turn_style else None
                try:
                    audio, sr = await self._synthesize_with_delivery(loop, sentence, emo, style)
                except Exception as e:
                    summary = safe_exception_summary(e)
                    logger.error(
                        "TTS合成に失敗: error=%s text_chars=%d text_hash=%s",
                        summary, len(sentence), _delivery_hash(sentence),
                    )
                    self._emit("error", message=f"TTS合成エラー: {summary}")
                    if finish_after_sentence:
                        for item in pending_playback:
                            _queue_for_playback(item)
                        pending_playback.clear()
                        break
                    continue
                if response_id is not None and not self._is_response_active(response_id):
                    if finish_after_sentence:
                        pending_playback.clear()
                        break
                    continue
                chunk = None
                if playback_tracker is not None:
                    chunk = playback_tracker.queue_chunk(
                        sentence, audio=audio, sample_rate=sr, total_frames=len(audio),
                        segment_index=sentence_index,
                    )
                metrics.mark("tts_first")
                metrics.mark("tts_audio_ready")
                tts_job_id = str(getattr(verdict, "key", "") or f"{speak_job_id}#{sentence_index}")
                text_hash = _delivery_hash(sentence)
                audio_hash = _delivery_hash(audio)
                self._turns.note_tts_chunk(
                    metrics, speech_request_id=speak_job_id, tts_job_id=tts_job_id,
                    chunk_index=sentence_index, text_hash=text_hash, audio_hash=audio_hash,
                )
                play_key = chunk.chunk_id if chunk is not None else f"{speak_job_id}#{sentence_index}"
                delivery = self._turns.start_playback_segment(
                    metrics, speech_request_id=speak_job_id,
                    playback_session_id=playback_session_id, playback_id=play_key,
                    segment_index=sentence_index, tts_job_id=tts_job_id,
                    text_hash=text_hash, audio_hash=audio_hash,
                )
                if not delivery.accepted:
                    logger.warning("再生配送を拒否: %s", play_key)
                    if finish_after_sentence:
                        break
                    continue

                def _on_play_start(_chunk_id=(chunk.chunk_id if chunk is not None else ""),
                                   _play_key: str = play_key,
                                   _sentence: str = sentence) -> None:
                    self._turns.mark_playback_segment_started(metrics, _play_key)
                    fallback_state = getattr(metrics, "_cognitive_fallback_state", None)
                    if fallback_state is not None:
                        fallback_state.playback_started = True
                    metrics.mark("play_start")
                    self._turns.note_text(metrics, TextStage.PLAYBACK_STARTED, _sentence)
                    self._turn_manager.assistant_speaking()
                    if playback_tracker is not None and _chunk_id:
                        playback_tracker.started(_chunk_id)

                def _on_complete(played_frames: int, total_frames: int, completed: bool,
                                 _sentence: str = sentence, _play_key: str = play_key,
                                 _chunk_id: str = (chunk.chunk_id if chunk is not None else "")) -> None:
                    self._turns.complete_playback_segment(metrics, _play_key, completed=completed)

                    def _record() -> None:
                        if playback_tracker is None:
                            return
                        if _chunk_id:
                            playback_tracker.completed(_chunk_id, played_frames, total_frames, completed)
                        else:
                            playback_tracker.played(_sentence, played_frames / max(total_frames, 1))
                        if not completed:
                            playback_tracker.interrupted("playback_cancelled")
                        self._emit("assistant_playback", **playback_tracker.snapshot())

                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(_record)

                item = (audio, sr, _on_play_start, _on_complete)
                if playback_started:
                    _queue_for_playback(item)
                    if finish_after_sentence:
                        break
                    continue
                pending_playback.append(item)
                pending_duration_s += len(audio) / max(sr, 1)
                if pending_duration_s >= opening_buffer_s:
                    for queued_item in pending_playback:
                        _queue_for_playback(queued_item)
                    pending_playback.clear()
                    playback_started = True
                if finish_after_sentence:
                    for queued_item in pending_playback:
                        _queue_for_playback(queued_item)
                    pending_playback.clear()
                    break
        finally:
            self._turns.seal_playback_session(metrics, playback_session_id)

    # ---------- テキストモード ----------

    async def run_text(self) -> None:
        """マイクなしのテキスト入力モード (動作確認用)。音声出力は行う。"""
        self._loop = asyncio.get_running_loop()
        self._playback.start()
        print("💬 テキストモード (exit で終了)")
        try:
            while True:
                line = await self._loop.run_in_executor(None, input, "\nYou> ")
                line = line.strip()
                if not line:
                    continue
                if line.lower() in {"exit", "quit"}:
                    break
                await self.respond_text(line)
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._playback.stop()
