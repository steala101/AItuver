"""TTS の抽象インターフェース。"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class TTSBackend(ABC):
    """synthesize() を実装すれば任意のTTSを追加できる。"""

    @abstractmethod
    def synthesize(
        self, text: str, emotion: str | None = None, style=None,
    ) -> tuple[np.ndarray, int]:
        """テキストから (float32 モノラル音声, サンプルレート) を生成する。

        emotion は疑似感情ラベル (joy/sad 等)。対応するバックエンドは声の抑揚・
        大きさ・速さを変える。style は StyleManager が決めた話し方の指示で、
        非対応バックエンドは無視してよい。
        """

    def close(self) -> None:
        """Release optional network/GPU resources at application exit."""
