"""設定から TTS バックエンドを生成するファクトリ。"""
from __future__ import annotations

from neuro_voice.tts.base import TTSBackend
from neuro_voice.utils.config import Config


def build_tts(cfg: Config, on_status=None, *, defer_launch: bool = False) -> TTSBackend:
    """tts.backend の値に応じたバックエンドを返す。

    on_status: 起動進捗メッセージを受け取るコールバック (GUI通知用、省略可)。
    defer_launch: True の場合、VOICEVOX/SBV2 サーバーの起動をブロックせず裏で行う。
        バックエンドは自己修復・prewarm を持つため、起動完了を待たずに即座に返す。
        遠隔ヘッドレス起動で Discord 接続 (Ready) を待たせないために使う。
    """
    # audio_output is the user-facing backend choice. Keep legacy tts.backend
    # fully supported for existing configs; Gemma is intentionally absent.
    backend = cfg.get("audio_output.backend", cfg.get("tts.backend", "voicevox"))

    if backend == "null":
        from neuro_voice.tts.null import NullTTS

        return NullTTS()

    if backend == "voicevox":
        from neuro_voice.tts.voicevox import VoicevoxTTS
        from neuro_voice.memory.persona import get_active_persona

        v = dict(cfg.section("tts.voicevox"))
        output_v = cfg.section("audio_output.voicevox")
        # New audio_output names mirror VOICEVOX's API while old tts.voicevox
        # names remain the canonical compatibility layer.
        if output_v.get("base_url"):
            v["base_url"] = output_v["base_url"]
        if output_v.get("speaker_id") is not None:
            v["speaker"] = output_v["speaker_id"]
        for output_key, legacy_key in (
            ("speed_scale", "speed"), ("pitch_scale", "pitch"),
            ("intonation_scale", "intonation"), ("volume_scale", "volume"),
        ):
            if output_v.get(output_key) is not None:
                v[legacy_key] = output_v[output_key]
        persona_key, persona = get_active_persona(cfg)
        persona_speaker = persona.get("voicevox_speaker") if persona_key else None
        speaker = persona_speaker if persona_speaker is not None else v.get("speaker", 3)
        auto_launch = bool(v.get("auto_launch", True))

        # エンジン未起動なら裏でCLI起動する (auto_launch: true のとき)
        if auto_launch:
            from neuro_voice.tts.voicevox_launcher import ensure_engine

            def _launch_vv():
                ok = ensure_engine(
                    base_url=v.get("base_url", "http://127.0.0.1:50021"),
                    engine_path=v.get("engine_path") or None,
                    on_status=on_status,
                )
                if not ok and on_status is not None:
                    on_status("⚠ VOICEVOX の起動に失敗。発話時に自動で再試行します")

            if defer_launch:
                import threading
                threading.Thread(target=_launch_vv, name="vv-boot", daemon=True).start()
            else:
                _launch_vv()

        return VoicevoxTTS(
            base_url=v.get("base_url", "http://127.0.0.1:50021"),
            speaker=speaker,
            speed=v.get("speed", 1.0),
            intonation=v.get("intonation", 1.0),
            volume=v.get("volume", 1.0),
            pitch=v.get("pitch", 0.0),
            auto_launch=auto_launch,
            engine_path=v.get("engine_path") or None,
            pronunciations=v.get("pronunciations") or {},
            on_status=on_status,
        )

    if backend in ("style_bert_vits2", "sbv2"):
        from neuro_voice.tts.style_bert_vits2 import StyleBertVits2TTS
        from neuro_voice.memory.persona import get_active_persona

        s = dict(cfg.section("tts.style_bert_vits2"))
        persona_key, persona = get_active_persona(cfg)
        persona_model = persona.get("style_bert_vits2_model") if persona_key else None
        persona_speaker = persona.get("style_bert_vits2_speaker_id") if persona_key else None
        auto_launch = bool(s.get("auto_launch", True))
        base_url = s.get("base_url", "http://127.0.0.1:5000")

        # サーバー未起動なら裏で起動する (auto_launch: true のとき)
        if auto_launch:
            from neuro_voice.tts.style_bert_vits2_launcher import ensure_server

            def _launch_sbv2():
                ok = ensure_server(
                    base_url,
                    s.get("path") or None,
                    device=str(s.get("device", "cuda")),
                    on_status=on_status,
                )
                if not ok and on_status is not None:
                    on_status("⚠ Style-Bert-VITS2 の起動に失敗。発話時に自動で再試行します")

            if defer_launch:
                # 起動をブロックしない (遠隔ヘッドレス起動でReadyを速くする)。
                # 未起動でも初回発話時に self-heal で再試行される。
                import threading
                threading.Thread(target=_launch_sbv2, name="sbv2-boot", daemon=True).start()
            else:
                _launch_sbv2()

        return StyleBertVits2TTS(
            base_url=base_url,
            model_name=persona_model or s.get("model_name") or None,
            speaker_id=int(persona_speaker if persona_speaker is not None else s.get("speaker_id", 0)),
            language=s.get("language", "JP"),
            default_style=s.get("default_style", "Neutral"),
            style_weight=float(s.get("style_weight", 1.0)),
            lock_single_style_weight=bool(s.get("lock_single_style_weight", True)),
            emotion_styles=s.get("emotion_styles") or {},
            delivery_styles=s.get("delivery_styles") or {},
            speed=float(s.get("speed", 1.0)),
            sdp_ratio=float(s.get("sdp_ratio", 0.2)),
            noise=float(s.get("noise", 0.6)),
            noisew=float(s.get("noisew", 0.8)),
            auto_launch=auto_launch,
            install_path=s.get("path") or None,
            device=str(s.get("device", "cuda")),
            pronunciations=s.get("pronunciations")
            or cfg.get("tts.voicevox.pronunciations")
            or {},
            on_status=on_status,
        )

    if backend == "qwen3":
        from neuro_voice.tts.qwen3 import Qwen3TTS

        q = cfg.section("tts.qwen3")
        return Qwen3TTS(
            model=q.get("model", "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
            speaker=q.get("speaker", "Ono_Anna"),
            language=q.get("language", "Auto"),
            instruct=q.get("instruct", ""),
            device=q.get("device", "cuda:0"),
            dtype=q.get("dtype", "bfloat16"),
            attn=q.get("attn", "sdpa"),
        )

    raise ValueError(f"未定義のTTSバックエンド: {backend}")
