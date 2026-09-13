"""声紋 (話者embedding) 抽出。

WavLM x-vector の ONNX 版 (Xenova/wavlm-base-plus-sv) を onnxruntime で実行。
ビルド不要・CPUで数百ms・512次元の単位ベクトルを返す。
同一話者ならコサイン類似度が高くなる (別人 ~0.5-0.7 / 本人 ~0.85-0.97)。
"""
from __future__ import annotations

import importlib.util
import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_REPO = "Xenova/wavlm-base-plus-sv"
_MODEL_FILES = {
    "full": "onnx/model.onnx",              # フル精度 (約380MB)。話者識別の精度が段違いに良い
    "quantized": "onnx/model_quantized.onnx",  # 量子化 (約100MB)。軽いが同一人物の類似度が下がる
}


def voiceprint_available() -> bool:
    return (
        importlib.util.find_spec("onnxruntime") is not None
        and importlib.util.find_spec("huggingface_hub") is not None
    )


class VoiceprintEncoder:
    """encode(audio) -> 単位ベクトル。初回呼び出し時にモデルをロードする。"""

    def __init__(self, device: str = "cpu", quality: str = "full"):
        self._device = device
        self._file = _MODEL_FILES.get(str(quality), _MODEL_FILES["full"])
        self._session = None
        self._input_name = None
        self._output_index = 0
        self._load_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._session is not None

    def _ensure(self) -> None:
        with self._load_lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download

            logger.info("声紋モデルをダウンロード/ロード中: %s (%s)", _MODEL_REPO, self._file)
            path = hf_hub_download(_MODEL_REPO, self._file)
            providers = ["CPUExecutionProvider"]
            if self._device.startswith("cuda"):
                providers.insert(0, "CUDAExecutionProvider")
            sess = ort.InferenceSession(path, providers=providers)
            # 音声入力とマスク入力 (あれば) を特定する
            self._mask_name = None
            names = [i.name for i in sess.get_inputs()]
            self._input_name = names[0]
            for n in names:
                if "mask" in n.lower():
                    self._mask_name = n
                elif "input" in n.lower() or "value" in n.lower():
                    self._input_name = n
            # WavLMForXVector の出力は (logits, embeddings)。embeddings を探す
            for i, out in enumerate(sess.get_outputs()):
                if "embed" in out.name.lower():
                    self._output_index = i
                    break
            else:
                self._output_index = len(sess.get_outputs()) - 1
            self._session = sess
            logger.info(
                "声紋モデルのロード完了 (input=%s, output=%s)",
                self._input_name,
                sess.get_outputs()[self._output_index].name,
            )

    def encode(self, audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray | None:
        """16kHz float32 音声から声紋ベクトルを得る。短すぎ/失敗は None。"""
        self._ensure()
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) < sample_rate:  # 1秒未満は不安定
            return None
        # 長すぎる発話は中央の8秒だけ使う (速度と精度のバランス)
        max_len = sample_rate * 8
        if len(audio) > max_len:
            ofs = (len(audio) - max_len) // 2
            audio = audio[ofs : ofs + max_len]
        # Wav2Vec系の標準前処理: ゼロ平均・単位分散
        audio = (audio - audio.mean()) / (audio.std() + 1e-7)
        feeds = {self._input_name: audio[None, :]}
        if self._mask_name:
            feeds[self._mask_name] = np.ones((1, len(audio)), dtype=np.int64)
        out = self._session.run(None, feeds)
        vec = np.asarray(out[self._output_index], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-8:
            return None
        return vec / norm
