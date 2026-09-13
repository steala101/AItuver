"""Local IPC client for the DAVE-capable Discord receiver sidecar.

Discord account identity and voiceprint identity deliberately remain separate:
``source_account`` records who sent the RTP stream; speaker resolution is left
to :class:`Mind` after utterance segmentation.  This module never persists a
mapping between the two.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from neuro_voice.discord_bridge.audio import UtteranceSegmenter, discord_to_16k

logger = logging.getLogger(__name__)


def reserve_loopback_port() -> int:
    """Pick an unused local port for one sidecar instance.

    A fresh application must not accidentally connect to an orphaned sidecar
    from a previous run (which used to cause a 20-second join timeout).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def decode_pcm_packet(packet: bytes) -> tuple[dict, bytes]:
    """Decode the sidecar's length-prefixed metadata and raw PCM packet."""
    if len(packet) < 4:
        raise ValueError("PCM packet is missing metadata length")
    header_len = int.from_bytes(packet[:4], "big")
    if header_len <= 0 or header_len > len(packet) - 4:
        raise ValueError("PCM packet has an invalid metadata length")
    header = json.loads(packet[4:4 + header_len].decode("utf-8"))
    if header.get("type") != "pcm" or header.get("encoding") != "pcm_s16le":
        raise ValueError("unsupported sidecar packet")
    return header, packet[4 + header_len:]


class DirectDiscordSpeaker:
    """One decoded utterance's transport identity.

    ``source_account_*`` is *only* the Discord account which sent the audio.
    It must not be treated as a voiceprint/profile name.
    """

    source = "discord_direct"

    def __init__(self, account_id: str, account_name: str, *, ssrc: int | None = None):
        self.source_account_id = str(account_id)
        self.source_account_name = str(account_name or account_id)
        self.ssrc = ssrc
        # Existing rendering/event surfaces use these fields.  They identify
        # the transport account, not a person inferred from voiceprint.
        self.id = int(account_id) if str(account_id).isdigit() else 0
        self.display_name = self.source_account_name
        self.name = self.source_account_name

    def transport_context(self) -> dict[str, str | int | None]:
        return {
            "id": self.source_account_id,
            "name": self.source_account_name,
            "ssrc": self.ssrc,
        }


