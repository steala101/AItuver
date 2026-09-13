"""Qwen3-TTS ローカル推論バックエンド。

現状は文単位の合成による疑似ストリーミング (LLM出力の文が確定するたびに
合成→再生キューへ投入)。qwen-tts / vLLM-Omni のトークン単位ストリーミング
APIが安定したら差し替える。
"""
from __future__ import annotations

import logging
import time

import numpy as np

from neuro_voice.tts.base import TTSBackend

logger = logging.getLogger(__name__)


class Qwen3TTS(TTSBackend):
    """Qwen3-TTS-12Hz CustomVoice モデルによる音声合成。"""

    def __init__(
        self,
        model: str = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        speaker: str = "Ono_Anna",
        language: str = "Auto",
        instruct: str = "",
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        attn: str = "sdpa",
    ):
        import torch
        from qwen_tts import Qwen3TTSModel

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        start = time.perf_counter()
        self._model = Qwen3TTSModel.from_pretrained(
            model,
            device_map=device,
            dtype=dtype_map.get(dtype, torch.bfloat16),
            attn_implementation=attn,
        )
        self._speaker = speaker
        self._language = language
        self._instruct = instruct
        logger.info(
            "Qwen3-TTS '%s' ロード完了 (%.1fs, speaker=%s)",
            model, time.perf_counter() - start, speaker,
        )
        # ウォームアップ: 初回合成はCUDA初期化等で遅いため起動時に済ませる
        warmup_start = time.perf_counter()
        logger.info("Qwen3-TTS ウォームアップ中...")
        self.synthesize("こんにちは。")
        logger.info("ウォームアップ完了 (%.1fs)", time.perf_counter() - warmup_start)

    def synthesize(self, text: str, emotion: str | None = None, style=None) -> tuple[np.ndarray, int]:
        # Qwen3-TTSは感情パラメータ非対応 (emotionは無視)。将来instructで反映可能
        start = time.perf_counter()
        kwargs: dict = {
            "text": text,
            "language": self._language,
            "speaker": self._speaker,
        }
        if self._instruct:
            kwargs["instruct"] = self._instruct
        wavs, sr = self._model.generate_custom_voice(**kwargs)
        wav = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        logger.info(
            "TTS合成 %.2fs (%d文字, 音声%.1f秒): %s",
            time.perf_counter() - start, len(text), len(wav) / sr, text[:20],
        )
        return wav, int(sr)
