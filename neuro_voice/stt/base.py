"""STT の抽象インターフェース。"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class Transcriber(ABC):
    """transcribe() を実装すれば任意のSTTを追加できる。"""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """float32 モノラル音声をテキストに変換する (確定認識)。"""

    def transcribe_partial(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """途中認識。既定は確定認識と同じ (実装側で速度優先に上書き可)。"""
        return self.transcribe(audio, sample_rate)

    def transcribe_detailed(self, audio: np.ndarray, sample_rate: int = 16000):
        """確定認識を、確信度を保ったまま返す。

        A backend that cannot report confidence still satisfies this contract;
        callers then simply see a transcript with nothing marked uncertain.
        """
        from neuro_voice.stt.transcript import Transcript

        return Transcript.plain(self.transcribe(audio, sample_rate))

    def set_context_bias(self, initial_prompt: str = "", hotwords: str = "") -> None:
        """会話文脈に合わせて認識の語彙バイアスを更新する (対応バックエンドのみ)。"""
        return None
