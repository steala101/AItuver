"""Silero VAD ラッパー。"""
from __future__ import annotations

import logging

import numpy as np
import torch

logger = logging.getLogger(__name__)


class SileroVAD:
    """512サンプル (16kHz) 単位で発話確率を返す。"""

    def __init__(self, sample_rate: int = 16000):
        from silero_vad import load_silero_vad

        self._model = load_silero_vad()
        self._sr = sample_rate
        logger.info("Silero VAD ロード完了")

    def prob(self, frame: np.ndarray) -> float:
        """フレームの発話確率 (0.0〜1.0) を返す。"""
        tensor = torch.from_numpy(np.ascontiguousarray(frame, dtype=np.float32))
        with torch.no_grad():
            return float(self._model(tensor, self._sr).item())

    def reset(self) -> None:
        """内部状態をリセットする (発話終了時に呼ぶ)。"""
        self._model.reset_states()
