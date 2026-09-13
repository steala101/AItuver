"""Discord ボイスチャンネル連携。

グループ通話に参加して全員の声を聞き分け (Discordはユーザーごとに音声が
分離されているため誰の発話かが確実に分かる)、名前を呼びながら会話する。

必要パッケージ:
    pip install "discord.py[voice]" discord-ext-voice-recv

セットアップ:
    1. https://discord.com/developers/applications でBotを作成
    2. Bot タブ → MESSAGE CONTENT INTENT を ON
    3. トークンを .env の DISCORD_BOT_TOKEN に設定
    4. OAuth2 → URL Generator → scope: bot / 権限: View Channels,
       Send Messages, Connect, Speak でサーバーへ招待
    5. python run.py --discord で起動し、VCに入ってから !join
"""
from __future__ import annotations

from typing import Any

import asyncio
import base64
import hashlib
import logging
import re
import time
from pathlib import Path
from uuid import uuid4
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np

from neuro_voice.llm.prompt_layout import insert_before_user_turn
from neuro_voice.discord_bridge.audio import (
    UtteranceSegmenter,
    discord_to_16k,
    to_discord_pcm,
)
from neuro_voice.discord_bridge.direct_receiver import DirectDAVEReceiver
from neuro_voice.autonomy import (
    AutonomousActionSystem, AutonomyEvent, AutonomyEventType,
    AutonomyHeartbeatScheduler, PrivacyScope,
)
from neuro_voice.dialogue import (
    ConversationEvent, ConversationEventType, ConversationOrchestrator, InitiativePolicy,
)
from neuro_voice.memory.interaction import InteractionClassifier, SpeechIntent
from neuro_voice.discord_bridge.music import (
    MusicPlayer,
    extract_music_reference_candidate,
    is_music_reference,
    make_mixed_source,
    now_playing_context,
    parse_music_command,
    plan_music_intent,
    should_execute_music_command,
)
from neuro_voice.utils.emotion import EmotionTagParser
from neuro_voice.utils.errors import safe_exception_summary
from neuro_voice.cognition.turn_integrity import CommitKind, TextStage
from neuro_voice.utils.latency import LatencyWindow, SourceType, TurnMetrics, _PAIRS
from neuro_voice.dialogue.leakage import leak_markers, strip_internal_leak
from neuro_voice.utils.textseg import SentenceSegmenter, strip_think
from neuro_voice.tts.style import StyleManager
from neuro_voice.realtime import PlayedTextTracker, TurnManager, response_is_active
from neuro_voice.memory.conversation import InterruptedTurn

logger = logging.getLogger(__name__)

_FRAME_BYTES = 3840  # 20ms @ 48kHz stereo int16

# サーバー再招待に必要な権限 (View Channels + Send Messages + Connect + Speak)
_INVITE_PERMISSIONS = 1024 | 2048 | 1048576 | 2097152  # = 3148800


def client_id_from_token(token: str) -> str | None:
    """Botトークンの先頭セグメントをbase64デコードしてClient ID(=BotのユーザーID)を得る。

    Discordトークンは `<base64(user_id)>.<...>.<...>` の形式で、
    先頭部分がアプリ(Client) IDそのもの。接続していなくてもリンクを作れる。
    """
    import base64

    token = (token or "").strip()
    if not token:
        return None
    first = token.split(".", 1)[0]
    first += "=" * (-len(first) % 4)  # base64パディング補完
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            cid = decoder(first).decode("utf-8", "ignore").strip()
            if cid.isdigit():
                return cid
        except Exception:
            continue
    return None


def build_invite_url(client_id: str, permissions: int = _INVITE_PERMISSIONS) -> str:
    """サーバーへBotを(再)招待するOAuth2 URLを組み立てる。"""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&permissions={permissions}&scope=bot"
    )


def _import_discord():
    try:
        import discord
        from discord.ext import voice_recv  # discord-ext-voice-recv
        return discord, voice_recv
    except ImportError as e:
        raise RuntimeError(
            "Discord連携には追加パッケージが必要です:\n"
            '  pip install "discord.py[voice]" discord-ext-voice-recv\n'
            f"(詳細: {e})"
        ) from e


def make_queue_source(discord_mod):
    """TTS音声をためて 20ms ずつ Discord へ流す AudioSource を作る。

    discord.py の play() は discord.AudioSource のサブクラスしか受け付けない
    ため、遅延importしたモジュールから動的に継承して生成する。
    """

    class QueueAudioSource(discord_mod.AudioSource):
        """キューが空になって0.5秒経つと送信を終了する (発言インジケータが消える)。
        次のTTSが来たら DiscordBridge 側が play() で再開する。"""

        _GRACE_FRAMES = 25  # 20ms × 25 = 0.5秒だけ無音でつなぐ (文と文の隙間対策)

        def __init__(self):
            self._chunks: deque[bytes] = deque()
            self._leftover = b""
            self._empty = 0
            self._paused = False

        def write(self, pcm: bytes) -> None:
            self._chunks.append(pcm)
            self._empty = 0

        def clear(self) -> None:
            self._chunks.clear()
            self._leftover = b""

        def pause_immediately(self) -> None:
            self._paused = True

        def resume_from_pause(self) -> None:
            self._paused = False

        def discard_paused_audio(self) -> None:
            self._paused = False
            self.clear()

        @property
        def playing(self) -> bool:
            return bool(self._chunks) or bool(self._leftover)

        @property
        def pending_seconds(self) -> float:
            """Queued but unheard audio, in seconds.

            Continuation segments use this as backpressure so a long directive
            never synthesizes far ahead of what the channel is actually hearing.
            """
            queued = sum(len(chunk) for chunk in self._chunks) + len(self._leftover)
            return round(queued / max(1, _FRAME_BYTES) * 0.02, 3)

        def read(self) -> bytes:
            if self._paused:
                # AudioSource.read is called every 20 ms, so this gives the
                # Discord.py path the same immediate-silence behaviour as the
                # local SpeakerPlayback without consuming queued PCM.
                return b"\x00" * _FRAME_BYTES
            buf = self._leftover
            while len(buf) < _FRAME_BYTES and self._chunks:
                buf += self._chunks.popleft()
            if not buf:
                self._empty += 1
                if self._empty > self._GRACE_FRAMES:
                    return b""  # 再生終了 → 発言中表示が消える
                return b"\x00" * _FRAME_BYTES
            self._empty = 0
            if len(buf) < _FRAME_BYTES:
                buf += b"\x00" * (_FRAME_BYTES - len(buf))
            self._leftover = buf[_FRAME_BYTES:]
            return buf[:_FRAME_BYTES]

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            self._empty = 0

    return QueueAudioSource()