class DirectDAVEReceiver:
    """Spawn/manage the localhost Node DAVE receiver and segment its PCM.

    The callback is invoked only after per-account VAD segmentation, so packets
    from simultaneously speaking Discord accounts are never mixed together.
    """

    def __init__(
        self,
        token: str,
        on_utterance: Callable[[DirectDiscordSpeaker, np.ndarray], Awaitable[None]],
        on_partial: Callable[[DirectDiscordSpeaker, np.ndarray], object] | None = None,
        on_audio: Callable[[DirectDiscordSpeaker, np.ndarray], object] | None = None,
        on_speech_start: Callable[[DirectDiscordSpeaker], object] | None = None,
        on_speech_discarded: Callable[[DirectDiscordSpeaker], object] | None = None,
        on_event: Callable[..., None] | None = None,
        *,
        root: Path | None = None,
        port: int = 0,
        endpoint_hold: Callable[[str], float] | None = None,
    ):
        self._token = token
        self._on_utterance = on_utterance
        self._on_partial = on_partial
        self._on_audio = on_audio
        self._on_speech_start = on_speech_start
        self._on_speech_discarded = on_speech_discarded
        self._on_event = on_event
        # 発話終了判定の追加待ち時間 (秒) を返すフック。account_id を渡す。
        # 「〜けど」「〜で」等、続きを言いそうな途中結果のときに終了を遅らせる。
        self._endpoint_hold = endpoint_hold
        self._root = root or Path(__file__).resolve().parents[2]
        self._port = int(port) if int(port) > 0 else reserve_loopback_port()
        self._secret = secrets.token_urlsafe(32)
        self._process: subprocess.Popen | None = None
        self._sidecar_log_file = None
        self._sidecar_log_path: Path | None = None
        self._ws = None
        self._task: asyncio.Task | None = None
        self._segmenters: dict[str, UtteranceSegmenter] = {}
        self._speakers: dict[str, DirectDiscordSpeaker] = {}
        self._last_packet_at: dict[str, float] = {}
        self._last_partial_at: dict[str, float] = {}
        self._connected = asyncio.Event()
        self._sidecar_ready = asyncio.Event()
        self._joined = asyncio.Event()
        self._left = asyncio.Event()
        self._startup_error: str | None = None
        self._join_error: str | None = None
        self._leave_error: str | None = None
        self._leaving = False
        self._stopped = False
        self._segment_watchdog_task: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    async def start(self) -> None:
        if self._task is not None and self._task.done():
            self._task = None
        if self._process is not None and self._process.poll() is not None:
            self._process = None
        if self._task is not None:
            await self._wait_until_sidecar_ready()
            return
        self._stopped = False
        node = shutil.which("node")
        script = self._root / "discord_receiver" / "receiver.js"
        if node is None:
            raise RuntimeError("Node.js が見つかりません。DAVE直接受信には Node.js 18+ が必要です")
        if not script.is_file():
            raise RuntimeError(f"DAVE受信サイドカーが見つかりません: {script}")
        try:
            import websockets  # type: ignore
        except ImportError as e:
            raise RuntimeError("websockets が未導入です。pip install websockets を実行してください") from e
        env = os.environ.copy()
        env.update({
            "DISCORD_BOT_TOKEN": self._token,
            "AITUBER_DAVE_SECRET": self._secret,
            "AITUBER_DAVE_PORT": str(self._port),
        })
        log_dir = self._root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._sidecar_log_path = log_dir / "direct_dave_sidecar.log"
        self._sidecar_log_file = self._sidecar_log_path.open(
            "a", encoding="utf-8", buffering=1,
        )
        self._process = subprocess.Popen(
            [node, str(script)], cwd=str(script.parent), env=env,
            stdin=subprocess.DEVNULL, stdout=self._sidecar_log_file,
            stderr=subprocess.STDOUT,
        )
        self._task = asyncio.create_task(self._run(websockets), name="discord-dave-ipc")
        self._segment_watchdog_task = asyncio.create_task(
            self._flush_idle_segments(), name="discord-dave-segment-watchdog"
        )
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=6.0)
            await self._wait_until_sidecar_ready()
        except Exception:
            await self.stop()
            raise

    async def _wait_until_sidecar_ready(self) -> None:
        try:
            await asyncio.wait_for(self._sidecar_ready.wait(), timeout=12.0)
        except asyncio.TimeoutError as e:
            raise RuntimeError("DAVE受信サイドカーのDiscordログインが12秒以内に完了しませんでした") from e
        if self._startup_error:
            raise RuntimeError(f"DAVE受信サイドカーのログインに失敗しました: {self._startup_error}")

    async def _run(self, websockets) -> None:
        url = f"ws://127.0.0.1:{self._port}"
        # websocket-client APIs differ slightly between v14/v15.
        kwargs = {"max_size": 8 * 1024 * 1024}
        try:
            ws_cm = websockets.connect(url, additional_headers={"X-AItuber-Secret": self._secret}, **kwargs)
        except TypeError:
            ws_cm = websockets.connect(url, extra_headers={"X-AItuber-Secret": self._secret}, **kwargs)
        try:
            async with ws_cm as ws:
                self._ws = ws
                self._connected.set()
                self._emit("discord_receive_status", mode="direct_dave", ready=True,
                           message="DAVE直接受信サイドカーに接続しました")
                async for message in ws:
                    if isinstance(message, bytes):
                        await self._handle_pcm(message)
                    else:
                        self._handle_event(json.loads(message))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._stopped:
                exit_code = self._process.poll() if self._process is not None else None
                details = {
                    "exit_code": exit_code,
                    "log_path": str(self._sidecar_log_path) if self._sidecar_log_path else "",
                }
                logger.exception("DAVE sidecar IPC disconnected")
                self._emit("discord_receive_status", mode="direct_dave", ready=False,
                           message=str(e), **details)
                self._emit("direct_dave_disconnected", message=str(e), **details)
        finally:
            self._ws = None
            self._connected.clear()

    def _handle_event(self, event: dict) -> None:
        typ = event.get("type")
        if typ == "sidecar_ready":
            self._sidecar_ready.set()
        elif typ == "sidecar_error":
            self._startup_error = str(event.get("message") or "unknown error")
            self._sidecar_ready.set()
        elif typ == "sidecar_fatal":
            message = str(event.get("message") or "unknown fatal error")
            logger.error("DAVE sidecar fatal error: %s", message)
            self._emit("discord_receive_status", mode="direct_dave", ready=False,
                       message=message)
        if typ == "joined":
            self._joined.set()
        elif typ == "left":
            self._left.set()
        elif typ == "command_error":
            error = str(event.get("message") or "unknown command error")
            if self._leaving:
                self._leave_error = error
                self._left.set()
            else:
                self._join_error = error
                self._joined.set()
        if typ in {"status", "command_error"}:
            level = logging.ERROR if typ == "command_error" or event.get("level") == "error" else logging.INFO
            logger.log(level, "DAVE sidecar: %s", event.get("message", event))
            self._emit("discord_receive_status", mode="direct_dave",
                       ready=typ != "command_error", **event)

    async def _handle_pcm(self, packet: bytes) -> None:
        try:
            header, pcm = decode_pcm_packet(packet)
            account = header.get("source_account") or {}
            account_id = str(account.get("id") or "")
            if not account_id:
                raise ValueError("PCM packet has no Discord account ID")
            speaker = self._speakers.get(account_id)
            if speaker is None:
                speaker = DirectDiscordSpeaker(account_id, str(account.get("name") or account_id),
                                                ssrc=header.get("ssrc"))
                self._speakers[account_id] = speaker
                logger.info("DAVE direct PCM started account=%s name=%s ssrc=%s",
                            speaker.source_account_id, speaker.source_account_name, speaker.ssrc)
            audio = discord_to_16k(pcm)
            if self._on_audio is not None:
                self._on_audio(speaker, audio)
            segmenter = self._segmenters.setdefault(account_id, UtteranceSegmenter())
            self._last_packet_at[account_id] = asyncio.get_running_loop().time()
            was_speaking = segmenter.speaking
            utterance = segmenter.feed(audio)
            if not was_speaking and segmenter.speaking and self._on_speech_start is not None:
                self._on_speech_start(speaker)
            if was_speaking and not segmenter.speaking and utterance is None and self._on_speech_discarded is not None:
                self._on_speech_discarded(speaker)
            if utterance is not None:
                await self._deliver_utterance(speaker, utterance)
                self._last_partial_at.pop(account_id, None)
            else:
                self._deliver_partial_if_due(account_id, speaker, segmenter)
        except Exception:
            logger.exception("Invalid PCM from DAVE sidecar")

    async def _flush_idle_segments(self) -> None:
        """Finalize direct streams when Discord sends no silent PCM packets.

        Discord normally stops sending packets when a member stops talking.  A
        packet-driven VAD therefore cannot see its trailing silence by itself;
        without this watchdog the next speaker's packet (or the 20-second
        safety limit) used to release the previous utterance all at once.
        """
        try:
            while True:
                await asyncio.sleep(0.08)
                now = asyncio.get_running_loop().time()
                for account_id, segmenter in list(self._segmenters.items()):
                    hold_s = 0.65
                    if self._endpoint_hold is not None:
                        try:
                            hold_s = max(0.3, min(2.5, 0.65 + float(self._endpoint_hold(account_id))))
                        except Exception:
                            hold_s = 0.65
                    if not segmenter.speaking or now - self._last_packet_at.get(account_id, now) < hold_s:
                        continue
                    utterance = segmenter.flush()
                    self._last_packet_at.pop(account_id, None)
                    speaker = self._speakers.get(account_id)
                    if utterance is not None and speaker is not None:
                        logger.info("DAVE direct VAD finalized idle stream account=%s duration=%.0fms",
                                    account_id, len(utterance) / 16.0)
                        await self._deliver_utterance(speaker, utterance)
                    elif speaker is not None and self._on_speech_discarded is not None:
                        self._on_speech_discarded(speaker)
        except asyncio.CancelledError:
            raise

    async def _deliver_utterance(self, speaker: DirectDiscordSpeaker, utterance: np.ndarray) -> None:
        """Support both async consumers and the bridge's synchronous enqueue."""
        result = self._on_utterance(speaker, utterance)
        if inspect.isawaitable(result):
            await result

    def _deliver_partial_if_due(self, account_id: str, speaker: DirectDiscordSpeaker,
                                segmenter: UtteranceSegmenter) -> None:
        """Emit a throttled in-progress buffer; it never finalizes a turn."""
        if self._on_partial is None:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_partial_at.get(account_id, 0.0) < 1.2:
            return
        audio = segmenter.current_audio(min_speech_ms=900)
        if audio is None:
            return
        self._last_partial_at[account_id] = now
        try:
            self._on_partial(speaker, audio)
        except Exception:
            logger.exception("DAVE partial-audio callback failed")

    async def join(self, guild_id: str | int, channel_id: str | int) -> None:
        await self.start()
        self._joined.clear()
        self._join_error = None
        await self._send({"type": "join", "guild_id": str(guild_id), "channel_id": str(channel_id)})
        try:
            await asyncio.wait_for(self._joined.wait(), timeout=12.0)
        except asyncio.TimeoutError as e:
            raise RuntimeError("DAVE受信側のVC参加が12秒以内に完了しませんでした") from e
        if self._join_error:
            raise RuntimeError(f"DAVE受信側のVC参加に失敗しました: {self._join_error}")

    async def leave(self) -> None:
        if self._ws is None:
            raise RuntimeError("DAVE受信サイドカーが接続されていません")
        self._left.clear()
        self._leave_error = None
        self._leaving = True
        try:
            await self._send({"type": "leave"})
            await asyncio.wait_for(self._left.wait(), timeout=8.0)
            if self._leave_error:
                raise RuntimeError(f"DAVE受信側のVC退出に失敗しました: {self._leave_error}")
        except asyncio.TimeoutError as e:
            raise RuntimeError("DAVE受信側のVC退出確認が8秒以内に完了しませんでした") from e
        finally:
            self._leaving = False
        self._segmenters.clear()
        self._speakers.clear()
        self._last_packet_at.clear()
        self._last_partial_at.clear()

    async def play_pcm(self, pcm: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("DAVE音声セッションが接続されていません")
        await self._send({"type": "play_pcm", "pcm_b64": base64.b64encode(pcm).decode("ascii")})

    async def stop_playback(self) -> None:
        """Discard sidecar PCM/Opus buffers after a substantive barge-in."""
        if self._ws is not None:
            await self._send({"type": "stop_pcm"})

    async def pause_playback(self) -> None:
        """Pause sidecar TTS without discarding its PCM/Opus cursor."""
        if self._ws is not None:
            await self._send({"type": "pause_pcm"})

    async def resume_playback(self) -> None:
        """Resume sidecar TTS after a backchannel or VAD false-positive."""
        if self._ws is not None:
            await self._send({"type": "resume_pcm"})

    async def _send(self, message: dict) -> None:
        if self._ws is None:
            raise RuntimeError("DAVE受信サイドカーが接続されていません")
        await self._ws.send(json.dumps(message, ensure_ascii=False))

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        watchdog, self._segment_watchdog_task = self._segment_watchdog_task, None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None
        log_file, self._sidecar_log_file = self._sidecar_log_file, None
        if log_file is not None:
            log_file.close()
        self._ws = None
        self._connected.clear()
        self._sidecar_ready.clear()
        self._joined.clear()
        self._left.clear()
        self._startup_error = None
        self._join_error = None
        self._leave_error = None

    def _emit(self, event_type: str, **data) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_type, **data)
            except Exception:
                logger.exception("DAVE receiver event callback failed")
