"""音声を出さないダミーTTS。LLM疎通確認やCI用。"""
from __future__ import annotations

import numpy as np

from neuro_voice.tts.base import TTSBackend


class NullTTS(TTSBackend):
    """無音を返す (テキスト表示のみで動作確認したいとき用)。"""

    def synthesize(self, text: str, emotion: str | None = None, style=None) -> tuple[np.ndarray, int]:
        return np.zeros(160, dtype=np.float32), 16000
