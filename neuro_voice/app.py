"""エントリポイント。設定読み込み・コンポーネント構築・起動。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict

from neuro_voice.llm.factory import build_llm
from neuro_voice.memory.conversation import ConversationManager
from neuro_voice.pipeline import VoicePipeline
from neuro_voice.tts.factory import build_tts
from neuro_voice.utils.config import Config
from neuro_voice.utils.logger import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Neuro Voice AI")
    parser.add_argument("--config", default="config/config.yaml", help="設定ファイルのパス")
    parser.add_argument("--gui", action="store_true", help="GUIモード (既定なので指定不要)")
    parser.add_argument("--cli", action="store_true", help="CLI音声会話モード (GUIなし)")
    parser.add_argument("--text", action="store_true", help="テキスト入力モード (マイク/STTなし)")
    parser.add_argument("--discord", action="store_true", help="Discordボットモード (VCで会話)")
    parser.add_argument("--list-devices", action="store_true", help="オーディオデバイス一覧を表示")
    parser.add_argument("--analyze-video", metavar="FILE", help="Gemma 4で動画ファイルを区間ごとに解析")
    cognition_session_group = parser.add_mutually_exclusive_group()
    cognition_session_group.add_argument("--cognition-test-session", action="store_true",
                                         help="Enable cognition only in this process")
    cognition_session_group.add_argument("--cognition-production-session", action="store_true",
                                         help="Enable production cognition only in this process")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return

    cfg = Config.load(args.config)
    setup_logging(cfg.get("logging.level", "INFO"), cfg.get("logging.dir", "logs"))

    if args.analyze_video:
        asyncio.run(_amain_video(cfg, args.analyze_video))
        return

    cognition_session_mode = (
        "production_session" if args.cognition_production_session else
        "test_session" if args.cognition_test_session else "disabled"
    )
    if args.discord:
        try:
            asyncio.run(_amain_discord(cfg, cognition_session_mode=cognition_session_mode))
        except KeyboardInterrupt:
            print("\n終了します。")
        return

    # 既定はGUIモード (--cli / --text 指定時のみコンソールモード)
    if not args.cli and not args.text:
        from neuro_voice.ui.webview_app import run_gui

        run_gui(cfg, args.config, cognition_session_mode=cognition_session_mode)
        return

    try:
        asyncio.run(_amain(cfg, text_mode=args.text,
                           cognition_session_mode=cognition_session_mode))
    except KeyboardInterrupt:
        print("\n終了します。")


async def _amain_discord(cfg: Config, *, cognition_session_mode: str = "disabled") -> None:
    """Discordボットモード: VCに参加してグループ会話する。"""
    from neuro_voice.discord_bridge.bot import DiscordBridge
    from neuro_voice.memory.persona import build_system_prompt, get_active_persona
    from neuro_voice.stt.factory import build_stt

    conv = ConversationManager(
        system_prompt=build_system_prompt(cfg),
        max_turns=int(cfg.get("conversation.max_turns", 20)),
    )
    llm = build_llm(cfg)
    # 遠隔起動を速くするため、重い初期化を並行化する:
    #  - TTS: SBV2/VOICEVOX サーバー起動をブロックせず裏で行う (defer_launch)
    #  - STT: Whisperモデルのロードを別スレッドで並行実行
    # これにより Discord 接続 (Ready) を重いモデルロードで待たせない。
    # SwitchableTTS でラップして set_volume/set_speaker を有効にする
    # (GUIと同じ。ラップしないと「声を10%に」等の音量指示が効かない)。
    from neuro_voice.tts.switchable import SwitchableTTS

    _tts_backend_name = str(cfg.get("audio_output.backend", cfg.get("tts.backend", "voicevox")))
    tts = SwitchableTTS(
        build_tts(cfg, defer_launch=True),
        _tts_backend_name,
        volume=float(cfg.get("tts.output_volume", 1.0)),
    )
    stt = await asyncio.to_thread(build_stt, cfg)

    mind = None
    if bool(cfg.get("mind.enabled", True)):
        from neuro_voice.mind import Mind

        pkey, pdef = get_active_persona(cfg)
        bridge_holder: dict = {"b": None}
        mind = Mind(
            cfg, llm, pkey or "default", str(pdef.get("name", "AI")),
            is_busy=lambda: bridge_holder["b"] is not None and bridge_holder["b"]._responding,
        )

    bridge = DiscordBridge(cfg, llm=llm, tts=tts, stt=stt, mind=mind, conv=conv)
    if cognition_session_mode != "disabled":
        bridge.start_cognition_session(cognition_session_mode)
    if mind is not None:
        bridge_holder["b"] = bridge

    # ヘッドレス(--discord)でも「声を10%にして」等のTTS音量指示を効かせる。
    # GUIの start_discord にしか無かったフックを、ここでも配線する。
    from neuro_voice.tts.volume import parse_tts_volume_command  # noqa: F401 (存在確認)

    def _apply_tts_volume(command) -> str:
        current = float(getattr(tts, "volume", cfg.get("tts.output_volume", 1.0)) or 1.0)
        value = command.value if command.mode == "set" else current + command.value
        if hasattr(tts, "set_volume"):
            value = float(tts.set_volume(value))
        else:
            value = max(0.0, min(2.0, value))
        cfg.set("tts.output_volume", value)
        return f"声の音量を{int(round(value * 100))}パーセントにしたよ。"

    if hasattr(bridge, "set_tts_volume_hook"):
        bridge.set_tts_volume_hook(_apply_tts_volume)

    # 遠隔ランチャーから起動された場合のみ、localhost制御サーバーを立てる。
    # 通常の手動 --discord 起動 (環境変数なし) では従来どおり動作する。
    import contextlib

    control_server = None
    shutdown_event = asyncio.Event()
    control_token = os.environ.get("AI_APP_CONTROL_TOKEN", "").strip()
    control_port = os.environ.get("AI_APP_CONTROL_PORT", "").strip()
    if control_token and control_port.isdigit():
        from neuro_voice.remote.app_control import AppControlServer

        loop = asyncio.get_running_loop()
        control_server = AppControlServer(
            bridge, loop, token=control_token,
            request_shutdown=lambda: loop.call_soon_threadsafe(shutdown_event.set),
            port=int(control_port),
        )
        with contextlib.suppress(Exception):
            control_server.start()

    try:
        bridge_task = asyncio.create_task(bridge.start())
        stop_wait = asyncio.create_task(shutdown_event.wait())
        done, _pending = await asyncio.wait(
            {bridge_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED,
        )
        # /shutdown 要求で抜けた場合は bridge を明示的に止める
        if stop_wait in done and not bridge_task.done():
            bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bridge_task
        else:
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stop_wait
            # bridge_task 側の例外があれば伝播させる
            if bridge_task.done() and bridge_task.exception():
                raise bridge_task.exception()
    finally:
        if control_server is not None:
            control_server.stop()

        with contextlib.suppress(Exception):
            await bridge.stop()
        if mind is not None:
            with contextlib.suppress(Exception):
                await mind.on_session_end()
            with contextlib.suppress(Exception):
                await mind.close()
        with contextlib.suppress(Exception):
            await asyncio.shield(llm.unload())


async def _amain_video(cfg: Config, filename: str) -> None:
    """CLI entrypoint for bounded, chronological video analysis."""
    if str(cfg.get("llm.backend", "")) != "ollama":
        raise RuntimeError("動画解析は vision.provider: ollama と llm.backend: ollama が必要です")
    from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient
    from neuro_voice.vision import PerceptionService, VisualAnalyzer, analyze_video_file

    client = OllamaMultimodalClient(
        str(cfg.get("llm.backends.ollama.base_url", "http://localhost:11434/v1")),
        str(cfg.get("vision.model", "gemma4-12b-qat")),
        timeout_s=float(cfg.get("vision.request_timeout_sec", 45)),
        keep_alive=cfg.get("llm.keep_alive"),
    )
    perception = PerceptionService(VisualAnalyzer(
        client,
        timeout_s=float(cfg.get("vision.request_timeout_sec", 45)),
        max_output_tokens=int(cfg.get("vision.realtime.max_output_tokens", 160)),
        context_size=int(cfg.get("vision.realtime.context_size", 4096)),
    ))
    try:
        result = await analyze_video_file(filename, cfg, perception)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    finally:
        await perception.close()


async def _amain(cfg: Config, text_mode: bool,
                 cognition_session_mode: str = "disabled") -> None:
    from neuro_voice.memory.persona import build_system_prompt, get_active_persona

    conv = ConversationManager(
        system_prompt=build_system_prompt(cfg),
        max_turns=int(cfg.get("conversation.max_turns", 20)),
    )
    llm = build_llm(cfg)
    tts = build_tts(cfg)

    mind = None
    if bool(cfg.get("mind.enabled", True)):
        from neuro_voice.mind import Mind

        pkey, pdef = get_active_persona(cfg)
        holder: dict = {"p": None}
        mind = Mind(
            cfg, llm, pkey or "default", str(pdef.get("name", "AI")),
            is_busy=lambda: holder["p"] is not None and holder["p"].state in ("thinking", "speaking"),
        )

    stt = None
    if not text_mode:
        from neuro_voice.stt.factory import build_stt

        stt = build_stt(cfg)

    pipeline = VoicePipeline(cfg, llm=llm, tts=tts, conv=conv, stt=stt, mind=mind)
    if cognition_session_mode != "disabled":
        pipeline.start_cognition_session(cognition_session_mode)
    if mind is not None:
        holder["p"] = pipeline
    try:
        if text_mode:
            await pipeline.run_text()
        else:
            await pipeline.run_voice()
    finally:
        # 終了時にセッションを思い出として保存し、ローカルLLMをアンロード
        import contextlib

        with contextlib.suppress(Exception):
            await pipeline.shutdown()
        if mind is not None:
            with contextlib.suppress(Exception):
                await mind.on_session_end()
            with contextlib.suppress(Exception):
                await mind.close()
        with contextlib.suppress(Exception):
            await asyncio.shield(llm.unload())
