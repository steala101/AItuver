"""記憶の意味検索用ローカル embedding。

sentence-transformers (multilingual-e5 系) を遅延ロードで使う。
未インストールでも動くよう、呼び出し側はNoneを許容すること
(その場合はキーワード検索にフォールバックする)。
"""
from __future__ import annotations

import importlib.util
import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)


def embedder_available() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


class LocalEmbedder:
    """multilingual-e5 系モデルのラッパー。初回 encode 時にモデルをロードする。"""

    def __init__(self, model: str = "intfloat/multilingual-e5-small", device: str = "cpu"):
        self._model_name = model
        self._device = device
        self._model = None
        self._load_lock = threading.Lock()
        self._is_e5 = "e5" in model.lower()

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _ensure(self) -> None:
        with self._load_lock:
            if self._model is not None:
                return
            from sentence_transformers import SentenceTransformer

            logger.info("embeddingモデルをロード中: %s (%s) ※初回はダウンロードあり",
                        self._model_name, self._device)
            self._model = SentenceTransformer(self._model_name, device=self._device)
            logger.info("embeddingモデルのロード完了")

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """テキスト列を正規化済み float32 ベクトル行列にする。

        e5系はプレフィックス ("query: " / "passage: ") が推奨されている。
        """
        self._ensure()
        if self._is_e5:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        vecs = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(vecs, dtype=np.float32)