class DiscordBridge:
    """Discordボット本体。LLM/TTS/STT/Mind を共有して動く。"""

    def __init__(self, cfg, llm, tts, stt, mind, conv, on_event=None):
        self._discord, self._voice_recv = _import_discord()
        dpy_ver = str(getattr(self._discord, "__version__", "?"))
        # 2.7系はDiscord音声ゲートウェイv8(seq)対応で接続が安定する。
        # 2.6は即切断でVC出入りループになるため、2.7以降を互換とみなす。
        self._dpy_compatible = dpy_ver.startswith("2.7") or dpy_ver.startswith("2.8")
        logger.info("discord.py %s (音声受信互換: %s)", dpy_ver,
                    "OK" if self._dpy_compatible else "NG")
        self._dpy_ver = dpy_ver
        self._cfg = cfg
        self._llm = llm
        self._pronunciation_learning_hook = None
        self._tts_volume_hook = None
        self._last_assistant_text = ""
        # 話者ごとの声トーン解析 (numpyのみ・軽量)。単調さ解消と多人数会話で、
        # 誰がどんな声色で話したかをLLMへ渡すために使う。話者別にベースラインを
        # 持たせると、複数人でも各自の普段の声との差で判定できる。
        self._voice_tone_enabled = bool(cfg.get("emotion.voice_tone", True))
        self._prosody = {}  # speaker_key -> ProsodyAnalyzer (話者別ベースライン)
        # 疑似フルデュプレックス (フェーズ1+2+5)
        from neuro_voice.realtime.full_duplex import (
            AcousticEchoGuard, InterruptKeywordDetector,
        )
        self._fd_enabled = bool(cfg.get("full_duplex.enabled", True))
        self._fd_echo_enabled = bool(cfg.get("full_duplex.echo_guard.enabled", True))
        self._fd_echo_thr = float(cfg.get("full_duplex.echo_guard.similarity_threshold", 0.78))
        self._fd_echo_max_chars = int(cfg.get("full_duplex.echo_guard.max_chars", 8))
        self._fd_echo_guard = AcousticEchoGuard(
            window_s=float(cfg.get("full_duplex.echo_guard.reference_buffer_s", 6.0)))
        self._fd_timeout_s = max(0.3, float(cfg.get("full_duplex.soft_interrupt_timeout_ms", 1500)) / 1000)
        self._fd_kw_enabled = bool(cfg.get("full_duplex.keyword_interrupt_enabled", True))
        self._fd_detector = InterruptKeywordDetector(
            cfg.get("full_duplex.extra_interrupt_words") or [])
        self._fd_pause_started = 0.0   # pause開始時刻 (0=非pause)
        self._fd_pause_deadline = 0.0  # この時刻を過ぎたら自動再開 (取り残し防止)
        self._pause_token = 0          # pauseごとに発行。古いresumeが新pauseを解除しないため
        # フェーズ6: 発話終了判定 (途中結果の語尾で無音閾値を動的化)
        from neuro_voice.realtime.full_duplex import ConversationControlLayer, endpoint_hint
        self._fd_endpoint_hint = endpoint_hint
        self._fd_endpoint_enabled = bool(cfg.get("full_duplex.endpoint.enabled", True))
        self._fd_thinking_extra_s = float(cfg.get("full_duplex.endpoint.thinking_extra_ms", 700)) / 1000
        self._fd_final_reduce_s = float(cfg.get("full_duplex.endpoint.final_reduce_ms", 150)) / 1000
        self._fd_last_partial: dict[int, dict] = {}  # user_id → {gen,text,first,last,acked}
        # フェーズ7: ルールベース会話制御層 (聞き手相づち+繋ぎ発話)
        self._fd_control = None
        if bool(cfg.get("full_duplex.control_layer.enabled", True)):
            self._fd_control = ConversationControlLayer(
                listener_enabled=bool(cfg.get("full_duplex.control_layer.listener_backchannel", True)),
                listener_cooldown_s=float(cfg.get("full_duplex.control_layer.listener_cooldown_s", 8.0)),
                filler_enabled=bool(cfg.get("full_duplex.control_layer.thinking_filler", True)),
                filler_cooldown_s=float(cfg.get("full_duplex.control_layer.filler_cooldown_s", 20.0)),
                contextual_enabled=bool(cfg.get("full_duplex.control_layer.contextual_backchannel", True)),
                contextual_min_chars=int(cfg.get("full_duplex.control_layer.contextual_min_chars", 12)),
                contextual_cooldown_s=float(cfg.get("full_duplex.control_layer.contextual_cooldown_s", 18.0)),
            )
        recognition_history_s = max(5.0, float(cfg.get("music_recognition.capture_seconds", 12.0)))
        self._recent_input_audio: deque[np.ndarray] = deque(maxlen=max(1, int(recognition_history_s * 16000 / 960) + 3))
        self._tts = tts
        self._stt = stt
        self._mind = mind
        #: 1ターンを追う所（Phase 7D ②③）。**Local と同じ物を使う。**
        #: 片側だけに置くと「重複」が経路ごとに違う意味になる。
        from neuro_voice.cognition.turn_tracker import tracker_from_config

        self._turns = tracker_from_config(cfg)
        self._turn_session_id = uuid4().hex[:12]
        from neuro_voice.cognition.test_session import CognitionTestSession
        self._cognition_test_session = CognitionTestSession()
        self._cognitive_trace_writer = None
        self._trace_writer()
        self._conv = conv
        self._on_event = on_event
        self._connected = False
        self._client = None
        self._mention_mode = False
        self._auto_join = False        # 接続完了後に自動でVC参加する (🎧ボタン用)
        self._last_channel_id = None   # 最後に参加したVC (再参加用)
        self._respond_all = bool(cfg.get("discord.respond_all", False))
        self._wake_words = [str(w) for w in
                            (cfg.get("discord.wake_words") or ["ニューロ", "neuro", "ねうろ"])]
        # 返事のあとこの秒数は、呼びかけなしでも会話の続きとして反応する
        self._follow_up_s = float(cfg.get("discord.follow_up_s", 30))
        # 配信者本人(=マイク)の名前。通話ループバック(他人)の声紋照合から除外して
        # 他人が本人と誤判定されるのを防ぐ。マイク由来の識別で自己補正する。
        self._streamer_name = str(cfg.get("discord.streamer_name", "チビ") or "チビ").strip()
        self._echo_text = bool(cfg.get("discord.echo_text", False))
        self._prefix = str(cfg.get("discord.command_prefix", "!"))
        self._search = None  # DeepSearch (遅延生成。search.enabled のとき使用)
        from neuro_voice.search.contracts import SearchEvidenceCache
        self._search_evidence_cache = SearchEvidenceCache(
            ttl_seconds=600.0, clock=time.monotonic)
        # 継続依頼 (BehaviorDirective)。判断はMindが持つControllerが一元管理し、
        # ここはVCで区間を実際に喋る側だけを担当する。
        self._directive_runtime_obj = None
        self._active_directive = None
        # 音声認識の誤変換対策。False = 未構築 (None は「無効」を意味する)。
        self._repairer_obj = False
        self._stt_vocabulary = None
        self._active_transcript = None
        # Discord has an independent response path from the local pipeline.
        # Keep a lazy knowledge handle here; first-time discovery/import runs
        # outside the asyncio audio loop below.
        self._minecraft_knowledge = None
        self._local_music_probe = None  # GUI内ローカルプレイヤーが再生中か (GUIが登録)
        # Discord screen-share vision is a separate, lazy session.  DAVE is
        # audio-only in the current sidecar, so the first frame transport is
        # an OBS source and can later be replaced without changing dialogue.
        self._screen_share_session_obj = None
        self._game_companion_director = None
        self._pending_game_event = None
        self._game_commentary_task: asyncio.Task | None = None
        self._last_game_commentary_text = ""
        # 自己エコー対策 (ニューロ自身のTTSをループバックで拾わないため)
        self._external_busy = None  # ローカル側の発話中判定 (GUIが設定)
        self._recent_tts: deque[tuple[float, str]] = deque(maxlen=16)  # 直近に喋った文
        self._mute_tail = float(cfg.get("audio.loopback_mute_tail_s", 1.2))
        # Keep a dedicated Discord loopback alive while local TTS plays by
        # default. Enable this only if TTS itself reaches the capture endpoint.
        self._mute_loopback_on_speak = bool(cfg.get("audio.loopback_mute_on_speak", False))

        from pathlib import Path

        self._log_dir = Path(str(cfg.get("logging.dir", "logs")))
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._stt_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dc-stt")
        self._tts_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dc-tts")
        self._closed = False
        self._style_manager = StyleManager(
            user_emotion_threshold=float(cfg.get("style_manager.user_emotion_confidence_threshold", 0.75)),
        )
        self._segmenters: dict[int, UtteranceSegmenter] = {}
        self._users: dict[int, object] = {}
        self._heard: set[int] = set()  # 受信確認済みユーザー (診断表示用)
        self._utter_q: asyncio.Queue = asyncio.Queue(maxsize=3)
        self._partial_busy_users: set[int] = set()
        self._partial_generation: dict[int, int] = {}
        self._partial_explicit_calls: dict[int, int] = {}
        self._turn_tasks: set[asyncio.Task] = set()
        self._active_response_task: asyncio.Task | None = None
        self._active_response_id: str | None = None
        self._cancelled_response_ids: set[str] = set()
        self._active_playback_tracker: PlayedTextTracker | None = None
        self._active_response_user_text = ""
        self._barge_paused = False
        self._direct_playback_until = 0.0
        self._last_user_activity = time.monotonic()
        self._last_proactive_at = 0.0
        self._initiative_policy = InitiativePolicy(cfg)
        self._latency_window = LatencyWindow()
        self._interaction_classifier = InteractionClassifier(
            acknowledgement_max_chars=int(cfg.get("barge_in.acknowledgement_max_chars", 12)),
        )
        self._turn_manager = TurnManager(
            lambda transition: self._emit(
                "turn_state", previous=transition.previous.value,
                state=transition.current.value, reason=transition.reason,
            ),
        )
        # TTS(QueueAudioSource)と音楽を1本に合成するソース (発話中は音楽を小さくする)
        self._tts_source = make_queue_source(self._discord)
        self._source = make_mixed_source(self._discord, self._tts_source)
        self._music: MusicPlayer | None = None  # 音楽プレイヤー (遅延生成)
        self._direct_music_task: asyncio.Task | None = None
        self._last_music_query: str | None = None
        self._humming_armed_until = 0.0
        self._humming_play_after = False
        self._vc = None
        self._direct_channel = None
        self._direct_receiver: DirectDAVEReceiver | None = None
        self._direct_active = False
        self._direct_recovery_task: asyncio.Task | None = None
        self._text_channel = None
        self._responding = False
        self._last_reply_at = 0.0
        self._autonomy = AutonomousActionSystem(cfg)
        self._autonomy_heartbeat = AutonomyHeartbeatScheduler(
            cfg, self._on_autonomy_heartbeat, name="discord",
        )
        self._autonomy_last_human_speech_ended_at = time.monotonic()
        self._autonomy_last_status: dict[str, Any] = {
            "evaluated_at": 0.0,
            "final_action": "未評価",
            "suppression_reasons": ["最初のHeartbeat待ち"],
        }
        self._active_autonomy_task: asyncio.Task | None = None
        self._pending_group_response: asyncio.Task | None = None
        self._pending_group_generation = 0
        # Latest-finalized-input wins.  Delayed STT/LLM work from an earlier
        # utterance cannot acquire the response slot after a newer request.
        self._latest_input_epoch = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._voice_members: dict[int, str] = {}
        #: **意味が変わった**（Phase 6D）。true は「挨拶を検討してよい」。
        #: 「必ず固定文を喋る」ではない。
        self._greet_on_join = bool(cfg.get("discord.greet_on_join", True))
        #: 挨拶を認知経路（Action Selector → Speech Gate）へ通すか。
        #: **false の間は従来の固定文。** 二重に喋らせないため排他。
        self._cognitive_greeting_enabled = bool(
            cfg.get("discord.cognitive_greeting_enabled", False))
        self._farewell_opportunity_enabled = bool(
            cfg.get("discord.farewell_opportunity_enabled", False))
        #: 同時入室を見分けるための、直前の入室時刻。
        self._join_times: dict[int, float] = {}
        # This layer only decides whether/how to respond.  It never changes
        # the DAVE receive path or treats a Discord account as a person.
        self._conversation = ConversationOrchestrator(
            cfg,
            wake_words=self._wake_words,
            assistant_name=self._persona_name(),
            on_event=self._on_conversation_event,
        )

        # ループバック受信 (DAVE E2EEでvoice-recv受信不可のときの代替)
        _mode = str(cfg.get("audio.input_mode", "mic") or "mic").lower()
        self._receive_mode = str(cfg.get("discord.receive_mode", "loopback") or "loopback").lower()
        self._direct_requested = self._receive_mode in ("direct_dave", "auto")
        self._use_loopback = _mode in ("loopback", "mix") and not self._direct_requested
        self._loopback_mix_mic = _mode == "mix"
        self._loop_cap = None          # LoopbackCapture
        self._loop_q: asyncio.Queue | None = None
        self._loopback_task = None     # VAD/セグメント タスク
        self._loopback_gate_task = None  # 発話中の自己音声ミュート同期タスク

    # ---------- 外部制御 (GUI用) ----------

    def _emit(self, event_type: str, **data) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, data)
        except Exception:
            logger.exception("Discordイベント通知でエラー")

    def _on_conversation_event(self, event: ConversationEvent) -> None:
        """Expose decisions without writing full transcripts in normal logs."""
        data = event.summary(
            include_text=bool(self._cfg.get("conversation.debug_transcripts", False)),
        )
        # ``_emit`` already uses ``event_type`` as its first argument.  Keep
        # the payload field distinct so telemetry cannot abort a voice turn.
        data["conversation_event_type"] = data.pop("event_type")
        self._emit("conversation_event", **data)

    def _autonomy_decide(self, event_type: AutonomyEventType, *, text: str = "",
                          payload: dict | None = None, speaker_id: str | None = None,
                          expires_in_s: float | None = None):
        """Run only the inexpensive policy here; generation remains cancellable."""
        now = time.monotonic()
        try:
            privacy_scope = PrivacyScope(self._mind.autonomy_privacy_scope(text)) if text else PrivacyScope.CURRENT_CONVERSATION
        except (AttributeError, ValueError):
            privacy_scope = PrivacyScope.CURRENT_CONVERSATION
        event = AutonomyEvent(
            event_type, "discord", payload={"text": text, **(payload or {})},
            speaker_id=speaker_id, timestamp=now,
            privacy_scope=privacy_scope,
            expires_at=(now + expires_in_s if expires_in_s is not None else None),
        )
        decision = self._autonomy.publish(
            event, group=self._current_voice_human_count() > 1,
            human_speaking=event_type is AutonomyEventType.HUMAN_SPEECH_STARTED,
            assistant_busy=self._assistant_audio_active(),
        )
        self._emit("autonomy_decision", action=decision.action_type.value,
                   reason=decision.reason_code, utility=decision.utility,
                   autonomy_event_type=event_type.value, source="discord")
        return decision

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and not self._client.is_closed()

    @property
    def channel_name(self) -> str:
        channel = self._voice_channel()
        if channel is not None:
            return str(channel.name)
        return ""

    def _voice_channel(self):
        """Discord.py channel for UI/membership, independent of audio backend."""
        return getattr(getattr(self, "_vc", None), "channel", None) or getattr(self, "_direct_channel", None)

    def voice_status(self) -> dict:
        """遠隔ランチャー用: 接続とVCの現在状況をまとめて返す (読み取り専用)。"""
        channel = self._voice_channel()
        guild = getattr(channel, "guild", None) if channel is not None else None
        return {
            "connected": self.is_connected,
            "in_voice": channel is not None,
            "guild_id": str(getattr(guild, "id", "") or ""),
            "guild_name": str(getattr(guild, "name", "") or ""),
            "channel_id": str(getattr(channel, "id", "") or ""),
            "channel_name": str(getattr(channel, "name", "") or ""),
        }

    # ---------- 遠隔PWA用: ペルソナ / 話者 / こころ ----------

    def list_personas(self) -> dict:
        """利用可能なペルソナ一覧とアクティブなキーを返す。"""
        from neuro_voice.memory.persona import get_active_persona

        presets = self._cfg.section("persona").get("presets") or {}
        active, _ = get_active_persona(self._cfg)
        items = []
        for key, pdef in (presets.items() if isinstance(presets, dict) else []):
            pdef = pdef or {}
            items.append({
                "key": key,
                "name": str(pdef.get("name", key)),
                "character": str(pdef.get("character", "")),
                "active": key == active,
            })
        return {"personas": items, "active": active}

    def switch_persona(self, key: str) -> dict:
        """ペルソナを切り替える (システムプロンプト・Mind・話者を反映)。"""
        from neuro_voice.memory.persona import build_system_prompt, get_active_persona

        presets = self._cfg.section("persona").get("presets") or {}
        if not isinstance(presets, dict) or key not in presets:
            return {"ok": False, "message": "そのペルソナは存在しません"}
        self._cfg.set("persona.active", key)
        _, pdef = get_active_persona(self._cfg)
        name = str(pdef.get("name", key))
        # 会話のシステムプロンプトを差し替え
        try:
            self._conv.set_system_prompt(build_system_prompt(self._cfg))
        except Exception:
            logger.exception("システムプロンプト更新に失敗")
        # 記憶・人格をそのペルソナへ切り替え
        if self._mind is not None:
            try:
                self._mind.switch_persona(key, name)
            except Exception:
                logger.exception("Mindのペルソナ切替に失敗")
        # ペルソナ別に保存された話者へ切り替え
        try:
            backend = str(getattr(self._tts, "backend_name", "voicevox"))
            if backend == "style_bert_vits2":
                model = str(pdef.get("style_bert_vits2_model")
                            or self._cfg.get("tts.style_bert_vits2.model_name", "") or "")
                sid = int(pdef.get("style_bert_vits2_speaker_id",
                                   self._cfg.get("tts.style_bert_vits2.speaker_id", 0)) or 0)
                if hasattr(self._tts, "set_speaker"):
                    self._tts.set_speaker(f"{model}::{sid}")
            elif backend == "voicevox" and hasattr(self._tts, "set_speaker"):
                self._tts.set_speaker(int(pdef.get("voicevox_speaker",
                                          self._cfg.get("tts.voicevox.speaker", 3)) or 3))
        except Exception:
            logger.exception("ペルソナ話者の切替に失敗")
        logger.info("遠隔操作: ペルソナを %s に切替", name)
        return {"ok": True, "message": f"ペルソナを「{name}」に切り替えたよ", "active": key, "name": name}

    def list_tts_speakers(self) -> dict:
        """現在のTTSバックエンドの話者一覧と選択中を返す。"""
        tts = self._tts
        if tts is None or not hasattr(tts, "list_speakers"):
            return {"ok": False, "message": "この音声は話者選択に対応していません", "speakers": []}
        try:
            speakers = []
            for sp in tts.list_speakers():
                item = dict(sp)
                key = str(item.get("key", item.get("id", "")))
                item["id"] = key
                item["key"] = key
                speakers.append(item)
            return {"ok": True, "speakers": speakers, "current": str(getattr(tts, "speaker", ""))}
        except Exception as e:
            return {"ok": False, "message": f"話者一覧を取得できません: {e}", "speakers": []}

    def set_tts_speaker(self, speaker_id: str) -> dict:
        """話者を切り替える。"""
        tts = self._tts
        if tts is None or not hasattr(tts, "set_speaker"):
            return {"ok": False, "message": "この音声は話者選択に対応していません"}
        try:
            backend = str(getattr(tts, "backend_name", "voicevox"))
            if backend == "style_bert_vits2":
                tts.set_speaker(str(speaker_id))
            else:
                tts.set_speaker(int(speaker_id))
            return {"ok": True, "message": "話者を変更したよ", "current": str(getattr(tts, "speaker", ""))}
        except Exception as e:
            return {"ok": False, "message": f"話者を変更できません: {e}"}

    def mind_status(self) -> dict:
        """こころステータス (人格・気分・なかよし度・記憶) を返す。"""
        if self._mind is None:
            return {"enabled": False}
        try:
            return self._mind.status()
        except Exception as e:
            logger.exception("こころステータス取得に失敗")
            return {"enabled": False, "error": str(e)}

    # ---------- 遠隔PWA用: STT / TTS 設定 ----------

    def stt_status(self) -> dict:
        """音声認識の「実際に動いているデバイス」と要求値を返す。

        config が cuda でも、CUDA初期化に失敗すると内部で無言のうちに cpu へ
        フォールバックする。ここでは requested(設定) と actual(実稼働) の両方を
        返すので、PWAで『GPUのはずがCPUで動いている』を検知できる。
        """
        stt = self._stt
        requested = str(self._cfg.get("stt.device", "cuda") or "cuda").lower()
        actual = str(getattr(stt, "device", "") or "").lower()
        return {
            "ok": stt is not None,
            "requested": requested,
            "device": actual or requested,
            "on_gpu": actual.startswith("cuda"),
            "fell_back": bool(actual) and requested.startswith("cuda") and actual == "cpu",
            "model": str(getattr(stt, "model_name", "") or ""),
            "compute_type": str(self._cfg.get("stt.compute_type", "") or ""),
            "switching": bool(getattr(self, "_stt_switching", False)),
        }

    def reload_stt(self, device: str) -> dict:
        """STTを指定デバイス(cuda/cpu)で再読込して差し替える。

        重いので呼び出し側(HTTPスレッド)で同期実行する。旧モデルを先に解放して
        から新規ロードし、ホストRAM/VRAMの二重確保を避ける。
        """
        device = str(device or "").strip().lower()
        if device not in ("cuda", "cpu"):
            return {"ok": False, "message": "device は cuda か cpu を指定してください"}
        if getattr(self, "_stt_switching", False):
            return {"ok": False, "message": "音声認識を切り替え中です"}
        self._stt_switching = True
        try:
            import gc

            from neuro_voice.stt.factory import build_stt

            old = self._stt
            self._stt = None
            del old
            gc.collect()
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
            new_stt = build_stt(self._cfg, device=device)
            self.set_stt(new_stt)
            actual = str(getattr(new_stt, "device", device) or device).lower()
            # 保存するのは「ユーザーが要求したデバイス」。実際にCPUへ落ちても要求値を
            # 残すことで、(a)次回起動でGPUを再挑戦でき、(b)requested≠actualの差から
            # フォールバックを検知して警告表示できる。
            self._cfg.persist("stt.device", device)
            fell_back = device.startswith("cuda") and actual == "cpu"
            logger.info("遠隔操作: 音声認識 要求=%s 実稼働=%s%s",
                        device.upper(), actual.upper(), " (GPU失敗→CPU)" if fell_back else "")
            if fell_back:
                msg = "GPUで起動できずCPUのままです。CUDA/cuDNNやVRAMを確認してね"
            else:
                msg = f"音声認識を {actual.upper()} に切り替えたよ"
            return {"ok": not fell_back, "message": msg, **self.stt_status()}
        except Exception as e:
            logger.exception("STT切替に失敗")
            return {"ok": False, "message": f"音声認識の切替に失敗: {e}"}
        finally:
            self._stt_switching = False

    def tts_status(self) -> dict:
        """読み上げ音声の現在のエンジン・音量・実稼働デバイスを返す。"""
        tts = self._tts
        engine = str(getattr(tts, "backend_name",
                             self._cfg.get("audio_output.backend", "voicevox")) or "voicevox")
        try:
            volume = float(getattr(tts, "volume", self._cfg.get("tts.output_volume", 1.0)) or 1.0)
        except Exception:
            volume = 1.0
        # SBV2は実際に載っているデバイス(_device)を報告。VOICEVOXはエンジン側依存で
        # ここからは制御しないため device は空にする。
        device = ""
        if engine == "style_bert_vits2":
            inner = getattr(tts, "inner", None)
            device = str(getattr(inner, "_device", None)
                         or self._cfg.get("tts.style_bert_vits2.device", "") or "").lower()
        return {
            "ok": tts is not None,
            "engine": engine,
            "volume": round(volume, 3),
            "device": device,
            "on_gpu": device.startswith("cuda"),
            "switching": bool(getattr(self, "_tts_switching", False)),
        }

    def runtime_info(self) -> dict:
        """STT・TTS・LLM の「今どこで動いているか」をまとめて返す (状態表示用)。"""
        backend = str(self._cfg.get("llm.backend", "") or "")
        try:
            llm_model = str(getattr(self._llm, "model", "") or "")
        except Exception:
            llm_model = ""
        if not llm_model:
            llm_model = str(self._cfg.get(f"llm.backends.{backend}.model", "") or "")
        llm = {"backend": backend, "model": llm_model}
        # Ollamaなら実際のGPU/CPU配分(VRAM載り具合)を取得。ここがGPU<100%だと
        # 生成がCPUへ退避=応答直前のCPU跳ね+遅延の主因。
        if backend == "ollama":
            base_url = str(self._cfg.get("llm.backends.ollama.base_url",
                                         "http://localhost:11434/v1") or "")
            ps = self._ollama_ps(base_url, llm_model)
            if ps:
                llm.update(ps)
        info = {
            "ok": True,
            "stt": self.stt_status(),
            "tts": self.tts_status(),
            "llm": llm,
        }
        gpu = self._gpu_mem()
        if gpu:
            info["gpu"] = gpu
        return info

    @staticmethod
    def _gpu_mem() -> dict | None:
        from neuro_voice.utils.gpu import gpu_memory

        return gpu_memory()

    @staticmethod
    def _ollama_ps(base_url: str, model: str | None = None) -> dict | None:
        """Ollamaの /api/ps を読み、ロード中モデルのGPU/CPU配分を返す。

        gpu_percent = size_vram / size * 100。100%未満はCPUへ退避しており、
        生成が遅く・CPU使用率が跳ねる。未ロード(keep_alive切れ)なら None。
        """
        import json
        import urllib.request

        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        try:
            with urllib.request.urlopen(root + "/api/ps", timeout=2.0) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
        models = data.get("models") or []
        if not models:
            return None
        chosen = None
        if model:
            for m in models:
                if str(m.get("name", "")) == model or str(m.get("model", "")) == model:
                    chosen = m
                    break
        chosen = chosen or models[0]
        size = float(chosen.get("size") or 0)
        vram = float(chosen.get("size_vram") or 0)
        pct = int(round(vram / size * 100)) if size > 0 else None
        return {
            "gpu_percent": pct,
            "fully_gpu": pct is not None and pct >= 99,
            "size_mb": int(size / 1_000_000),
            "vram_mb": int(vram / 1_000_000),
        }

    def set_tts_volume(self, volume) -> dict:
        """読み上げ音量を設定する (0.0〜2.0)。"""
        tts = self._tts
        try:
            value = float(volume)
        except (TypeError, ValueError):
            return {"ok": False, "message": "音量の値が不正です"}
        value = max(0.0, min(2.0, value))
        if tts is not None and hasattr(tts, "set_volume"):
            value = float(tts.set_volume(value))
        self._cfg.persist("tts.output_volume", value)
        return {"ok": True, "message": f"声の音量を{int(round(value * 100))}%にしたよ", "volume": round(value, 3)}

    def set_tts_engine(self, engine: str) -> dict:
        """読み上げエンジンを切り替える (voicevox / style_bert_vits2)。

        使わなくなった側のサーバーを落としてVRAM/メモリを解放する。重いので
        呼び出し側(HTTPスレッド)で同期実行する。
        """
        aliases = {"sbv2": "style_bert_vits2"}
        engine = aliases.get(str(engine).strip().lower(), str(engine).strip().lower())
        allowed = {"voicevox", "style_bert_vits2", "null"}
        if engine not in allowed:
            return {"ok": False, "message": f"未対応の読み上げ音声です: {engine}"}
        tts = self._tts
        if tts is None:
            return {"ok": False, "message": "TTSを初期化中です。少し待ってね"}
        from neuro_voice.tts.switchable import SwitchableTTS
        if not isinstance(tts, SwitchableTTS):
            return {"ok": False, "message": "実行中TTSがライブ切替に対応していません"}
        if getattr(self, "_tts_switching", False):
            return {"ok": False, "message": "読み上げ音声を切り替え中です"}
        current = str(getattr(tts, "backend_name", "voicevox") or "voicevox")
        if engine == current:
            return {"ok": True, "changed": False, "message": "すでにその音声だよ", "engine": engine}
        self._tts_switching = True
        try:
            from neuro_voice.tts.factory import build_tts

            self._cfg.persist("audio_output.backend", engine)
            new_tts = build_tts(self._cfg)
            tts.replace(new_tts, engine)
            self._shutdown_unused_tts_server(keep=engine)
            logger.info("遠隔操作: 読み上げ音声を %s に切替", engine)
            return {"ok": True, "changed": True, "message": f"読み上げ音声を{engine}に切り替えたよ", "engine": engine}
        except Exception as e:
            logger.exception("TTS切替に失敗")
            self._cfg.persist("audio_output.backend", current)
            return {"ok": False, "message": f"読み上げ音声の切替に失敗: {e}", "engine": current}
        finally:
            self._tts_switching = False

    def _shutdown_unused_tts_server(self, *, keep: str) -> None:
        """使わない方のTTSサーバーを落としてVRAM/メモリを解放する。

        自分で起動したサーバーだけが対象 (各 shutdown 関数が内部で判定)。
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

    @property
    def _in_voice(self) -> bool:
        return self._voice_channel() is not None

    def voice_members(self) -> list[dict[str, str | int]]:
        """Human members currently in the voice channel where the bot is present."""
        channel = self._voice_channel()
        if channel is not None:
            self._refresh_voice_members(channel)
        return [{"id": member_id, "name": name} for member_id, name in self._voice_members.items()]

    def _refresh_voice_members(self, channel=None) -> None:
        channel = channel or self._voice_channel()
        if channel is None:
            self._voice_members.clear()
        else:
            self._voice_members = {
                int(member.id): str(getattr(member, "display_name", None) or member.name)
                for member in getattr(channel, "members", [])
                if not getattr(member, "bot", False)
            }
        self._emit(
            "discord_members", channel=self.channel_name,
            count=len(self._voice_members), members=self.voice_members_snapshot(),
        )
        self.note_voice_presence()

    def note_voice_presence(self) -> dict:
        """**実経路D**: いま実際にVCに居る人 → 世界状態。

        ここを接続点にしたのは、入退室イベントを1件ずつ追うより
        **現在の一覧で毎回合わせ直す方が取りこぼしが無い**から。
        切断中に起きた出入りはイベントが来ないし、再接続直後は
        前回の参加状態を信じてはいけない（第5条: 接続が真実の持ち主）。

        Bot は `_voice_members` の時点で既に除外されている。
        **自分を人間の参加者として数えない。**
        """
        try:
            result = self.initiative().reconcile_discord(
                [{"id": member_id, "name": name}
                 for member_id, name in self._voice_members.items()],
                channel=self.channel_name, session_id=self._session_id())
        except Exception:  # noqa: BLE001 — 世界状態の失敗で会話を止めない（第17条）
            logger.debug("Discord参加状態の取り込みに失敗", exc_info=True)
            return {"joined": [], "left": [], "present": 0}
        # **参加状態の変化から直接発話しない。** 注意層へ入れるだけにして、
        # 話すかどうかは Action Selector と Speech Gate に決めさせる。
        # ここで喋ると、入退室のたびに必ず声が出る実装になる。
        changed = len(result.get("joined", ())) + len(result.get("left", ()))
        if changed:
            with contextlib_suppress():
                from neuro_voice.cognition.attention import AttentionEventType

                self.note_attention_event(
                    str(AttentionEventType.PARTICIPANT_CHANGE),
                    summary=f"VCの人数が{result.get('present', 0)}人になった",
                    topic_ids=("participants",),
                    salience=.35, novelty=.5, urgency=.0,
                    dedup=f"presence:{result.get('present', 0)}", ttl=10.0)
        return result

    def _session_id(self) -> str:
        channel_id = getattr(self._voice_channel(), "id", "")
        return f"discord:{channel_id}" if channel_id else "discord"

    def voice_members_snapshot(self) -> list[dict[str, str | int]]:
        return [{"id": member_id, "name": name} for member_id, name in self._voice_members.items()]

    def _current_voice_human_count(self) -> int:
        """Refresh a stale Discord.py cache before address detection."""
        if not self._voice_members and self._voice_channel() is not None:
            self._refresh_voice_members()
        return len(self._voice_members)

    async def _greet_voice_member(self, member) -> None:
        """入ってきた人への反応。

        `greet_on_join` は **「必ず挨拶する」ではなく「挨拶を検討してよい」**。

        以前はここで固定文を直接TTSへ流していた。相手が話している最中でも、
        危険警告の途中でも、5人が同時に入っても、必ず同じ声が出た。
        いまは候補を作って他の候補と同じ列に並べ、**Action Selector と
        Speech Gate に決めさせる**。黙る方が自然なら黙る。

        `cognitive_greeting_enabled` が下りている間は従来どおり固定文。
        **二重に挨拶しないよう、経路はどちらか一方だけ通る。**
        """
        if not self._greet_on_join or not self._in_voice or self._loop is None:
            return
        name = str(getattr(member, "display_name", None) or getattr(member, "name", ""))
        if not name:
            return
        self._emit("status", message=f"👋 {name} さんがVCに参加しました")
        if self._cognitive_greeting_enabled:
            self._note_greeting_opportunity(member)
            return
        await self._speak(self._loop, f"{name}さん、こんにちは。")

    def _note_greeting_opportunity(self, member) -> bool:
        """挨拶の**候補**を作る。**ここでは喋らない。**"""
        try:
            user_id = int(getattr(member, "id", -1) or -1)
        except (TypeError, ValueError):
            return False
        if user_id < 0 or getattr(member, "bot", False):
            return False
        name = str(getattr(member, "display_name", None)
                   or getattr(member, "name", "") or "")
        with contextlib_suppress():
            return bool(self.initiative().note_participant_joined(
                transport_key=f"discord:{user_id}", display_name=name,
                channel=self.channel_name,
                group_size=max(1, len(self._voice_members)),
                joined_together=len(self._recent_joins()) or 1,
                session_id=self._session_id()))
        return False

    def _note_farewell_opportunity(self, member) -> bool:
        """出ていった人への一言の**候補**。**毎回は作らない。**

        `切断後に宛先不在のTTSを遅れて再生しない`——候補には短い期限が
        付いており、期限切れは発話直前の再検証で落ちる。
        """
        if not self._farewell_opportunity_enabled:
            return False
        try:
            user_id = int(getattr(member, "id", -1) or -1)
        except (TypeError, ValueError):
            return False
        if user_id < 0 or getattr(member, "bot", False):
            return False
        self._join_times.pop(user_id, None)
        with contextlib_suppress():
            return bool(self.initiative().note_participant_left(
                transport_key=f"discord:{user_id}",
                display_name=str(getattr(member, "display_name", None)
                                 or getattr(member, "name", "") or ""),
                group_size=max(1, len(self._voice_members)),
                session_id=self._session_id()))
        return False

    def _note_direct_voice_identity(self, user, profile) -> None:
        """**実経路F**: Discord の user ID を正本に、声紋を根拠として積む。

        `discord_direct` では誰が喋ったかを Discord が保証している。
        その音声から出た声紋は、**その人のものだと言い切れる**——
        マイク由来やループバックとは違い、推測が入らない。

        ただし**1回で人物を確定しない。** 声紋は数%揺れるので、
        たまたま高く出た1回で確定すると、以後の関係値が別人へ流れる。
        """
        from neuro_voice.cognition.identity import TransportIdentity, VoiceIdentity

        try:
            user_id = int(getattr(user, "id", -1) or -1)
        except (TypeError, ValueError):
            return
        if user_id < 0 or not profile or int(profile.get("id", -1)) < 0:
            return
        runtime = self.initiative()
        transport = TransportIdentity(
            provider="discord", transport_user_id=str(user_id),
            display_name=str(getattr(user, "display_name", None)
                             or getattr(user, "name", "") or ""),
            authoritative=True)
        voice = VoiceIdentity(
            voiceprint_id=str(profile.get("id")),
            confidence=float(profile.get("score", 0.0) or 0.0),
            model_version=str(self._cfg.get("mind.voiceprint.model", "") or ""))
        runtime.note_voice_sample(
            transport=transport, voice=voice, event_id=f"direct:{user_id}")
        # **誰と話したか**を残す。別れの一言はここが根拠。
        resolution = runtime.resolve_identity(
            transport=transport, voice=voice, event_id=f"direct:{user_id}")
        if resolution.person_id:
            runtime.note_talked_with(resolution.person_id)

    def _recent_joins(self, window: float = 4.0) -> list[int]:
        """直前に一緒に入ってきた人。**大人数の同時入室を見分ける。**"""
        now = time.monotonic()
        self._join_times = {
            key: at for key, at in getattr(self, "_join_times", {}).items()
            if now - at <= window}
        return list(self._join_times)

    def invite_url(self) -> str | None:
        """サーバーへの再招待URL。接続中ならBotの実IDを使い、未接続なら
        トークンからClient IDを導出して組み立てる (どちらでも動く)。"""
        client_id = None
        if self._client is not None and self._client.user is not None:
            client_id = str(self._client.user.id)
        if not client_id:
            client_id = client_id_from_token(str(self._cfg.get("discord.token", "") or ""))
        if not client_id:
            return None
        return build_invite_url(client_id)

    def set_llm(self, llm) -> None:
        self._llm = llm

    def runtime_diagnostics(self) -> dict[str, Any]:
        return {
            "connected": bool(self._in_voice),
            "participant_count": self._current_voice_human_count(),
            "proactive_enabled": self._autonomy.active,
            "response_active": self._responding,
            "autonomous_turn_alive": (
                self._active_autonomy_task is not None
                and not self._active_autonomy_task.done()
            ),
            "heartbeat": self._autonomy_heartbeat.health_status(),
            "autonomy": self._autonomy.snapshot(),
            "last_evaluation": dict(self._autonomy_last_status),
        }

    def set_proactive(self, enabled: bool, *, min_interval_s: float | None = None) -> None:
        """Update Discord autonomous-speech policy from the settings screen."""
        self._autonomy.configure(enabled=enabled)
        self._initiative_policy.configure(enabled=False, min_interval_s=min_interval_s)
        if not enabled:
            self._last_proactive_at = 0.0
        # The scheduler also advances the shared research queue before it
        # evaluates autonomous speech.  Keep it alive when speech autonomy is
        # OFF; the callback itself returns without speaking.
        if self._loop is not None:
            self._autonomy_heartbeat.resume()
            self._autonomy_heartbeat.start()
        self._emit(
            "initiative_settings", enabled=self._autonomy.active,
            min_interval_s=min_interval_s,
        )

    def set_stt(self, stt) -> None:
        """共有しているSTTを差し替える。次のDiscord発話から反映する。"""
        self._stt = stt

    async def _prewarm_tts(self) -> None:
        """Prepare VOICEVOX while joining, before the first VC greeting arrives."""
        ensure_ready = getattr(self._tts, "ensure_ready", None)
        if not callable(ensure_ready):
            return
        self._emit("status", message="VOICEVOX の発話準備を確認中...")
        try:
            loop = asyncio.get_running_loop()
            ready = await loop.run_in_executor(self._tts_ex, ensure_ready)
        except Exception:
            logger.exception("VOICEVOX prewarm failed during Discord VC join")
            ready = False
        if not ready:
            self._emit("status", message="VOICEVOX の準備に失敗しました。発話時に再試行します")

    def set_local_music_probe(self, fn) -> None:
        """GUI内ローカルプレイヤーの再生状態を返す関数を登録する。

        VC参加中の発話はDiscord経路で処理されるため、ローカル再生中の
        「止めて」等をGUIプレイヤーへ転送するのに必要。
        """
        self._local_music_probe = fn

    def set_pronunciation_learning_hook(self, fn) -> None:
        """Route spoken reading corrections to the shared TTS dictionary."""
        self._pronunciation_learning_hook = fn

    def set_tts_volume_hook(self, fn) -> None:
        """Route AI-voice volume commands to the shared GUI/TTS controller."""
        self._tts_volume_hook = fn

    def set_external_busy(self, fn) -> None:
        """ローカル側 (GUIパイプライン) が発話中かを返す関数を登録する。

        ローカルTTSがPC既定出力 (=ループバック対象) で鳴ると自分の声を
        拾ってしまうため、その間も取り込みをミュートする。
        """
        self._external_busy = fn

    @property
    def uses_loopback_mic(self) -> bool:
        """mixモードで本人マイクを取り込んでいるか (🎤ミュートの対象になる)。"""
        return (
            self._use_loopback
            and self._loopback_mix_mic
            and self._loop_cap is not None
        )

    @property
    def mic_muted(self) -> bool:
        return bool(self._loop_cap.mic_muted) if self._loop_cap is not None else False

    def set_mic_muted(self, muted: bool) -> None:
        """ループバック(mix)の本人マイク成分をミュート/解除する。通話相手の声は聞いたまま。"""
        if self._loop_cap is not None:
            self._loop_cap.mic_muted = bool(muted)

    def update_settings(self, respond_all=None, wake_words=None, echo_text=None,
                        follow_up_s=None, greet_on_join=None, receive_mode=None) -> None:
        if respond_all is not None:
            self._respond_all = bool(respond_all)
        if wake_words is not None:
            self._wake_words = [str(w).strip() for w in wake_words if str(w).strip()]
        if echo_text is not None:
            self._echo_text = bool(echo_text)
        if follow_up_s is not None:
            try:
                self._follow_up_s = float(follow_up_s)
            except (TypeError, ValueError):
                pass
        # The event-driven addressing layer owns the actual decision.  Keep it
        # in sync with the settings screen immediately, rather than waiting for
        # a Discord reconnect or application restart.
        self._conversation.configure_addressing(
            respond_all=respond_all,
            wake_words=wake_words,
            follow_up_s=follow_up_s,
        )
        if greet_on_join is not None:
            self._greet_on_join = bool(greet_on_join)
        if receive_mode is not None:
            mode = str(receive_mode).lower()
            if mode in ("direct_dave", "loopback", "auto"):
                self._receive_mode = mode
                self._direct_requested = mode in ("direct_dave", "auto")
                audio_mode = str(self._cfg.get("audio.input_mode", "mic") or "mic").lower()
                self._use_loopback = audio_mode in ("loopback", "mix") and not self._direct_requested

    def _get_screen_share_session(self):
        if self._screen_share_session_obj is None:
            from neuro_voice.vision.discord_share import DiscordScreenShareSession

            self._screen_share_session_obj = DiscordScreenShareSession(
                self._cfg,
                self._llm,
                is_busy=lambda: self._assistant_audio_active() or self._responding,
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

    async def _on_game_observation(self, observation) -> None:
        """Turn meaningful OBS Minecraft changes into sparse Discord reactions."""
        session = self._screen_share_session_obj
        if (
            session is None
            or session.source_kind != "minecraft_obs"
            or not bool(self._cfg.get("video.game_commentary_enabled", False))
            or not bool(self._cfg.get("vision.proactive_reaction_enabled", True))
        ):
            return
        if self._game_companion_director is None:
            from neuro_voice.games import GameCompanionDirector

            self._game_companion_director = GameCompanionDirector(
                minimum_confidence=float(
                    self._cfg.get("video.game_commentary_confidence", .55)
                ),
                minimum_gap_sec=float(self._cfg.get(
                    "vision.proactive_reaction_cooldown_sec",
                    self._cfg.get("video.game_commentary_cooldown_sec", 6.0),
                )),
                urgent_gap_sec=float(
                    self._cfg.get("video.game_commentary_urgent_gap_sec", 1.25)
                ),
                duplicate_ttl_sec=float(
                    self._cfg.get("video.game_commentary_duplicate_ttl_sec", 30.0)
                ),
                minimum_importance=float(
                    self._cfg.get("vision.important_event_threshold", .55)
                ),
                reaction_probability=float(
                    self._cfg.get("vision.proactive_reaction_probability", .85)
                ),
            )
        event = self._game_companion_director.observe(observation)
        if event is None:
            logger.debug(
                "Discord visual reaction suppressed: %s",
                self._game_companion_director.last_suppression_reason,
            )
            return
        autonomy = self._autonomy_decide(
            AutonomyEventType.GAME_EVENT,
            payload={
                "summary": event.summary,
                "priority": event.priority,
                "kind": event.kind,
            },
            expires_in_s=float(
                self._cfg.get("autonomy.speech_event_max_age_ms", 5000)
            ) / 1000,
        )
        if not autonomy.should_speak:
            logger.info(
                "Discord game companion event kept as state only: action=%s",
                autonomy.action_type.value,
            )
            return
        pending = self._pending_game_event
        if (
            pending is None
            or event.priority >= pending.priority
            or event.created_at > pending.created_at + 2.0
        ):
            self._pending_game_event = event
        if self._game_commentary_task is None or self._game_commentary_task.done():
            task = asyncio.create_task(
                self._discord_game_companion_loop(),
                name="discord-minecraft-companion-commentary",
            )
            self._game_commentary_task = task
            self._turn_tasks.add(task)
            task.add_done_callback(self._on_turn_task_done)

    async def _discord_game_companion_loop(self) -> None:
        """Wait for a natural opening, then use the normal cancellable VC path."""
        current_task = asyncio.current_task()
        ttl = max(
            2.0,
            float(self._cfg.get("video.game_commentary_pending_ttl_sec", 10.0)),
        )
        try:
            while self._pending_game_event is not None:
                event = self._pending_game_event
                self._pending_game_event = None
                deadline = event.created_at + ttl
                while self._assistant_audio_active():
                    newer = self._pending_game_event
                    if newer is not None and newer.priority >= event.priority:
                        event = newer
                        self._pending_game_event = None
                        deadline = event.created_at + ttl
                    if time.monotonic() >= deadline:
                        event = None
                        break
                    await asyncio.sleep(.1)
                if (
                    event is None
                    or time.monotonic() >= deadline
                    or not self._in_voice
                ):
                    continue
                from neuro_voice.games import companion_prompt

                knowledge_query = " ".join(
                    item for item in (
                        event.summary, *event.evidence, event.suggested_help,
                    ) if item
                )
                knowledge = await self._minecraft_knowledge_context(knowledge_query)
                prompt = companion_prompt(
                    event,
                    previous_comment=self._last_game_commentary_text,
                    knowledge=knowledge or "",
                )
                reply = await self._run_game_companion_turn(prompt)
                if reply:
                    self._last_game_commentary_text = reply
        except asyncio.CancelledError:
            raise
        finally:
            if self._game_commentary_task is current_task:
                self._game_commentary_task = None

    async def _run_game_companion_turn(self, prompt: str) -> str:
        if not self._in_voice or self._assistant_audio_active():
            return ""
        channel_id = str(getattr(self._voice_channel(), "id", "") or "") or None
        self._responding = True
        self._active_response_task = asyncio.current_task()
        await self._reset_playback_for_new_response("game_companion_turn")
        self._conversation.response_started(source="discord", channel_id=channel_id)
        reply = ""
        interrupted = False
        try:
            reply = await self._generate_and_speak(
                self._persona_name() or "ポッポ", prompt, proactive=True,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if self._active_response_task is asyncio.current_task():
                self._active_response_task = None
                self._responding = False
            self._conversation.response_finished(
                source="discord",
                interrupted=interrupted,
                channel_id=channel_id,
                asked_question=reply.rstrip().endswith(("?", "？")),
                assistant_text=reply,
            )
        if reply:
            self._last_assistant_text = reply
            self._emit(
                "initiative_reply", reply=reply, reason="game_companion_event"
            )
        return reply

    async def stop(self) -> None:
        """VCから退出し、Discordから切断する。"""
        if self._closed:
            return
        self._closed = True
        active = self._active_response_task
        if active is not None and active is not asyncio.current_task() and not active.done():
            active.cancel()
        for task in list(self._turn_tasks):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        self._pending_game_event = None
        await self._cmd_leave(silent=True)
        if self._screen_share_session_obj is not None:
            await self._screen_share_session_obj.close()
            self._screen_share_session_obj = None
        if self._direct_receiver is not None:
            # Leave only disconnects the voice channel.  Terminate the local
            # Node gateway too so a later app launch never connects to it.
            await self._direct_receiver.stop()
            self._direct_receiver = None
        if self._client is not None and not self._client.is_closed():
            await self._client.close()
        if self._cognitive_trace_writer is not None:
            self._cognitive_trace_writer.close(timeout=0.75)
        for executor in (self._stt_ex, self._tts_ex):
            with contextlib_suppress():
                executor.shutdown(wait=False, cancel_futures=True)
        self._connected = False
        self._emit("discord_state", connected=False, channel="")

    # ---------- 起動 ----------

    def _make_client(self, use_content_intent: bool):
        discord = self._discord
        intents = discord.Intents.default()
        intents.message_content = use_content_intent  # 特権インテント (ポータルでON必須)
        intents.voice_states = True
        client = discord.Client(intents=intents)
        self._client = client
        self._mention_mode = not use_content_intent

        @client.event
        async def on_ready():
            self._connected = True
            how = (
                f"@{client.user.name} join とメンション付きで発言"
                if self._mention_mode else f"{self._prefix}join と発言"
            )
            logger.info("Discord にログイン: %s (mention_mode=%s)",
                        client.user, self._mention_mode)
            print(f"✅ Discord ログイン完了: {client.user}")
            print(f"   VCに入ってから {how}してください")
            self._emit("status", message=(
                f"✅ Discord にログインしました ({client.user})。"
                f"VCに入ってからテキストチャンネルで {how}してください"
            ))
            self._emit("discord_state", connected=True, channel="")
            if self._auto_join:
                asyncio.create_task(self._try_auto_join())

        @client.event
        async def on_voice_state_update(member, before, after):
            current_channel = self._voice_channel()
            current_id = getattr(current_channel, "id", None)
            before_id = getattr(getattr(before, "channel", None), "id", None)
            after_id = getattr(getattr(after, "channel", None), "id", None)
            if current_id is not None and (before_id == current_id or after_id == current_id):
                joining = (after_id == current_id and before_id != current_id
                           and not getattr(member, "bot", False)
                           and (client.user is None or member.id != client.user.id))
                if joining:
                    # 同時入室を見分けるため、人数を更新する**前に**印を付ける。
                    self._join_times[int(member.id)] = time.monotonic()
                leaving = (before_id == current_id and after_id != current_id
                           and not getattr(member, "bot", False)
                           and (client.user is None or member.id != client.user.id))
                self._refresh_voice_members(current_channel)
                if leaving:
                    self._note_farewell_opportunity(member)
                if joining:
                    asyncio.create_task(self._greet_voice_member(member))
            # 自分がVCから外部要因で切断された (キック・チャンネル削除など)
            if client.user is None or member.id != client.user.id:
                return
            if before.channel is not None and after.channel is None and self._in_voice:
                logger.info("VCから外部切断されました (キック等): %s", before.channel.name)
                self._stop_loopback()
                self._vc = None
                self._direct_active = False
                self._direct_channel = None
                self._refresh_voice_members()
                self._segmenters.clear()
                self._heard.clear()
                self._source.clear()
                self._emit("status", message=(
                    "⚠ VCから切断されました (キックされた?)。🎧ボタンで再参加できます"
                ))
                self._emit("discord_state", connected=self.is_connected, channel="")

        @client.event
        async def on_message(message):
            if message.author.bot:
                return
            # メンション部分 (<@123…>) を除いてコマンド判定
            content = re.sub(r"<@!?\d+>", "", message.content or "").strip().lower()
            if content in (f"{self._prefix}join", "join", "参加"):
                await self._cmd_join(message)
            elif content in (f"{self._prefix}leave", "leave", "退出", "ばいばい"):
                await self._cmd_leave(message)

        return client

    async def start(self) -> None:
        token = str(self._cfg.get("discord.token", "") or "")
        if not token or token.startswith("${"):
            raise RuntimeError(
                "Discord トークンが未設定です。.env に DISCORD_BOT_TOKEN を設定してください"
            )
        self._loop = asyncio.get_running_loop()
        self._autonomy_heartbeat.start()
        worker = asyncio.create_task(self._respond_worker())
        watchdog = asyncio.create_task(self._segment_watchdog())
        proactive = asyncio.create_task(self._proactive_loop())
        try:
            try:
                await self._make_client(use_content_intent=True).start(token)
            except self._discord.PrivilegedIntentsRequired:
                # Developer Portal で MESSAGE CONTENT INTENT がOFF
                # → メンション必須モードで自動リトライ (メンション付きメッセージは特権なしで読める)
                logger.warning("MESSAGE CONTENT INTENT が無効。メンション方式で再接続します")
                self._emit("status", message=(
                    "⚠ Developer Portal で「MESSAGE CONTENT INTENT」がOFFのため、"
                    "メンション方式で接続します。コマンドは「@Bot名 join」のように"
                    "メンション付きで発言してください。通常方式にするには "
                    "discord.com/developers/applications → あなたのApp → Bot → "
                    "MESSAGE CONTENT INTENT をONにして、再接続してください"
                ))
                await self._make_client(use_content_intent=False).start(token)
        finally:
            await self._autonomy_heartbeat.stop()
            worker.cancel()
            watchdog.cancel()
            proactive.cancel()

    # ---------- コマンド / GUI操作 ----------

    def list_voice_channels(self) -> list[dict]:
        """参加可能なVC一覧 (GUIのドロップダウン用)。IDはJSの精度対策で文字列。"""
        out = []
        if self._client is None or not self._connected:
            return out
        try:
            for guild in self._client.guilds:
                for ch in guild.voice_channels:
                    humans = sum(1 for m in ch.members if not m.bot)
                    out.append({
                        "id": str(ch.id),
                        "name": f"{guild.name} / {ch.name}",
                        "members": humans,
                    })
        except Exception:
            logger.exception("VC一覧の取得に失敗")
        out.sort(key=lambda c: -c["members"])
        return out

    async def join_channel(self, channel_id: str | None = None) -> dict:
        """GUIからのVC参加。channel_id 未指定なら人がいるVCへ自動参加。"""
        if self._client is None or not self._connected:
            return {"ok": False, "message": "Discordに接続していません"}
        channel = None
        if channel_id:
            channel = self._client.get_channel(int(channel_id))
            if channel is None or not hasattr(channel, "voice_states"):
                return {"ok": False, "message": "そのVCが見つかりません (⟳で一覧を更新してください)"}
        else:
            for guild in self._client.guilds:
                for ch in guild.voice_channels:
                    if any(not m.bot for m in ch.members):
                        channel = ch
                        break
                if channel is not None:
                    break
            if channel is None:
                return {"ok": False, "message": (
                    "人がいるVCが見つかりません。先にVCへ入るか、チャンネルを選択してください"
                )}
        # 応答テキストの書き込み先 (書ける最初のチャンネル)
        text = self._text_channel
        if text is None:
            try:
                me = channel.guild.me
                candidates = [channel.guild.system_channel, *channel.guild.text_channels]
                for tch in candidates:
                    if tch is not None and tch.permissions_for(me).send_messages:
                        text = tch
                        break
            except Exception:
                text = None
        return await self._join_vc(channel, text_channel=text)

    async def leave(self) -> dict:
        """GUIからのVC退出。"""
        if not self._in_voice:
            return {"ok": False, "message": "VCに参加していません"}
        result = await self._cmd_leave(silent=True)
        return result if isinstance(result, dict) else {"ok": True}

    def request_auto_join(self) -> None:
        """接続完了後に自動でVCへ参加させる (🎧ボタン用)。接続済みなら即実行。"""
        self._auto_join = True
        if self._connected and self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._try_auto_join())
            )

    async def _try_auto_join(self) -> None:
        if not self._auto_join:
            return
        self._auto_join = False
        # 前回のVC → だめなら人がいるVCへ
        result = {"ok": False}
        if self._last_channel_id:
            result = await self.join_channel(str(self._last_channel_id))
        if not result.get("ok"):
            result = await self.join_channel(None)
        if not result.get("ok"):
            self._emit("status", message=f"VC自動参加できませんでした: {result.get('message', '')}")

    async def _join_direct_dave(self, channel) -> None:
        """Join through the Node/Dysnomia DAVE backend, not voice-recv.

        The Discord.py client remains responsible for text/UI/membership.  The
        Node process owns the single Discord voice session and returns decoded
        audio per Discord account over localhost IPC.
        """
        if self._direct_receiver is None:
            self._direct_receiver = self._new_direct_receiver()
        await self._direct_receiver.join(channel.guild.id, channel.id)
        self._direct_channel = channel
        self._direct_active = True
        self._refresh_voice_members(channel)
        self._emit("discord_receive_status", mode="direct_dave", ready=True,
                   message="DAVEで復号したDiscord音声をアカウント別に受信しています")

    def _new_direct_receiver(self) -> DirectDAVEReceiver:
        token = str(self._cfg.get("discord.token", "") or "")
        return DirectDAVEReceiver(
            token, self._enqueue_utterance,
            on_partial=self._enqueue_partial,
            on_audio=self._remember_direct_audio,
            on_speech_start=lambda _speaker: self._on_discord_speech_start(),
            on_speech_discarded=lambda _speaker: self._resume_discord_barge_if_pending(),
            on_event=self._on_direct_receiver_event,
            endpoint_hold=lambda account_id: self._fd_endpoint_extra(
                int(account_id) if str(account_id).isdigit() else 0),
            # A per-run port avoids stale sidecars from a previous app
            # process hijacking the new session and delaying VC join.
            port=0,
        )

    def _on_direct_receiver_event(self, event_type: str, **data) -> None:
        """Forward sidecar diagnostics and recover a crashed local receiver."""
        self._emit(event_type, **data)
        if event_type != "direct_dave_disconnected":
            return
        if not self._direct_active or self._direct_channel is None or self._loop is None:
            return
        task = self._direct_recovery_task
        if task is None or task.done():
            self._direct_recovery_task = asyncio.create_task(
                self._recover_direct_dave(), name="discord-dave-recover",
            )

    async def _recover_direct_dave(self) -> None:
        """Rejoin after a sidecar crash without making the whole app exit."""
        channel = self._direct_channel
        previous = self._direct_receiver
        attempts = max(1, int(self._cfg.get("discord.direct_reconnect_attempts", 3)))
        if channel is None:
            return
        self._emit("discord_receive_status", mode="direct_dave", ready=False,
                   message="DAVE受信が切断されたため、VCへ再接続しています")
        if previous is not None:
            with contextlib_suppress():
                await previous.stop()
        for attempt in range(1, attempts + 1):
            if not self._direct_active or self._direct_channel is not channel:
                return
            if attempt > 1:
                await asyncio.sleep(min(4.0, float(attempt)))
            receiver = self._new_direct_receiver()
            self._direct_receiver = receiver
            try:
                await receiver.join(channel.guild.id, channel.id)
            except asyncio.CancelledError:
                await receiver.stop()
                raise
            except Exception as exc:
                logger.warning("DAVE sidecar recovery attempt %s/%s failed: %s", attempt, attempts, exc)
                with contextlib_suppress():
                    await receiver.stop()
                continue
            self._emit("discord_receive_status", mode="direct_dave", ready=True,
                       recovered=True, attempt=attempt,
                       message="DAVE受信を再接続しました")
            return
        self._direct_active = False
        self._direct_channel = None
        self._direct_receiver = None
        self._emit("discord_receive_status", mode="direct_dave", ready=False,
                   message="DAVE受信の再接続に失敗しました。ログを確認して再参加してください")
        self._emit("discord_state", connected=self.is_connected, channel="")

    async def _join_vc(self, channel, text_channel=None, message=None) -> dict:
        if self._vc is not None:
            await self._cmd_leave(silent=True)
        # キック等でこちらの状態と食い違った居残り接続があれば強制切断
        try:
            stale = getattr(channel.guild, "voice_client", None)
            if stale is not None:
                await stale.disconnect(force=True)
        except Exception:
            logger.debug("居残りVC接続の掃除に失敗", exc_info=True)
        if message is not None:
            text_channel = message.channel
        self._text_channel = text_channel
        try:
            if self._direct_requested:
                try:
                    await self._join_direct_dave(channel)
                except Exception as direct_error:
                    # Direct receive is intentionally fail-safe: the previous
                    # WASAPI route remains usable instead of leaving the bot
                    # connected but unable to hear anybody.
                    logger.exception("DAVE direct receive startup failed")
                    self._direct_active = False
                    self._direct_channel = None
                    if self._direct_receiver is not None:
                        with contextlib_suppress():
                            await self._direct_receiver.stop()
                        self._direct_receiver = None
                    self._emit("discord_receive_status", mode="loopback", ready=False,
                               message=f"DAVE直接受信を開始できないためループバックへ戻します: {direct_error}")
                    self._use_loopback = True
                else:
                    self._last_channel_id = channel.id
                    await self._prewarm_tts()
                    self._emit("discord_state", connected=True, channel=str(channel.name))
                    if self._echo_text and self._text_channel is not None:
                        with contextlib_suppress():
                            await self._text_channel.send(
                                f"🎧 {channel.name} に参加しました。DAVE直接受信で聞いています。"
                            )
                    return {"ok": True, "channel": str(channel.name), "receive_mode": "direct_dave"}
            vc = await channel.connect(cls=self._voice_recv.VoiceRecvClient)
            self._vc = vc
            self._refresh_voice_members(channel)
            if self._use_loopback:
                # Discord DAVE(E2EE) は voice-recv 側での受信実装が未対応のため、
                # ホストPCのDiscord出力をループバックで取得する。
                self._start_loopback()
            else:
                vc.listen(self._make_sink())
            await self._prewarm_tts()
            # 再生はTTS音声が来たときに _speak 側で開始する (常時送信しない)
        except Exception as e:
            logger.exception("VC音声の初期化に失敗")
            self._emit("error", message=f"Discord VC音声の初期化に失敗: {e}")
            if self._echo_text and self._text_channel is not None:
                with contextlib_suppress():
                    await self._text_channel.send(f"⚠ 音声の初期化に失敗したよ: {e}")
            return {"ok": False, "message": f"VC参加に失敗: {e}"}
        if self._echo_text and self._text_channel is not None:
            with contextlib_suppress():
                await self._text_channel.send(
                    f"🎧 {channel.name} に参加したよ。話しかけてね (退出は {self._prefix}leave)"
                )
        logger.info("VC参加: %s", channel.name)
        self._last_channel_id = channel.id
        self._emit("discord_state", connected=True, channel=str(channel.name))
        if not self._dpy_compatible:
            # 2.6以前は即切断でVC出入りループになる。2.7系を推奨。
            self._emit("error", message=(
                f"⚠ discord.py {self._dpy_ver} は接続が不安定になりやすいです "
                "(2.6以前=即切断でVC出入りループ)。ツールを閉じて "
                'pip install "discord.py[voice]==2.7.1" を実行してから再起動してください'
            ))
        return {"ok": True, "channel": str(channel.name)}

    async def _cmd_join(self, message) -> None:
        state = getattr(message.author, "voice", None)
        if state is None or state.channel is None:
            await message.channel.send("先にボイスチャンネルへ入ってから !join してね")
            return
        await self._join_vc(state.channel, message=message)

    async def _cmd_leave(self, message=None, silent: bool = False) -> dict:
        recovery = self._direct_recovery_task
        if recovery is not None and recovery is not asyncio.current_task() and not recovery.done():
            recovery.cancel()
        music_task = self._direct_music_task
        if music_task is not None and music_task is not asyncio.current_task() and not music_task.done():
            music_task.cancel()
        # Leaving the channel removes the audience the directive was for.
        # Keeping it alive would resume a monologue into an empty room.
        self._stop_directive("voice_session_ended")
        if self._screen_share_session_obj is not None:
            await self._screen_share_session_obj.stop(reason="voice_session_ended")
        self._stop_loopback()
        if self._music is not None:
            self._music.shutdown()
        if self._direct_active and self._direct_receiver is not None:
            try:
                await self._direct_receiver.leave()
            except Exception as exc:
                logger.exception("DAVE direct receive leave failed")
                detail = f"DAVE側のVC退出を確認できませんでした: {exc}"
                self._emit("discord_receive_status", mode="direct_dave", ready=False,
                           message=detail)
                return {"ok": False, "message": detail}
        self._direct_active = False
        self._direct_channel = None
        if self._vc is not None:
            try:
                self._vc.stop()
                await self._vc.disconnect()
            except Exception as exc:
                logger.exception("VC切断でエラー")
                return {"ok": False, "message": f"Discord VCの退出に失敗しました: {exc}"}
            self._vc = None
        self._refresh_voice_members()
        self._segmenters.clear()
        self._heard.clear()
        self._tts_source.discard_paused_audio()
        self._barge_paused = False
        self._source.clear()
        self._search_evidence_cache.clear(surface="discord")
        if message is not None and not silent:
            await message.channel.send("👋 またね")
        self._emit("discord_state", connected=self.is_connected, channel="")
        return {"ok": True}

    # ---------- ループバック受信 (DAVE代替: PC出力を取り込んでVCで応答) ----------

    def _start_loopback(self) -> None:
        """PC出力(Discord通話)をループバックで取り込み、VAD分割して応答キューへ流す。"""
        if self._loop_cap is not None:
            return
        try:
            from neuro_voice.audio.loopback import LoopbackCapture
        except Exception as e:
            self._emit("error", message=f"ループバック取り込みの初期化に失敗: {e}")
            return
        self._loop_q = asyncio.Queue(maxsize=64)
        try:
            self._loop_cap = LoopbackCapture(
                self._loop_q, self._loop, sample_rate=16000, frame_samples=512,
                loopback_device=(
                    self._cfg.get("audio.discord_capture_device")
                    or self._cfg.get("audio.loopback_device")
                ),
                mic_device=self._cfg.get("audio.input_device"),
                mix_mic=self._loopback_mix_mic,
            )
            self._loop_cap.start()
        except Exception as e:
            logger.exception("ループバック取り込みの開始に失敗")
            self._loop_cap = None
            self._emit("error", message=(
                f"ループバック取り込みを開始できませんでした: {e} "
                "(soundcard が必要。py -m pip install soundcard)"
            ))
            return
        self._loopback_task = asyncio.create_task(self._loopback_vad_loop())
        self._loopback_gate_task = asyncio.create_task(self._loopback_gate())
        mode = "本人マイク+通話" if self._loopback_mix_mic else "通話ループバック"
        sep = "本人と通話相手を声紋で区別" if self._loopback_mix_mic else "声紋で話者を識別"
        self._emit("status", message=f"🎧 {mode} を取り込んで応答します ({sep})")
        logger.info(
            "ループバック受信を開始 (mix_mic=%s, mute_during_local_tts=%s)",
            self._loopback_mix_mic,
            self._mute_loopback_on_speak,
        )

    def _stop_loopback(self) -> None:
        for task in (self._loopback_task, self._loopback_gate_task):
            if task is not None:
                task.cancel()
        self._loopback_task = None
        self._loopback_gate_task = None
        if self._loop_cap is not None:
            try:
                self._loop_cap.stop()
            except Exception:
                logger.debug("ループバック停止でエラー", exc_info=True)
            self._loop_cap = None
        self._loop_q = None

    async def _loopback_gate(self) -> None:
        """ニューロ発話中はループバックを止め、自分のTTSの二重認識(無限ループ)を防ぐ。

        発話終了後もしばらく(0.6s)はミュートを保つ。TTSがVCへ流れDiscordがPCで
        再生し直すまで遅延があり、その残響を自分の声として拾わないため。
        """
        last_busy = 0.0
        stale_since = None   # 再生されないTTS残データの検出
        restart_ok_at = 0.0  # 取り込み再起動のクールダウン
        try:
            while True:
                await asyncio.sleep(0.05)
                cap = self._loop_cap
                if cap is None:
                    continue
                ext_busy = False
                if self._external_busy is not None:
                    try:
                        ext_busy = bool(self._external_busy())
                    except Exception:
                        ext_busy = False
                busy = bool(self._responding or self._source.playing or ext_busy)
                if busy:
                    last_busy = time.monotonic()
                # 音楽再生中も通話ループバックを止める(自分が流す曲を聞き取らない)。
                # mixモードなら本人マイクは生きたままなので「止めて」等は言える。
                music_on = bool(getattr(self._source, "music_active", False))
                # 尻尾(mute_tail)は Discord往復の再生遅延ぶん長めに取る
                # With a dedicated Discord virtual output, muting here would
                # discard the remote user's speech and barge-in. Keep the old
                # echo guard only when the user explicitly enables it.
                cap.feedback_muted = self._mute_loopback_on_speak and (
                    busy or (time.monotonic() - last_busy < self._mute_tail)
                )

                # --- 自己修復1: プレイヤーが死んでいるのにTTS残データで busy が
                #     解けない状態を検出し、残骸を破棄して聞き取りを再開する ---
                if (self._tts_source.playing and not self._responding
                        and (self._vc is None or not self._vc.is_playing())):
                    if stale_since is None:
                        stale_since = time.monotonic()
                    elif time.monotonic() - stale_since > 2.0:
                        logger.warning("再生されないTTS残データを破棄 (聞き取り再開)")
                        self._tts_source.clear()
                        stale_since = None
                else:
                    stale_since = None

                # --- 自己修復2: 取り込みスレッドの死活監視。出力デバイス変更などで
                #     recordが返らなくなったら取り込みを再起動する ---
                now = time.monotonic()
                last = float(getattr(cap, "last_data", 0.0) or 0.0)
                if last and now - last > 5.0 and now >= restart_ok_at:
                    restart_ok_at = now + 30.0
                    logger.warning("ループバック取り込みが %.0f 秒停止 → 再起動します",
                                   now - last)
                    self._emit("status", message=(
                        "🎧 音声の取り込みが止まったため再起動します (出力デバイスが変わった?)"
                    ))
                    aloop = asyncio.get_running_loop()
                    try:
                        await aloop.run_in_executor(None, cap.stop)
                        await aloop.run_in_executor(None, cap.start)
                        self._emit("status", message="🎧 音声の取り込みを再開しました")
                    except Exception as e:
                        logger.exception("ループバック再起動に失敗")
                        self._emit("error", message=(
                            f"音声取り込みの再起動に失敗: {e}。"
                            "🎧ボタンでVCに入り直すと復旧できます"
                        ))
        except asyncio.CancelledError:
            pass

    async def _loopback_vad_loop(self) -> None:
        """タグ付きフレームを音源別(mic=本人 / loopback=通話相手)に Silero VAD で
        区切り、それぞれをクリーンな単独音声として応答キューへ流す。

        足し算した混合音では声紋が混ざり話者分離できないため、音源ごとに独立した
        VAD・発話バッファを持ち、確定した発話は _handle_utterance 内の声紋識別で
        本人/他人を判定させる。"""
        from collections import deque

        from neuro_voice.vad.silero import SileroVAD

        frame_ms = 512 / 16000 * 1000.0  # ≈32ms
        tuning = self._cfg.get("discord.loopback_vad", {}) or {}

        def _number(name: str, default: float) -> float:
            try:
                return float(tuning.get(name, default))
            except (AttributeError, TypeError, ValueError):
                return default

        loopback_tuning = {
            "start_threshold": _number("start_threshold", 0.35),
            "end_threshold": _number("end_threshold", 0.22),
            "start_frames": max(1, int(_number("start_frames", 2))),
            "end_silence_ms": max(frame_ms, _number("end_silence_ms", 700.0)),
            "min_speech_ms": max(frame_ms, _number("min_speech_ms", 180.0)),
            "pre_roll_frames": max(1, int(_number("pre_roll_ms", 384.0) / frame_ms)),
            "energy_threshold": max(0.0, _number("energy_threshold", 0.004)),
            "energy_end_threshold": max(0.0, _number("energy_end_threshold", 0.0025)),
            "max_utterance_ms": max(1000.0, _number("max_utterance_s", 25.0) * 1000.0),
        }
        # Keep the physical-mic path at its previous conservative settings.
        mic_tuning = {
            "start_threshold": 0.5, "end_threshold": 0.35, "start_frames": 3,
            "end_silence_ms": 500.0, "min_speech_ms": 350.0, "pre_roll_frames": 7,
            "energy_threshold": float("inf"), "energy_end_threshold": float("inf"),
            "max_utterance_ms": 30000.0,
        }

        def _new_state(source: str) -> dict:
            source_tuning = loopback_tuning if source == "loopback" else mic_tuning
            vad = SileroVAD(16000)
            vad.prob(np.zeros(512, dtype=np.float32))
            vad.reset()
            return {
                "vad": vad, "tuning": source_tuning,
                "pre": deque(maxlen=source_tuning["pre_roll_frames"]),
                "speaking": False, "voiced": 0, "silence": 0, "utter": [],
                "probs": [], "rms_values": [],
            }

        states: dict[str, dict] = {"mic": _new_state("mic"), "loopback": _new_state("loopback")}

        def _finalize(source: str, st: dict, reason: str) -> None:
            audio = np.concatenate(st["utter"]) if st["utter"] else np.zeros(0, dtype=np.float32)
            duration_ms = len(audio) * 1000.0 / 16000
            probs = st["probs"]
            rms_values = st["rms_values"]
            st["speaking"] = False
            st["voiced"] = st["silence"] = 0
            st["pre"].clear()
            st["utter"] = []
            st["probs"] = []
            st["rms_values"] = []
            st["vad"].reset()
            if duration_ms < st["tuning"]["min_speech_ms"]:
                logger.info("Discord VAD discarded short %s audio: %.0fms (%s)", source, duration_ms, reason)
                return
            logger.info(
                "Discord VAD finalized [%s]: %.0fms reason=%s vad=%.2f..%.2f rms=%.4f..%.4f",
                source, duration_ms, reason,
                min(probs, default=0.0), max(probs, default=0.0),
                min(rms_values, default=0.0), max(rms_values, default=0.0),
            )
            self._enqueue_utterance(_LoopbackSpeaker(source), audio)
        try:
            while True:
                item = await self._loop_q.get()
                # 後方互換: タグ無し(生ndarray)は通話相手として扱う
                if isinstance(item, tuple):
                    source, frame = item
                else:
                    source, frame = "loopback", item
                st = states.get(source) or states["loopback"]
                source = source if source in states else "loopback"
                opts = st["tuning"]
                prob = st["vad"].prob(frame)
                rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
                start_voice = prob >= opts["start_threshold"] or rms >= opts["energy_threshold"]
                keep_speaking = prob >= opts["end_threshold"] or rms >= opts["energy_end_threshold"]
                if not st["speaking"]:
                    st["pre"].append(frame)
                    st["voiced"] = st["voiced"] + 1 if start_voice else 0
                    if st["voiced"] >= opts["start_frames"]:
                        st["speaking"] = True
                        st["silence"] = 0
                        st["utter"] = list(st["pre"])
                        st["probs"] = [prob]
                        st["rms_values"] = [rms]
                    continue
                st["utter"].append(frame)
                st["probs"].append(prob)
                st["rms_values"].append(rms)
                st["silence"] = st["silence"] + 1 if not keep_speaking else 0
                duration_ms = len(st["utter"]) * frame_ms
                if st["silence"] * frame_ms >= opts["end_silence_ms"]:
                    _finalize(source, st, "silence")
                elif duration_ms >= opts["max_utterance_ms"]:
                    _finalize(source, st, "max_duration")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ループバックVADループでエラー")

    # ---------- 受信 (デコーダスレッドから呼ばれる) ----------

    def _make_sink(self):
        """自前デコードの頑丈な受信シンクを作る。

        ライブラリ標準の BasicSink はデコード失敗 (OpusError: corrupted stream)
        で受信スレッドごと死ぬ。opusパケットを直接受け取り自分でデコードし、
        壊れたパケットはスキップして受信を継続する。
        """
        discord = self._discord
        voice_recv = self._voice_recv
        bridge = self

        class RobustSink(voice_recv.AudioSink):
            def __init__(self):
                super().__init__()
                self._decoders: dict[int, object] = {}
                self._errors = 0

            def wants_opus(self) -> bool:
                return True  # デコードは自分でやる

            def write(self, user, data) -> None:
                try:
                    opus = getattr(data, "opus", None)
                    if user is None or not opus:
                        return
                    dec = self._decoders.get(user.id)
                    if dec is None:
                        dec = self._decoders[user.id] = discord.opus.Decoder()
                    pcm = dec.decode(opus, fec=False)
                    bridge._on_voice_pcm(user, pcm)
                except discord.opus.OpusError as e:
                    self._errors += 1
                    if self._errors <= 3 or self._errors % 100 == 0:
                        logger.warning("Opusデコード失敗をスキップ (%d回目): %s",
                                       self._errors, e)
                    if self._errors == 20:
                        bridge._emit("error", message=(
                            "Discord音声のデコード失敗が続いています。"
                            "discord.py のバージョン非互換の可能性があります "
                            '(pip install "discord.py[voice]==2.5.2" で入れ直してください)'
                        ))
                except Exception:
                    logger.exception("音声シンクでエラー")

            def cleanup(self) -> None:
                self._decoders.clear()

        return RobustSink()

    def _on_voice_pcm(self, user, pcm: bytes) -> None:
        try:
            if self._loop is None or not pcm:
                return
            # 最初の受信をツールへ通知 (受信経路が生きている確認用)
            uid = user.id
            if uid not in self._heard:
                self._heard.add(uid)
                name = getattr(user, "display_name", None) or getattr(user, "name", "?")
                logger.info("Discord音声の受信開始: %s", name)
                self._emit("status", message=f"🎙 {name} の声が届いています")
            pcm16k = discord_to_16k(pcm)
            self._loop.call_soon_threadsafe(self._feed, user, pcm16k)
        except Exception:
            logger.exception("音声パケット処理でエラー")

    def _remember_direct_audio(self, _speaker, chunk: np.ndarray) -> None:
        self._on_discord_speech_start()
        self._remember_input_audio(chunk)

    def _remember_input_audio(self, chunk: np.ndarray) -> None:
        self._recent_input_audio.append(np.asarray(chunk, dtype=np.float32).copy())

    def _recent_music_audio(self, seconds: float) -> np.ndarray:
        frames = list(self._recent_input_audio)
        if not frames:
            return np.empty(0, dtype=np.float32)
        limit = max(1, int(float(seconds) * 16000 / 960) + 1)
        return np.concatenate(frames[-limit:]).astype(np.float32, copy=False)

    def _feed(self, user, chunk: np.ndarray) -> None:
        # A still-speaking participant must also defer proactive speech; waiting
        # for VAD finalization would let a long sentence be mistaken for silence.
        self._last_user_activity = time.monotonic()
        self._remember_input_audio(chunk)
        seg = self._segmenters.get(user.id)
        if seg is None:
            seg = self._segmenters[user.id] = UtteranceSegmenter()
        self._users[user.id] = user
        was_speaking = seg.speaking
        utter = seg.feed(chunk)
        if not was_speaking and seg.speaking:
            self._on_discord_speech_start()
        elif was_speaking and not seg.speaking and utter is None:
            self._resume_discord_barge_if_pending()
        if utter is not None:
            self._enqueue_utterance(user, utter)
        else:
            partial = seg.current_audio(min_speech_ms=900)
            if partial is not None:
                self._enqueue_partial(user, partial)

    def _enqueue_partial(self, user, audio: np.ndarray) -> None:
        """Run throttled display-only STT without changing the response turn."""
        user_id = int(getattr(user, "id", 0) or 0)
        if user_id in self._partial_busy_users or self._responding or self._loop is None:
            return
        self._partial_busy_users.add(user_id)
        generation = self._partial_generation.get(user_id, 0)
        loop = self._loop
        # 途中認識は速度優先 (小さいbeam)。割り込み語検出だけが目的
        future = loop.run_in_executor(self._stt_ex, self._stt.transcribe_partial, audio, 16000)

        def _done(done) -> None:
            self._partial_busy_users.discard(user_id)
            if self._partial_generation.get(user_id, 0) != generation:
                return
            try:
                text = done.result()
            except Exception:
                logger.debug("Discord partial STT failed", exc_info=True)
                return
            if not text:
                return
            if self._conversation.partial_has_explicit_call(text):
                # Latch only this in-progress utterance.  The final STT still
                # decides all other intent; this merely prevents a leading
                # wake word from being lost between interim and final decode.
                self._partial_explicit_calls[user_id] = generation
            # フェーズ2: 「待って/ストップ」等の強い割り込み語は、発話の
            # 確定を待たずにSTT途中結果で即ハード割り込みする。
            # (executorスレッドからなのでイベントループへ安全に渡す)
            if (self._fd_enabled and self._fd_kw_enabled and self._barge_paused
                    and self._fd_detector.is_hard_interrupt(text)):
                interim_text = text

                def _kw_interrupt() -> None:
                    if self._barge_paused:  # 既に解決済みなら何もしない
                        logger.info("FD: 途中結果で割り込み語を検出 → 即時停止 (%s)",
                                    interim_text[:30])
                        asyncio.create_task(self._cancel_discord_response(
                            "keyword_hard_interrupt", interruption_text=interim_text))

                loop.call_soon_threadsafe(_kw_interrupt)
            # フェーズ6/7: 途中結果を記録 (発話終了判定の動的化・聞き手相づち用)
            now_mono = time.monotonic()
            state = self._fd_last_partial.get(user_id)
            if state is None or state.get("gen") != generation:
                state = {"gen": generation, "first": now_mono, "acked": -1}
                self._fd_last_partial[user_id] = state
            state["text"] = text
            state["last"] = now_mono
            self._conversation.events.publish(ConversationEvent(
                ConversationEventType.SPEECH_PARTIAL, getattr(user, "source", "discord"),
                text=text, metadata={"speaker_key": f"transport:{user_id}"},
            ))
            self._emit("discord_partial", name=(getattr(user, "display_name", None) or "?"), text=text)

        future.add_done_callback(_done)

    def _enqueue_utterance(self, user, utter: np.ndarray) -> None:
        """発話完了 → キューへ (満杯なら一番古いものを捨てる)。"""
        self._last_user_activity = time.monotonic()
        user_id = int(getattr(user, "id", 0) or 0)
        self._latest_input_epoch += 1
        input_epoch = self._latest_input_epoch
        active = self._active_response_task
        if active is not None and not active.done() and active is not asyncio.current_task():
            asyncio.create_task(
                self._cancel_discord_response("newer_finalized_input"),
                name=f"discord-stale-response-cancel:{input_epoch}",
            )
        generation = self._partial_generation.get(user_id, 0)
        partial_explicit_call = self._partial_explicit_calls.pop(user_id, None) == generation
        self._partial_generation[user_id] = generation + 1
        # 発話確定→この後STT+分類がpauseを解決する。タイムアウトで先に
        # 再開してしまわないよう、処理ぶんの猶予を与える
        if self._barge_paused and self._fd_pause_deadline > 0:
            self._fd_pause_deadline = time.monotonic() + max(self._fd_timeout_s, 4.0)
        item = (user, utter, partial_explicit_call, input_epoch)
        try:
            self._utter_q.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib_suppress():
                self._utter_q.get_nowait()
            with contextlib_suppress():
                self._utter_q.put_nowait(item)

    async def _segment_watchdog(self) -> None:
        """パケット途絶による発話終了の検出。

        Discord は無音の間パケットを送らない (無音抑制) ため、
        「無音フレームが続いたら終了」の判定が成立しない。
        一定時間パケットが来なければ発話終了として強制確定する。
        """
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            # フルデュプレックス: pauseの取り残し防止 (ソフト割り込みタイムアウト)。
            # 誰かがまだ発話中ならデッドラインを延ばす (発話→STT確定で解決されるため)。
            if self._fd_enabled and self._barge_paused and self._fd_pause_deadline > 0:
                if any(seg.speaking for seg in self._segmenters.values()):
                    self._fd_pause_deadline = now + self._fd_timeout_s
                elif now > self._fd_pause_deadline:
                    logger.warning("FD: pauseが%.1f秒解決されないため再生を自動再開します",
                                   self._fd_timeout_s)
                    self._resume_discord_barge_if_pending(
                        "soft_interrupt_timeout", pause_token=self._pause_token)
            for uid, seg in list(self._segmenters.items()):
                if seg.speaking and now - seg.last_activity >= max(
                        0.3, 0.7 + self._fd_endpoint_extra(uid)):
                    utter = seg.flush()
                    if utter is not None:
                        user = self._users.get(uid)
                        if user is not None:
                            self._enqueue_utterance(user, utter)

    # ---------- 応答ワーカー ----------

    async def _respond_worker(self) -> None:
        while True:
            item = await self._utter_q.get()
            if len(item) == 4:
                user, audio, partial_explicit_call, input_epoch = item
            else:  # compatibility with direct unit fixtures
                user, audio, partial_explicit_call = item
                input_epoch = self._latest_input_epoch
            # Keep receiving/STT active while an LLM response is streaming.
            # A later finalized utterance can then classify as an acknowledgement
            # or a real barge-in instead of waiting behind the old response.
            task = asyncio.create_task(self._handle_utterance(
                user, audio, partial_explicit_call=partial_explicit_call,
                input_epoch=input_epoch,
            ))
            self._turn_tasks.add(task)
            task.add_done_callback(self._on_turn_task_done)

    def _on_turn_task_done(self, task: asyncio.Task) -> None:
        self._turn_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Discord応答でエラー")

    def _assistant_audio_active(self) -> bool:
        """Return true while either generation or queued Discord audio is active."""
        active = self._active_response_task
        return bool(
            (active is not None and not active.done())
            or self._source.playing
            or (self._direct_active and time.monotonic() < self._direct_playback_until)
        )

    def _is_response_active(self, response_id: str) -> bool:
        return response_is_active(response_id, self._active_response_id, self._cancelled_response_ids)

    def _on_discord_speech_start(self) -> None:
        """Enter BARGE_IN_PENDING on the first incoming PCM packet."""
        self._autonomy.note_human_speech_started()
        self._autonomy_heartbeat.cancel_pending_tick()
        # A running continuation stops *producing* the moment a human starts,
        # but keeps what is already synthesized: backchannel versus real
        # interruption is not known until STT finishes (第7条).
        self._pause_directive_for_human(discard_audio=False)
        self._autonomy_decide(AutonomyEventType.HUMAN_SPEECH_STARTED, expires_in_s=2.0)
        autonomous = self._active_autonomy_task
        if autonomous is not None and not autonomous.done():
            # A self-initiated turn never gets priority over a human.  The
            # cancellation token is shared with the normal TTS path, so late
            # chunks cannot reappear after this point.
            autonomous.cancel()
        if self._barge_paused or not self._assistant_audio_active():
            return
        self._barge_paused = True
        self._pause_token += 1
        now = time.monotonic()
        self._fd_pause_started = now
        self._fd_pause_deadline = now + self._fd_timeout_s
        self._turn_manager.user_started(assistant_busy=True)
        self._tts_source.pause_immediately()
        if self._direct_active and self._direct_receiver is not None and self._loop is not None:
            self._loop.create_task(self._direct_receiver.pause_playback())
        # フルデュプレックス状態スナップショット (入力/出力/推論の独立状態を可視化)
        logger.info(
            "FD state: input=speech_started output=paused reasoning=%s",
            "generating" if (self._active_response_task is not None
                             and not self._active_response_task.done()) else "idle",
        )
        self._emit("speaker_paused", source="discord", reason="barge_in_pending")

    async def _reset_playback_for_new_response(self, reason: str) -> None:
        """新しい応答の音声を出す前に、再生経路をクリーンな状態へ戻す。

        soft interrupt の pause が (AI発話が自然終了した等で) 解決されないまま
        残っていると、新応答の音声を停止中のencoderへ書き込んでしまい、後で
        古い音声が遅れて再生される。新応答の開始時に必ず: pauseを解除し、
        溜まっている古い音声を破棄する (指示書§17)。
        """
        if not self._barge_paused:
            return
        self._log_fd_resolution("reset", reason)  # 遅延計測+pause時刻クリア
        self._barge_paused = False
        self._pause_token += 1  # 以後の古いresume/timeoutを無効化
        with contextlib_suppress():
            self._tts_source.discard_paused_audio()  # ローカル側の古い停止中音声を破棄
        if self._direct_active and self._direct_receiver is not None:
            with contextlib_suppress():
                # サイドカーの再生を「解除」する (破棄=stopは再生ストリームごと
                # 壊して以降無音になるため使わない)。溜まっていた僅かな旧音声は
                # 即座に流れて解消し、直後に新応答の音声が続く。
                await self._direct_receiver.resume_playback()
        self._direct_playback_until = 0.0
        logger.info("FD: 新応答開始前に再生をリセット (reason=%s)", reason)

    def _fd_endpoint_extra(self, user_id: int) -> float:
        """途中結果の語尾から、発話終了判定に足す追加待ち秒数を返す (フェーズ6)。"""
        if not (self._fd_enabled and self._fd_endpoint_enabled):
            return 0.0
        state = self._fd_last_partial.get(int(user_id))
        if not state or state.get("gen") != self._partial_generation.get(int(user_id), 0):
            return 0.0
        hint = self._fd_endpoint_hint(str(state.get("text", "")))
        if hint == "continue":
            return self._fd_thinking_extra_s   # 接続詞等 → 考え中。話を奪わない
        if hint == "final":
            return -self._fd_final_reduce_s    # 文末表現 → 早めに応答してよい
        return 0.0

    def _resume_discord_barge_if_pending(self, reason: str = "vad_too_short",
                                         *, pause_token: int | None = None) -> None:
        """Resolve a VAD-only false positive without waiting for STT."""
        if not self._barge_paused:
            return
        # 古いtimeout/判定が、その後に発行された新しいpauseを解除しないようにする
        if pause_token is not None and pause_token != self._pause_token:
            logger.info("FD: 古いresumeを無視 (token=%s cur=%s reason=%s)",
                        pause_token, self._pause_token, reason)
            return
        self._tts_source.resume_from_pause()
        self._barge_paused = False
        self._log_fd_resolution("resume", reason)
        self._turn_manager.speech_ignored()
        if self._direct_active and self._direct_receiver is not None and self._loop is not None:
            self._loop.create_task(self._direct_receiver.resume_playback())
        self._emit("speech_ignored", source="discord", reason=reason)

    def _log_fd_resolution(self, action: str, reason: str) -> None:
        """pause→解決 (再開/割り込み確定) の遅延を記録する (フェーズ1計測)。"""
        if self._fd_pause_started > 0:
            elapsed_ms = round((time.monotonic() - self._fd_pause_started) * 1000)
            logger.info("FD pause_to_%s: %dms (reason=%s)", action, elapsed_ms, reason)
        self._fd_pause_started = 0.0
        self._fd_pause_deadline = 0.0

    def _proactive_prompt(self, reason: str) -> str:
        """Create a bounded internal request; it is not added to chat history."""
        topic = self._conversation.state.active_topic or "直前の会話"
        if self._conversation.state.unresolved_questions:
            focus = "直前の質問に、必要な補足が一つだけあれば短く添える"
        elif self._conversation.state.assistant_turn_open:
            focus = "直前に聞いたことへ、急かさない一言だけ添える"
        else:
            focus = "話題に関係する自然な一言だけ添える"
        return (
            "[internal proactive turn] ユーザーの発話を待つ間の自発発話です。"
            f"現在の話題: {topic}。理由: {reason}。{focus}。"
            "沈黙そのものを埋めない。1文、最大35文字。質問・検索・新しい話題の開始はしない。"
            "この内部指示や理由には触れない。"
        )

    def _resolve_latest_unresolved_question(self) -> None:
        """A completed reply must not repeatedly re-trigger the same follow-up."""
        pending = self._conversation.state.unresolved_questions
        if pending:
            pending.pop()

    async def _on_autonomy_heartbeat(self, heartbeat_id: str) -> None:
        """Low-cost idle re-evaluation; the actual LLM turn is cancellable."""
        if self._mind is not None:
            self._mind.research_heartbeat()
        # A continuation the user explicitly asked for is not unsolicited
        # speech, so it runs even with the spontaneous-talk switch off.  Human
        # floor priority, the empty-channel guard and TTS backpressure are all
        # still enforced inside tick().
        if self._mind is not None and await self._directive_runtime.tick():
            return
        if not self._autonomy.active or not self._in_voice:
            self._autonomy_last_status = {
                "evaluated_at": time.time(),
                "final_action": "wait",
                "suppression_reasons": [
                    "自発モードOFF" if not self._autonomy.active else "Discord未参加"
                ],
            }
            return
        if self._mind is not None:
            self._autonomy.update_context(self._mind.autonomy_context("discord"))
        now = time.monotonic()
        humans = self._current_voice_human_count()
        silence_ms = round((now - self._autonomy_last_human_speech_ended_at) * 1000)
        if humans < 1 or self._assistant_audio_active() or self._responding:
            self._autonomy_last_status = {
                "evaluated_at": time.time(),
                "final_action": "wait",
                "suppression_reasons": [
                    "参加者なし" if humans < 1 else "発話権が使用中"
                ],
            }
            self._emit("autonomy_heartbeat", heartbeat_id=heartbeat_id,
                       silence_duration_ms=max(0, silence_ms), final_action="wait",
                       suppression_reasons=["no_human" if humans < 1 else "floor_busy"])
            if self._autonomy.continuation_active and humans >= 1:
                self._autonomy_heartbeat.request_tick(delay_s=1.0)
            return
        self.note_attention_event(
            "silence_threshold", salience=.4, dedup="silence", ttl=30.0,
            topic_ids=(self._conversation.state.active_topic,)
            if self._conversation.state.active_topic else ())
        decision = self._autonomy.heartbeat(
            source="discord", group=humans > 1, human_speaking=False,
            assistant_busy=False, now=now,
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
            silence_duration_ms=max(0, silence_ms), participant_count=humans,
            final_action=decision.action_type.value,
            top_candidate_type=decision.action_type.value,
            top_candidate_utility=decision.utility,
            speech_threshold=float(self._cfg.get("autonomy.min_speech_utility", .68)),
            suppression_reasons=suppression,
        )
        if not decision.should_speak or not decision.should_call_llm:
            return
        active = self._active_autonomy_task
        if active is not None and not active.done():
            return
        # Re-check the conversational floor immediately before allocating the
        # one allowed autonomous LLM request.
        if self._assistant_audio_active() or self._responding:
            self._emit("autonomy_speech_cancelled", heartbeat_id=heartbeat_id, reason="pre_speech_gate")
            return
        self._autonomy.mark_speech_started(decision)
        task = asyncio.create_task(self._run_autonomous_turn(decision), name=f"discord-autonomy:{heartbeat_id}")
        self._turn_tasks.add(task)
        task.add_done_callback(self._on_turn_task_done)

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
            context = self._mind.speech_recognition_context("discord")
            names = [
                str(member.get("name") or "")[:24]
                for member in self.voice_members_snapshot()
            ]
            context["proper_nouns"] = list(context.get("proper_nouns") or []) + names
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
        repairer = self._transcript_repairer
        if repairer is None or transcript is None:
            return transcript
        try:
            repaired = repairer.repair(transcript, self._stt_vocabulary)
        except Exception:
            logger.exception("STT文脈補正に失敗 (元の認識結果を使用)")
            return transcript
        if repaired.repairs or repaired.uncertain_words:
            self._emit("stt_repair", source="discord", **repaired.snapshot())
        return repaired

    async def _synthesize_with_delivery(self, loop, sentence: str, emotion, style):
        """Same delivery semantics as the local surface (第19条)."""
        import dataclasses

        import numpy as np

        from neuro_voice.tts.delivery import plan_delivery

        section = self._cfg.section("conversation") or {}
        settings = (section.get("hesitation") or {}) if isinstance(section, dict) else {}
        plain = lambda: loop.run_in_executor(  # noqa: E731 - one expression, used 3x
            self._tts_ex, self._tts.synthesize, sentence, emotion, style,
        )
        if not bool(settings.get("enabled", True)):
            return await plain()
        pieces = plan_delivery(
            sentence,
            unit_pause_ms=int(settings.get("unit_pause_ms", 320)),
            max_pause_ms=int(settings.get("max_pause_ms", 900)),
            trailing_pause_ms=int(settings.get("trailing_pause_ms", 220)),
            filler_speed=float(settings.get("filler_speed", 0.88)),
        )
        if len(pieces) <= 1 and (not pieces or pieces[0].pause_after_ms <= 0):
            return await plain()
        chunks, sample_rate = [], 0
        for piece in pieces:
            piece_style = style
            if piece.speed_scale != 1.0 and style is not None:
                with contextlib_suppress():
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
            return await plain()
        return np.concatenate(chunks), sample_rate

    def _hesitation_block(self, transcript) -> str:
        if self._mind is None:
            return ""
        try:
            block, snapshot = self._mind.hesitation_permission(
                "discord", transcript=transcript,
            )
        except Exception:
            logger.debug("確信度の評価に失敗", exc_info=True)
            return ""
        if snapshot and snapshot.get("allowance"):
            logger.info("今回の確信度(discord): %s", snapshot)
        return block

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

    # ---------- 継続依頼 (BehaviorDirective) ----------

    def _build_directive_runtime(self):
        """Compose the shared runtime with the Discord half of the work."""
        from neuro_voice.dialogue.directive_runtime import DirectiveHost, DirectiveRuntime

        host = DirectiveHost(
            speak=self._directive_speak,
            # Our own queued speech must not block writing the next segment,
            # or every gap costs a model round-trip of silence.  Only a human
            # turn holds the floor here; lookahead is capped by backpressure.
            floor_busy=lambda: bool(self._responding or self._barge_paused),
            # _tts_source is the queue our own speech goes into; _source is the
            # mix that also carries music, which is not our backpressure signal.
            playback_ahead_s=lambda: float(
                getattr(self._tts_source, "pending_seconds", 0.0)
            ),
            request_tick=lambda delay: self._autonomy_heartbeat.request_tick(delay_s=delay),
            # Talking to an empty channel is never correct, no matter what the
            # directive says.  This is the group equivalent of floor priority.
            ready=lambda: bool(self._in_voice and self._current_voice_human_count() >= 1),
            run_tool=self._directive_run_tool,
            conversation_tail=self._directive_conversation_tail,
            environment_available=lambda: False,
            cancel_generation=self._directive_cancel_generation,
            discard_pending_audio=self._tts_source.discard_paused_audio,
            search_enabled=lambda: bool(self._cfg.get("search.enabled", False)),
            on_user_stop=self._on_directive_user_stop,
        )
        return DirectiveRuntime(
            self._cfg, mind=self._mind, llm=self._llm, source="discord",
            host=host, emit=self._emit,
            persona_name=lambda: self._persona_name() or "ポッポ",
        )

    @property
    def _directive_runtime(self):
        runtime = self._directive_runtime_obj
        if runtime is None or runtime._mind is not self._mind or runtime._llm is not self._llm:
            # Both Mind and the LLM backend can be swapped at runtime.
            runtime = self._build_directive_runtime()
            self._directive_runtime_obj = runtime
        return runtime

    def _directive_conversation_tail(self) -> list[str]:
        return [
            f"{'相手' if item.get('role') == 'user' else '自分'}: "
            f"{str(item.get('content', ''))[:120]}"
            for item in self._conv.messages()[-4:]
            if item.get("role") in {"user", "assistant"}
        ]

    def _directive_cancel_generation(self, response_id_prefix: str) -> None:
        response_id = self._active_response_id
        if response_id and str(response_id).startswith(response_id_prefix):
            self._cancelled_response_ids.add(response_id)
            with contextlib_suppress():
                self._llm.cancel_request(response_id)

    def _on_directive_user_stop(self, _user_text: str) -> None:
        """Stop requested talk and suppress follow-up monologues in the VC."""
        quiet_s = max(
            0.0, float(self._cfg.get("autonomy.post_stop_quiet_seconds", 300)),
        )
        self._autonomy.suppress_speech(
            duration_s=quiet_s, reason="directive_user_stop",
        )
        self._autonomy_heartbeat.cancel_pending_tick()
        task = self._active_autonomy_task
        if task is not None and not task.done():
            task.cancel()
        self._emit(
            "autonomy_suppressed", source="discord",
            reason="directive_user_stop", quiet_seconds=quiet_s,
        )

    def _handle_directive_turn(self, user_text: str, frame: dict):
        """Instant part only; plan/revision run alongside the reply."""
        if self._mind is None:
            return None
        return self._directive_runtime.begin_user_turn(user_text, frame)

    def _note_silent_user_turn(self, user_text: str):
        """Same semantics as the local surface (第19条): a turn answered with
        silence still belongs to the running directive."""
        if self._mind is None:
            return None
        return self._directive_runtime.note_suppressed_user_turn(user_text)

    def _directive_prompt_block(self, directive) -> str:
        if self._mind is None:
            return ""
        return self._directive_runtime.prompt_block(directive)

    async def _directive_run_tool(self, request: dict) -> str:
        """Execute a directive tool request under the existing permission gates."""
        tool = str(request.get("tool") or "")
        query = str(request.get("query") or "")
        if tool != "web_search" or not query:
            return ""
        if not bool(self._cfg.get("search.enabled", False)):
            return "検索は許可されていない"
        if self._mind is not None:
            allowed, reason = self._mind.dialogue_allows_tool(
                "search", query, source="discord",
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
            return str(await asyncio.to_thread(self._search.search, query) or "")[:600]
        except Exception:
            logger.exception("Discord directive web search failed")
            return "検索に失敗した"

    async def _directive_speak(self, text: str, segment_id: str, state_version: int) -> bool:
        """Speak one segment into VC through the ordinary cancellable path."""
        runtime = self._directive_runtime
        directive = runtime.active()
        if directive is None or not self._in_voice:
            return False
        response_id = runtime.response_id_for(directive, segment_id)
        channel_id = str(getattr(self._voice_channel(), "id", "") or "") or None
        self._responding = True
        self._active_response_task = asyncio.current_task()
        self._active_response_id = response_id
        self._cancelled_response_ids.discard(response_id)
        tracker = PlayedTextTracker(response_id=response_id)
        self._active_playback_tracker = tracker
        await self._reset_playback_for_new_response("directive_segment")
        self._conversation.response_started(source="discord", channel_id=channel_id)
        interrupted = False
        try:
            self._emit("assistant_start")
            self._emit("assistant_token", token=text)
            tracker.generated(text)
            # A segment is a paragraph.  Synthesizing it whole builds a query
            # string long enough to be rejected, and blocks barge-in until the
            # entire paragraph has been rendered.
            loop = asyncio.get_running_loop()
            segmenter = SentenceSegmenter(
                max_chars=min(60, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
            )
            sentences = [*segmenter.feed(text), *([segmenter.flush()] or [])]
            for sentence in [item.strip() for item in sentences if item and item.strip()]:
                if not directive.accepts(state_version=state_version):
                    break
                if not self._is_response_active(response_id):
                    break
                await self._speak(
                    loop, sentence, response_id=response_id, playback_tracker=tracker,
                )
            self._conv.add_assistant(text)
            self._last_assistant_text = text
            self._emit("assistant_done", text=text)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if self._active_response_task is asyncio.current_task():
                self._active_response_task = None
                self._responding = False
            if self._active_playback_tracker is tracker:
                self._active_playback_tracker = None
            self._conversation.response_finished(
                source="discord", interrupted=interrupted, channel_id=channel_id,
                asked_question=text.rstrip().endswith(("?", "？")),
                assistant_text=text,
            )
        return response_id not in self._cancelled_response_ids

    def _pause_directive_for_human(self, *, discard_audio: bool = True) -> None:
        if self._mind is not None:
            self._directive_runtime.pause_for_human(discard_audio=discard_audio)

    def _resume_directive_after_backchannel(self) -> None:
        """Same meaning as the local surface (第19条)."""
        if self._mind is not None:
            with contextlib_suppress():
                self._directive_runtime.resume_after_backchannel()

    def _stop_directive(self, reason: str) -> None:
        if self._mind is not None:
            self._directive_runtime.stop(reason)

    async def _proactive_loop(self) -> None:
        """Schedule only high-value, interruptible autonomous turns."""
        while True:
            await asyncio.sleep(0.5)
            # The independent heartbeat owns event-driven autonomy.  This
            # loop remains only as a compatibility path for the disabled
            # legacy initiative policy.
            if self._autonomy.active:
                continue
            decision = self._initiative_policy.decide(
                self._conversation.state,
                last_user_at=self._last_user_activity,
                last_proactive_at=self._last_proactive_at,
                participant_count=len(self._voice_members),
                assistant_busy=self._assistant_audio_active(),
            )
            if not decision.should_speak or not self._in_voice:
                continue
            # Consume cooldown before scheduling so polling cannot duplicate it.
            self._last_proactive_at = time.monotonic()
            self._emit(
                "initiative_decision", should_speak=True, reason=decision.reason,
                relevance=decision.relevance, interruption_risk=decision.interruption_risk,
            )
            task = asyncio.create_task(self._run_proactive_turn(decision.reason))
            self._turn_tasks.add(task)
            task.add_done_callback(self._on_turn_task_done)

    # -- 注意と発話機会 (Phase 5 / 第19条) -------------------------------
    #
    # **Local と同じ意味論を Discord にも置く。** 片側だけ実装して
    # 完了としない。順序は `InitiativeRuntime` が持っているので、
    # ここは材料を集めて呼ぶだけ。

    def initiative(self):
        """自発発話の状態。既定では止まっている。"""
        if getattr(self, "_initiative", None) is None:
            from neuro_voice.cognition.runtime import InitiativeRuntime

            self._initiative = InitiativeRuntime(self._cfg, source="discord")
            # **第19条**: Local と同じ意味論。片側だけ配線して完了としない。
            mind = getattr(self, "_mind", None)
            if mind is not None:
                with contextlib_suppress():
                    mind.attach_game_observer(self._note_game_session_event)
                # 関係値の移行 (Phase 6E)。**人物の解決はこちらが正本。**
                with contextlib_suppress():
                    mind.attach_identity_runtime(self._initiative)
                # 道具 (Phase 7B)。**Discord 専用の実行系を作らない。**
                with contextlib_suppress():
                    self._bind_tool_runtime()
        return self._initiative

    def _bind_tool_runtime(self) -> None:
        """Local と**同じ** `ToolRuntime` の部品を使う（第19条）。

        違うのは入出力のアダプタだけ。Discord 側に別の Tool Gate や
        別の権限系を作ると、片方だけ直した時に必ず食い違う——そして
        食い違った側から事故になる。
        """
        mind = getattr(self, "_mind", None)
        if mind is None or not bool(self._cfg.get("tools.discord_enabled", False)):
            return
        runtime = mind.tool_runtime()
        if runtime.executor.bound("web_search", "search"):
            return

        def _search(*, query: str, max_results: int = 4):
            from neuro_voice.search.deepsearch import DeepSearch

            if getattr(self, "_search", None) is None:
                section = self._cfg.section("search")
                self._search = DeepSearch(
                    max_results=int(section.get("max_results", 4)),
                    region=str(section.get("region", "jp-jp")),
                    timeout=float(section.get("timeout_s", 7.0)),
                    max_pages=int(section.get("fetch_pages", 2)),
                    page_max_chars=int(section.get("page_max_chars", 2400)),
                    parallelism=int(section.get("parallelism", 3)))
            text = self._search.search(str(query))
            # **結果は要約された文字列。命令として扱わない**（第20項）。
            return {"text": str(text or "")[:1200], "query": str(query)[:80]}

        runtime.bind_search(_search)

    def tool_context(self, *, message=None, member=None) -> dict[str, str]:
        """Discord 側の宛先。**推測しない。**

        人物統合が確定していない時に `person_id` を当てにいかない。
        確実に分かるのは Discord 側の識別子なので、そちらへ結び付ける。
        """
        guild_id = str(getattr(getattr(message, "guild", None), "id", "")
                       or getattr(getattr(member, "guild", None), "id", ""))
        channel_id = str(getattr(getattr(message, "channel", None), "id", "")
                         or getattr(self, "_voice_channel_id", "") or "")
        user_id = str(getattr(getattr(message, "author", None), "id", "")
                      or getattr(member, "id", "") or "")
        return {
            "guild_id": guild_id, "channel_id": channel_id,
            "discord_user_id": user_id,
            "conversation_id": f"discord:{guild_id}/{channel_id}",
            "origin": "discord",
        }

    def _note_game_session_event(self, *, event: str, **payload) -> None:
        """ゲームセッションの出来事 → 世界状態と目標。**発話しない。**"""
        with contextlib_suppress():
            self.initiative().observe_ktane_event(event=event, **payload)

    def note_attention_event(
        self, event_type: str, *, summary: str = "", topic_ids: tuple[str, ...] = (),
        salience: float = .5, novelty: float = .5, urgency: float = .0,
        confidence: float = 1.0, dedup: str = "", ttl: float = 0.0,
        participant_ids: tuple[str, ...] = (),
    ) -> bool:
        runtime = self.initiative()
        if not runtime.enabled:
            return False
        try:
            from neuro_voice.cognition.attention import AttentionEvent

            now = time.monotonic()
            return runtime.observe(AttentionEvent(
                event_type=event_type, source="discord", summary=summary[:180],
                topic_ids=tuple(topic_ids)[:3], participant_ids=tuple(participant_ids),
                salience=salience, novelty=novelty, urgency=urgency,
                confidence=confidence, deduplication_key=dedup,
                occurred_at=now, expires_at=(now + ttl) if ttl > 0 else None,
            ), now=now)
        except Exception:
            logger.exception("注意イベントの取り込みでエラー")
            return False

    def _speaking_conditions(self):
        """いま話してよいかの材料。**判定はここでしない。**"""
        from neuro_voice.cognition.initiative import SpeakingConditions

        pending = bool(self._pending_group_generation)
        return SpeakingConditions(
            user_speaking=pending,
            # **通話では他人同士の会話が既定。** 誰かの発話待ちがあるなら、
            # それは自分宛とは限らない。
            other_participant_speaking=pending,
            assistant_speaking=bool(self._assistant_audio_active() or self._responding),
            last_outcome_status=str(getattr(self, "_last_outcome_status", "") or ""),
            last_topic_id=str(self._conversation.state.active_topic or ""),
            # 誰もいない通話で自分から話さない。
            only_unknown_speakers=self._current_voice_human_count() < 1,
        )

    def _proactive_gate(self) -> str:
        """自発発話を出してよいか。空文字なら出してよい。

        Local の `_proactive_decide` と**同じ抑制条件**を通す。
        Discord は通話なので、複数人のコストが最初から乗る。
        """
        runtime = self.initiative()
        if not (runtime.enabled and runtime.speech_enabled):
            return ""
        try:
            state = None
            if self._mind is not None:
                state = self._mind.cognitive_state(source="discord")
            result = runtime.evaluate(
                self._speaking_conditions(),
                obligations=tuple(getattr(state, "unresolved_obligations", ()) or ()),
                participant_count=(
                    self._mind.participant_count() if self._mind is not None else 2),
                others_talking=self._current_voice_human_count() > 1,
            )
            if result.empty:
                return "no_opportunity"
            speaking = [item for item in result.opportunities
                        if str(item.proposed_action) != "remain_silent"]
            if not speaking:
                reasons = "/".join(reason for _id, reason in result.suppressed[:2])
                return reasons or "silence_preferred"
            return ""
        except Exception:
            # 落ちたら黙る。**自発発話は落ちた時に喋る方が危ない。**
            logger.exception("自発発話の判断でエラー（今回は黙る）")
            return "gate_error"

    async def _run_autonomous_turn(self, decision) -> None:
        """Use the ordinary cancellable TTS path; no separate autonomous queue."""
        if not self._in_voice or self._assistant_audio_active():
            return
        blocked = self._proactive_gate()
        if blocked:
            logger.info("Discord の自発発話を見送り: %s", blocked)
            self._emit("autonomy_speech_cancelled", reason=blocked, source="discord")
            return
        channel_id = str(getattr(self._voice_channel(), "id", "") or "") or None
        self._responding = True
        self._active_response_task = asyncio.current_task()
        self._active_autonomy_task = asyncio.current_task()
        await self._reset_playback_for_new_response("autonomous_turn")
        self._conversation.response_started(source="discord", channel_id=channel_id)
        reply = ""
        interrupted = False
        try:
            reply = await self._generate_and_speak(
                self._persona_name() or "ポッポ", self._autonomy.autonomous_prompt(decision), proactive=True,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if self._active_response_task is asyncio.current_task():
                self._active_response_task = None
                self._responding = False
            if self._active_autonomy_task is asyncio.current_task():
                self._active_autonomy_task = None
            self._conversation.response_finished(
                source="discord", interrupted=interrupted, channel_id=channel_id,
                asked_question=self._autonomy.response_asked_question(reply),
                assistant_text=reply,
            )
        if reply:
            self._autonomy.record_autonomous_response(decision, reply)
            # The internal autonomy instruction is transient, but the words
            # spoken by the assistant are part of the shared conversation.
            # Persisting only the assistant turn lets the next human reply
            # attach to the actual question/thought without polluting history
            # with a fabricated user message.
            self._conv.add_assistant(reply)
            self._last_assistant_text = reply
            self._emit("initiative_reply", reply=reply, reason=decision.reason_code,
                       autonomous_action=decision.action_type.value)
            if self._autonomy.continuation_active:
                self._autonomy_heartbeat.request_tick(delay_s=float(
                    self._cfg.get("autonomy.explicit_continuation_interval_s", 2.5)
                ))

    async def _run_proactive_turn(self, reason: str) -> None:
        if not self._in_voice or self._assistant_audio_active():
            return
        channel_id = str(getattr(self._voice_channel(), "id", "") or "") or None
        self._responding = True
        self._active_response_task = asyncio.current_task()
        await self._reset_playback_for_new_response("proactive_turn")
        self._conversation.response_started(source="discord", channel_id=channel_id)
        reply = ""
        interrupted = False
        try:
            reply = await self._generate_and_speak(
                self._persona_name() or "ポッポ", self._proactive_prompt(reason), proactive=True,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if self._active_response_task is asyncio.current_task():
                self._active_response_task = None
                self._responding = False
            self._conversation.response_finished(
                source="discord", interrupted=interrupted, channel_id=channel_id,
                asked_question=reply.rstrip().endswith(("?", "？")),
                assistant_text=reply,
            )
        if reply:
            self._last_assistant_text = reply
            self._resolve_latest_unresolved_question()
            self._emit("initiative_reply", reply=reply, reason=reason)

    async def _cancel_discord_response(self, reason: str, interruption_text: str | None = None) -> None:
        """Stop TTS/LLM for a substantive interruption and retain the topic."""
        active = self._active_response_task
        response_id = self._active_response_id
        can_cancel_task = active is not None and not active.done() and active is not asyncio.current_task()
        self._log_fd_resolution("interrupt", reason)
        self._turn_manager.barge_in_confirmed()
        if response_id:
            self._cancelled_response_ids.add(response_id)
        if can_cancel_task:
            tracker = self._active_playback_tracker
            if tracker is not None:
                tracker.interrupted(reason)
                interrupted = InterruptedTurn(
                    response_id=response_id or tracker.response_id,
                    original_user_text=self._active_response_user_text,
                    full_assistant_text=tracker.generated_text,
                    played_text=tracker.played_text,
                    unplayed_text=tracker.unplayed_text,
                    interrupted_at=time.monotonic(),
                    interruption_text=interruption_text,
                    resume_recommended=bool(tracker.unplayed_text.strip()),
                )
                topic = self._conv.hold_current_topic(interrupted)
            else:
                topic = self._conv.hold_current_topic()
            if topic is not None:
                self._emit("topic_deferred", topic=topic.summary, reason=reason)
        self._tts_source.discard_paused_audio()
        self._barge_paused = False
        self._direct_playback_until = 0.0
        if self._direct_active and self._direct_receiver is not None:
            try:
                await self._direct_receiver.stop_playback()
            except Exception:
                logger.exception("DAVE sidecar playback stop failed")
        if not can_cancel_task:
            self._emit("interrupted", reason=reason)
            return
        active.cancel()
        try:
            await active
        except asyncio.CancelledError:
            pass
        self._emit("interrupted", reason=reason)

    @staticmethod
    def _normalize(s: str) -> str:
        """呼びかけ照合用の正規化。カタカナ→ひらがな、長音/中黒/空白除去、小文字化。

        STTが「ニューロ/にゅーろ/Neuro」等ゆれて出しても拾えるようにする。
        """
        s = (s or "").lower()
        out = []
        for ch in s:
            code = ord(ch)
            if 0x30A1 <= code <= 0x30F6:  # カタカナ → ひらがな
                out.append(chr(code - 0x60))
            elif ch in "ー・ 　\t":       # 長音符・中黒・空白は無視
                continue
            else:
                out.append(ch)
        return "".join(out)

    def _ensure_music(self) -> MusicPlayer:
        if self._music is None:
            self._music = MusicPlayer(
                self._discord, self._source,
                get_vc=lambda: self._vc,
                loop=self._loop,
                on_status=lambda msg: self._emit("status", message=msg),
                default_query=str(self._cfg.get("music.default_query", "作業用BGM")),
                on_video=lambda info, pos, speed: self._emit(
                    "music_video",
                    title=str(info.get("title", "")),
                    webpage=str(info.get("webpage", "")),
                    video_id=str(info.get("video_id", "")),
                    pos=int(pos),
                    speed=float(speed),
                ),
                # Direct DAVE owns the Discord VC, so MusicPlayer must be
                # allowed to create its FFmpeg source without discord.py's VC.
                allow_without_vc=lambda: bool(
                    self._direct_active and self._direct_receiver is not None
                    and self._direct_receiver.ready
                ),
            )
        return self._music

    def _ensure_direct_music_pump(self) -> None:
        """Send mixed FFmpeg music PCM to the DAVE sidecar in bounded chunks."""
        if not self._direct_active or self._direct_receiver is None or self._loop is None:
            return
        if self._direct_music_task is not None and not self._direct_music_task.done():
            return
        self._direct_music_task = asyncio.create_task(
            self._direct_music_pump(), name="discord-dave-music-pump",
        )

    async def _direct_music_pump(self) -> None:
        # Ten 20 ms frames per IPC message avoids 50 WebSocket messages/s,
        # while keeping pause/stop latency comfortably below a quarter second.
        frames_per_packet = 10
        try:
            while self._direct_active and self._direct_receiver is not None:
                player = self._music
                if player is None or not player.is_playing or not self._source.music_active:
                    return
                # During TTS this same source mixes voice with attenuated
                # music.  DAVE receives one PCM stream, never two writers.
                packet = b"".join(self._source.read() for _ in range(frames_per_packet))
                if packet:
                    await self._direct_receiver.play_pcm(packet)
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DAVE direct music pump failed")
            self._emit("error", message="Discord音楽の送信でエラーが出たよ")

    async def _omakase_query(self) -> str | None:
        """おまかせ再生の検索クエリ。AI自身に気分/好みで選曲させ、
        失敗時は既定クエリにフォールバックする。"""
        pick = None
        if self._mind is not None:
            try:
                pick = await self._mind.suggest_song()
            except Exception:
                logger.warning("おまかせ選曲に失敗", exc_info=True)
        if pick:
            self._emit("status", message=f"🎵 今の気分だと…「{pick}」を流すね")
            return pick
        return str(self._cfg.get("music.default_query", "作業用BGM"))

    async def _arm_humming_recognition(self, text: str) -> bool:
        from neuro_voice.music_recognition import parse_humming_recognition_request

        request = parse_humming_recognition_request(text)
        if not request.requested:
            return False
        settings = self._cfg.section("humming_recognition")
        if not bool(settings.get("enabled", False)):
            self._emit("status", message="鼻歌認識は未設定です。設定で有効化し、ACRCloudの接続情報を設定してください。")
            return True
        self._humming_armed_until = time.monotonic() + float(settings.get("arm_timeout_s", 20.0))
        self._humming_play_after = request.play_after
        self._emit("status", message="うん、鼻歌か口笛を3〜15秒ほど聞かせてください。")
        await self._speak(asyncio.get_running_loop(), "うん、鼻歌か口笛を聞かせて。")
        return True

    async def _consume_humming_audio(self, audio: np.ndarray) -> bool:
        if self._humming_armed_until <= 0:
            return False
        if time.monotonic() > self._humming_armed_until:
            self._humming_armed_until = 0.0
            self._humming_play_after = False
            self._emit("status", message="鼻歌の待機時間が切れました。もう一度、鼻歌で曲を探すよう頼んでください。")
            return False
        self._humming_armed_until = 0.0
        play_after, self._humming_play_after = self._humming_play_after, False
        settings = self._cfg.section("humming_recognition")
        min_samples = int(float(settings.get("min_audio_seconds", 3.0)) * 16000)
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if len(audio) else 0.0
        if len(audio) < min_samples or rms < float(settings.get("min_rms", 0.008)):
            self._emit("status", message="鼻歌が短すぎるか小さすぎます。3秒以上、伴奏なしで歌ってください。")
            return True
        from neuro_voice.music_recognition import ACRCloudHummingRecognizer

        recognizer = ACRCloudHummingRecognizer(
            host=str(settings.get("host", "")),
            access_key_env=str(settings.get("access_key_env", "ACRCLOUD_ACCESS_KEY")),
            access_secret_env=str(settings.get("access_secret_env", "ACRCLOUD_ACCESS_SECRET")),
        )
        if not recognizer.configured:
            self._emit("status", message="鼻歌認識のACRCloud Host / APIキーが未設定です。")
            return True
        self._emit("status", message="鼻歌から曲を探しています...")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, recognizer.recognize, audio, 16000)
        except Exception as exc:
            logger.exception("Discord humming recognition failed")
            self._emit("status", message=f"鼻歌認識に失敗しました: {exc}")
            return True
        if result is None:
            self._emit("status", message="候補を特定できませんでした。サビなど特徴的な部分を、伴奏なしで歌ってみてください。")
            return True
        query = result.query
        self._last_music_query = query
        self._emit("music_recognized", title=result.title, artist=result.artist,
                   album=result.album, query=query, source="humming")
        self._emit("status", message=f"鼻歌から特定しました: {query}")
        if play_after:
            try:
                await self._ensure_music().play(query)
                self._ensure_direct_music_pump()
            except Exception:
                logger.exception("Recognized humming track could not be played")
                self._emit("status", message="曲は特定しましたが、再生を開始できませんでした。")
            return True
        await self._speak(loop, f"鼻歌から特定しました。{result.artist}の{result.title}です。")
        return True

    async def _maybe_recognize_music(self, text: str) -> bool:
        """Identify a recent Discord audio clip only after an explicit request."""
        from neuro_voice.music_recognition import AudDRecognizer, parse_music_recognition_request

        request = parse_music_recognition_request(text)
        if not request.requested:
            return False
        if self._music is not None and self._music.is_playing:
            track = self._music.current_track or {}
            title = str(track.get("title") or track.get("query") or "").strip()
            if title:
                self._emit("status", message=f"再生中の曲: {title}")
                await self._speak(asyncio.get_running_loop(), f"今流しているのは、{title}だよ。")
                return True
        settings = self._cfg.section("music_recognition")
        if not bool(settings.get("enabled", False)):
            self._emit("status", message="曲名認識は未設定です。設定で有効化し、AUDD_API_TOKEN を指定してください。")
            return True
        audio = self._recent_music_audio(float(settings.get("capture_seconds", 12.0)))
        min_samples = int(float(settings.get("min_audio_seconds", 5.0)) * 16000)
        if len(audio) < min_samples:
            self._emit("status", message="曲をもう少し流してから、もう一度「この曲何？」と聞いてください。")
            return True
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if rms < float(settings.get("min_rms", 0.003)):
            self._emit("status", message="音楽が受信できていません。曲が流れる入力を確認してください。")
            return True
        recognizer = AudDRecognizer(token_env=str(settings.get("token_env", "AUDD_API_TOKEN")))
        if not recognizer.configured:
            self._emit("status", message=f"曲名認識のAPIトークンが未設定です: {settings.get('token_env', 'AUDD_API_TOKEN')}")
            return True
        self._emit("status", message="ちょっと曲を聞いて調べています...")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, recognizer.recognize, audio, 16000)
        except Exception as exc:
            logger.exception("Discord music recognition failed")
            self._emit("status", message=f"曲名認識に失敗しました: {exc}")
            return True
        if result is None:
            self._emit("status", message="曲を特定できませんでした。雑音の少ない状態で8〜12秒ほど聞かせてください。")
            return True
        query = result.query
        self._last_music_query = query
        self._emit("music_recognized", title=result.title, artist=result.artist,
                   album=result.album, song_link=result.song_link, query=query, source="discord")
        self._emit("status", message=f"曲を特定しました: {query}")
        if request.play_after:
            if not self._in_voice:
                self._emit("status", message="曲は特定しましたが、VCに参加していないため再生できません。")
                return True
            try:
                await self._ensure_music().play(query)
                self._ensure_direct_music_pump()
            except Exception:
                logger.exception("Recognized Discord track could not be played")
                self._emit("status", message="曲は特定しましたが、再生を開始できませんでした。")
            return True
        await self._speak(loop, f"曲を特定しました。{result.artist}の{result.title}です。")
        return True

    async def _maybe_music_command(self, text: str, *, speaker_key: str | None = None) -> bool:
        """文脈上明確な音楽操作だけを実行して True を返す。"""
        if not bool(self._cfg.get("music.enabled", True)):
            return False
        playing = self._music is not None and self._music.is_playing
        local_playing = False
        if self._local_music_probe is not None:
            try:
                local_playing = bool(self._local_music_probe())
            except Exception:
                local_playing = False
        cmd = parse_music_command(text, playing or local_playing)
        if cmd is None:
            candidate = extract_music_reference_candidate(text)
            if candidate:
                self._last_music_query = candidate
            return False
        action, query = cmd
        plan = None
        if bool(self._cfg.get("music.intent_planner_enabled", True)):
            history = self._conv.messages()[1:] if self._conv is not None else []
            plan = await plan_music_intent(
                self._llm, text,
                parsed_action=action, parsed_query=query,
                music_playing=playing or local_playing,
                now_playing=(self._music.current_track if self._music is not None else None),
                last_query=self._last_music_query,
                conversation_excerpt=history,
                timeout_s=float(self._cfg.get("music.intent_planner_timeout_s", 5.0)),
            )
        if plan is not None:
            if not plan.execute:
                logger.info("Discord音楽操作をLLMが見送り: action=%s reason=%s text=%s",
                            action, plan.reason, text[:80])
                self._emit("music_command_deferred", action=action, reason=plan.reason, text=text)
                return False
            if action == "play" and plan.query is not None:
                query = plan.query
        state = self._conversation.state
        now = time.monotonic()
        active_exchange = bool(
            speaker_key
            and state.expected_response_from == speaker_key
            and state.last_assistant_at is not None
            and now - state.last_assistant_at <= self._follow_up_s
        )
        human_count = self._current_voice_human_count()
        one_human = human_count == 1 or (
            human_count <= 0 and len(state.participants) <= 1
        )
        permitted, reason = should_execute_music_command(
            text,
            action,
            assistant_addressed=self._has_wake_word(text),
            one_human_conversation=one_human,
            active_exchange=active_exchange,
        )
        if not permitted:
            logger.info("音楽コマンドを会話文脈のため保留: action=%s reason=%s text=%s",
                        action, reason, text[:80])
            self._emit("music_command_deferred", action=action, reason=reason, text=text)
            return False
        # GUI内ローカルプレイヤーだけが再生中 → 操作をGUIへ転送する
        # (VC参加中は発話がDiscord経路に来るため、ここで橋渡ししないと届かない)
        if local_playing and not playing and action != "play":
            if action == "skip":
                self._emit("status", message="ツール内再生はキュー非対応だよ。曲名で「〇〇流して」と言ってね")
            elif action == "stop":
                self._emit("local_music", action="stop")
            elif action == "clear_queue":
                self._emit("status", message="ツール内再生はキュー非対応だよ")
            else:
                self._emit("local_music", action=action, value=float(query))
            return True
        if action == "play" and not self._in_voice:
            self._emit("status", message="先にVCに参加してから「〇〇流して」と言ってね")
            return True
        player = self._ensure_music()
        try:
            if action == "play":
                if query is not None and is_music_reference(query):
                    if not self._last_music_query:
                        return False
                    query = self._last_music_query
                elif query is None:
                    # おまかせ → AI自身に今の気分/好みで選曲させる
                    query = await self._omakase_query()
                self._last_music_query = query
                await player.play(query)
                self._ensure_direct_music_pump()
            elif action == "stop":
                player.stop()
            elif action == "clear_queue":
                player.clear_queue()
            elif action == "skip":
                await player.skip()
            elif action == "seek":
                await player.seek(float(query))
            elif action == "seek_abs":
                await player.seek_abs(float(query))
            elif action == "speed":
                await player.set_speed(float(query))
            elif action == "volume":
                player.volume_step(float(query))
            elif action == "volume_set":
                player.set_volume(float(query))
            if action != "play":
                self._ensure_direct_music_pump()
        except Exception:
            logger.exception("音楽コマンド処理でエラー")
            self._emit("error", message="音楽の操作でエラーが出たよ")
        return True

    def _is_self_echo(self, text: str) -> bool:
        """認識結果が直近の自分 (ニューロ) の発話と酷似していたら自己エコーと判定。

        ミュートの隙間 (Discord再生遅延など) をすり抜けた自分の声を、
        テキスト照合で最終防衛する。
        """
        norm = self._normalize(text)
        if len(norm) < 8:
            return False

        def bigrams(s: str) -> set:
            return {s[i:i + 2] for i in range(len(s) - 1)}

        qb = bigrams(norm)
        now = time.monotonic()
        for ts, sent in list(self._recent_tts):
            if now - ts > 20.0:
                continue
            sn = self._normalize(sent)
            if len(sn) < 8:
                continue
            if norm == sn:
                return True
            shorter, longer = sorted((len(norm), len(sn)))
            if shorter / longer >= 0.85 and (norm in sn or sn in norm):
                return True
            tb = bigrams(sn)
            if qb and tb and len(qb & tb) / max(1, len(qb | tb)) >= 0.7:
                return True
        return False

    @staticmethod
    def _prepare_loopback_for_stt(audio: np.ndarray) -> tuple[np.ndarray, float, float, float]:
        """Remove DC offset and lift quiet Discord audio to a stable STT level."""
        source = np.asarray(audio, dtype=np.float32)
        raw_rms = float(np.sqrt(np.mean(np.square(source, dtype=np.float64))))
        centered = source - float(np.mean(source))
        centered_rms = float(np.sqrt(np.mean(np.square(centered, dtype=np.float64))))
        if centered_rms <= 1e-8:
            return centered, raw_rms, centered_rms, 1.0
        gain = min(8.0, max(1.0, 0.08 / centered_rms))
        prepared = np.clip(centered * gain, -1.0, 1.0).astype(np.float32, copy=False)
        prepared_rms = float(np.sqrt(np.mean(np.square(prepared, dtype=np.float64))))
        return prepared, raw_rms, prepared_rms, gain

    def _persona_name(self) -> str:
        """現在のペルソナ名。呼びかけトリガーに常に含め、ペルソナ改名に自動追従する。"""
        try:
            from neuro_voice.memory.persona import get_active_persona

            _, p = get_active_persona(self._cfg)
            return str(p.get("name", "") or "").strip()
        except Exception:
            return ""

    def _has_wake_word(self, text: str) -> bool:
        norm = self._normalize(text)
        # 設定の呼びかけワードに加え、現在のペルソナ名(例:ポチ)にも必ず反応する
        words = list(self._wake_words)
        pname = self._persona_name()
        if pname:
            words.append(pname)
        return any(self._normalize(w) in norm for w in words if str(w).strip())

    def _should_respond(self, text: str) -> bool:
        if self._respond_all:
            return True
        if self._has_wake_word(text):
            return True
        # 直前に自分が喋ってから follow_up_s 秒以内は会話の続きとみなす
        return time.monotonic() - self._last_reply_at < self._follow_up_s

    async def _handle_utterance(self, user, audio: np.ndarray, *, partial_explicit_call: bool = False,
                                force_group_response: bool = False, input_epoch: int = 0) -> None:
        if input_epoch and input_epoch != self._latest_input_epoch:
            return
        loop = asyncio.get_running_loop()
        metrics = TurnMetrics()
        metrics.mark("speech_end")
        self._begin_turn(metrics)
        if not force_group_response and self._pending_group_response is not None:
            self._pending_group_generation += 1
            self._pending_group_response.cancel()
            self._pending_group_response = None
        # Discord does not share the local VAD callback.  Yield a background
        # growth/reflection request as soon as its finalized audio arrives.
        if self._mind is not None:
            self._mind.prioritize_realtime_turn()
        src = getattr(user, "source", "?")  # mic=本人 / loopback=通話相手 (話者分離の診断用)
        if await self._consume_humming_audio(audio):
            return
        # デバッグ: STTに渡す音声を保存 (何が聞こえているかの確認用)
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        logger.info("Discord発話セグメント[音源=%s]: %.1f秒, rms=%.4f",
                    src, len(audio) / 16000, rms)
        try:
            import soundfile as sf

            sf.write(str(self._log_dir / "last_discord_utterance.wav"), audio, 16000)
        except Exception:
            logger.debug("デバッグ音声の保存に失敗", exc_info=True)
        min_rms = (float(self._cfg.get("discord.loopback_min_rms", 0.0025))
                   if src == "loopback" else 0.004)
        if rms < min_rms:
            logger.info("ほぼ無音のためスキップ (STT幻聴防止)")
            return
        stt_audio = audio
        if src == "loopback":
            stt_audio, raw_rms, prepared_rms, gain = self._prepare_loopback_for_stt(audio)
            logger.info(
                "Discord loopback STT preparation: raw_rms=%.4f prepared_rms=%.4f gain=%.2fx",
                raw_rms, prepared_rms, gain,
            )
            try:
                import soundfile as sf

                sf.write(str(self._log_dir / "last_discord_utterance_stt.wav"), stt_audio, 16000)
            except Exception:
                logger.debug("Failed to save prepared Discord audio", exc_info=True)
        # Bias the recognizer with what this conversation is about *before*
        # decoding.  Fixing what it hears is cheaper than fixing what it wrote.
        self._apply_stt_context_bias()
        from neuro_voice.stt.transcript import detailed_transcribe

        transcript = await loop.run_in_executor(
            self._stt_ex, detailed_transcribe, self._stt, stt_audio, 16000,
        )
        transcript = self._repair_transcript(transcript)
        from neuro_voice.stt.hallucination import is_known_hallucination

        duration_ms = len(stt_audio) / 16000 * 1000.0
        if is_known_hallucination(transcript, duration_ms=duration_ms):
            logger.info(
                "Discord短音声の既知ASR幻聴を破棄: %s (%.0fms)",
                transcript.text, duration_ms,
            )
            self._emit(
                "stt_hallucination_rejected", source="discord",
                text=transcript.text, duration_ms=round(duration_ms),
            )
            return
        text = transcript.text
        metrics.mark("stt_done")
        if not text:
            return
        if input_epoch and input_epoch != self._latest_input_epoch:
            return
        # 自分 (ニューロ) の声のエコーは無視する (テキスト類似)
        if self._is_self_echo(text):
            logger.info("自己エコーを破棄: %s", text[:40])
            self._resume_discord_barge_if_pending("self_echo_text")
            return
        # フェーズ5: 音響類似によるスピーカー回り込み判定。
        # ・明確な割り込み語がある場合はエコーガードより割り込みを優先する。
        # ・回り込みは短い断片が多い。長い発話は本物の割り込みとみなし、
        #   エコーガードを掛けない (スピーカー利用時に割り込みが飲まれる問題対策)。
        _echo_norm = self._interaction_classifier.normalize(text)
        if (self._fd_enabled and self._fd_echo_enabled
                and self._assistant_audio_active()
                and len(_echo_norm) <= self._fd_echo_max_chars
                and not self._fd_detector.is_hard_interrupt(text, final=True)):
            echo_score = self._fd_echo_guard.score(audio, 16000)
            if echo_score >= self._fd_echo_thr:
                logger.info("FD echo-guard: 音響類似%.2f≥%.2f → 回り込みとして破棄 (%s)",
                            echo_score, self._fd_echo_thr, text[:30])
                self._resume_discord_barge_if_pending("echo_candidate")
                return
            if echo_score >= 0.4:
                logger.info("FD echo-guard: 類似%.2f (閾値未満、通常処理継続)", echo_score)
        active = self._active_response_task
        assistant_audio_active = self._assistant_audio_active() and active is not asyncio.current_task()
        if assistant_audio_active:
            # フェーズ2: 明確な割り込み語 (「違う」「ストップ」等) は、短い発話でも
            # 相づち/誤検知に分類させず、確定的にハード割り込みとして扱う
            if (self._fd_enabled and self._fd_kw_enabled
                    and self._fd_detector.is_hard_interrupt(text, final=True)):
                logger.info("Discord barge-in: 割り込み語を確定検出 (%s)", text[:30])
                await self._cancel_discord_response(
                    "keyword_hard_interrupt", interruption_text=text)
                assistant_audio_active = False  # 以降は通常の新規発話として処理
        if assistant_audio_active:
            classification = self._interaction_classifier.classify(
                text, duration_ms=len(audio) * 1000 / 16000,
            )
            self._emit(
                "speech_classified", intent=classification.intent.value,
                text=text, reason=classification.reason, source="discord",
            )
            if classification.intent is SpeechIntent.ACKNOWLEDGEMENT:
                logger.info("Discord barge-in: 相槌として継続 (%s)", classification.normalized_text)
                self._tts_source.resume_from_pause()
                self._barge_paused = False
                self._resume_directive_after_backchannel()
                self._turn_manager.backchannel(assistant_busy=True)
                if self._direct_active and self._direct_receiver is not None:
                    await self._direct_receiver.resume_playback()
                self._emit("user_acknowledgement", text=text, source="discord")
                return
            if classification.intent is SpeechIntent.FALSE_POSITIVE:
                logger.info("Discord barge-in: 誤検知として破棄 (%s)", classification.reason)
                self._tts_source.resume_from_pause()
                self._barge_paused = False
                self._resume_directive_after_backchannel()
                self._turn_manager.speech_ignored()
                if self._direct_active and self._direct_receiver is not None:
                    await self._direct_receiver.resume_playback()
                return
            logger.info("Discord barge-in: 実質的な割り込みを検出 (%s)", text[:40])
            # Confirmed interruption: now the old plan's audio may go.
            self._pause_directive_for_human(discard_audio=True)
            await self._cancel_discord_response("substantive_barge_in", interruption_text=text)
        name = getattr(user, "display_name", None) or getattr(user, "name", "誰か")
        # ループバックはDiscordが話者分離できないので、声紋で誰の声かを識別する。
        # (voice-recv経路ではDiscordの表示名が確実なので識別は不要)
        prof = None
        if (self._use_loopback or src == "discord_direct") and self._mind is not None:
            # 声紋フィルタ: ループバックは動画・BGMの音声も混ざるため、
            # 「登録済みの人の声」以外は基本無視する。知らない声でも
            # 呼びかけワード付きなら新しい相手として登録して応答する。
            known_only = bool(self._cfg.get("discord.known_voices_only", False))
            # 通話ループバック(他人)には本人(配信者)はいない → 照合候補から本人を
            # 除外し、声紋が甘くても他人が本人と誤判定されるのを防ぐ。
            # マイク由来は本人なので除外しない。
            exclude = ({self._streamer_name}
                       if src == "loopback" and self._streamer_name else None)
            # 通話相手(ループバック)はDiscord圧縮で声紋が不安定なので、一度覚えた人が
            # 外れにくいよう照合を緩める(既定0.35)。マイク(本人)は通常の厳しさ。
            mm = (float(self._cfg.get("discord.direct_voiceprint_match_threshold", 0.72))
                  if src == "discord_direct" else
                  float(self._cfg.get("discord.loopback_match_threshold", 0.35)))
            allow_new = False if src == "discord_direct" else not known_only
            try:
                prof = await self._mind.identify_speaker(
                    audio, allow_new=allow_new, exclude_names=exclude, min_match=mm
                )
                if prof is not None and prof.get("unknown"):
                    if src == "discord_direct":
                        self._mind.clear_speaker_context()
                        logger.info("Direct Discord voice is not voiceprint-matched: %s", text[:40])
                    elif not known_only:
                        logger.info("Discord unregistered voice accepted: %s", text[:40])
                    elif self._has_wake_word(text):
                        # 呼びかけあり → 新しい話し相手として登録
                        prof = await self._mind.identify_speaker(
                            audio, allow_new=True, exclude_names=exclude, min_match=mm)
                    else:
                        logger.info("未登録の声を無視 (動画/BGM?): %s", text[:40])
                        return
            except Exception:
                logger.exception("Discord話者識別でエラー")
            if src == "discord_direct":
                # **接続が正本と分かっている音声。** Discord は「この user が
                # 喋った」を保証するので、その時の声紋を本人のものとして
                # 数えられる。**1回では確定しない**（`CONFIRM_SUPPORT` 回要る）。
                # 声紋が接続と食い違った場合は接続を優先し、声紋側を
                # `CONFLICTED` として記録するだけ——関係値は動かさない。
                with contextlib_suppress():
                    self._note_direct_voice_identity(user, prof)
            if prof and int(prof.get("id", -1)) >= 0:
                # Discord nicknames are how this person is known *here*.  Record
                # it against the one profile so a later merge keeps both names
                # instead of the surfaces each inventing a separate person.
                nickname = str(
                    getattr(user, "display_name", None) or getattr(user, "name", "") or ""
                ).strip()
                if nickname and src != "mic":
                    with contextlib_suppress():
                        self._mind.note_speaker_alias(int(prof["id"]), "discord", nickname)
            if src != "discord_direct" and prof and prof.get("name"):
                # One person can be 「チビ」 at the local mic and 「ジーレン」 in
                # this channel.  Use the name this room expects, while the
                # profile itself stays a single person.
                name = self._mind.speaker_display_name("discord") or prof["name"]
            # マイクは常に本人。プロファイルがゲストN(自動命名)になっていたら配信者名
            # (チビ)に直す。話者リセット等で本人がゲスト扱いにならないようにする。
            if src == "mic" and prof and int(prof.get("id", -1)) >= 0:
                if prof.get("auto_name") and prof.get("name") != self._streamer_name:
                    try:
                        self._mind.rename_speaker(int(prof["id"]), self._streamer_name)
                        name = self._streamer_name
                    except Exception:
                        logger.debug("本人プロファイルの改名に失敗", exc_info=True)
                elif prof.get("name"):
                    self._streamer_name = prof["name"]  # 手動で付けた本人名に追従
        if src == "discord_direct":
            account = getattr(user, "transport_context", lambda: {})()
            detected = None if not prof or prof.get("unknown") else {
                "id": prof.get("id"), "name": prof.get("name"), "score": prof.get("score"),
            }
            self._emit("discord_speaker_resolution", source_account=account,
                       detected_speaker=detected,
                       status="matched" if detected else "unknown")
        metrics.mark("speaker_ready")
        # 診断: 音源・確定した話者名・スコア・テキストを1行で記録
        logger.info("Discord話者判定[音源=%s] → %s (score=%s): %s",
                    src, name,
                    (prof or {}).get("score") if prof else None, text[:40])
        print(f"🗣 {name}: {text}")
        # GUIチャットへ即時表示 (Discord経由と分かる色で描画される。名前=識別結果)
        self._emit("discord_user", name=name, text=text,
                   source_account=(getattr(user, "transport_context", lambda: None)()
                                   if src == "discord_direct" else None),
                   detected_speaker=(prof.get("name") if src == "discord_direct" and prof and not prof.get("unknown") else None))
        # 声紋で新しい人を初めて覚えたら通知 (こころ画面の話者リストにも反映される)
        if prof is not None and prof.get("is_new"):
            self._emit("status", message=f"👋 新しい声を覚えました: {prof.get('name', name)}")
        # Account/SSRC identifies an incoming transport stream, not a person.
        # A resolved voiceprint is the only value used as ``speaker_id``;
        # transport is retained separately so an unanswered question can still
        # be correlated with the same Discord stream.
        account = (getattr(user, "transport_context", lambda: {})()
                   if src == "discord_direct" else {}) or {}
        transport_id = str(account.get("id") or getattr(user, "id", "") or name)
        speaker_key = f"transport:{transport_id}" if transport_id else f"source:{src}:{name}"
        # 音楽は構文だけで即時実行せず、呼びかけ・参加人数・直近の会話相手を使って
        # 明確な指示かを確認する。見送った場合は通常の会話判定へ進める。
        if self._pronunciation_learning_hook is not None:
            try:
                learned = await self._pronunciation_learning_hook(text, self._last_assistant_text)
                if learned:
                    surface, reading = learned
                    self._emit("pronunciation_learned", surface=surface, reading=reading, source="discord")
            except Exception:
                logger.exception("Discord pronunciation correction learning failed")
        if await self._arm_humming_recognition(text):
            return
        if await self._maybe_recognize_music(text):
            return
        if await self._maybe_music_command(text, speaker_key=speaker_key):
            return
        # A correction must be verified even if the multi-party addressing
        # policy later decides not to answer this utterance.
        minecraft_correction = await self._minecraft_spoken_correction(
            text, self._last_assistant_text,
        )
        voiceprint_id = None
        if prof and not prof.get("unknown") and int(prof.get("id", -1)) >= 0:
            voiceprint_id = f"voiceprint:{int(prof['id'])}"
        channel_id = str(getattr(self._direct_channel, "id", "") or "") or None
        self._autonomy_last_human_speech_ended_at = time.monotonic()
        self._autonomy.note_human_speech_ended(now=self._autonomy_last_human_speech_ended_at)
        autonomy = self._autonomy_decide(
            AutonomyEventType.HUMAN_UTTERANCE_FINALIZED,
            text=text, speaker_id=speaker_key, expires_in_s=5.0,
        )
        decision = self._conversation.on_speech_final(ConversationEvent(
            ConversationEventType.SPEECH_FINAL,
            source=src,
            text=text,
            speaker_id=voiceprint_id,
            speaker_confidence=(float(prof.get("score", 0.0)) if prof else None),
            channel_id=channel_id,
            metadata={
                "speaker_key": speaker_key,
                "speaker_name": (prof.get("name") if prof and not prof.get("unknown") else "未確認の話者"),
                # Account display name is transport metadata only.  It helps
                # detect "Aさん、…" directed at another participant, while
                # ``speaker_id`` remains voiceprint-only.
                "transport_name": name,
                "source_account": account or None,
                "partial_explicit_call": partial_explicit_call,
                "voice_human_count": self._current_voice_human_count(),
                "activity_active": (
                    (
                        self._mind.activity_active()
                        or self._mind.game_profile_active()
                    ) if self._mind is not None else False
                ),
                "directive_waiting_for_user": (
                    self._mind.directive_waiting_for_user("discord")
                    if self._mind is not None else False
                ),
            },
        ))
        # An explicit screen-share start/stop command is necessarily addressed
        # to the assistant.  Multi-party floor heuristics must not discard the
        # tool command before it reaches the screen-share controller.
        from neuro_voice.vision.discord_share import classify_screen_share_control

        share_control = classify_screen_share_control(text)
        active_visual_request = False
        share_session = self._screen_share_session_obj
        if share_session is not None and share_session.active:
            active_visual_request = (
                (not share_session.requester or share_session.requester == name)
                and share_session.wants_current_visual_answer(text)
            )
        if (
            (share_control.matched or active_visual_request)
            and not decision.response_plan.should_respond
        ):
            from neuro_voice.dialogue.addressing import AddressingAction, AddressingDecision

            addressing = AddressingDecision(
                1.0,
                AddressingAction.RESPOND,
                (
                    ("explicit_screen_share_control",)
                    if share_control.matched
                    else ("active_visual_session_requester",)
                ),
                addressee="assistant",
            )
            plan = self._conversation._planner.plan(
                text, addressing, self._conversation.state,
            )
            decision = type(decision)(addressing, plan, decision.state)
        if force_group_response and decision.addressing.decision.value == "wait_for_human":
            from neuro_voice.dialogue.addressing import AddressingAction, AddressingDecision
            addressing = AddressingDecision(
                decision.addressing.target_probability, AddressingAction.RESPOND,
                tuple((*decision.addressing.reasons, "human_priority_elapsed")), addressee="open_to_group",
            )
            plan = self._conversation._planner.plan(text, addressing, self._conversation.state)
            decision = type(decision)(addressing, plan, decision.state)
        # Do not let optional autonomy veto a finalized user turn.  Addressing
        # and the ConversationManager remain the authority for direct replies;
        # autonomy only decides whether *it* may start an extra action later.
        utterance_id = uuid4().hex
        response_id = uuid4().hex
        self._emit(
            "conversation_decision",
            target_probability=decision.addressing.target_probability,
            action=decision.addressing.decision.value,
            addressee=decision.addressing.addressee,
            wait_ms=decision.addressing.wait_ms,
            reasons=list(decision.addressing.reasons),
            response_role=decision.response_plan.response_role.value,
            should_respond=decision.response_plan.should_respond,
            plan_reason=decision.response_plan.reason,
            settles_exchange=decision.response_plan.settles_exchange,
            state=decision.state,
        )
        if decision.addressing.decision.value == "wait_for_human":
            if self._mind is not None:
                with contextlib_suppress():
                    self._mind.begin_conversation_turn(
                        text,
                        source="discord",
                        response_id=response_id,
                        utterance_id=utterance_id,
                        conversation_topic=self._conversation.state.active_topic,
                    )
            # This turn is deliberately not sent to the LLM yet.  A later
            # final human utterance increments the generation and invalidates
            # this timer before it can speak over someone.
            self._pending_group_generation += 1
            generation = self._pending_group_generation
            if self._pending_group_response is not None:
                self._pending_group_response.cancel()
            self._pending_group_response = asyncio.create_task(
                self._group_wait_for_response(generation, decision.addressing.wait_ms or 850,
                                              user, audio, partial_explicit_call, input_epoch),
                name="discord-human-priority-window",
            )
            logger.info("Discord group decision: WAIT_FOR_HUMAN score=%.2f wait=%dms reasons=%s",
                        decision.addressing.target_probability, decision.addressing.wait_ms or 850,
                        ",".join(decision.addressing.reasons))
            return
        if not decision.response_plan.should_respond:
            if self._mind is not None:
                with contextlib_suppress():
                    self._mind.begin_conversation_turn(
                        text,
                        source="discord",
                        response_id=response_id,
                        utterance_id=utterance_id,
                        conversation_topic=self._conversation.state.active_topic,
                    )
            with contextlib_suppress():
                self._note_silent_user_turn(text)
            logger.info("Discord応答を見送り: action=%s reasons=%s plan=%s",
                        decision.addressing.decision, ",".join(decision.addressing.reasons),
                        decision.response_plan.reason)
            return
        if input_epoch and input_epoch != self._latest_input_epoch:
            return

        cognitive_decision = self._cognitive_decide_discord(text, metrics)
        if cognitive_decision is not None and not cognitive_decision.speaks:
            self._emit("cognitive_silence", turn_id=metrics.turn_id,
                       reason=cognitive_decision.decision_reason)
            return

        direct_reply = None
        activity_context = None
        activity_requires_assistant_move = False
        from neuro_voice.tts.volume import parse_tts_volume_command

        share_handled, share_reply = await self._get_screen_share_session().handle_control(
            text, requester=name,
        )
        if share_handled:
            direct_reply = share_reply
        else:
            share_session = self._get_screen_share_session()
            if (
                share_session.active
                and share_session.source_kind == "minecraft_obs"
                and not share_session.needs_visual_reasoning(text)
            ):
                direct_reply = await self._minecraft_verified_recipe_answer(text)
            if direct_reply is None:
                direct_reply = await (
                    share_session.answer_current_visual_question(
                        text, response_id=response_id,
                    )
                )
            volume_command = parse_tts_volume_command(text)
            if (
                direct_reply is None
                and volume_command is not None
                and self._tts_volume_hook is not None
            ):
                direct_reply = self._tts_volume_hook(volume_command)
            else:
                activity_outcome = None
                if direct_reply is None and self._mind is not None:
                    game_outcome = self._mind.game_profile_handle_final_input(
                        text, actor_id=speaker_key, source="discord",
                        utterance_id=utterance_id,
                    )
                    activity_outcome = (
                        game_outcome if game_outcome.handled
                        else self._mind.activity_handle_final_input(
                            text, actor_id=speaker_key, source="discord",
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
                    if activity_outcome is not None:
                        activity_requires_assistant_move = bool(activity_outcome.requires_assistant_move)
                if activity_outcome is not None and activity_outcome.handled:
                    direct_reply = activity_outcome.reply
        if direct_reply is None:
            direct_reply = await self._grounded_search_reply(
                text, response_id=response_id, metrics=metrics,
                input_epoch=input_epoch,
                audience=str(channel_id or speaker_key))
            if input_epoch and input_epoch != self._latest_input_epoch:
                return
        if direct_reply is None and decision.response_plan.direct_reply:
            direct_reply = decision.response_plan.direct_reply

        if self._mind is not None and not self._use_loopback and src != "discord_direct":
            # voice-recv経路のみ: Discord表示名で話者を確定 (声紋不要)。
            # ループバックは上の identify_speaker が current_speaker を設定済み。
            self._mind.set_speaker_hint(name)

        directive_created = None
        if self._mind is not None:
            try:
                frame = self._mind.begin_conversation_turn(
                    text,
                    source="discord",
                    response_id=response_id,
                    utterance_id=utterance_id,
                    conversation_topic=self._conversation.state.active_topic,
                ) or {}
            except Exception:
                logger.exception("Discord Conversation Kernel turn initialization failed")
                frame = {}
            try:
                directive_created = self._handle_directive_turn(text, frame)
            except Exception:
                logger.exception("Discord behavior directive handling failed")
        self._active_directive = directive_created

        self._responding = True
        self._active_response_task = asyncio.current_task()
        self._active_response_id = response_id
        self._cancelled_response_ids.discard(response_id)
        self._active_response_user_text = text
        playback_tracker = PlayedTextTracker(response_id=response_id)
        self._active_playback_tracker = playback_tracker
        self._turn_manager.assistant_thinking()
        self._source.clear()  # 新しい発話が来たら再生中の音声は打ち切る
        # pauseが未解決のまま残っていたら、ここで確実にリセットする。
        # (AI発話が自然終了した後の新ターンで再生が固まる問題の根本対策)
        await self._reset_playback_for_new_response("new_response")
        self._conversation.response_started(
            source=src, expected_response_from=speaker_key, channel_id=channel_id,
        )
        reply = ""
        interrupted = False
        try:
            if direct_reply is not None:
                if self._mind is not None:
                    self._mind.mark_kernel_generation(
                        source="discord", response_id=response_id,
                        generation_id=response_id,
                    )
                    self._mind.finalize_kernel_output(
                        direct_reply, source="discord", response_id=response_id,
                    )
                reply = await self._speak_direct_reply(
                    name, text, direct_reply, metrics=metrics,
                    response_id=response_id, playback_tracker=playback_tracker,
                )
            else:
                voice_tone = None
                if self._voice_tone_enabled:
                    voice_tone = self._analyze_voice_tone(speaker_key, audio, text)
                reply = await self._generate_and_speak(
                    name, text, response_plan=decision.response_plan,
                    conversation_state=decision.state, metrics=metrics,
                    response_id=response_id, playback_tracker=playback_tracker,
                    utterance_id=utterance_id,
                    minecraft_correction=minecraft_correction,
                    activity_context=activity_context,
                    activity_requires_assistant_move=activity_requires_assistant_move,
                    voice_tone=voice_tone, transcript=transcript,
                )
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            if self._active_response_task is asyncio.current_task():
                self._active_response_task = None
                self._responding = False
            if self._active_response_id == response_id:
                self._active_response_id = None
                self._active_playback_tracker = None
            self._turn_manager.assistant_done()
            self._last_reply_at = time.monotonic()
            self._conversation.response_finished(
                source=src, interrupted=interrupted, channel_id=channel_id,
                asked_question=reply.rstrip().endswith(("?", "？")),
                expected_response_from=speaker_key,
                assistant_text=reply,
            )
        if reply:
            self._last_assistant_text = reply
            self._resolve_latest_unresolved_question()
            self._emit("discord_reply", reply=reply)
            self._report_discord_latency(metrics)
            # The legacy regex continuation is a second request classifier.
            # While BehaviorDirective owns continuing requests, running both
            # would let Discord and local voice reach different conclusions.
            runtime = None if self._mind is None else self._directive_runtime
            directive_enabled = bool(runtime is not None and runtime.enabled)
            if directive_enabled:
                settled = await runtime.settle(timeout=float(
                    self._cfg.get("behavior_directive.plan_settle_timeout_s", 8.0)
                ))
                directive_created = directive_created or settled
                if directive_created is not None:
                    runtime.note_opening_segment(directive_created, reply)
                if runtime.active() is not None:
                    self._autonomy_heartbeat.request_tick(delay_s=runtime.tick_delay())
            continuation_started = (
                False if directive_enabled
                else self._autonomy.note_conversation_turn(text, reply)
            )
            if continuation_started:
                self._autonomy_heartbeat.request_tick(delay_s=float(
                    self._cfg.get("autonomy.explicit_continuation_interval_s", 2.5)
                ))
                self._emit(
                    "autonomy_continuation_started",
                    remaining=self._autonomy.snapshot()["continuation"]["remaining"],
                    source="discord",
                )
        if reply and self._mind is not None:
            try:
                # **Local と同じ後始末。** 手順は `Mind.close_turn` の1箇所だけ。
                self._mind.close_turn(
                    text, reply, source="discord",
                    topic=str(decision.state.get("active_topic") or ""),
                )
                self._mind.maybe_queue_user_research(text)
            except Exception:
                logger.exception("Mind記録でエラー")

    async def _group_wait_for_response(self, generation: int, wait_ms: int, user,
                                       audio: np.ndarray, partial_explicit_call: bool,
                                       input_epoch: int) -> None:
        """Give humans the floor first; stale timers never revive old turns."""
        try:
            await asyncio.sleep(max(0.05, wait_ms / 1000.0))
            if (generation != self._pending_group_generation or self._responding
                    or input_epoch != self._latest_input_epoch):
                return
            self._pending_group_response = None
            await self._handle_utterance(
                user, audio, partial_explicit_call=partial_explicit_call,
                force_group_response=True, input_epoch=input_epoch,
            )
        except asyncio.CancelledError:
            logger.info("Discord human-priority response cancelled")
            raise

    def _minecraft_assistant_enabled(self) -> bool:
        return (
            bool(self._cfg.get("game_assistant.minecraft.enabled", True))
            and str(self._cfg.get("video.game_profile", "")).strip().lower() == "minecraft"
        )

    async def _ensure_minecraft_knowledge(self):
        if not self._minecraft_assistant_enabled():
            return None
        if self._minecraft_knowledge is None:
            def _create():
                from neuro_voice.games import MinecraftKnowledgeBase

                return MinecraftKnowledgeBase.from_config(self._cfg)

            self._minecraft_knowledge = await asyncio.to_thread(_create)
        return self._minecraft_knowledge

    async def _minecraft_knowledge_context(self, user_text: str) -> str | None:
        try:
            knowledge = await self._ensure_minecraft_knowledge()
            if knowledge is None:
                return None
            limit = max(
                1,
                min(5, int(self._cfg.get("game_assistant.minecraft.max_context_entries", 3))),
            )
            return await asyncio.to_thread(
                knowledge.context_for, user_text, None, limit=limit,
            )
        except Exception:
            logger.exception("Discord Minecraft local knowledge retrieval failed")
            return None

    async def _minecraft_verified_recipe_answer(self, user_text: str) -> str | None:
        """Return official cooking guidance before any current-frame inference."""
        if not bool(
            self._cfg.get(
                "game_assistant.minecraft.direct_verified_recipe_answer", True,
            )
        ):
            return None
        try:
            knowledge = await self._ensure_minecraft_knowledge()
            if knowledge is None:
                return None
            return await asyncio.to_thread(
                knowledge.verified_recipe_answer,
                user_text,
            )
        except Exception:
            logger.exception("Discord Minecraft verified recipe retrieval failed")
            return None

    async def _minecraft_spoken_correction(self, user_text: str, last_assistant_text: str):
        """Verify a spoken recipe correction against official game data."""
        if not bool(self._cfg.get("game_assistant.minecraft.verify_spoken_corrections", True)):
            return None
        try:
            knowledge = await self._ensure_minecraft_knowledge()
            if knowledge is None:
                return None
            result = await asyncio.to_thread(
                knowledge.maybe_apply_spoken_correction,
                user_text,
                last_assistant_text,
            )
            if result is None:
                return None
            if result.verified:
                self._emit(
                    "status",
                    message=(
                        "✓ Minecraft DBを公式データで訂正しました: "
                        f"{result.item_name}（Java {knowledge.active_version}）"
                    ),
                )
                self._emit(
                    "minecraft_knowledge_corrected",
                    item=result.item_name,
                    fact=result.canonical_fact,
                    version=knowledge.active_version,
                    source="discord",
                )
            elif result.status == "rejected":
                self._emit(
                    "status",
                    message=f"⚠ 訂正はDBへ保存していません: {result.reason}",
                )
            return result
        except Exception:
            logger.exception("Discord Minecraft spoken correction verification failed")
            return None

    def _begin_turn(self, metrics: TurnMetrics, *, kind=None):
        """ターンの入口。**Local の `_begin_turn` と同じ意味。**

        `source_type` をここで Discord にする。既定のままだと Discord の
        ターンが `local_mic` として集計され、「Local と Discord を
        混ぜない」はずの経路別合計が実際には混ざっていた。
        """
        from neuro_voice.cognition.turn_integrity import ConversationKind

        if metrics is None:
            return None
        if not metrics.session_id:
            metrics.session_id = self._turn_session_id
        metrics.source_type = SourceType.DISCORD_VOICE
        session = self._cognition_test_session.snapshot()
        from neuro_voice.cognition.rollout import CognitionRolloutResolver
        resolved = CognitionRolloutResolver.resolve(self._cfg, session=session)
        metrics.effective_cognition_enabled = resolved.effective_enabled
        metrics.cognition_rollout_mode = resolved.resolved_mode
        metrics.cognition_requested_rollout_mode = resolved.requested_mode
        metrics.cognition_execution_path = resolved.execution_path
        metrics.cognition_session_epoch = session.epoch
        metrics.cognition_activation_source = resolved.activation_source
        metrics.cognition_config_fingerprint = resolved.config_fingerprint
        metrics.transport = "DISCORD"
        self._active_cognition_metrics = metrics
        self._cognitive_decision = None
        if self._mind is not None:
            with contextlib_suppress():
                metrics.bind_persona(self._mind.persona_context)
        return self._turns.begin(
            metrics, kind=kind or ConversationKind.NORMAL_CONVERSATION,
        )

    def _cognitive_decide_discord(self, user_text: str, metrics: TurnMetrics):
        """Use the same no-LLM selector for an enabled Discord turn."""
        if self._mind is None or not bool(metrics.effective_cognition_enabled):
            return None
        try:
            from neuro_voice.cognition import CognitiveEvent, CognitiveKernel, EventType
            from neuro_voice.cognition.trace import CognitiveTrace

            trace = CognitiveTrace(
                turn_id=str(metrics.turn_id), input_event_ids=(str(metrics.turn_id),),
                active_persona_id=str(metrics.persona_id),
                active_persona_version=str(metrics.persona_version),
                persona_epoch=int(metrics.persona_epoch or 0),
                cognition_enabled=True,
                rollout_mode=str(metrics.cognition_rollout_mode),
                requested_rollout_mode=str(metrics.cognition_requested_rollout_mode),
                execution_path="cognitive",
                cognition_session_epoch=int(metrics.cognition_session_epoch or 0),
                cognition_activation_source=str(metrics.cognition_activation_source),
                cognition_config_fingerprint=str(metrics.cognition_config_fingerprint),
                transport="DISCORD", action_selector_used=True,
                speech_gate_active=True, legacy_response_path_used=False,
            )
            event = CognitiveEvent(
                event_type=EventType.USER_UTTERANCE, source="discord", content=user_text,
            )
            state = self._mind.cognitive_state(user_text, source="discord")
            recall = self._mind.recall_for_action(user_text, state, source="discord")
            trace.memory_retrieval_trigger = getattr(recall, "trigger", "") or "not_applicable"
            trace.memory_trigger_result = "RETRIEVE" if getattr(recall, "trigger", "") else "NOT_APPLICABLE"
            trace.retrieved_memory_ids = [item.memory.memory_id for item in getattr(recall, "scored", ())]
            for name, value in self._mind.persona_trace_fields(trace.retrieved_memory_ids).items():
                if hasattr(trace, name):
                    setattr(trace, name, value)
            internal = self._mind.internal_state("discord")
            bias = self._mind.internal_action_bias(internal, source="discord")
            kernel = CognitiveKernel(
                dict(self._cfg.get("cognition.weights", {}) or {}),
                closure_policy=str(self._cfg.get("cognition.closure_response_policy", "adaptive")),
                memory_influence=getattr(recall, "influence", None),
                state_bias=bias if not bias.empty else None,
            )
            candidates = kernel.propose(event, state)
            decision = kernel.decide(event, state, candidates)
            self._mind.record_cognitive_action(str(decision.selected_action))
            trace.selected_action = str(decision.selected_action)
            trace.decision_reason = str(decision.decision_reason)
            trace.decision_scores = {
                str(item.action_type): item.total_score for item in candidates
            }
            trace.speech_gate_active = True
            trace.speech_gate_result = {"allowed": bool(decision.speaks), "reason": "decision"}
            metrics._discord_cognitive_trace = trace
            self._cognitive_decision = decision
            return decision
        except Exception:
            logger.exception("Discord cognitive selector failed before side effects")
            return None

    def start_cognition_session(self, mode: str = "test_session") -> dict[str, object]:
        """Discord launcher override; it is process-local and never persisted."""
        snapshot = self._cognition_test_session.request_mode(
            mode, idle=not bool(getattr(self, "_responding", False)))
        return {
            "effective_cognition_enabled": snapshot.effective_enabled,
            "rollout_mode": snapshot.rollout_mode,
            "cognition_session_epoch": snapshot.epoch,
            "state": snapshot.state,
        }

    def _report_discord_latency(self, metrics: TurnMetrics) -> None:
        report = metrics.report()
        if not report:
            return
        logger.info("Discord %s", report)
        logging.getLogger("latency").info("Discord %s", report)
        items = []
        for start, end, label in _PAIRS:
            value = metrics.delta_ms(start, end)
            if value is not None:
                items.append({"label": label, "ms": round(value)})
        self._emit("latency", source="discord", items=items,
                   summary=self._latency_window.add(metrics))
        self._emit_discord_trace(metrics)

    def _trace_writer(self):
        """Local と同じ非同期 Trace Writer を Discord 通常会話にも使う。"""
        if self._cognitive_trace_writer is None:
            from neuro_voice.cognition.async_trace_writer import TraceWriter

            self._cognitive_trace_writer = TraceWriter(
                self._cfg.get("cognition.trace.path", "logs/cognitive_trace.jsonl"),
                enabled=bool(self._cfg.get("cognition.trace.enabled", False)),
                project_root=Path(__file__).resolve().parents[2],
            )
            status = self._cognitive_trace_writer.status()
            logger.info(
                "Discord Cognitive Trace startup: cognitive_trace_enabled=%s "
                "trace_writer_initialized=%s trace_output_path=%s "
                "trace_queue_enabled=%s trace_last_error=%s project_root=%s "
                "config_source_path=%s runtime_source_root=%s",
                status["cognitive_trace_enabled"], status["trace_writer_initialized"],
                status["trace_output_path"], status["trace_queue_enabled"],
                status["trace_last_error"], status["project_root"],
                getattr(self._cfg, "path", None), Path(__file__).resolve().parents[2],
            )
        return self._cognitive_trace_writer

    def _emit_discord_trace(self, metrics: TurnMetrics) -> None:
        """通常 Discord ターンを、本文なしで1行だけ Trace へ残す。"""
        writer = self._trace_writer()
        if not writer.enabled:
            return
        from neuro_voice.cognition.trace import CognitiveTrace

        trace = CognitiveTrace(
            turn_id=str(metrics.turn_id), input_event_ids=(str(metrics.turn_id),),
            selected_action="discord_conversation", execution_status="turn_committed",
            memory_retrieval_trigger="legacy_context",
            active_persona_id=str(metrics.persona_id),
            active_persona_version=str(metrics.persona_version),
            persona_epoch=int(metrics.persona_epoch or 0),
        )
        trace.cognition_enabled = bool(metrics.effective_cognition_enabled)
        trace.rollout_mode = str(metrics.cognition_rollout_mode)
        trace.requested_rollout_mode = str(metrics.cognition_requested_rollout_mode)
        trace.execution_path = str(metrics.cognition_execution_path)
        trace.cognition_session_epoch = int(metrics.cognition_session_epoch or 0)
        trace.cognition_activation_source = str(metrics.cognition_activation_source)
        trace.cognition_config_fingerprint = str(metrics.cognition_config_fingerprint)
        trace.transport = str(metrics.transport)
        cognitive_trace = getattr(metrics, "_discord_cognitive_trace", None)
        if cognitive_trace is not None:
            for name in (
                "selected_action", "decision_reason", "decision_scores",
                "memory_retrieval_trigger", "memory_trigger_result",
                "retrieved_memory_ids", "retrieved_memory_persona_ids",
                "retrieved_memory_scopes", "persona_leak", "speech_gate_result",
            ):
                if hasattr(cognitive_trace, name):
                    setattr(trace, name, getattr(cognitive_trace, name))
            trace.action_selector_used = True
            trace.speech_gate_active = True
            trace.legacy_response_path_used = False
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
            with contextlib_suppress():
                for name, value in self._mind.persona_trace_fields(memory_ids).items():
                    setattr(trace, name, value)
            with contextlib_suppress():
                trace.memory_write_decisions = list(
                    self._mind._episodes.snapshot().get("last_write_decisions", []))
        trace.turn_latency = metrics.snapshot()
        trace.latency_summary = self._latency_window.totals()
        trace.latency_breakdown = {
            label: round(value, 2)
            for start, end, label in _PAIRS
            if (value := metrics.delta_ms(start, end)) is not None
        }
        self._turns.seal_all_playback_sessions(metrics)
        trace.speech_delivery = self._turns.delivery_snapshot(metrics)
        search_presentation = getattr(metrics, "_discord_search_presentation", None)
        if search_presentation is not None:
            presentation, normalized_count = search_presentation
            trace.note_tool_result_usage(used_by_planner=True)
            trace.note_search_result_presentation(
                presentation, normalized_count=int(normalized_count), elapsed_ms=0.0)

        async def _finish() -> None:
            deadline = time.monotonic() + 30.0
            while not self._turns.playback_finalized(metrics) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            trace.speech_delivery = self._turns.delivery_snapshot(metrics)
            writer.emit(trace)

        try:
            asyncio.get_running_loop().create_task(
                _finish(), name=f"discord-trace-playback-close-{metrics.turn_id}")
        except RuntimeError:
            writer.emit(trace)

    async def _grounded_search_reply(
        self, user_text: str, *, response_id: str, metrics: TurnMetrics,
        input_epoch: int = 0, audience: str = "discord",
    ) -> str | None:
        """Return an extractive, source-bounded answer for an explicit search.

        Discord previously placed raw search text in an LLM prompt.  That made
        a title such as a rakugo name look like permission to invent its plot.
        This route has no LLM call: it speaks only compact evidence already
        sanitised by the shared Result Presenter.
        """
        from neuro_voice.search.contracts import SearchDisposition
        from neuro_voice.search.intent import route_search_request

        cached = self._search_evidence_cache.get(
            persona_id=metrics.persona_id, surface="discord",
            audience=audience, epoch=metrics.persona_epoch)
        route = route_search_request(
            user_text, enabled=bool(self._cfg.get("search.enabled", False)),
            has_reusable_evidence=cached is not None)
        if route.disposition is SearchDisposition.NOT_REQUESTED:
            return None
        if route.disposition is SearchDisposition.DEFERRED:
            return None
        if route.disposition is SearchDisposition.CLARIFY:
            return "何を検索するか、対象をもう少し具体的に教えて。"
        if route.disposition is SearchDisposition.BLOCKED:
            return "今は検索機能を使えないよ。"
        if route.disposition is SearchDisposition.REUSE_EVIDENCE:
            if cached is None:
                return "直前の検索根拠を確認できないよ。"
            from neuro_voice.cognition.search_result_presenter import present_search_outcome
            return present_search_outcome(
                cached.outcome, answer_mode=route.answer_mode).response
        if self._mind is not None:
            allowed, reason = self._mind.dialogue_allows_tool(
                "search", user_text, source="discord")
            if not allowed:
                logger.info("Discord grounded search skipped (%s)", reason)
                return "この内容は検索へ送れないよ。"
        from neuro_voice.search.deepsearch import DeepSearch
        from neuro_voice.cognition.search_result_presenter import (
            execute_search_route, present_search_outcome,
        )

        try:
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
            outcome = await asyncio.to_thread(
                execute_search_route, route, self._search,
                is_current=lambda: (
                    (not input_epoch or input_epoch == self._latest_input_epoch)
                    and response_id not in self._cancelled_response_ids
                ),
            )
            presentation = present_search_outcome(outcome)
            views = list(outcome.results)
            self._search_evidence_cache.put(
                outcome, persona_id=metrics.persona_id, surface="discord",
                audience=audience, epoch=metrics.persona_epoch,
                source_turn_id=metrics.turn_id)
        except Exception:
            logger.exception("Discord grounded search failed")
            from neuro_voice.search.contracts import SearchOutcome
            presentation = present_search_outcome(SearchOutcome(status="FAILED", route=route))
            views = []
        if input_epoch and input_epoch != self._latest_input_epoch:
            return ""
        if response_id in self._cancelled_response_ids:
            return ""
        if presentation.ui_result is not None:
            self._emit("tool_search_result", result=presentation.ui_result)
        # Content remains process-local.  The Trace writer records only the
        # selected ID/domain and the grounded-result bit below.
        metrics._discord_search_presentation = (presentation, len(views))
        return presentation.response

    async def _maybe_search(
        self, user_text: str, messages: list[dict], *,
        response_id: str = "",
    ) -> list[dict]:
        """DeepSearch (Web検索)。「調べて」「検索して」「最新」等で自動発動 (ローカルと同仕様)。"""
        from neuro_voice.search.deepsearch import (
            conversation_repair_context, is_contextual_clarification,
        )

        if re.search(r"(?:あとで|後で|ついでに|自動で)?(?:調べて|検索して|学習して)(?:おいて|おいてね|ほしい|欲しい)", user_text):
            logger.info("Tool Scheduler: Discord deferred research request; immediate Web search skipped")
            return messages
        if is_contextual_clarification(user_text):
            logger.info("Tool Scheduler: Discord dialogue clarification; Web search skipped")
            repaired = list(messages)
            position = 1 if repaired and repaired[0].get("role") == "system" else 0
            repaired.insert(position, {
                "role": "system",
                "content": conversation_repair_context(user_text, messages),
            })
            return repaired
        if not bool(self._cfg.get("search.enabled", False)):
            return messages
        try:
            from neuro_voice.search.deepsearch import DeepSearch, parse_search_plan, should_search

            if not should_search(user_text):
                return messages
            if self._mind is not None:
                allowed, reason = self._mind.dialogue_allows_tool(
                    "search", user_text, source="discord",
                )
                if not allowed:
                    logger.info("Tool Scheduler: Discord search skipped (%s)", reason)
                    return messages
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
            started_at = time.perf_counter()
            plan = parse_search_plan(
                "", user_text, max_queries=int(self._cfg.get("search.max_queries", 3)),
            )
            query = " / ".join(plan.queries)
            if self._mind is not None:
                self._mind.mark_kernel_search_execution(
                    source="discord", response_id=response_id,
                    query=query,
                )
            self._emit("search_start", query=query)
            logger.info("Discord: Web検索中: %s", query)
            loop = asyncio.get_running_loop()
            if bool(self._cfg.get("search.speak_notice", True)):
                notice = str(self._cfg.get("search.notice_text", "ちょっと調べてみるね。"))
                asyncio.create_task(self._speak(loop, notice), name="discord-search-notice")
                self._emit("search_notice", text=notice)
            # Keep the single STT worker free for incoming Discord speech.
            results = await asyncio.to_thread(self._search.search_many, plan.queries)
            if self._mind is not None:
                self._mind.mark_dialogue_tool_use("search", user_text)
            self._emit("search_done", query=query, found=bool(results))
            if self._mind is not None:
                self._mind.mark_kernel_search_execution(
                    source="discord", response_id=response_id,
                    query=query, found=bool(results),
                )
            logger.info("Discord 検索高速化: total=%dms", round((time.perf_counter() - started_at) * 1000))
            if not results:
                return messages
            messages = list(messages)
            messages.insert(-1, {
                "role": "system",
                "content": (
                    "以下は今のユーザー発話に関するWeb検索結果 (最新情報)。"
                    "これを参考に、音声会話として自然に短く答えること。\n"
                    "【重要・出所の区別】これは今この瞬間にWebから取得した外部情報であり、"
                    "あなたが過去にやったこと・調べたこと・経験したことではない。"
                    "『〜を調べてたよ』のように自分の過去の行動として語ってはいけない。"
                    "自分の行動を聞かれているなら、検索結果ではなく会話履歴と記憶だけを"
                    "根拠にし、思い出せないなら正直にそう言うこと。\n\n"
                    + results[:int(self._cfg.get("search.final_context_max_chars", 6000))]
                ),
            })
            return messages
        except Exception as exc:
            logger.exception("Discord DeepSearchでエラー")
            if self._mind is not None:
                self._mind.mark_kernel_search_execution(
                    source="discord", response_id=response_id,
                    query=locals().get("query", ""),
                    found=False, error_code=type(exc).__name__,
                )
            return messages

    def _analyze_voice_tone(self, speaker_key: str, audio, text: str) -> str | None:
        """相手の声色ラベルを返す (話者別ベースライン)。判定不能なら None。

        ProsodyAnalyzer は numpy のみで数十msの軽量処理。話者ごとに別インスタンスを
        持たせ、各自の普段の声との差でトーンを判定する (多人数会話でも有効)。
        「いつも通り」の時は None にして、余計なノイズをLLMへ渡さない。
        """
        try:
            analyzer = self._prosody.get(speaker_key)
            if analyzer is None:
                from neuro_voice.audio.prosody import ProsodyAnalyzer
                analyzer = ProsodyAnalyzer(sample_rate=16000)
                self._prosody[speaker_key] = analyzer
            result = analyzer.analyze(audio, text)
            if not result:
                return None
            label = str(result.get("label") or "").strip()
            if not label or label.startswith("いつも通り"):
                return None
            return label
        except Exception:
            logger.debug("Discord声トーン解析に失敗 (無視)", exc_info=True)
            return None

    async def _generate_and_speak(self, name: str, text: str, *, response_plan=None,
                                  conversation_state: dict | None = None,
                                  proactive: bool = False,
                                  metrics: TurnMetrics | None = None,
                                  response_id: str | None = None,
                                  utterance_id: str = "",
                                  playback_tracker: PlayedTextTracker | None = None,
                                  minecraft_correction=None,
                                  activity_context: str | None = None,
                                  activity_requires_assistant_move: bool = False,
                                  voice_tone: str | None = None,
                                  transcript=None) -> str:
        loop = asyncio.get_running_loop()
        if metrics is not None:
            self._begin_turn(metrics)
            # **1つの発話から出る最終応答は1件**（Phase 7D ③）。Local の
            # `respond_text` と同じ判定を、同じ台帳で行う。
            if self._turns.blocks(self._turns.commit(
                metrics, CommitKind.SPEECH_REQUEST, str(response_id or ""),
                decision_id=str(utterance_id or ""),
            )):
                logger.warning("同じ発話から2件目の応答が立ち上がったため中止: %s", response_id)
                return ""
        if proactive:
            # Never make an internal prompt look like a user message or keep
            # it in the durable transcript.
            messages = self._conv.messages_for_autonomous_turn(text)
            resumed_deferred_topic = False
        else:
            self._conv.add_user(f"{name}: {text}")
            messages, resumed_deferred_topic = self._conv.messages_for_turn(text)
            from neuro_voice.dialogue.repair import analyze_conversation_repair

            conversation_repair = analyze_conversation_repair(text)
            if conversation_repair.is_repair:
                messages = insert_before_user_turn(
                    messages, conversation_repair.system_context(),
                )
        # 相手の声色をLLMへ渡し、単調でなく声のトーンに反応させる (多人数会話でも
        # 誰がどんな調子で話したかが伝わる)。メタ情報には触れないよう指示する。
        if voice_tone:
            tone_note = (
                f"【{name}さんの今の声の様子】{voice_tone}。"
                "この声色に自然に反応して。ただし『声のトーンが〜』のようなメタ発言はしない。"
                "相手の気分に合わせて相槌や語調に感情を乗せる。"
            )
            messages = insert_before_user_turn(messages, tone_note)
        from neuro_voice.memory.persona import epistemic_turn_prompt

        truth_guard = epistemic_turn_prompt(text)
        if truth_guard:
            messages = insert_before_user_turn(messages, truth_guard)
        if not proactive:
            # Per-turn blocks sit next to the user turn so the prefix in
            # front of them stays reusable (Local と同形。第19条)。
            messages = insert_before_user_turn(
                messages, self._directive_prompt_block(self._active_directive),
            )
            # The words are a recognition hypothesis, not authoritative kanji.
            messages = insert_before_user_turn(
                messages, self._asr_uncertainty_block(transcript),
            )
            messages = insert_before_user_turn(
                messages, self._hesitation_block(transcript),
            )
        if activity_context:
            if "【選択中のゲームプロファイル】" in activity_context:
                activity_suffix = (
                    "\n[GAME PROFILE INPUT] Follow the selected game role and canonical session "
                    "policy. Do not invent missing observations or use web search during play."
                )
            elif activity_requires_assistant_move:
                activity_suffix = (
                    "\n[ACTIVITY INPUT] The human made a valid game move or asked you to retry. "
                    "Briefly respond if needed, then submit exactly one legal quoted hiragana word."
                )
            else:
                activity_suffix = (
                    "\n[ACTIVITY INPUT] This is not a game move. Answer normally and do not claim "
                    "or submit a new game move."
                )
            position = next(
                (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
                len(messages),
            )
            messages = [
                *messages[:position],
                {"role": "system", "content": activity_context + activity_suffix},
                *messages[position:],
            ]
        if response_plan is not None:
            state = conversation_state or {}
            policy = (
                "【会話方針】"
                f"役割={response_plan.response_role.value}; 長さ={response_plan.target_length}; "
                f"深掘り={response_plan.depth}; 話題={state.get('active_topic') or 'なし'}。"
                "この方針に従い、無意味な締めや質問を足さず、音声会話として自然に返答する。"
            )
            if response_plan.response_role.value == "acknowledgement":
                policy += (
                    "今回は相槌だけ: 「うんうん」「へえ、そうなんだ」「わかる」のような"
                    "短い一言 (10文字前後) のみ。説明や質問はしない。"
                )
            elif response_plan.response_role.value == "brief_answer":
                policy += "今回は輪の中の短い反応: 一言〜二言で、会話を奪わずテンポよく返す。"
            messages = insert_before_user_turn(messages, policy)
        members = self.voice_members_snapshot()
        if members:
            roster = ", ".join(str(item["name"]) for item in members)
            messages = [
                messages[0],
                {"role": "system", "content": (
                    f"【Discord VC状況】人間の参加者は{len(members)}人: {roster}。"
                    "この情報は現在の通話状況。参加者や人数について聞かれたらこれを使い、"
                    "不確かな推測はしない。"
                )},
                *messages[1:],
            ]
        if self._music is not None and self._music.is_playing:
            context = now_playing_context(self._music.current_track)
            if context:
                messages = insert_before_user_turn(messages, context)
        if not proactive:
            game_context = await self._minecraft_knowledge_context(text)
            if game_context:
                messages = [
                    messages[0],
                    {"role": "system", "content": game_context},
                    *messages[1:],
                ]
            correction_context = (
                minecraft_correction.system_context()
                if minecraft_correction is not None else None
            )
            if correction_context:
                messages = [
                    messages[0],
                    {"role": "system", "content": correction_context},
                    *messages[1:],
                ]
            if self._screen_share_session_obj is not None:
                visual_context = await self._screen_share_session_obj.context_for_turn(
                    text, response_id=response_id or "",
                )
                # DiscordScreenShareSession owns preview publication. A normal
                # dialogue turn must not relabel queued video as fresh.
                if visual_context:
                    messages = [
                        messages[0],
                        {"role": "system", "content": visual_context},
                        *messages[1:],
                    ]
        if self._mind is not None and not proactive:
            try:
                members_now = self.voice_members_snapshot()
                self._mind.set_privacy_context(
                    group=len(members_now) > 1,
                    audience_user_ids={str(item.get("id")) for item in members_now},
                )
                self._mind.observe_dialogue_turn(text, source="discord")
                followup_provider = getattr(self._mind, "autonomy_followups", None)
                followups = (
                    followup_provider("discord")
                    if callable(followup_provider) else []
                )
                for item in followups:
                    self._autonomy.add_grounded_followup(
                        topic=item["topic"], summary=item["question"],
                        owner_user_id=str(name),
                        callback_after_s=float(
                            self._cfg.get("autonomy.followup_callback_after_s", 600)
                        ),
                        source_excerpt=str(item.get("source_excerpt") or ""),
                    )
                ctx = await self._mind.build_context(
                    text, conversation_topic=self._conversation.state.active_topic,
                    source="discord",
                    response_id=response_id or "",
                    utterance_id=utterance_id,
                )
                if ctx:
                    ctx += (
                        "\n【Discord通話中】複数人がいる場合は、誰に返しているか"
                        "分かるように時々名前を呼ぶ。短い話し言葉で。"
                    )
                    messages = insert_before_user_turn(messages, ctx)
                recalled = self._mind.last_recall
                if recalled:
                    self._conversation.events.publish(ConversationEvent(
                        ConversationEventType.MEMORY_RECALLED, "discord",
                        metadata={"memories": recalled},
                    ))
                    self._emit("memory_recalled", memories=recalled)
            except Exception:
                logger.exception("Mindコンテキスト生成でエラー")
        if not proactive and not activity_context:
            messages = await self._maybe_search(
                text, messages, response_id=response_id or "",
            )
        if metrics is not None:
            metrics.mark("context_ready")

        planned_voice = (
            self._mind.conversation_voice_direction("discord")
            if self._mind is not None and not proactive else {}
        )
        if planned_voice and self._mind is not None:
            plan_snapshot = self._mind.conversation_plan_snapshot("discord")
            logger.info(
                "Conversation Plan[discord]: style=%s shape=%s emotion=%s temperature=%s",
                plan_snapshot.get("primary_style"), plan_snapshot.get("response_shape"),
                plan_snapshot.get("ai_emotion"), plan_snapshot.get("temperature"),
            )
            self._emit(
                "conversation_plan", source="discord", plan=plan_snapshot,
                emotion=planned_voice.get("ai_emotion"),
                delivery=planned_voice.get("delivery"),
            )
            expression_plan = planned_voice.get("expression_plan")
            if isinstance(expression_plan, dict):
                self._emit(
                    "expression_plan",
                    source="discord",
                    plan=expression_plan,
                    avatar_connected=False,
                )
        segmenter = SentenceSegmenter(
            max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
        )
        # 感情タグをUIオーブへ通知しつつ、TTSの声色にも反映する (ローカルと同じ挙動)
        emotion_on = bool(self._cfg.get("emotion.enabled", True))
        def _resolve_voice(emotion_value, delivery_value):
            style = self._style_manager.resolve(emotion_value, delivery_value)
            if self._mind is not None:
                style = self._mind.smooth_voice_style(style, source="discord", generation_id=response_id)
            return style

        cur_emotion: dict[str, str | None] = {"v": planned_voice.get("emotion")}
        cur_style: dict[str, object] = {
            "delivery": planned_voice.get("delivery"),
            "speech": _resolve_voice(planned_voice.get("emotion"), planned_voice.get("delivery")),
        }

        def _emo_cb(emo: str) -> None:
            cur_emotion["v"] = emo
            if self._mind is not None:
                self._mind.observe_affect(emo, reason="llm_expression")
            cur_style["speech"] = _resolve_voice(emo, cur_style.get("delivery"))
            if emotion_on:
                self._emit("emotion", emotion=emo)

        def _style_cb(delivery: str) -> None:
            cur_style["delivery"] = delivery
            cur_style["speech"] = _resolve_voice(cur_emotion["v"], delivery)

        emotion = EmotionTagParser(_emo_cb, _style_cb)
        reply = ""
        reply_suppressed_reason = ""
        rejected_reply = ""
        if self._mind is not None and not proactive and response_id:
            self._mind.mark_kernel_generation(
                source="discord", response_id=response_id,
                generation_id=response_id,
            )
        # 爆弾解除で検算が要るターンは、Discordでも全文を保持してから話す。
        # 文単位で流すと、間違った指示が耳へ届いてから訂正することになる（第19条）。
        defusal_pending = bool(
            self._mind is not None and self._mind.game_profile_check_pending()
        )
        activity_guarded = bool(
            activity_context and activity_requires_assistant_move
        ) or defusal_pending
        print("🤖 ", end="", flush=True)
        messages = self._fit_prompt(messages)
        echo_guard = None if proactive else self._reply_echo_guard(segmenter)
        async for token in strip_think(self._llm.generate(messages)):
            token = emotion.feed(token)
            if not token:
                continue
            if metrics is not None:
                metrics.mark("llm_first")
            reply += token
            if echo_guard is not None:
                verdict, ready, shown = echo_guard.feed(token)
                if verdict == "holding":
                    continue
                if verdict == "repeat":
                    match = echo_guard.match
                    reason = f"repeat_{match.kind}" if match is not None else "repeat_assistant"
                    logger.info(
                        "Discord重複候補を発話前に抑止: kind=%s score=%.2f candidate=%s",
                        match.kind if match is not None else "unknown",
                        match.score if match is not None else 0.0,
                        reply.strip()[:40],
                    )
                    self._emit(
                        "reply_suppressed", source="discord",
                        reason=reason,
                    )
                    rejected_reply = reply.strip()
                    reply = ""
                    reply_suppressed_reason = reason
                    echo_guard = None
                    break
                echo_guard = None
                if playback_tracker is not None:
                    playback_tracker.generated(shown)
                print(shown, end="", flush=True)
                if not activity_guarded:
                    for sentence in ready:
                        await self._speak(
                            loop, sentence, cur_emotion["v"], cur_style["speech"],
                            metrics=metrics, response_id=response_id,
                            playback_tracker=playback_tracker,
                        )
                continue
            if playback_tracker is not None:
                playback_tracker.generated(token)
            print(token, end="", flush=True)
            if not activity_guarded:
                for sentence in segmenter.feed(token):
                    await self._speak(loop, sentence, cur_emotion["v"], cur_style["speech"], metrics=metrics,
                                      response_id=response_id, playback_tracker=playback_tracker)
        leftover = emotion.flush()
        if leftover and echo_guard is None:
            reply += leftover
            if playback_tracker is not None:
                playback_tracker.generated(leftover)
            if not activity_guarded:
                for sentence in segmenter.feed(leftover):
                    await self._speak(loop, sentence, cur_emotion["v"], cur_style["speech"], metrics=metrics,
                                      response_id=response_id, playback_tracker=playback_tracker)
        elif leftover and echo_guard is not None:
            reply += leftover
            echo_guard.feed(leftover)
        if echo_guard is not None:
            verdict, ready, shown = echo_guard.flush()
            if verdict == "repeat":
                match = echo_guard.match
                reason = f"repeat_{match.kind}" if match is not None else "repeat_assistant"
                logger.info(
                    "Discord重複候補を発話前に抑止: kind=%s score=%.2f candidate=%s",
                    match.kind if match is not None else "unknown",
                    match.score if match is not None else 0.0,
                    reply.strip()[:40],
                )
                self._emit(
                    "reply_suppressed", source="discord",
                    reason=reason,
                )
                rejected_reply = reply.strip()
                reply = ""
                reply_suppressed_reason = reason
            else:
                if playback_tracker is not None:
                    playback_tracker.generated(shown)
                print(shown, end="", flush=True)
                if not activity_guarded:
                    for sentence in ready:
                        await self._speak(
                            loop, sentence, cur_emotion["v"], cur_style["speech"],
                            metrics=metrics, response_id=response_id,
                            playback_tracker=playback_tracker,
                        )
            echo_guard = None
        tail = "" if reply_suppressed_reason else segmenter.flush()
        if tail and not activity_guarded:
            await self._speak(loop, tail, cur_emotion["v"], cur_style["speech"], metrics=metrics,
                              response_id=response_id, playback_tracker=playback_tracker)
        if not reply.strip() and not activity_guarded:
            # A retrieval-enhanced prompt can occasionally exhaust a local
            # model before it emits visible content. Discord must not become
            # silent in that case either.
            if reply_suppressed_reason.startswith("repeat_"):
                from neuro_voice.dialogue.repair import (
                    normalize_repeated_reply_retry,
                    repeated_reply_fallback,
                    repeated_reply_retry_messages,
                )

                reply = repeated_reply_fallback(text)
                recovered_from_safe_fallback = bool(reply)
                regenerated_match = None
                if not reply:
                    retry_parser = EmotionTagParser(_emo_cb, _style_cb)
                    retry_raw = ""
                    retry_messages = repeated_reply_retry_messages(
                        messages, text, rejected_reply,
                    )
                    async for retry_token in strip_think(self._llm.generate(retry_messages)):
                        retry_raw += retry_parser.feed(retry_token)
                    retry_raw += retry_parser.flush()
                    reply = normalize_repeated_reply_retry(retry_raw)
                    retry_guard = self._reply_echo_guard(SentenceSegmenter(
                        max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))),
                        min_chars=8,
                    ))
                    if (
                        reply and retry_guard is not None
                        and retry_guard.repeats(reply)
                    ):
                        regenerated_match = retry_guard.match
                        match = regenerated_match
                        logger.warning(
                            "Discord重複応答の再生成も抑止: kind=%s score=%.2f",
                            match.kind if match is not None else "unknown",
                            match.score if match is not None else 0.0,
                        )
                    self._emit(
                        "reply_regenerated", source="discord",
                        reason=reply_suppressed_reason, success=bool(reply),
                    )
                if reply and not recovered_from_safe_fallback and regenerated_match is None:
                    logger.info("Discord repeated reply regenerated once")
                elif rejected_reply:
                    # An old response is not proof of duplicate delivery in
                    # this turn.  Preserve the unshipped primary response.
                    reply = rejected_reply
                    logger.info("Discord historical similarity allowed for explicit reply")
            else:
                reply = "回答の生成が途中で切れちゃった。もう一度短く聞いてくれたら、すぐ答えるね。"
                logger.warning("Discord LLM returned no visible content; using spoken fallback")
            if reply:
                if playback_tracker is not None:
                    playback_tracker.generated(reply)
                await self._speak(
                    loop, reply, cur_emotion["v"], cur_style["speech"],
                    metrics=metrics, response_id=response_id,
                    playback_tracker=playback_tracker,
                )
        if activity_guarded:
            validation, reason = self._mind.activity_validate_assistant_text(reply, generation_id=response_id)
            if validation != "VALID":
                logger.warning("Discord activity output rejected; retrying once: %s (%s)", validation, reason)
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
                    validation, reason = self._mind.activity_validate_assistant_text(reply, generation_id=response_id)
            if validation != "VALID":
                logger.warning("Discord activity output blocked before TTS after retry: %s (%s)", validation, reason)
                self._emit("activity_output_blocked", validation=validation, reason=reason, source="discord")
                reply = (
                    "同じ手番で有効な言葉を確定できなかったから、しりとりはいったん止めるね。"
                    if reason == "max_reproposals_exceeded" else
                    "最初の言葉と手番は確認できたよ。今は私の番だから、もう一度だけ言葉を選び直すね。"
                )
            # Localと同じ検算（第19条）。差し替えても本文はログへ残さない（第12条）。
            if defusal_pending and self._mind is not None:
                try:
                    verdict, correction = self._mind.game_profile_verify_reply(reply)
                except Exception:
                    logger.exception("爆弾解除の検算に失敗")
                else:
                    if verdict != "unclear":
                        self._emit("ktane_verified", verdict=verdict, source="discord")
                        if correction:
                            logger.warning("爆弾解除: ポッポの判断が規則と食い違ったため差し替え")
                            reply = correction
            guarded_segmenter = SentenceSegmenter(
                max_chars=min(30, int(self._cfg.get("tts.max_chars", 120))), min_chars=8,
            )
            for sentence in guarded_segmenter.feed(reply):
                await self._speak(loop, sentence, cur_emotion["v"], cur_style["speech"], metrics=metrics,
                                  response_id=response_id, playback_tracker=playback_tracker,
                                  enforce_conversation_contract=False)
            tail = guarded_segmenter.flush()
            if tail:
                await self._speak(loop, tail, cur_emotion["v"], cur_style["speech"], metrics=metrics,
                                  response_id=response_id, playback_tracker=playback_tracker,
                                  enforce_conversation_contract=False)
        if (
            self._mind is not None
            and not proactive
            and not activity_guarded
            and response_id
        ):
            flushed_sentence = self._mind.flush_conversation_sentence(
                source="discord", response_id=response_id,
            )
            if flushed_sentence:
                await self._speak(
                    loop, flushed_sentence, cur_emotion["v"], cur_style["speech"],
                    metrics=metrics, response_id=response_id,
                    playback_tracker=playback_tracker,
                    enforce_conversation_contract=False,
                )
        # **生成された文章そのもの**。Local と同じく、ここが繰り返して
        # いたら下流を見ても直す場所は無い。本文は残さず指紋だけ。
        self._turns.note_text(metrics, TextStage.LLM_OUTPUT, reply)
        if (
            reply.strip()
            and self._mind is not None
            and not proactive
            and not activity_guarded
            and response_id
        ):
            reply = self._mind.enforce_conversation_reply(
                reply, source="discord", response_id=response_id,
            )
        self._turns.note_text(metrics, TextStage.SURFACE_REALIZED, reply)
        print()
        if reply and not proactive:
            if self._mind is not None and response_id:
                self._mind.finalize_kernel_output(
                    reply, source="discord", response_id=response_id,
                )
            self._conv.add_assistant(reply)
            if resumed_deferred_topic:
                self._conv.complete_deferred_topic()
                self._emit("topic_resumed")
            if self._echo_text and self._text_channel is not None:
                with contextlib_suppress():
                    await self._text_channel.send(reply[:1900])
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
        )
        if result.trimmed:
            self._emit(
                "context_trimmed", label=label, dropped=result.dropped,
                estimated_tokens=result.estimated_tokens, budget=result.budget,
            )
        return result.messages

    def _reply_echo_guard(self, segmenter):
        """Use heard and interrupted replies as shared Discord echo evidence."""
        if not bool(self._cfg.get("conversation.suppress_repeated_reply", True)):
            return None
        previous: list[str] = []
        user_said = ""
        with contextlib_suppress():
            recent = getattr(self._conv, "recent_assistant_texts", None)
            if callable(recent):
                previous.extend(recent(limit=2))
            for message in reversed(self._conv.messages()):
                if message.get("role") == "user" and not user_said:
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

    async def _speak_direct_reply(
        self, name: str, user_text: str, reply: str, *, metrics: TurnMetrics | None = None,
        response_id: str | None = None, playback_tracker: PlayedTextTracker | None = None,
    ) -> str:
        """Speak a validated game/control response without asking the LLM."""
        self._conv.add_user(f"{name}: {user_text}")
        self._emit("assistant_start")
        self._emit("assistant_token", token=reply)
        if playback_tracker is not None:
            playback_tracker.generated(reply)
        await self._speak(
            asyncio.get_running_loop(), reply, metrics=metrics,
            response_id=response_id, playback_tracker=playback_tracker,
        )
        self._conv.add_assistant(reply)
        self._emit("assistant_done", text=reply)
        return reply

    def _speech_allowed_now(self) -> bool:
        """認知層が発話を許したターンか。既定（無効時）は常に許可。"""
        metrics = getattr(self, "_active_cognition_metrics", None)
        if metrics is None or not bool(metrics.effective_cognition_enabled):
            return True
        decision = getattr(self, "_cognitive_decision", None)
        return True if decision is None else decision.speaks

    async def _speak(self, loop, sentence: str, emotion: str | None = None, style=None,
                     metrics: TurnMetrics | None = None, response_id: str | None = None,
                     playback_tracker: PlayedTextTracker | None = None,
                     enforce_conversation_contract: bool = True) -> None:
        if response_id is not None and not self._is_response_active(response_id):
            return
        if self._mind is not None:
            sentence = self._mind.privacy_safe_output(sentence)
            if sentence and enforce_conversation_contract and response_id is not None:
                sentence = self._mind.enforce_conversation_sentence(
                    sentence, source="discord", response_id=response_id,
                )
            if not sentence:
                return
        # Same guard as the local path (第19条): internal field names and
        # candidate lists must not be spoken, and that is decidable by reading
        # the text rather than by asking the model not to.
        # Localと同じ最終出口の判定（第19条）。認知層が沈黙を選んだ
        # ターンで、Discord側だけ喋ってしまうのを防ぐ。
        if not self._speech_allowed_now():
            return
        markers = leak_markers(sentence)
        if markers:
            logger.warning("内部値が発話へ漏れたため落としました: %s", "、".join(markers[:3]))
            sentence = strip_internal_leak(sentence)
            if not sentence:
                return
        try:
            wav, sr = await self._synthesize_with_delivery(
                loop, sentence, emotion, style,
            )
        except Exception as exc:
            summary = safe_exception_summary(exc)
            logger.error(
                "Discord TTSでエラー: error=%s text_chars=%d text_hash=%s",
                summary, len(sentence), _delivery_hash(sentence),
            )
            self._emit(
                "error", message=f"Discord TTS合成エラー: {summary}")
            return
        if response_id is not None and not self._is_response_active(response_id):
            return
        chunk = None
        if playback_tracker is not None:
            chunk = playback_tracker.queue_chunk(
                sentence, audio=wav, sample_rate=sr, total_frames=len(wav),
            )
        # **同じ断片を二度送出しない**（Phase 7D ③）。ここは断片ごとに
        # 鍵が変わるので、捕まえられるのは「同じ断片が再配送された」形
        # だけ。文が似ているかどうかは見ない。
        play_key = "" if chunk is None else str(chunk.chunk_id)
        if play_key and metrics is not None:
            if self._turns.blocks(self._turns.commit(
                metrics, CommitKind.TTS_JOB, play_key,
            )):
                logger.warning("同じ断片が2回TTSへ来たため飛ばします: %s", play_key)
                return
            self._turns.note_tts_chunk(
                metrics, speech_request_id=str(response_id or metrics.turn_id),
                tts_job_id=play_key, chunk_index=(chunk.segment_index if chunk else 0),
                text_hash=_delivery_hash(sentence), audio_hash=_delivery_hash(wav),
            )
        self._recent_tts.append((time.monotonic(), sentence))  # 自己エコー照合用
        if self._fd_enabled and self._fd_echo_enabled:
            # 音響エコーガード用: 送信するTTSの包絡を参照バッファへ (フェーズ5)
            self._fd_echo_guard.add_reference(wav, sr)
        if metrics is not None:
            metrics.mark("tts_first")
            # **音声が出来ただけ。まだ鳴っていない**（Phase 7D）。
            metrics.mark("tts_audio_ready")
        pcm = to_discord_pcm(wav, sr)
        if play_key and metrics is not None:
            delivery = self._turns.start_playback_segment(
                metrics, speech_request_id=str(response_id or metrics.turn_id),
                playback_session_id=(playback_tracker.playback_session_id if playback_tracker else str(response_id or metrics.turn_id)),
                playback_id=play_key, segment_index=(chunk.segment_index if chunk else 0),
                tts_job_id=play_key, text_hash=_delivery_hash(sentence), audio_hash=_delivery_hash(wav),
            )
            if not delivery.accepted:
                logger.warning("同じ断片が2回鳴ろうとしました: %s", play_key)
                return
            self._turns.note_text(metrics, TextStage.PLAYBACK_STARTED, sentence)
        if self._direct_active and self._direct_receiver is not None:
            try:
                # With DAVE music active, voice and music must share its one
                # PCM writer.  MixedAudioSource ducks music to 55% while its
                # TTS queue is nonempty; direct TTS is retained when no music
                # exists so normal speech keeps its low-latency path.
                if self._music is not None and self._music.is_playing and self._source.music_active:
                    self._source.write(pcm)
                    self._ensure_direct_music_pump()
                else:
                    await self._direct_receiver.play_pcm(pcm)
                if metrics is not None and play_key:
                    self._turns.mark_playback_segment_started(metrics, play_key)
                self._turn_manager.assistant_speaking()
                if chunk is not None and response_id is not None:
                    asyncio.create_task(
                        self._track_discord_chunk(response_id, playback_tracker, chunk.chunk_id, len(wav), sr, metrics=metrics),
                    )
                if metrics is not None:
                    # サイドカーが受け取った時点。**キューではなく送出済み。**
                    metrics.mark("discord_enqueued")
                    metrics.mark("play_start")
                self._direct_playback_until = max(
                    self._direct_playback_until,
                    time.monotonic() + len(pcm) / (48000 * 2 * 2),
                )
            except Exception:
                logger.exception("DAVE sidecar TTS playback failed")
            return
        self._source.write(pcm)
        if metrics is not None and play_key:
            self._turns.mark_playback_segment_started(metrics, play_key)
        self._turn_manager.assistant_speaking()
        if chunk is not None and response_id is not None:
            asyncio.create_task(
                self._track_discord_chunk(response_id, playback_tracker, chunk.chunk_id, len(wav), sr, metrics=metrics),
            )
        if metrics is not None:
            # **`write()` は Opus ソースのキューへ積むだけ**（Phase 7D）。
            # ここを `play_start` にしていたので、送出待ちの時間が
            # 合計から丸ごと抜けていた。実際に鳴り始めるのは、この下の
            # `vc` 再開のあと。
            metrics.mark("discord_enqueued")
        # 再生が止まっていたら (前回の発話が終わって送信終了した後なら) 再開する
        vc = self._vc
        already_playing = vc is not None and vc.is_playing()
        if vc is not None and not already_playing:
            try:
                vc.play(self._source)
            except Exception:
                logger.exception("Discord再生の再開に失敗")
            else:
                already_playing = True
        if metrics is not None and already_playing:
            # **ここが「鳴っている」と言える最初の地点**（Phase 7D）。
            # 既に再生中なら、積んだ分はそのまま続けて流れる。
            metrics.mark("play_start")

    async def _track_discord_chunk(self, response_id: str, tracker: PlayedTextTracker,
                                   chunk_id: str, frames: int, sample_rate: int,
                                   *, metrics: TurnMetrics | None = None) -> None:
        """Approximate Discord playback progress, excluding a paused interval."""
        tracker.started(chunk_id)
        total_s = frames / max(1, sample_rate)
        elapsed = 0.0
        last = time.monotonic()
        while elapsed < total_s:
            await asyncio.sleep(min(0.02, total_s - elapsed))
            now = time.monotonic()
            if not self._barge_paused:
                elapsed += now - last
            last = now
            if not self._is_response_active(response_id):
                tracker.completed(chunk_id, round(frames * min(1.0, elapsed / total_s)), frames, False)
                if metrics is not None:
                    self._turns.complete_playback_segment(metrics, chunk_id, completed=False)
                return
        tracker.completed(chunk_id, frames, frames, True)
        if metrics is not None:
            self._turns.complete_playback_segment(metrics, chunk_id, completed=True)


class _LoopbackSpeaker:
    """ループバック取り込みの発話者。音源(mic=本人 / loopback=通話相手)を持ち、
    声紋識別が失敗したときのフォールバック表示名に使う。

    _handle_utterance が使う display_name / name / id / source を持つ最小オブジェクト。
    """

    id = 0

    def __init__(self, source: str = "loopback"):
        self.source = source
        # 声紋が付くまでの仮表示 (mic=本人はチビ、loopbackは通話相手)
        self.display_name = "チビ" if source == "mic" else "通話相手"
        self.name = self.display_name


class contextlib_suppress:
    """with contextlib_suppress(): … の簡易版 (全例外を握りつぶす)。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
def _delivery_hash(value: str | np.ndarray) -> str:
    """Short diagnostic fingerprint; never retains text or PCM."""
    payload = (value.encode("utf-8") if isinstance(value, str)
               else np.ascontiguousarray(value).tobytes())
    return hashlib.sha256(payload).hexdigest()[:16]
